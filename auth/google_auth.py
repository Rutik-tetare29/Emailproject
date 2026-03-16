"""
Google OAuth 2.0 authentication using google-auth-oauthlib.
Stores OAuth credentials in Flask session so they survive requests.
"""
from __future__ import annotations
import json
import os
import secrets
from flask import Blueprint, redirect, request, session, url_for
from flask_login import login_user, UserMixin
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
import google.oauth2.id_token
import google.auth.transport.requests as google_requests
from config import Config
from services.security_admin import resolve_role, register_user

google_auth_bp = Blueprint("google_auth", __name__)
_GOOGLE_DEVICE_TOKENS_FILE = os.path.join(Config.DATA_DIR, "google_device_tokens.json")


def _load_device_tokens() -> dict:
    try:
        with open(_GOOGLE_DEVICE_TOKENS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _save_device_tokens(tokens: dict) -> None:
    tmp_path = f"{_GOOGLE_DEVICE_TOKENS_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(tokens, f, indent=2)
    os.replace(tmp_path, _GOOGLE_DEVICE_TOKENS_FILE)


def _remember_google_user(user: "GoogleUser") -> tuple[str, str]:
    device_token = (request.cookies.get("google_device_token") or "").strip() or secrets.token_urlsafe(32)
    tokens = _load_device_tokens()
    tokens[device_token] = {
        "email": user.email,
        "name": user.name,
        "auth_type": "google",
        "role": user.role,
        "credentials": user.credentials_dict or {},
    }
    _save_device_tokens(tokens)
    return device_token, user.email


def _restore_google_user_from_device() -> "GoogleUser | None":
    device_token = (request.cookies.get("google_device_token") or "").strip()
    if not device_token:
        return None
    tokens = _load_device_tokens()
    user_data = tokens.get(device_token)
    if not user_data or user_data.get("auth_type") != "google":
        return None
    credentials = user_data.get("credentials") or {}
    # Old remembered sessions stored only identity. Force a real OAuth login
    # until usable credentials are available so Gmail send/refresh won't fail.
    required = {"token", "refresh_token", "token_uri", "client_id", "client_secret"}
    if not required.issubset(set(credentials.keys())):
        return None
    try:
        return GoogleUser(
            email=user_data["email"],
            name=user_data.get("name", user_data["email"]),
            credentials_dict=credentials,
            role=user_data.get("role"),
        )
    except Exception:
        return None


# ── User model ────────────────────────────────────────────────────────────────
class GoogleUser(UserMixin):
    """Lightweight user object stored in the Flask session."""

    def __init__(self, email: str, name: str, credentials_dict: dict, role: str | None = None):
        self.id = email
        self.email = email
        self.name = name
        self.credentials_dict = credentials_dict  # serialised google Credentials
        self.auth_type = "google"
        self.role = role or resolve_role(email)

    # Rebuild from session dict
    @staticmethod
    def from_session(data: dict) -> "GoogleUser":
        return GoogleUser(
            email=data["email"],
            name=data["name"],
            credentials_dict=data["credentials"],
            role=data.get("role"),
        )

    def to_dict(self) -> dict:
        return {
            "email": self.email,
            "name": self.name,
            "credentials": self.credentials_dict,
            "auth_type": "google",
            "role": self.role,
        }

    def get_credentials(self) -> Credentials:
        """Return a refreshed Credentials object."""
        required = {"token", "refresh_token", "token_uri", "client_id", "client_secret"}
        if not required.issubset(set((self.credentials_dict or {}).keys())):
            raise RuntimeError("Google login needs reconnect for Gmail send access")
        creds = Credentials(**self.credentials_dict)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # Persist refreshed token back to session
            session["user"]["credentials"] = _creds_to_dict(creds)
            self.credentials_dict = session["user"]["credentials"]
            _remember_google_user(self)
        return creds


# ── Helper ────────────────────────────────────────────────────────────────────
def _build_flow(state: str = None) -> Flow:
    flow = Flow.from_client_secrets_file(
        Config.GOOGLE_CLIENT_SECRETS_FILE,
        scopes=Config.GOOGLE_SCOPES,
        state=state,
    )
    # Use the pinned URI from config so it always matches Google Cloud Console.
    # url_for(_external=True) varies between 127.0.0.1 and localhost depending
    # on how Flask is started, causing redirect_uri_mismatch errors.
    flow.redirect_uri = Config.GOOGLE_REDIRECT_URI
    return flow


def _creds_to_dict(creds: Credentials) -> dict:
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or []),
    }


