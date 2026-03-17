import os
from dotenv import load_dotenv

# Always load .env from the project root regardless of CWD where Python is launched
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_writable_dir(env_name: str, default_path: str, fallback_path: str) -> str:
    """
    Resolve a writable directory for runtime data.

    Render deployments may provide paths via env vars (for example /var/data/*).
    If that path is not writable (disk missing/misconfigured), fall back to a
    known writable path so workers can still boot.
    """
    preferred = os.getenv(env_name, default_path)
    try:
        os.makedirs(preferred, exist_ok=True)
        return preferred
    except OSError:
        os.makedirs(fallback_path, exist_ok=True)
        return fallback_path


class Config:
    # Flask
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
    DEBUG = _env_bool("DEBUG", False)
    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = int(os.getenv("PORT", "5000"))

    # Cookies / sessions
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE", "Lax")
    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", not DEBUG)
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SECURE = _env_bool("REMEMBER_COOKIE_SECURE", SESSION_COOKIE_SECURE)
    PREFERRED_URL_SCHEME = os.getenv("PREFERRED_URL_SCHEME", "https" if SESSION_COOKIE_SECURE else "http")

    # Reverse proxy support (required for Render / load balancers)
    TRUST_PROXY_HEADERS = _env_bool("TRUST_PROXY_HEADERS", True)

    # Audio temp storage
    UPLOAD_FOLDER = _resolve_writable_dir(
        "UPLOAD_FOLDER",
        os.path.join(BASE_DIR, "static", "audio"),
        "/tmp/voice_email_audio",
    )

    # Data storage
    DATA_DIR = _resolve_writable_dir(
        "DATA_DIR",
        os.path.join(BASE_DIR, "data"),
        "/tmp/voice_email_data",
    )

    # Google OAuth
    GOOGLE_CLIENT_SECRETS_FILE = os.getenv(
        "GOOGLE_CLIENT_SECRETS_FILE",
        os.path.join(BASE_DIR, "client_secrets.json"),
    )
    GOOGLE_SCOPES = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/userinfo.email",
        "openid",
    ]
    OAUTHLIB_INSECURE_TRANSPORT = os.getenv("OAUTHLIB_INSECURE_TRANSPORT", "0")

    # STT backend language hint (used by lightweight cloud recognition backend)
    STT_DEFAULT_LANG = os.getenv("STT_DEFAULT_LANG", "en")

    # Pinned OAuth redirect URI - must match exactly what is registered in
    # Google Cloud Console -> APIs & Services -> Credentials -> Authorised redirect URIs
    GOOGLE_REDIRECT_URI = os.getenv(
        "GOOGLE_REDIRECT_URI",
        "http://127.0.0.1:5000/login/google/callback",
    )

    # Gmail IMAP / SMTP (App Password flow)
    GMAIL_IMAP_HOST = "imap.gmail.com"
    GMAIL_SMTP_HOST = "smtp.gmail.com"
    GMAIL_SMTP_PORT = 587

    # Multi-Language Support (Feature 5)
    DEFAULT_LANGUAGE = os.getenv("VOICEMAIL_LANGUAGE", "en")

    SUPPORTED_LANGUAGES: dict = {
        "en": "English",
        "hi": "Hindi",
        "mr": "Marathi",
        "es": "Spanish",
        "fr": "French",
        "de": "German",
        "it": "Italian",
        "pt": "Portuguese",
        "ja": "Japanese",
        "zh": "Chinese",
    }

    # Messaging service
    MESSAGING_BACKEND = os.getenv("MESSAGING_BACKEND", "simulation")

    TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID", "")
    TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
    TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE", "")

    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_IDS_RAW = os.getenv("TELEGRAM_CHAT_IDS", "")

    # Summarization (Feature 2)
    SUMMARIZATION_MODE = os.getenv("SUMMARIZATION_MODE", "simple")

    # Milestone 4: Security / RBAC / Admin
    ADMIN_EMAILS_RAW = os.getenv("ADMIN_EMAILS", "")
    ADMIN_EMAILS = {
        e.strip().lower()
        for e in ADMIN_EMAILS_RAW.split(",")
        if e.strip()
    }
    DEFAULT_USER_ROLE = os.getenv("DEFAULT_USER_ROLE", "user")

    # Voice PIN used for high-risk actions (send email / send message).
    VOICE_ACTION_PIN = os.getenv("VOICE_ACTION_PIN", "12345")
    PIN_MAX_ATTEMPTS = int(os.getenv("PIN_MAX_ATTEMPTS", "3"))

    # Challenge / token expiry windows (seconds)
    ACTION_CHALLENGE_TTL = int(os.getenv("ACTION_CHALLENGE_TTL", "300"))
    ACTION_TOKEN_TTL = int(os.getenv("ACTION_TOKEN_TTL", "300"))
