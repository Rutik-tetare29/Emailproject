# VoiceMail - Voice-Enabled Email and Messaging Assistant

VoiceMail is a Flask application that lets users interact with email and messaging features using voice and text commands. It includes Gmail integration, Telegram messaging flows, summarization and reply assistance, multilingual speech support, and admin/security controls.

## Features

- Voice and text command processing
- Gmail login options:
  - Google OAuth flow
  - Gmail app-password flow
- Email workflows:
  - Read inbox
  - Compose and send email
- Telegram workflows:
  - Discover/register contacts
  - Send/read messages
- AI-style helpers:
  - Text/email/message summarization
  - Reply suggestion
- Multilingual speech support (configurable language)
- Security and governance:
  - Role-based admin access
  - Activity logs and user management
  - PIN challenge for sensitive actions
- Production-friendly deployment:
  - Dockerfile + Docker Compose
  - Gunicorn runtime
  - Health checks and persistent data mounts

## Tech Stack

- Python 3.11
- Flask, Flask-Login
- Gunicorn
- SpeechRecognition (lightweight STT backend)
- pyttsx3 and gTTS (TTS)
- Google Auth + Gmail API client libraries
- Telethon

## Project Structure

- app.py: Main Flask app and API routes
- config.py: Environment-driven configuration
- auth/: Authentication blueprints (OAuth and app-password flow)
- services/: Domain services (email, voice, messaging, security, summarization)
- templates/: HTML pages
- static/: JS, CSS, generated audio
- data/: JSON persistence files
- Dockerfile, docker-compose.yml: Container setup

## Prerequisites

- Python 3.11+
- pip
- (Optional) Docker Desktop

## Local Setup

1. Clone and open the project folder.
2. Create and activate a virtual environment.
3. Install dependencies.
4. Create your environment file.

Windows (PowerShell):

    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    pip install -r requirements.txt
    Copy-Item .env.example .env

5. Edit .env values as needed (see Environment Configuration).

## Environment Configuration

Important variables from .env.example:

- SECRET_KEY: Flask secret
- DEBUG: true or false
- HOST, PORT: server bind settings
- SESSION_COOKIE_SECURE, REMEMBER_COOKIE_SECURE: cookie security
- STT_DEFAULT_LANG: speech language hint (for example en, hi, mr)
- GOOGLE_CLIENT_SECRETS_FILE: path to Google OAuth client JSON
- OAUTHLIB_INSECURE_TRANSPORT: set 1 only for local HTTP testing
- GOOGLE_REDIRECT_URI: must match Google Console redirect URI exactly
- ADMIN_EMAILS: comma-separated admin emails
- VOICE_ACTION_PIN: PIN required for secure actions
- PIN_MAX_ATTEMPTS, ACTION_CHALLENGE_TTL, ACTION_TOKEN_TTL: security tuning

## Google OAuth Setup (Optional)

1. Open Google Cloud Console.
2. Create/select a project.
3. Enable Gmail API.
4. Create OAuth 2.0 Client ID (Web application).
5. Add redirect URI:

    http://localhost:5000/login/google/callback

6. Download the credentials JSON and place it as client_secrets.json in the project root (or update GOOGLE_CLIENT_SECRETS_FILE).

## Gmail App Password Setup (Optional)

1. Enable 2-step verification on your Google account.
2. Generate an app password for Mail.
3. Use that app password in the app-password login flow.

## Run the Application

Development:

    python app.py

Open:

    http://localhost:5000

Health check endpoint:

    http://localhost:5000/health

## Docker Run

Build and start:

    docker compose up --build

Detached mode:

    docker compose up -d --build

Stop:

    docker compose down

The compose setup maps:

- ./data -> /app/data
- ./static/audio -> /app/static/audio

## Deployment Notes

- Use DEBUG=false in production.
- Keep OAUTHLIB_INSECURE_TRANSPORT=0 in production.
- Use a strong SECRET_KEY.
- Ensure GOOGLE_REDIRECT_URI matches your deployed domain callback URL.
- Put the app behind HTTPS/reverse proxy when internet-facing.

## API Highlights

Common route groups include:

- Public: /, /login, /health
- Voice: /voice/process, /voice/login-transcribe
- Email: /emails, /send-email
- Messaging: /messages, /messages/send, /telegram/*
- Utilities: /summarize, /reply/suggest
- Admin: /admin, /admin/metrics, /admin/users, /admin/activity

## License

MIT License. See LICENSE.
