import json
import os
import hashlib
import hmac
import re
import unicodedata
from datetime import datetime, timezone

from config import Config

_PROFILE_PATH = os.path.join(Config.DATA_DIR, "user_profiles.json")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_profiles() -> dict:
    if not os.path.exists(_PROFILE_PATH):
        return {}
    try:
        with open(_PROFILE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_profiles(data: dict) -> None:
    with open(_PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _ensure_profile(user_email: str) -> tuple[dict, dict]:
    profiles = _read_profiles()
    key = (user_email or "").strip().lower()
    if not key:
        return profiles, {"saved_emails": [], "saved_contacts": [], "pin_hash": ""}
    profile = profiles.get(key)
    if not isinstance(profile, dict):
        profile = {"saved_emails": [], "saved_contacts": [], "pin_hash": ""}
        profiles[key] = profile
    profile.setdefault("saved_emails", [])
    profile.setdefault("saved_contacts", [])
    profile.setdefault("pin_hash", "")
    return profiles, profile


def _normalize_pin(pin: str) -> str:
    text = str(pin or "").strip().lower()
    text = re.sub(r"[^\w]+", " ", text)

    words = {
        "zero": "0", "oh": "0", "o": "0",
        "one": "1", "two": "2", "to": "2", "too": "2",
        "three": "3", "four": "4", "for": "4",
        "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    }

    parts: list[str] = []
    for token in text.split():
        if token in words:
            parts.append(words[token])
            continue

        digits = []
        for ch in token:
            try:
                digits.append(str(unicodedata.digit(ch)))
            except (TypeError, ValueError):
                if "0" <= ch <= "9":
                    digits.append(ch)
        if digits:
            parts.append("".join(digits))

    return "".join(parts)


def _pin_digest(pin: str) -> str:
    normalized = _normalize_pin(pin)
    salted = f"{Config.SECRET_KEY}:{normalized}".encode("utf-8")
    return hashlib.sha256(salted).hexdigest()


def _upsert_entry(entries: list[dict], value: str, display: str | None = None) -> None:
    value = (value or "").strip()
    if not value:
        return
    display = (display or value).strip() or value
    value_key = value.lower()
    now = _utc_now_iso()

    existing = next((item for item in entries if (item.get("value") or "").strip().lower() == value_key), None)
    if existing:
        existing["display"] = display
        existing["last_used_at"] = now
        existing["use_count"] = int(existing.get("use_count", 0)) + 1
        return

    entries.append({
        "value": value,
        "display": display,
        "last_used_at": now,
        "use_count": 1,
    })


def add_saved_email(user_email: str, email_addr: str) -> None:
    profiles, profile = _ensure_profile(user_email)
    key = (user_email or "").strip().lower()
    if not key:
        return
    _upsert_entry(profile["saved_emails"], email_addr)
    profile["saved_emails"] = sorted(
        profile["saved_emails"],
        key=lambda item: (item.get("last_used_at", ""), item.get("use_count", 0)),
        reverse=True,
    )
    _write_profiles(profiles)


def add_saved_contact(user_email: str, contact_name: str) -> None:
    profiles, profile = _ensure_profile(user_email)
    key = (user_email or "").strip().lower()
    if not key:
        return
    _upsert_entry(profile["saved_contacts"], contact_name)
    profile["saved_contacts"] = sorted(
        profile["saved_contacts"],
        key=lambda item: (item.get("last_used_at", ""), item.get("use_count", 0)),
        reverse=True,
    )
    _write_profiles(profiles)


def get_profile(user_email: str) -> dict:
    _, profile = _ensure_profile(user_email)
    return {
        "saved_emails": profile.get("saved_emails", []),
        "saved_contacts": profile.get("saved_contacts", []),
        "has_custom_pin": bool(profile.get("pin_hash")),
    }


def has_custom_pin(user_email: str) -> bool:
    _, profile = _ensure_profile(user_email)
    return bool(profile.get("pin_hash"))


def set_profile_pin(user_email: str, new_pin: str) -> tuple[bool, str]:
    key = (user_email or "").strip().lower()
    if not key:
        return False, "Missing user email"
    normalized = _normalize_pin(new_pin)
    if len(normalized) < 4 or len(normalized) > 8:
        return False, "PIN must be 4 to 8 digits"

    profiles, profile = _ensure_profile(key)
    profile["pin_hash"] = _pin_digest(normalized)
    _write_profiles(profiles)
    return True, "PIN updated"


def verify_profile_pin(user_email: str, raw_pin: str) -> bool:
    key = (user_email or "").strip().lower()
    if not key:
        return False
    _, profile = _ensure_profile(key)
    pin_hash = profile.get("pin_hash") or ""
    if not pin_hash:
        return False
    candidate = _pin_digest(raw_pin)
    return hmac.compare_digest(candidate, pin_hash)