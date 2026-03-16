import os
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, request, jsonify, session, send_from_directory, render_template, redirect, url_for
from flask_login import LoginManager, login_required, current_user, logout_user
from config import Config
from auth.google_auth import google_auth_bp, GoogleUser
from auth.app_password_auth import apppass_auth_bp, AppPasswordUser
from services.voice_processor import (
    process_voice_command,
    process_text_compose_input,
    process_text_msg_input,
)
from services.email_service import fetch_emails, send_email

# ── Milestone 3 service imports ───────────────────────────────────────────────
from services.messaging_service import (
    send_message as msg_send,
    read_latest_message,
    get_all_messages,
    get_contacts,
    get_telegram_status,
    discover_contacts,
    register_contact,
    tl_auth_status,
    tl_auth_start,
    tl_auth_verify,
    tl_list_contacts,
)
from services.summarizer import summarize_text, summarize_email, summarize_message
from services.reply_engine import suggest_reply
from services.voice_service import speak_confirmation, check_confirmation_answer, speak_text_lang
from services.security_admin import (
    get_activity_log,
    get_metrics,
    get_users,
    hash_payload,
    log_activity,
    resolve_role,
    update_user_role,
    verify_pin,
)
from services.profile_service import (
    add_saved_contact,
    add_saved_email,
    get_profile,
    has_custom_pin,
    set_profile_pin,
    verify_profile_pin,
)

# ── App factory ──────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config.from_object(Config)
# Keep sessions alive across server restarts (cookie survives as long as
# SECRET_KEY doesn't change — it's pinned in .env)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Allow OAuth over plain HTTP during local development
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = app.config["OAUTHLIB_INSECURE_TRANSPORT"]

# ── Flask-Login setup ─────────────────────────────────────────────────────────
login_manager = LoginManager(app)
login_manager.login_view = "index"


def _current_role() -> str:
    user_data = session.get("user", {})
    role = user_data.get("role") or getattr(current_user, "role", None)
    if role:
        return role
    return resolve_role(user_data.get("email", ""))


def _current_email() -> str:
    user_data = session.get("user", {})
    email = user_data.get("email") or getattr(current_user, "email", "")
    return (email or "").strip().lower()


def _log_user_action(action: str, status: str = "success", details: dict | None = None):
    user_data = session.get("user", {})
    log_activity(
        user_email=user_data.get("email", "anonymous"),
        role=_current_role(),
        action=action,
        status=status,
        details=details,
        ip=request.remote_addr,
    )


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if _current_role() != "admin":
            _log_user_action("admin_access_denied", status="error")
            return jsonify({"error": "Admin access required"}), 403
        return fn(*args, **kwargs)

    return wrapper


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _cleanup_security_state() -> None:
    now = _utc_now().timestamp()
    challenges = session.get("action_challenges", {})
    tokens = session.get("action_tokens", {})

    challenges = {
        key: value
        for key, value in challenges.items()
        if value.get("expires_at", 0) >= now and value.get("attempts", 0) < Config.PIN_MAX_ATTEMPTS
    }
    tokens = {
        key: value
        for key, value in tokens.items()
        if value.get("expires_at", 0) >= now and not value.get("used", False)
    }

    session["action_challenges"] = challenges
    session["action_tokens"] = tokens
    session.modified = True


def _validate_action_payload(action_type: str, payload: dict) -> tuple[bool, str]:
    if action_type == "email_send":
        to_addr = (payload.get("to") or "").strip()
        subject = (payload.get("subject") or "").strip()
        body = (payload.get("body") or "").strip()
        if not to_addr or "@" not in to_addr or "." not in to_addr.split("@")[-1]:
            return False, "Invalid recipient email"
        if not subject:
            return False, "Subject is required"
        if not body:
            return False, "Message body is required"
        if len(subject) > 255:
            return False, "Subject is too long"
        return True, "ok"

    if action_type == "message_send":
        receiver = (payload.get("receiver") or "").strip()
        message = (payload.get("message") or "").strip()
        if not receiver:
            return False, "Receiver is required"
        if not message:
            return False, "Message is required"
        if len(message) > 4000:
            return False, "Message is too long"
        return True, "ok"

    return False, "Unsupported action_type"


def _create_security_challenge(action_type: str, action_text: str, payload: dict) -> str:
    _cleanup_security_state()
    challenge_id = uuid.uuid4().hex
    challenges = session.get("action_challenges", {})
    challenges[challenge_id] = {
        "action_type": action_type,
        "action_text": action_text,
        "payload_hash": hash_payload(payload),
        "created_at": _utc_now().timestamp(),
        "expires_at": (_utc_now() + timedelta(seconds=Config.ACTION_CHALLENGE_TTL)).timestamp(),
        "attempts": 0,
        "confirmed": False,
    }
    session["action_challenges"] = challenges
    session.modified = True
    return challenge_id


def _issue_confirmation_token(challenge_id: str) -> str:
    challenges = session.get("action_challenges", {})
    challenge = challenges.get(challenge_id)
    if not challenge:
        return ""

    token = uuid.uuid4().hex
    tokens = session.get("action_tokens", {})
    tokens[token] = {
        "challenge_id": challenge_id,
        "action_type": challenge.get("action_type"),
        "payload_hash": challenge.get("payload_hash"),
        "issued_at": _utc_now().timestamp(),
        "expires_at": (_utc_now() + timedelta(seconds=Config.ACTION_TOKEN_TTL)).timestamp(),
        "used": False,
    }
    session["action_tokens"] = tokens
    session.modified = True
    return token


def _consume_confirmation_token(token: str, action_type: str, payload: dict) -> tuple[bool, str]:
    _cleanup_security_state()
    if not token:
        return False, "confirmation_token is required"

    tokens = session.get("action_tokens", {})
    info = tokens.get(token)
    if not info:
        return False, "Invalid or expired confirmation token"
    if info.get("action_type") != action_type:
        return False, "Action type mismatch"
    if info.get("payload_hash") != hash_payload(payload):
        return False, "Confirmation token does not match request payload"

    info["used"] = True
    tokens[token] = info
    session["action_tokens"] = tokens
    session.modified = True
    return True, "ok"


@login_manager.user_loader
def load_user(user_id: str):
    """Rebuild user object from session data stored at login."""
    if "user" not in session:
        return None
    user_data = session["user"]
    if user_data.get("auth_type") == "app_password":
        return AppPasswordUser.from_session(user_data)
    return GoogleUser.from_session(user_data)


# ── Blueprints ────────────────────────────────────────────────────────────────
app.register_blueprint(google_auth_bp)
app.register_blueprint(apppass_auth_bp)


# ── Page routes ───────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/login")
def login_page():
    return render_template("login.html")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


# ── Voice login transcription (no @login_required) ────────────────────────────

