import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from config import Config
from services.profile_service import verify_profile_pin

_ACTIVITY_LOG_PATH = os.path.join(Config.DATA_DIR, "activity_log.json")
_USER_REGISTRY_PATH = os.path.join(Config.DATA_DIR, "user_registry.json")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_file(path: str, default: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=2)


def _read_json(path: str, default: Any):
    _ensure_file(path, default)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: str, value: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, indent=2)


def resolve_role(email: str) -> str:
    email = (email or "").strip().lower()
    if email and email in Config.ADMIN_EMAILS:
        return "admin"
    return Config.DEFAULT_USER_ROLE


def register_user(email: str, auth_type: str, role: str | None = None) -> None:
    email = (email or "").strip().lower()
    if not email:
        return
    role = role or resolve_role(email)

    users = _read_json(_USER_REGISTRY_PATH, [])
    now = _utc_now().isoformat()

    existing = next((u for u in users if u.get("email") == email), None)
    if existing:
        existing["last_login_at"] = now
        existing["auth_type"] = auth_type
        existing["role"] = role
    else:
        users.append(
            {
                "email": email,
                "role": role,
                "auth_type": auth_type,
                "first_seen_at": now,
                "last_login_at": now,
            }
        )

    _write_json(_USER_REGISTRY_PATH, users)


def log_activity(
    user_email: str,
    role: str,
    action: str,
    status: str = "success",
    details: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    logs = _read_json(_ACTIVITY_LOG_PATH, [])
    logs.append(
        {
            "timestamp": _utc_now().isoformat(),
            "user_email": (user_email or "anonymous").lower(),
            "role": role or "user",
            "action": action,
            "status": status,
            "ip": ip or "",
            "details": details or {},
        }
    )

    # Keep recent history bounded for predictable file size.
    if len(logs) > 5000:
        logs = logs[-5000:]

    _write_json(_ACTIVITY_LOG_PATH, logs)


def get_metrics(activity_limit: int = 50, error_limit: int = 30) -> dict[str, Any]:
    users = _read_json(_USER_REGISTRY_PATH, [])
    logs = _read_json(_ACTIVITY_LOG_PATH, [])

    logs_sorted = sorted(logs, key=lambda x: x.get("timestamp", ""), reverse=True)
    recent_activity = logs_sorted[:activity_limit]
    recent_errors = [x for x in logs_sorted if x.get("status") == "error"][:error_limit]

    total_emails_sent = sum(1 for x in logs if x.get("action") == "email_sent" and x.get("status") == "success")
    total_messages_sent = sum(1 for x in logs if x.get("action") == "message_sent" and x.get("status") == "success")

    return {
        "totals": {
            "users": len(users),
            "emails_sent": total_emails_sent,
            "messages_sent": total_messages_sent,
            "events": len(logs),
            "errors": len([x for x in logs if x.get("status") == "error"]),
        },
        "recent_activity": recent_activity,
        "recent_errors": recent_errors,
    }


def get_users() -> list[dict[str, Any]]:
    """Return all registered users sorted by last login (most recent first)."""
    users = _read_json(_USER_REGISTRY_PATH, [])
    return sorted(users, key=lambda u: u.get("last_login_at", ""), reverse=True)


def update_user_role(email: str, new_role: str) -> bool:
    """Update the role for a registered user. Returns True if found and updated."""
    email = (email or "").strip().lower()
    if not email or new_role not in ("user", "admin"):
        return False
    users = _read_json(_USER_REGISTRY_PATH, [])
    existing = next((u for u in users if u.get("email") == email), None)
    if not existing:
        return False
    existing["role"] = new_role
    _write_json(_USER_REGISTRY_PATH, users)
    return True


def get_activity_log(
    limit: int = 200,
    status_filter: str | None = None,
    user_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Return filtered activity log entries, newest first."""
    logs = _read_json(_ACTIVITY_LOG_PATH, [])
    logs_sorted = sorted(logs, key=lambda x: x.get("timestamp", ""), reverse=True)
    if status_filter:
        logs_sorted = [x for x in logs_sorted if x.get("status") == status_filter]
    if user_filter:
        needle = user_filter.strip().lower()
        logs_sorted = [x for x in logs_sorted if needle in (x.get("user_email") or "")]
    return logs_sorted[:limit]


def hash_payload(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_pin_input(raw: str) -> str:
    text = (raw or "").strip().lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)

    words = {
        "zero": "0", "oh": "0", "o": "0",
        "one": "1", "two": "2", "to": "2", "too": "2",
        "three": "3", "four": "4", "for": "4",
        "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    }

    parts = []
    for token in text.split():
        if token in words:
            parts.append(words[token])
        elif token.isdigit():
            parts.append(token)

    return "".join(parts)


def verify_pin(raw_pin: str, user_email: str = "") -> bool:
    if user_email and verify_profile_pin(user_email, raw_pin):
        return True
    candidate = normalize_pin_input(raw_pin)
    target = normalize_pin_input(Config.VOICE_ACTION_PIN)
    return bool(candidate) and hmac.compare_digest(candidate, target)
