# VoiceMail - Voice Based Email Assistant

A production-ready Flask application for voice-driven email and messaging workflows, with admin observability, role-based access control, and Docker deployment support.

## Highlights
- Voice-first interaction for email and messaging operations
- Gmail support through OAuth and app-password flows
- Telegram messaging support (bot and Telethon-based workflows)
- Multi-language experience with language switching and TTS responses
- Admin dashboard with metrics, activity logs, and user management (edit/delete)
- Secure action confirmation using challenge tokens and PIN verification
- Containerized deployment with Gunicorn, health checks, and persistent volumes

## Core Features
### 1. Authentication and Access
- Session-based login with Flask-Login
- User/admin role resolution and protected admin routes
- Secure logout and session cleanup

### 2. Voice Command Engine
- Browser audio upload to backend for STT + intent handling
- Guided compose flows for email/message actions
- Voice responses with generated audio endpoints

### 3. Email Workflows
- Read inbox messages
- Voice and text compose flows
- Secure send flow with confirmation token + PIN check

### 4. Messaging Workflows
- Contact listing and discovery
- Send and fetch Telegram messages
- Telethon auth status/start/verify routes

### 5. AI Utility Endpoints
- Text/email/message summarization
- Suggested reply generation
- Optional TTS output for summaries/replies

### 6. Admin Console
- Dashboard metrics (users, sends, events, errors)
- Activity log with filtering and export
- User role edit and user delete operations

## Technology Stack
- Python 3.11
- Flask, Flask-Login, Gunicorn
- faster-whisper / openai-whisper (fallback package)
- pyttsx3, gTTS
- Google OAuth + Gmail APIs
- Telethon + Telegram bot workflows
- Docker + Docker Compose

## Project Structure
```text
voice_email_app/
  app.py
  config.py
  services/
  templates/
  static/
  auth/
  data/
  Dockerfile
  docker-compose.yml
  requirements.txt
  LICENSE
  SETUP.md
  FINAL_PROJECT_REPORT.md
```

## Quick Start (Local)
1. Create and activate virtual environment
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

2. Install dependencies
```powershell
pip install -r requirements.txt
```

3. Configure environment
```powershell
copy .env.example .env
```
Update `.env` with your values (`SECRET_KEY`, `ADMIN_EMAILS`, auth credentials, etc.).

4. Run app
```powershell
python app.py
```
Open `http://localhost:5000`.

## Docker Run (Recommended)
Build and start:
```powershell
docker compose up -d --build
```

Check health:
```powershell
curl http://localhost:5000/health
```

Follow logs:
```powershell
docker compose logs -f voice-email-app
```

## Production Readiness
This project includes production-focused container settings:
- Gunicorn process manager
- Container health checks (`/health`)
- Non-root runtime user
- Persistent volume mounts for `data/`, `static/audio/`, and Whisper cache
- Docker Compose restart policy

Before internet-facing deployment:
- Set a strong `SECRET_KEY`
- Configure HTTPS and update `GOOGLE_REDIRECT_URI`
- Keep `DEBUG=false`
- Keep `OAUTHLIB_INSECURE_TRANSPORT=0`
- Rotate/secure all API tokens and credentials

## API Overview
### Voice
- `POST /voice/login-transcribe`
- `POST /voice/process`
- `GET /voice/service-greeting`
- `POST /voice/compose-text`
- `POST /voice/msg-compose-text`

### Email and Messages
- `GET /emails`
- `POST /send-email`
- `GET /messages`
- `POST /messages/send`

### Admin
- `GET /admin`
- `GET /admin/metrics`
- `GET /admin/users`
- `POST /admin/user/edit`
- `POST /admin/user/remove`
- `GET /admin/activity`

### Utilities
- `POST /summarize`
- `POST /reply/suggest`
- `GET /language`
- `POST /language`

## Security Notes
- High-risk actions are guarded by confirmation-token and PIN checks
- Admin actions are protected via role-based middleware
- Activity logging is persisted with resilient JSON write/read mechanisms

## Documentation
- Setup guide: `SETUP.md`
- Final report: `FINAL_PROJECT_REPORT.md`
- License: `LICENSE`

## License
MIT License. See `LICENSE`.