def _normalize_app_password(raw: str) -> str:
    """
    Convert Whisper's transcription of a letter-by-letter App Password
    back to the actual 16-character string.

    Handles all common Whisper outputs for Indian-English speakers:
      • Single letters: 'a', 'b', 'c' …
      • Phonetic names: 'bee', 'see', 'dee', 'ef', 'gee', 'aitch', 'jay', 'kay',
                        'el', 'em', 'en', 'oh', 'pee', 'cue', 'are', 'ess',
                        'tee', 'you', 'vee', 'ex', 'why', 'zee', 'zed'
      • Aye/ay vs eye: 'ay'/'aye' → 'a',  'eye' → 'i'
      • NATO alphabet: 'alpha', 'bravo', 'charlie', ... 'zulu'
      • Stray punctuation Whisper inserts: 'a.', 'B,' etc.
      • Digit words: 'zero'…'nine'  (App Passwords sometimes include digits)
      • Multi-word: 'double you' → 'w', 'x ray' → 'x'
    """
    import re
    text = raw.strip().lower()
    # Strip punctuation Whisper inserts after single letters ('A.' 'B,' 'C;')
    text = re.sub(r'[.,;:!?\'"()\[\]{}]', ' ', text)
    # Handle two-word phonetics before splitting
    text = re.sub(r'\bdouble\s+(?:you|u)\b', 'w', text)
    text = re.sub(r'\bx[\s\-]ray\b', 'x', text)
    text = re.sub(r'\s+', ' ', text).strip()

    LETTER_NAMES = {
        # ── keep bare single letters as-is ───────────────────────────────────
        **{c: c for c in 'abcdefghijklmnopqrstuvwxyz0123456789'},
        # ── A ────────────────────────────────────────────────────────────────
        'ay': 'a', 'aye': 'a', 'alpha': 'a',
        # ── B ────────────────────────────────────────────────────────────────
        'bee': 'b', 'be': 'b', 'bravo': 'b',
        # ── C ────────────────────────────────────────────────────────────────
        'see': 'c', 'sea': 'c', 'si': 'c', 'charlie': 'c',
        # ── D ────────────────────────────────────────────────────────────────
        'dee': 'd', 'de': 'd', 'delta': 'd',
        # ── E ────────────────────────────────────────────────────────────────
        'ee': 'e', 'echo': 'e',
        # ── F ────────────────────────────────────────────────────────────────
        'ef': 'f', 'eff': 'f', 'foxtrot': 'f',
        # ── G ────────────────────────────────────────────────────────────────
        'gee': 'g', 'ji': 'g', 'golf': 'g',
        # ── H ────────────────────────────────────────────────────────────────
        'aitch': 'h', 'haitch': 'h', 'hotel': 'h',
        # ── I ────────────────────────────────────────────────────────────────
        'eye': 'i', 'india': 'i',
        # ── J ────────────────────────────────────────────────────────────────
        'jay': 'j', 'juliett': 'j', 'juliet': 'j',
        # ── K ────────────────────────────────────────────────────────────────
        'kay': 'k', 'kilo': 'k',
        # ── L ────────────────────────────────────────────────────────────────
        'el': 'l', 'ell': 'l', 'lima': 'l',
        # ── M ────────────────────────────────────────────────────────────────
        'em': 'm', 'mike': 'm',
        # ── N ────────────────────────────────────────────────────────────────
        'en': 'n', 'november': 'n',
        # ── O ────────────────────────────────────────────────────────────────
        'oh': 'o', 'owe': 'o', 'oscar': 'o',
        # ── P ────────────────────────────────────────────────────────────────
        'pee': 'p', 'pe': 'p', 'papa': 'p',
        # ── Q ────────────────────────────────────────────────────────────────
        'cue': 'q', 'queue': 'q', 'quebec': 'q',
        # ── R ────────────────────────────────────────────────────────────────
        'are': 'r', 'ar': 'r', 'romeo': 'r',
        # ── S ────────────────────────────────────────────────────────────────
        'ess': 's', 'es': 's', 'sierra': 's',
        # ── T ────────────────────────────────────────────────────────────────
        'tee': 't', 'ti': 't', 'tango': 't',
        # ── U ────────────────────────────────────────────────────────────────
        'you': 'u', 'yoo': 'u', 'uniform': 'u',
        # ── V ────────────────────────────────────────────────────────────────
        'vee': 'v', 've': 'v', 'victor': 'v',
        # ── W ────────────────────────────────────────────────────────────────
        'whiskey': 'w',
        # ── X ────────────────────────────────────────────────────────────────
        'ex': 'x', 'eks': 'x',
        # ── Y ────────────────────────────────────────────────────────────────
        'why': 'y', 'yankee': 'y',
        # ── Z ────────────────────────────────────────────────────────────────
        'zee': 'z', 'zed': 'z', 'zulu': 'z',
        # ── digit words ──────────────────────────────────────────────────────
        'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4',
        'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
    }

    tokens = text.split()
    out = []
    for tok in tokens:
        # Strip any residual punctuation glued to the token
        tok_clean = re.sub(r'[^a-z0-9]', '', tok)
        if not tok_clean:
            continue
        if tok_clean in LETTER_NAMES:
            out.append(LETTER_NAMES[tok_clean])
        else:
            # Unknown word (Whisper sometimes groups letters into a run like "abc")
            # Keep it as-is; it may already be the correct characters
            out.append(tok_clean)
    return ''.join(out)


