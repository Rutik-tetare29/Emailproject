# VoiceMail — Setup Guide

## 1. Install dependencies
```bash
cd voice_email_app
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

## 2. Whisper model
No manual download needed. The Whisper model is **automatically downloaded** to `~/.cache/whisper/` on first run.
Default model: `base` (~145 MB). You can change it in `.env` (`WHISPER_MODEL=tiny/base/small/medium`).

## 3. Configure environment
```bash
copy .env.example .env
# Edit .env — set SECRET_KEY (and optionally WHISPER_MODEL)
```

## 4. Google OAuth setup (optional — for Gmail API login)
1. Go to https://console.cloud.google.com/
2. Create a project → Enable **Gmail API**
3. Credentials → OAuth 2.0 Client ID → Web Application
4. Add `http://localhost:5000/login/google/callback` as redirect URI
5. Download JSON → save as `voice_email_app/client_secrets.json`

## 5. Gmail App Password (optional — for SMTP/IMAP login)
1. Enable 2FA on your Google Account
2. Go to https://myaccount.google.com/apppasswords
3. Generate a password for "Mail" — use it in the login form

## 6. Run the app
```bash
python app.py
# Open http://localhost:5000
```

## 7. Milestone 4 security configuration
Set these in `.env`:

```bash
ADMIN_EMAILS=admin@example.com
VOICE_ACTION_PIN=2468
PIN_MAX_ATTEMPTS=3
ACTION_CHALLENGE_TTL=300
ACTION_TOKEN_TTL=300
```

- `ADMIN_EMAILS` users can access `/admin`.
- `VOICE_ACTION_PIN` is required before secure actions (email/message send).

## 8. Run with Docker
Build and run with Docker Compose:

```bash
docker compose up --build
```

Open `http://localhost:5000` in your browser.

## 9. Production Docker and Deploy Checklist
Use these before deploying to a server or cloud container platform:

1. Set secure environment values in `.env`:
```bash
DEBUG=false
OAUTHLIB_INSECURE_TRANSPORT=0
SECRET_KEY=<long-random-secret>
GOOGLE_REDIRECT_URI=https://<your-domain>/login/google/callback
ADMIN_EMAILS=<admin1@example.com,admin2@example.com>
```

2. Build and run in detached mode:
```bash
docker compose up -d --build
```

3. Verify health endpoint:
```bash
curl http://localhost:5000/health
```

4. Data persistence:
- User registry and admin activity logs persist in `./data`.
- Generated audio persists in `./static/audio`.
- Whisper model cache persists in Docker volume `whisper-cache`.

5. Reverse proxy/TLS:
- Put Nginx, Caddy, or cloud ingress in front of the app.
- Terminate HTTPS at the proxy.
- Forward traffic to container port `5000`.

## Quick command reference
| Voice command | Action |
|---|---|
| "read my emails" | Fetches & reads inbox |
| "send an email" | Guides to compose form |
| "yes" (after compose) | Moves to PIN verification |
| "2468" (or your PIN) | Completes secure send |
| "help" | Lists available commands |
| "logout" | Signs you out |