# ── Routes ─────────────────────────────────────────────────────────────────────
@google_auth_bp.route("/login/google")
def google_login():
    flow = _build_flow()
    fast_login = request.args.get("fast") == "1"
    session["voice_google_login"] = fast_login

    if fast_login:
        restored_user = _restore_google_user_from_device()
        if restored_user:
            register_user(restored_user.email, auth_type="google", role=restored_user.role)
            session["user"] = restored_user.to_dict()
            login_user(restored_user, remember=True)
            session["announce_login_success"] = True
            return redirect(url_for("dashboard"))

    auth_kwargs = {
        "access_type": "offline",
        "include_granted_scopes": "true",
    }
    if fast_login:
        # If token-restore was not possible, let user explicitly choose account.
        # Do not send stale login_hint from cookies, which can prefill a wrong email.
        auth_kwargs["prompt"] = "select_account"
    else:
        # First-time login: keep the explicit consent flow so refresh tokens
        # continue to be issued reliably.
        auth_kwargs["prompt"] = "consent"

    auth_url, state = flow.authorization_url(**auth_kwargs)
    session["oauth_state"] = state
    return redirect(auth_url)


@google_auth_bp.route("/login/google/callback")
def oauth_callback():
    # If the app restarted between the login redirect and Google's callback the
    # session is empty and oauth_state is gone.  Fall back to the state Google
    # echoes back in the query-string so the token exchange still succeeds.
    state = session.get("oauth_state") or request.args.get("state", "")
    flow = _build_flow(state=state)

    try:
        # Normalise the callback URL to the same host as GOOGLE_REDIRECT_URI
        # so the token exchange never gets a host mismatch (127.0.0.1 vs localhost).
        callback_url = request.url.replace("http://localhost:", "http://127.0.0.1:") \
                                  .replace("https://localhost:", "https://127.0.0.1:")
        if "127.0.0.1" not in Config.GOOGLE_REDIRECT_URI and "localhost" in Config.GOOGLE_REDIRECT_URI:
            callback_url = request.url.replace("http://127.0.0.1:", "http://localhost:") \
                                      .replace("https://127.0.0.1:", "https://localhost:")
        flow.fetch_token(authorization_response=callback_url)
    except Exception as exc:
        return {"error": f"OAuth token exchange failed: {exc}"}, 400

    creds = flow.credentials
    id_info = google.oauth2.id_token.verify_oauth2_token(
        creds.id_token,
        google_requests.Request(),
        clock_skew_in_seconds=10,
    )

    user = GoogleUser(
        email=id_info["email"],
        name=id_info.get("name", id_info["email"]),
        credentials_dict=_creds_to_dict(creds),
    )
    register_user(user.email, auth_type="google", role=user.role)
    session["user"] = user.to_dict()
    login_user(user, remember=True)
    if session.pop("voice_google_login", False):
        session["announce_login_success"] = True

    device_token, remembered_email = _remember_google_user(user)
    response = redirect(url_for("dashboard"))
    response.set_cookie(
        "google_device_token",
        device_token,
        max_age=60 * 60 * 24 * 180,
        httponly=True,
        samesite="Lax",
    )
    response.set_cookie(
        "last_google_email",
        remembered_email,
        max_age=60 * 60 * 24 * 180,
        samesite="Lax",
    )
    return response