@app.route("/voice/login-transcribe", methods=["POST"])
def voice_login_transcribe():
    """
    Transcribes a WAV blob for the login page (no auth required).
    Form fields: audio=<wav>, step=service|email|password
    Returns: { "text": "<raw>", "normalized": "<cleaned>" }
    """
    from services.stt_whisper import transcribe
    from services.voice_processor import _normalize_email_address

    step = request.form.get("step", "email")
    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided"}), 400

    audio_file = request.files["audio"]
    tmp_path = os.path.join(Config.UPLOAD_FOLDER, f"login_{uuid.uuid4().hex}.wav")
    audio_file.save(tmp_path)
    try:
        raw_text = transcribe(tmp_path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    if step == "email":
        normalized = _normalize_email_address(raw_text)
    elif step in ("email-correct", "yesno", "service"):
        # Return raw lowercased text for client-side matching
        normalized = raw_text.lower().strip()
    else:
        # App Passwords: map phonetic letter-names → actual characters
        normalized = _normalize_app_password(raw_text)

    return jsonify({"text": raw_text, "normalized": normalized})


# ── Voice email correction ─────────────────────────────────────────────────────

# Full phonetic/spoken-letter → actual character map (shared by correction + password)
_PHONETIC_CHARS = {
    # single bare letters kept as-is
    **{c: c for c in 'abcdefghijklmnopqrstuvwxyz0123456789'},
    # ── A ─── 
    'ay': 'a', 'aye': 'a', 'alpha': 'a',
    # ── B ───
    'bee': 'b', 'be': 'b', 'bravo': 'b',
    # ── C ───
    'see': 'c', 'sea': 'c', 'si': 'c', 'charlie': 'c',
    # ── D ───
    'dee': 'd', 'de': 'd', 'delta': 'd',
    # ── E ───
    'ee': 'e', 'echo': 'e',
    # ── F ───
    'ef': 'f', 'eff': 'f', 'foxtrot': 'f',
    # ── G ───
    'gee': 'g', 'ji': 'g', 'golf': 'g',
    # ── H ───
    'aitch': 'h', 'haitch': 'h', 'hotel': 'h',
    # ── I ───
    'eye': 'i', 'india': 'i',
    # ── J ───
    'jay': 'j', 'juliett': 'j', 'juliet': 'j',
    # ── K ───
    'kay': 'k', 'kilo': 'k',
    # ── L ───
    'el': 'l', 'ell': 'l', 'lima': 'l',
    # ── M ───
    'em': 'm', 'mike': 'm',
    # ── N ───
    'en': 'n', 'november': 'n',
    # ── O ───
    'oh': 'o', 'owe': 'o', 'oscar': 'o',
    # ── P ───
    'pee': 'p', 'pe': 'p', 'papa': 'p',
    # ── Q ───
    'cue': 'q', 'queue': 'q', 'quebec': 'q',
    # ── R ───
    'are': 'r', 'ar': 'r', 'romeo': 'r',
    # ── S ───
    'ess': 's', 'es': 's', 'sierra': 's',
    # ── T ───
    'tee': 't', 'ti': 't', 'tango': 't',
    # ── U ───
    'you': 'u', 'yoo': 'u', 'uniform': 'u',
    # ── V ───
    'vee': 'v', 've': 'v', 'victor': 'v',
    # ── W ───
    'double you': 'w', 'double u': 'w', 'whiskey': 'w',
    # ── X ───
    'ex': 'x', 'eks': 'x', 'x ray': 'x',
    # ── Y ───
    'why': 'y', 'yankee': 'y',
    # ── Z ───
    'zee': 'z', 'zed': 'z', 'zulu': 'z',
    # ── special chars ───
    'at sign': '@', 'at the rate': '@', 'at': '@',
    'dot': '.', 'period': '.', 'full stop': '.', 'point': '.',
    'dash': '-', 'hyphen': '-', 'minus': '-',
    'underscore': '_', 'under score': '_',
    'plus': '+',
    # ── digit words ───
    'zero': '0', 'one': '1', 'two': '2', 'to': '2', 'too': '2',
    'three': '3', 'four': '4', 'for': '4', 'five': '5',
    'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
}


def _vc_clean(text: str) -> str:
    """
    Convert a spoken phrase into the actual email characters it represents.
    Handles: phonetic letter names, NATO alphabet, digit words, special char names,
    multi-token phrases like 'double you', bare letters/digits, and mixed strings.
    """
    import re
    text = text.strip().lower()
    # Strip leading spoken fillers
    text = re.sub(
        r'^(?:the letter|letter|the number|number|the character|character|the digit|digit|the)\s+',
        '', text
    )
    # Normalise punctuation Whisper inserts ('A.' 'B,')
    text = re.sub(r'[.,;:!?\'"()\[\]{}]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()

    # Try full phrase first (e.g. "double you", "at sign", "x ray")
    if text in _PHONETIC_CHARS:
        return _PHONETIC_CHARS[text]

    # Tokenise and map each token
    tokens = text.split()
    out = []
    i = 0
    while i < len(tokens):
        # Try two-token phrases first
        if i + 1 < len(tokens):
            two = tokens[i] + ' ' + tokens[i + 1]
            if two in _PHONETIC_CHARS:
                out.append(_PHONETIC_CHARS[two])
                i += 2
                continue
        tok = re.sub(r'[^a-z0-9]', '', tokens[i])
        if tok in _PHONETIC_CHARS:
            out.append(_PHONETIC_CHARS[tok])
        elif tok:
            out.append(tok)   # unknown → keep as-is (may already be correct chars)
        i += 1
    return ''.join(out)


@app.route("/voice/correct-email", methods=["POST"])
def voice_correct_email():
    """
    Apply a voice correction command to an email address.
    JSON body: { "email": "<current>", "command": "<voice command text>" }
    Returns:   { "corrected": "<new email>", "message": "<readable result>", "changed": bool }

    Supported command patterns (case-insensitive, phonetic-letter-aware):
      replace/change/fix/make/turn/set/correct X to/with/as/into Y
      add/insert/put/place/type X before Y
      add/insert/put/place/type X after Y
      add/insert/put/place/append/prepend X at the end / beginning / start
      add X at position N
      remove/delete/take out/drop/erase/eliminate/strip X
      remove/delete the last letter / first letter
      move/shift X to the end / beginning
      the email/address is X             → replace entire local part
      the domain is/should be X          → replace domain
      redo / retype / whole email is X   → rewrite entire address
    """
    import re
    data    = request.json or {}
    email   = data.get("email", "").strip().lower()
    command = data.get("command", "").strip().lower()
    if not email or not command:
        return jsonify({"error": "Missing email or command"}), 400

    # Ensure we always have a local + domain even for malformed input
    if "@" in email:
        local, domain = email.split("@", 1)
    else:
        local, domain = email, "gmail.com"

    def _ok(new_local, new_domain, message):
        c = new_local + "@" + new_domain
        return jsonify({"corrected": c, "message": message, "changed": True})

    def _no_change(reason):
        return jsonify({"corrected": email, "message": reason, "changed": False})

    def _find_and_replace(source, old, new):
        """Replace first occurrence of old in source; return (new_source, found)."""
        if old in source:
            return source.replace(old, new, 1), True
        return source, False

    # ── Whole-email rewrite: "the email is X" / "redo as X" / "whole email is X" ─
    m = re.search(
        r'(?:whole email is|redo(?:\s+as)?|retype(?:\s+as)?|entire(?:\s+address)? is|'
        r'email(?:\s+address)? (?:is|should be)|my email is)\s+(.+)', command)
    if m:
        raw = m.group(1).strip()
        # If they say the full address (contains "at" or "@"), normalise it
        new_email = _vc_clean(raw) if '@' not in raw else raw.replace(' ', '').lower()
        if '@' not in new_email:
            # They probably only said the local part
            new_local = new_email; new_domain = domain
        else:
            new_local, new_domain = new_email.split('@', 1)
        return _ok(new_local, new_domain, "Email rewritten to " + new_local + "@" + new_domain)

    # ── Domain replacement: "the domain is/should be X" / "change domain to X" ──
    m = re.search(
        r'(?:domain (?:is|should be|to|as)|change domain to|set domain to|'
        r'domain name is)\s+(.+)', command)
    if m:
        raw_domain = m.group(1).strip()
        # Map spoken words: "gmail" → "gmail.com", "yahoo" → "yahoo.com", etc.
        domain_map = {
            'gmail': 'gmail.com', 'google mail': 'gmail.com',
            'yahoo': 'yahoo.com', 'hotmail': 'hotmail.com',
            'outlook': 'outlook.com', 'icloud': 'icloud.com',
            'proton': 'protonmail.com', 'protonmail': 'protonmail.com',
        }
        new_domain = domain_map.get(raw_domain, _vc_clean(raw_domain))
        return _ok(local, new_domain, "Domain changed to " + new_domain)

    # ── replace / change / fix / make / turn / set / correct X to/with/as Y ──────
    m = re.search(
        r'(?:replace|change|fix|make|turn|set|correct|swap|edit)\s+(.+?)'
        r'\s+(?:with|to|by|as|into|for)\s+(.+)', command)
    if m:
        old = _vc_clean(m.group(1).strip())
        new = _vc_clean(m.group(2).strip())
        new_local, found = _find_and_replace(local, old, new)
        if found:
            return _ok(new_local, domain, "Replaced '" + old + "' with '" + new + "'")
        new_domain, found = _find_and_replace(domain, old, new)
        if found:
            return _ok(local, new_domain, "Replaced '" + old + "' with '" + new + "' in domain")
        return _no_change("Could not find '" + old + "' in the email address")

    # ── "fix X" alone (shorthand for "fix X" with no replacement — same as remove) ─
    # Only treat as standalone fix if no "to/with" clause was found above
    m_fix = re.search(r'^(?:fix|correct)\s+(.+)$', command)

    # ── add / insert X before Y ──────────────────────────────────────────────
    m = re.search(r'(?:add|insert|put|place|type)\s+(.+?)\s+before\s+(.+)', command)
    if m:
        char = _vc_clean(m.group(1).strip())
        ref  = _vc_clean(m.group(2).strip())
        if ref in local:
            pos   = local.find(ref)
            return _ok(local[:pos] + char + local[pos:], domain,
                       "Added '" + char + "' before '" + ref + "'")
        if ref in domain:
            pos   = domain.find(ref)
            return _ok(local, domain[:pos] + char + domain[pos:],
                       "Added '" + char + "' before '" + ref + "' in domain")
        return _no_change("Could not find '" + ref + "' in the email address")

    # ── add / insert X after Y ───────────────────────────────────────────────
    m = re.search(r'(?:add|insert|put|place|type)\s+(.+?)\s+after\s+(.+)', command)
    if m:
        char = _vc_clean(m.group(1).strip())
        ref  = _vc_clean(m.group(2).strip())
        if ref in local:
            pos   = local.find(ref) + len(ref)
            return _ok(local[:pos] + char + local[pos:], domain,
                       "Added '" + char + "' after '" + ref + "'")
        if ref in domain:
            pos   = domain.find(ref) + len(ref)
            return _ok(local, domain[:pos] + char + domain[pos:],
                       "Added '" + char + "' after '" + ref + "' in domain")
        return _no_change("Could not find '" + ref + "' in the email address")

    # ── add X at position N ──────────────────────────────────────────────────
    m = re.search(r'(?:add|insert|put)\s+(.+?)\s+at\s+position\s+(\d+)', command)
    if m:
        char = _vc_clean(m.group(1).strip())
        pos  = int(m.group(2)) - 1   # 1-based → 0-based
        pos  = max(0, min(pos, len(local)))
        new_local = local[:pos] + char + local[pos:]
        return _ok(new_local, domain, "Inserted '" + char + "' at position " + str(pos + 1))

    # ── add / append X at end ────────────────────────────────────────────────
    m = re.search(
        r'(?:add|append|put|insert|place|type)\s+(.+?)\s+'
        r'(?:at the end|at end|to the end|to end|at last|in the end)', command)
    if m:
        char = _vc_clean(m.group(1).strip())
        return _ok(local + char, domain, "Added '" + char + "' at end")

    # ── add / prepend X at start ─────────────────────────────────────────────
    m = re.search(
        r'(?:add|prepend|put|insert|place|type)\s+(.+?)\s+'
        r'(?:at the start|at start|to the start|at beginning|at the beginning|'
        r'in the beginning|at front|at the front|to the front)', command)
    if m:
        char = _vc_clean(m.group(1).strip())
        return _ok(char + local, domain, "Added '" + char + "' at start")

    # ── remove / delete last letter ──────────────────────────────────────────
    if re.search(r'(?:remove|delete|take out|erase|drop)\s+(?:the\s+)?last\s+(?:letter|char|character)?', command):
        if local:
            return _ok(local[:-1], domain, "Removed last character '" + local[-1] + "'")
        return _no_change("Local part is already empty")

    # ── remove / delete first letter ─────────────────────────────────────────
    if re.search(r'(?:remove|delete|take out|erase|drop)\s+(?:the\s+)?first\s+(?:letter|char|character)?', command):
        if local:
            return _ok(local[1:], domain, "Removed first character '" + local[0] + "'")
        return _no_change("Local part is already empty")

    # ── remove / delete X (general) ──────────────────────────────────────────
    m = re.search(r'(?:remove|delete|take out|drop|erase|eliminate|strip)\s+(.+)', command)
    if m:
        char = _vc_clean(m.group(1).strip())
        new_local, found = _find_and_replace(local, char, '')
        if found:
            return _ok(new_local, domain, "Removed '" + char + "'")
        new_domain, found = _find_and_replace(domain, char, '')
        if found:
            return _ok(local, new_domain, "Removed '" + char + "' from domain")
        return _no_change("Could not find '" + char + "' in the email address")

    # ── move X to end ────────────────────────────────────────────────────────
    m = re.search(r'move\s+(.+?)\s+to\s+(?:the\s+)?end', command)
    if m:
        char = _vc_clean(m.group(1).strip())
        if char in local:
            new_local = local.replace(char, '', 1) + char
            return _ok(new_local, domain, "Moved '" + char + "' to end")
        return _no_change("Could not find '" + char + "'")

    # ── move X to start ───────────────────────────────────────────────────────
    m = re.search(r'move\s+(.+?)\s+to\s+(?:the\s+)?(?:start|beginning|front)', command)
    if m:
        char = _vc_clean(m.group(1).strip())
        if char in local:
            new_local = char + local.replace(char, '', 1)
            return _ok(new_local, domain, "Moved '" + char + "' to start")
        return _no_change("Could not find '" + char + "'")

    # ── standalone "fix X" (no replacement given) → same as remove ───────────
    if m_fix:
        char = _vc_clean(m_fix.group(1).strip())
        new_local, found = _find_and_replace(local, char, '')
        if found:
            return _ok(new_local, domain, "Removed '" + char + "'")
        return _no_change("Could not find '" + char + "' to fix")

    return _no_change(
        "Could not understand the correction. "
        "Try: replace X with Y, add X before Y, remove X, or fix X to Y"
    )


@app.route("/dashboard")
@login_required
def dashboard():
    role = _current_role()
    _log_user_action("dashboard_viewed", details={"role": role})
    announce_login_success = bool(session.pop("announce_login_success", False))
    return render_template(
        "dashboard.html",
        user=current_user,
        is_admin=(role == "admin"),
        announce_login_success=announce_login_success,
    )


@app.route("/admin")
@login_required
@admin_required
def admin_dashboard():
    _log_user_action("admin_dashboard_viewed")
    initial_metrics = get_metrics()
    initial_users = get_users()
    initial_activity = get_activity_log(limit=200)
    return render_template(
        "admin.html",
        user=current_user,
        initial_metrics=initial_metrics,
        initial_users=initial_users,
        initial_activity=initial_activity,
    )


@app.route("/admin/metrics", methods=["GET"])
@login_required
@admin_required
def admin_metrics():
    _log_user_action("admin_metrics_viewed")
    return jsonify(get_metrics())


@app.route("/admin/users", methods=["GET"])
@login_required
@admin_required
def admin_users():
    """Return the full user registry for the admin dashboard."""
    _log_user_action("admin_users_viewed")
    return jsonify(get_users())


@app.route("/admin/user/edit", methods=["POST"])
@login_required
@admin_required
def admin_user_edit():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    role = (data.get("role") or "user").strip().lower()
    if not email:
        return jsonify({"success": False, "message": "Email required"}), 400
    if role not in ("user", "admin"):
        return jsonify({"success": False, "message": "Role must be user or admin"}), 400

    updated = update_user_role(email, role)
    if not updated:
        return jsonify({"success": False, "message": "User not found"}), 404

    _log_user_action("admin_user_edited", details={"target": email, "new_role": role})
    return jsonify({"success": True, "message": f"User {email} updated to {role}"})


@app.route("/admin/user/remove", methods=["POST"])
@login_required
@admin_required
def admin_user_remove():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"success": False, "message": "Email required"}), 400

    from services.security_admin import remove_user

    removed = remove_user(email)
    if not removed:
        return jsonify({"success": False, "message": "User not found"}), 404

    _log_user_action("admin_user_removed", details={"target": email})
    return jsonify({"success": True, "message": f"User {email} removed"})


