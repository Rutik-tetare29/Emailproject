import os
from dotenv import load_dotenv

# Always load .env from the project root regardless of CWD where Python is launched
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)


class Config:
    # Flask
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
    DEBUG = os.getenv("DEBUG", "false").lower() == "true"

    # Audio temp storage
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "audio")
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)

    # Data storage
    DATA_DIR = os.path.join(BASE_DIR, "data")
    os.makedirs(DATA_DIR, exist_ok=True)

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
    OAUTHLIB_INSECURE_TRANSPORT = os.getenv("OAUTHLIB_INSECURE_TRANSPORT", "1")

    # Whisper STT model
    # Options: tiny (~75MB), base (~145MB), small (~465MB, recommended), medium (~1.5GB)
    # 'small' gives ~30% better accuracy over 'base' with ~8-15 s on CPU.
    # Model is auto-downloaded to ~/.cache/whisper/ on first run.
    WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")

    # Pinned OAuth redirect URI — must match exactly what is registered in
    # Google Cloud Console → APIs & Services → Credentials → Authorised redirect URIs
    GOOGLE_REDIRECT_URI = os.getenv(
        "GOOGLE_REDIRECT_URI",
        "http://127.0.0.1:5000/login/google/callback",
    )

    # Gmail IMAP / SMTP (App Password flow)
    GMAIL_IMAP_HOST = "imap.gmail.com"
    GMAIL_SMTP_HOST = "smtp.gmail.com"
    GMAIL_SMTP_PORT = 587

    # ── Multi-Language Support (Feature 5) ────────────────────────────────────
    # Default language for TTS and (future) STT language hints.
    # BCP-47 prefix codes: "en", "hi", "es", "fr", "de", "it", "pt", "ja", "zh"
    # Change the env var VOICEMAIL_LANGUAGE in your .env file to switch language.
    DEFAULT_LANGUAGE = os.getenv("VOICEMAIL_LANGUAGE", "en")

    # All languages that the UI language-switcher will offer.
    # Keys = BCP-47 prefix code, Values = human-readable display name.
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

    # ── Messaging service ─────────────────────────────────────────────────────
    # MESSAGING_BACKEND options:
    #   "telethon"   - Telethon User API: send to ANYONE by @username / +phone (recommended)
    #   "telegram"   - Bot API: recipients must message the bot first
    #   "simulation" - No real network, offline demo
    MESSAGING_BACKEND     = os.getenv("MESSAGING_BACKEND", "simulation")

    # Telethon User API (get from https://my.telegram.org -> API Development Tools)
    TELEGRAM_API_ID       = os.getenv("TELEGRAM_API_ID",   "")   # integer app id
    TELEGRAM_API_HASH     = os.getenv("TELEGRAM_API_HASH",  "")   # 32-char hex string
    TELEGRAM_PHONE        = os.getenv("TELEGRAM_PHONE",     "")   # e.g. +919876543210

    # Bot API (kept for backward-compat / MESSAGING_BACKEND=telegram)
    TELEGRAM_BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
    # Comma-separated contact:chat_id pairs e.g. "Alice:123456789,Bob:987654321"
    TELEGRAM_CHAT_IDS_RAW = os.getenv("TELEGRAM_CHAT_IDS",  "")

    # ── Summarization (Feature 2) ─────────────────────────────────────────────
    # "simple" uses the built-in rule-based summarizer (no extra dependencies).
    # "transformers" uses HuggingFace — run: pip install transformers torch
    SUMMARIZATION_MODE = os.getenv("SUMMARIZATION_MODE", "simple")  # "simple" | "transformers"

    # ── Milestone 4: Security / RBAC / Admin ────────────────────────────────
    # Comma-separated admin emails: "admin@example.com,owner@example.com"
    ADMIN_EMAILS_RAW = os.getenv("ADMIN_EMAILS", "")
    ADMIN_EMAILS = {
        e.strip().lower()
        for e in ADMIN_EMAILS_RAW.split(",")
        if e.strip()
    }
    DEFAULT_USER_ROLE = os.getenv("DEFAULT_USER_ROLE", "user")

    # Voice PIN used for high-risk actions (send email / send message)
    VOICE_ACTION_PIN = os.getenv("VOICE_ACTION_PIN", "2468")
    PIN_MAX_ATTEMPTS = int(os.getenv("PIN_MAX_ATTEMPTS", "3"))

    # Challenge / token expiry windows (seconds)
    ACTION_CHALLENGE_TTL = int(os.getenv("ACTION_CHALLENGE_TTL", "300"))
    ACTION_TOKEN_TTL = int(os.getenv("ACTION_TOKEN_TTL", "300"))