@app.route("/admin/users/<path:email>/role", methods=["PUT"])
@login_required
@admin_required
def admin_update_role(email: str):
    """Toggle a user's role between 'user' and 'admin'."""
    data = request.get_json(silent=True) or {}
    new_role = data.get("role", "")
    if new_role not in ("user", "admin"):
        return jsonify({"error": "role must be 'user' or 'admin'"}), 400
    updated = update_user_role(email, new_role)
    if not updated:
        return jsonify({"error": "User not found"}), 404
    _log_user_action("admin_role_changed", details={"target": email, "new_role": new_role})
    return jsonify({"ok": True, "email": email, "role": new_role})


@app.route("/admin/activity", methods=["GET"])
@login_required
@admin_required
def admin_activity():
    """Filtered activity log with optional ?status= and ?user= query params."""
    status_filter = request.args.get("status", "").strip() or None
    user_filter   = request.args.get("user",   "").strip() or None
    limit         = min(int(request.args.get("limit", 200)), 1000)
    _log_user_action("admin_activity_viewed")
    return jsonify(get_activity_log(limit=limit, status_filter=status_filter, user_filter=user_filter))


@app.route("/admin/export/activity.json", methods=["GET"])
@login_required
@admin_required
def admin_export_activity():
    """Download the full activity log as a JSON file."""
    _log_user_action("admin_export_activity")
    from flask import Response
    import json as _json
    data = get_activity_log(limit=5000)
    return Response(
        _json.dumps(data, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=activity_log.json"},
    )



@app.route("/voice/process", methods=["POST"])
@login_required
def voice_process():
    """
    Receives a WAV blob from the browser, runs STT → intent → TTS,
    and returns a JSON payload with transcription + audio URL.
    """
    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided"}), 400

    # The browser always sends the current localStorage language in the form.
    # Overwrite the session immediately — this is the authoritative source
    # of truth and ensures STT + TTS both use the user's chosen language,
    # even if the Flask session cookie was stale or expired.
    lang_from_client = request.form.get("lang", "").strip().lower()
    if lang_from_client and lang_from_client in Config.SUPPORTED_LANGUAGES:
        session["language"] = lang_from_client
        session.modified = True

    audio_file       = request.files["audio"]
    choosing_service = request.form.get("choosing_service") == "true"
    try:
        result = process_voice_command(audio_file, session, choosing_service=choosing_service)
        _log_user_action("voice_command_processed", details={"intent": result.get("intent", "unknown")})
    except Exception as exc:
        app.logger.exception("process_voice_command failed")
        _log_user_action("voice_command_error", status="error", details={"error": str(exc)})
        return jsonify({
            "transcription": "",
            "intent":        "error",
            "response_text": f"Sorry, something went wrong: {exc}",
            "audio_url":     None,
        }), 500

    # If the voice command was a logout, clear the Flask-Login session here
    # (process_voice_command is a service and cannot call logout_user directly).
    if result.get("intent") == "logout":
        logout_user()
        session.clear()

    return jsonify(result)


@app.route("/voice/service-greeting", methods=["GET"])
@login_required
def voice_service_greeting():
    """
    Returns TTS audio that asks the user to choose Email or Telegram.
    Called by the frontend on the very first mic tap.
    Spoken in the user's active session language.
    """
    from services.lang_utils import speak_multilang, translate_text, LANG_BCP47

    # English master text
    _text_en = (
        "Welcome! Which service would you like? "
        "Say Email for your inbox, or Telegram for chat messages."
    )

    # Per-language greeting (avoids round-trip translation for the common cases)
    _greetings = {
        "en": _text_en,
        "hi": (
            "स्वागत है! आप कौन सी सेवा चाहते हैं? "
            "ईमेल इनबॉक्स के लिए 'ईमेल' कहें, या चैट के लिए 'टेलीग्राम' कहें।"
        ),
        "mr": (
            "स्वागत आहे! तुम्हाला कोणती सेवा हवी आहे? "
            "ईमेल इनबॉक्ससाठी 'ईमेल' म्हणा, किंवा चॅटसाठी 'टेलीग्राम' म्हणा."
        ),
        "es": (
            "¡Bienvenido! ¿Qué servicio deseas? "
            "Di 'Email' para tu bandeja, o 'Telegram' para los mensajes."
        ),
        "fr": (
            "Bienvenue ! Quel service souhaitez-vous ? "
            "Dites 'Email' pour votre boîte de réception, ou 'Telegram' pour les messages."
        ),
        "de": (
            "Willkommen! Welchen Dienst möchten Sie? "
            "Sagen Sie 'E-Mail' für Ihren Posteingang oder 'Telegram' für Nachrichten."
        ),
        "it": (
            "Benvenuto! Quale servizio desideri? "
            "Di' 'Email' per la posta in arrivo o 'Telegram' per i messaggi."
        ),
        "pt": (
            "Bem-vindo! Qual serviço deseja? "
            "Diga 'Email' para sua caixa de entrada ou 'Telegram' para mensagens."
        ),
    }

    lang = session.get("language", "en")
    # Look up the pre-written greeting; fall back to online translation
    text = _greetings.get(lang)
    if not text:
        text = translate_text(_text_en, lang) or _text_en

    tts_path  = speak_multilang(text, lang=lang)
    audio_url = f"/static/audio/{os.path.basename(tts_path)}" if tts_path else None
    return jsonify({
        "audio_url":    audio_url,
        "response_text": text,
        "voice_lang":   lang,
    })


@app.route("/voice/compose-text", methods=["POST"])
@login_required
def voice_compose_text():
    """
    Accepts a typed field value for the active voice compose step.
    Body: { "field": "to" | "subject" | "body", "value": "..." }
    Returns the same JSON shape as /voice/process.
    """
    data  = request.get_json(force=True) or {}
    field = data.get("field", "").strip()
    value = data.get("value", "").strip()
    if not field or not value:
        return jsonify({"error": "Missing field or value"}), 400
    result = process_text_compose_input(field, value, session)
    return jsonify(result)


@app.route("/voice/msg-compose-text", methods=["POST"])
@login_required
def voice_msg_compose_text():
    """
    Accepts a typed field value for the active Telegram message compose step.
    Body: { "field": "to" | "text" | "confirm", "value": "..." }
    Returns the same JSON shape as /voice/process.
    """
    data  = request.get_json(force=True) or {}
    field = data.get("field", "").strip()
    value = data.get("value", "").strip()
    if not field or not value:
        return jsonify({"error": "Missing field or value"}), 400
    result = process_text_msg_input(field, value, session)
    return jsonify(result)


# ── Email API ─────────────────────────────────────────────────────────────────
@app.route("/emails", methods=["GET"])
@login_required
def get_emails():
    emails = fetch_emails(session)
    _log_user_action("emails_fetched", details={"count": len(emails)})
    return jsonify({"emails": emails})


@app.route("/send-email", methods=["POST"])
@login_required
def send_email_route():
    data = request.get_json() or {}
    to_addr = data.get("to", "").strip()
    subject = data.get("subject", "").strip()
    body = data.get("body", "").strip()
    payload = {"to": to_addr, "subject": subject, "body": body}

    valid, validation_message = _validate_action_payload("email_send", payload)
    if not valid:
        _log_user_action("email_send_rejected", status="error", details={"reason": validation_message})
        return jsonify({"error": validation_message}), 400

    token = (data.get("confirmation_token") or "").strip()
    allowed, token_message = _consume_confirmation_token(token, "email_send", payload)
    if not allowed:
        _log_user_action("email_send_blocked", status="error", details={"reason": token_message})
        return jsonify({"error": token_message}), 403

    success, message = send_email(session, to_addr, subject, body)
    status = 200 if success else 500
    if success:
        add_saved_email(session.get("user", {}).get("email", ""), to_addr)
    _log_user_action(
        "email_sent" if success else "email_send_failed",
        status="success" if success else "error",
        details={"to": to_addr, "subject": subject[:80]},
    )
    return jsonify({"success": success, "message": message}), status


# ── Serve TTS audio ───────────────────────────────────────────────────────────
@app.route("/static/audio/<path:filename>")
def serve_audio(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


# ── Logout ────────────────────────────────────────────────────────────────────
@app.route("/logout")
@login_required
def logout():
    _log_user_action("logout")
    logout_user()
    session.clear()
    return redirect(url_for("index"))


# ════════════════════════════════════════════════════════════════════════════════
# MILESTONE 3 — New Feature Routes
# ════════════════════════════════════════════════════════════════════════════════

# ── Feature 1: Messaging ──────────────────────────────────────────────────────

@app.route("/messages", methods=["GET"])
@login_required
def get_messages():
    """
    GET /messages?contact=<name>
    Return all messages, optionally filtered by contact name.
    """
    contact = request.args.get("contact", None)
    result  = get_all_messages(contact=contact)
    return jsonify(result)


@app.route("/messages/contacts", methods=["GET"])
@login_required
def list_contacts():
    """GET /messages/contacts — Return a list of all known contacts."""
    user_email = session.get("user", {}).get("email", "")
    saved_contacts = [item.get("value", "") for item in get_profile(user_email).get("saved_contacts", [])]
    contacts = sorted({c for c in (get_contacts() + saved_contacts) if c}, key=str.lower)
    return jsonify({"contacts": contacts})


@app.route("/messages/send", methods=["POST"])
@login_required
def send_message_route():
    """
    POST /messages/send
    JSON body: { "receiver": "Alice", "message": "Hello!" }
    Returns:   { "success": bool, "message": str, "data": msg_dict }

    The voice confirmation step is handled client-side:
      1. Browser calls POST /confirm/start with the action text.
      2. Server returns audio URL → browser plays it.
      3. User speaks yes/no → browser records + POSTs to /confirm/answer.
      4. Only on "yes" does the browser call this endpoint.
    """
    data     = request.get_json(force=True) or {}
    receiver = data.get("receiver", "").strip()
    message  = data.get("message", "").strip()
    payload  = {"receiver": receiver, "message": message}

    valid, validation_message = _validate_action_payload("message_send", payload)
    if not valid:
        _log_user_action("message_send_rejected", status="error", details={"reason": validation_message})
        return jsonify({"success": False, "message": validation_message}), 400

    token = (data.get("confirmation_token") or "").strip()
    allowed, token_message = _consume_confirmation_token(token, "message_send", payload)
    if not allowed:
        _log_user_action("message_send_blocked", status="error", details={"reason": token_message})
        return jsonify({"success": False, "message": token_message}), 403

    result = msg_send(receiver, message)
    status = 200 if result["success"] else 400
    if result.get("success"):
        add_saved_contact(session.get("user", {}).get("email", ""), receiver)
    _log_user_action(
        "message_sent" if result.get("success") else "message_send_failed",
        status="success" if result.get("success") else "error",
        details={"receiver": receiver},
    )
    return jsonify(result), status


@app.route("/profile", methods=["GET"])
@login_required
def get_profile_route():
    user_email = _current_email()
    return jsonify(get_profile(user_email))


@app.route("/profile/pin", methods=["POST"])
@login_required
def update_profile_pin_route():
    data = request.get_json(force=True) or {}
    user_email = _current_email()
    if not user_email:
        return jsonify({"success": False, "error": "Missing user context"}), 401

    current_pin = str(data.get("current_pin", "")).strip()
    new_pin = str(data.get("new_pin", "")).strip()
    confirm_pin = str(data.get("confirm_pin", "")).strip()

    if not new_pin:
        return jsonify({"success": False, "error": "New PIN is required"}), 400
    if new_pin != confirm_pin:
        return jsonify({"success": False, "error": "PIN confirmation does not match"}), 400

    normalized = "".join(ch for ch in new_pin if ch.isdigit())
    if len(normalized) < 4 or len(normalized) > 8:
        return jsonify({"success": False, "error": "PIN must be 4 to 8 digits"}), 400

    if has_custom_pin(user_email) and not verify_profile_pin(user_email, current_pin):
        return jsonify({"success": False, "error": "Current PIN is incorrect"}), 401

    ok, message = set_profile_pin(user_email, normalized)
    if not ok:
        return jsonify({"success": False, "error": message}), 400

    _log_user_action("profile_pin_updated")
    return jsonify({"success": True, "message": "PIN updated successfully"})


@app.route("/messages/latest", methods=["GET"])
@login_required
def latest_message():
    """
    GET /messages/latest?contact=<name>   (contact is optional)
    Return the most recent message.
    """
    contact = request.args.get("contact", None)
    result  = read_latest_message(contact=contact)
    return jsonify(result)


# ── Telegram management routes ────────────────────────────────────────────────

@app.route("/telegram/status", methods=["GET"])
@login_required
def telegram_status():
    """
    GET /telegram/status
    Returns bot info if TELEGRAM_BOT_TOKEN is valid, or simulation mode details.
    """
    return jsonify(get_telegram_status())


@app.route("/telegram/discover", methods=["GET"])
@login_required
def telegram_discover():
    """
    GET /telegram/discover
    Fetches Telegram getUpdates and auto-registers any contacts who have
    already messaged your bot.  Call this after setting up your token.
    Returns: { "contacts": {name: chat_id, ...}, "new_messages": int, "mode": str }
    """
    return jsonify(discover_contacts())


@app.route("/telegram/register", methods=["POST"])
@login_required
def telegram_register():
    """
    POST /telegram/register
    JSON body: { "name": "Alice", "chat_id": "123456789" }
    Manually map a contact name to a Telegram chat_id so you can send them
    messages even before they have messaged your bot.
    """
    data    = request.get_json(force=True) or {}
    name    = data.get("name",    "").strip()
    chat_id = data.get("chat_id", "").strip()
    if not name or not chat_id:
        return jsonify({"success": False, "message": "name and chat_id are required"}), 400
    result = register_contact(name, chat_id)
    return jsonify(result)


# ── Telethon User API auth routes ─────────────────────────────────────────────

@app.route("/telegram/my-contacts", methods=["GET"])
@login_required
def telegram_my_contacts():
    """
    GET /telegram/my-contacts
    Returns all Telegram contacts from your account (phone-book + recent
    conversations) so you know exactly what name to use when sending a message.
    Example response:
      { "contacts": [ { "name": "Vaibhav Ingle", "username": "@vai", ... } ] }
    """
    return jsonify(tl_list_contacts())


@app.route("/telegram/auth/status", methods=["GET"])
@login_required
def telegram_auth_status():
    """
    GET /telegram/auth/status
    Returns whether Telethon is configured and whether we are already
    authenticated (session file exists). Authed = can send to anyone.
    """
    return jsonify(tl_auth_status())


@app.route("/telegram/auth/start", methods=["POST"])
@login_required
def telegram_auth_start():
    """
    POST /telegram/auth/start
    Sends an OTP to the TELEGRAM_PHONE number configured in .env.
    Call this once; then verify with /telegram/auth/verify.
    No body required.
    """
    result = tl_auth_start()
    status = 200 if result.get("success") else 400
    return jsonify(result), status


@app.route("/telegram/auth/verify", methods=["POST"])
@login_required
def telegram_auth_verify():
    """
    POST /telegram/auth/verify
    JSON body: { "code": "12345" }            (required)
               { "password": "2FA_pass" }     (only if 2FA is enabled)
    Completes Telethon authentication and saves the session file.
    After this you can send messages to any Telegram user.
    """
    data     = request.get_json(force=True) or {}
    code     = str(data.get("code",     "")).strip()
    password = str(data.get("password", "")).strip()
    if not code:
        return jsonify({"success": False, "message": "code is required"}), 400
    result = tl_auth_verify(code, password)
    status = 200 if result.get("success") else 400
    return jsonify(result), status


# ── Feature 2: Summarization ──────────────────────────────────────────────────

@app.route("/summarize", methods=["POST"])
@login_required
def summarize_route():
    """
    POST /summarize
    JSON body: {
        "text": "Long text here...",         # raw text  (option A)
        "email": { email dict },             # email obj (option B)
        "message": { message dict },         # msg obj   (option C)
        "mode": "simple" | "transformers"    # optional, default "simple"
    }
    Returns: { "summary": str }
    """
    data = request.get_json(force=True) or {}
    mode = data.get("mode", Config.SUMMARIZATION_MODE)

    if "email" in data:
        summary = summarize_email(data["email"], mode=mode)
    elif "message" in data:
        summary = summarize_message(data["message"], mode=mode)
    elif "text" in data:
        summary = summarize_text(data["text"], mode=mode)
    else:
        return jsonify({"error": "Provide 'text', 'email', or 'message' in request body"}), 400

    return jsonify({"summary": summary})


@app.route("/summarize/tts", methods=["POST"])
@login_required
def summarize_tts_route():
    """
    POST /summarize/tts
    Same as /summarize, but also generates a TTS audio file for the summary.
    Returns: { "summary": str, "audio_url": str }
    """
    data    = request.get_json(force=True) or {}
    mode    = data.get("mode", Config.SUMMARIZATION_MODE)
    lang    = data.get("lang", Config.DEFAULT_LANGUAGE)

    if "email" in data:
        summary = summarize_email(data["email"], mode=mode)
    elif "message" in data:
        summary = summarize_message(data["message"], mode=mode)
    elif "text" in data:
        summary = summarize_text(data["text"], mode=mode)
    else:
        return jsonify({"error": "Provide 'text', 'email', or 'message'"}), 400

    try:
        out_path  = os.path.join(Config.UPLOAD_FOLDER, f"summary_{uuid.uuid4().hex}.wav")
        speak_text_lang(summary, lang=lang, out_path=out_path)
        audio_url = f"/static/audio/{os.path.basename(out_path)}"
    except Exception as exc:
        app.logger.exception("TTS failed in /summarize/tts")
        audio_url = None

    return jsonify({"summary": summary, "audio_url": audio_url})


# ── Feature 3: Suggested Replies ─────────────────────────────────────────────

@app.route("/reply/suggest", methods=["POST"])
@login_required
def suggest_reply_route():
    """
    POST /reply/suggest
    JSON body: { "message": "Can we meet tomorrow?" }
    Returns:   { "category": str, "suggestions": [str, ...], "primary": str }
    """
    data    = request.get_json(force=True) or {}
    message = data.get("message", "").strip()

    if not message:
        return jsonify({"error": "message field is required"}), 400

    result = suggest_reply(message)
    return jsonify(result)


@app.route("/reply/suggest-tts", methods=["POST"])
@login_required
def suggest_reply_tts_route():
    """
    POST /reply/suggest-tts
    Same as /reply/suggest but also returns a TTS audio URL for the primary
    suggestion so the assistant can read it aloud.
    JSON body: { "message": str, "lang": str (optional) }
    Returns:   { ...suggest_reply fields..., "audio_url": str }
    """
    data    = request.get_json(force=True) or {}
    message = data.get("message", "").strip()
    lang    = data.get("lang", Config.DEFAULT_LANGUAGE)

    if not message:
        return jsonify({"error": "message field is required"}), 400

    result   = suggest_reply(message)
    primary  = result["primary"]
    # Prepend a brief intro for natural TTS reading
    spoken   = f"Suggested reply: {primary}"
    try:
        out_path = os.path.join(Config.UPLOAD_FOLDER, f"reply_{uuid.uuid4().hex}.wav")
        speak_text_lang(spoken, lang=lang, out_path=out_path)
        result["audio_url"] = f"/static/audio/{os.path.basename(out_path)}"
    except Exception as exc:
        app.logger.exception("TTS failed in /reply/suggest-tts")
        result["audio_url"] = None

    return jsonify(result)


# ── Feature 4: Voice Confirmation ─────────────────────────────────────────────

@app.route("/confirm/start", methods=["POST"])
@login_required
def confirm_start():
    """
    POST /confirm/start
    JSON body: { "action_text": "send a message to Alice: Hello!", "lang": "en" }

    Step 1 of voice confirmation:
      • Generates a TTS file that reads back the action and asks for yes/no.
      • Returns { "audio_url": str } — browser automatically plays it.
    """
    data        = request.get_json(force=True) or {}
    action_text = data.get("action_text", "").strip()
    lang        = data.get("lang", Config.DEFAULT_LANGUAGE)
    action_type = (data.get("action_type") or "").strip()
    payload     = data.get("payload") or {}

    if not action_text:
        return jsonify({"error": "action_text is required"}), 400

    valid, validation_message = _validate_action_payload(action_type, payload)
    if not valid:
        _log_user_action("confirm_start_rejected", status="error", details={"reason": validation_message})
        return jsonify({"error": validation_message}), 400

    challenge_id = _create_security_challenge(action_type, action_text, payload)

    audio_url = speak_confirmation(action_text, lang=lang)
    _log_user_action("confirm_start", details={"action_type": action_type})
    return jsonify(
        {
            "audio_url": audio_url,
            "action_text": action_text,
            "challenge_id": challenge_id,
            "requires_pin": True,
            "pin_hint": "Speak or enter your PIN to continue",
        }
    )


@app.route("/confirm/answer", methods=["POST"])
@login_required
def confirm_answer():
    """
    POST /confirm/answer
    JSON body: { "transcription": "yes please" }

    Step 2 of voice confirmation:
      • Browser records the user's yes/no answer and sends the transcription.
      • Returns { "confirmed": true/false }
    """
    data          = request.get_json(force=True) or {}
    transcription = data.get("transcription", "").strip()
    challenge_id  = (data.get("challenge_id") or "").strip()
    pin           = str(data.get("pin", "")).strip()

    _cleanup_security_state()
    challenges = session.get("action_challenges", {})
    challenge = challenges.get(challenge_id)
    if not challenge:
        _log_user_action("confirm_answer_failed", status="error", details={"reason": "challenge_not_found"})
        return jsonify({"error": "Invalid or expired challenge"}), 400

    confirmed = check_confirmation_answer(transcription)
    if not confirmed:
        _log_user_action("confirm_rejected", details={"challenge_id": challenge_id})
        return jsonify(
            {
                "confirmed": False,
                "transcription": transcription,
                "result": "cancelled",
            }
        )

    user_email = _current_email()
    if not verify_pin(pin, user_email=user_email):
        challenge["attempts"] = int(challenge.get("attempts", 0)) + 1
        challenges[challenge_id] = challenge
        session["action_challenges"] = challenges
        session.modified = True
        remaining = max(0, Config.PIN_MAX_ATTEMPTS - challenge["attempts"])
        _log_user_action("pin_verification_failed", status="error", details={"remaining": remaining})
        return jsonify(
            {
                "confirmed": False,
                "pin_verified": False,
                "result": "pin_failed",
                "remaining_attempts": remaining,
            }
        ), 401

    challenge["confirmed"] = True
    challenges[challenge_id] = challenge
    session["action_challenges"] = challenges
    token = _issue_confirmation_token(challenge_id)

    _log_user_action("confirm_approved", details={"challenge_id": challenge_id})
    return jsonify(
        {
            "confirmed": True,
            "pin_verified": True,
            "confirmation_token": token,
            "transcription": transcription,
            "result": "confirmed",
        }
    )


# ── Feature 5: Language API ────────────────────────────────────────────────────

@app.route("/language", methods=["GET"])
@login_required
def get_language():
    """GET /language — Return the current language and all supported languages."""
    current_lang = session.get("language", Config.DEFAULT_LANGUAGE)
    return jsonify({
        "current"  : current_lang,
        "languages": Config.SUPPORTED_LANGUAGES,
    })


@app.route("/language", methods=["POST"])
@login_required
def set_language():
    """
    POST /language
    JSON body: { "lang": "hi" }
    Switch the session language for TTS output.
    Returns: { "lang": str, "name": str }
    """
    data = request.get_json(force=True) or {}
    lang = data.get("lang", "en").strip().lower()

    if lang not in Config.SUPPORTED_LANGUAGES:
        return jsonify({
            "error": f"Unsupported language '{lang}'. "
                     f"Supported: {list(Config.SUPPORTED_LANGUAGES.keys())}"
        }), 400

    session["language"] = lang
    session.modified = True   # force Flask to persist the session cookie
    return jsonify({
        "lang"   : lang,
        "name"   : Config.SUPPORTED_LANGUAGES[lang],
        "message": f"Language switched to {Config.SUPPORTED_LANGUAGES[lang]}.",
    })


@app.route("/language/tts-demo", methods=["POST"])
@login_required
def language_tts_demo():
    """
    POST /language/tts-demo
    JSON body: { "lang": "hi" }
    Generate a short TTS demo clip in the requested language using gTTS
    for Hindi/Marathi or pyttsx3 for English.
    Returns: { "audio_url": str }
    """
    from services.lang_utils import speak_multilang, LANG_NAMES
    data = request.get_json(force=True) or {}
    lang = data.get("lang", Config.DEFAULT_LANGUAGE).strip().lower()

    # Demo texts in each supported language
    _demo_texts = {
        "en": "Hello! Voice assistant is now in English mode. You can say commands like read email, send email, or summarize email.",
        "hi": "नमस्ते! वॉइस असिस्टेंट अब हिंदी मोड में है। आप रीड ईमेल, सेंड ईमेल जैसे कमांड दे सकते हैं।",
        "mr": "नमस्कार! वॉइस असिस्टंट आता मराठी मोडमध्ये आहे. तुम्ही रीड ईमेल, सेंड ईमेल असे कमांड देऊ शकता.",
    }
    text     = _demo_texts.get(lang, f"Language switched to {LANG_NAMES.get(lang, lang)}.")
    out_path = speak_multilang(text, lang=lang)
    if not out_path:
        return jsonify({"error": "TTS failed"}), 500
    return jsonify({"audio_url": f"/static/audio/{os.path.basename(out_path)}"})



@app.route("/login/success-audio", methods=["POST"])
@login_required
def login_success_audio():
    """
    Generate a one-time TTS clip announcing successful sign-in.
    Returns: { "audio_url": str }
    """
    data = request.get_json(silent=True) or {}
    lang = (data.get("lang") or session.get("language") or Config.DEFAULT_LANGUAGE).strip().lower()
    text = "Sign in successful. Welcome to VoiceMail."
    out_path = os.path.join(Config.UPLOAD_FOLDER, f"login_success_{uuid.uuid4().hex}.wav")
    try:
        speak_text_lang(text, lang=lang, out_path=out_path)
    except Exception as exc:
        return jsonify({"error": f"TTS failed: {exc}"}), 500
    return jsonify({"audio_url": f"/static/audio/{os.path.basename(out_path)}"})

if __name__ == "__main__":
    app.run(debug=app.config["DEBUG"], port=5000)
