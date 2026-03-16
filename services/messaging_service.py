"""
Messaging Service - Telethon User API (send to anyone like Gmail)

MODES
-----
MESSAGING_BACKEND=telethon    <- Real Telegram, sends to ANY user by username/phone
MESSAGING_BACKEND=telegram    <- Bot API (only users who messaged bot first)
MESSAGING_BACKEND=simulation  <- Offline demo mode

TELETHON SETUP (one-time)
--------------------------
1. Visit https://my.telegram.org -> Log in -> API Development Tools
2. Create app, copy api_id (number) and api_hash (string)
3. Add to .env:
       MESSAGING_BACKEND=telethon
       TELEGRAM_API_ID=12345678
       TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890
       TELEGRAM_PHONE=+919876543210
4. Restart Flask, then visit GET /telegram/auth/status
5. POST /telegram/auth/start                     -> sends OTP to your phone
6. POST /telegram/auth/verify  {"code":"12345"}  -> completes auth
7. Session saved to data/telegram.session        -> no re-auth needed
After auth you can send to: @username  |  +91XXXXXXXXXX  |  contact name
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime

from config import Config

logger = logging.getLogger(__name__)

# ---- paths ------------------------------------------------------------------
_DATA_DIR      = Config.DATA_DIR
_SIM_FILE      = os.path.join(_DATA_DIR, "messages.json")
_SESSION_FILE  = os.path.join(_DATA_DIR, "telegram")   # .session appended by Telethon
_CONTACTS_FILE = os.path.join(_DATA_DIR, "telegram_contacts.json")
_OFFSET_FILE   = os.path.join(_DATA_DIR, "telegram_offset.json")
_PENDING_FILE  = os.path.join(_DATA_DIR, "tl_pending_auth.json")

os.makedirs(_DATA_DIR, exist_ok=True)

# ---- module state -----------------------------------------------------------
_sim_store:   dict = {}
_chat_id_map: dict = {}
_last_offset: int  = 0


# =============================================================================
# MODE HELPERS
# =============================================================================

def _mode() -> str:
    return (Config.MESSAGING_BACKEND or "simulation").lower().strip()

def _is_telethon() -> bool:
    return (
        _mode() == "telethon"
        and bool(getattr(Config, "TELEGRAM_API_ID",  ""))
        and bool(getattr(Config, "TELEGRAM_API_HASH", ""))
        and bool(getattr(Config, "TELEGRAM_PHONE",   ""))
    )

def _is_bot() -> bool:
    return _mode() == "telegram" and bool(getattr(Config, "TELEGRAM_BOT_TOKEN", ""))

def _is_authed() -> bool:
    return os.path.exists(_SESSION_FILE + ".session")


# =============================================================================
# ASYNC RUNNER  (Telethon is async; Flask is sync)
# =============================================================================

def _run(coro):
    """Run an async coroutine from synchronous Flask code."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# =============================================================================
# TELETHON CLIENT FACTORY
# =============================================================================

def _make_client():
    from telethon import TelegramClient
    return TelegramClient(
        _SESSION_FILE,
        int(Config.TELEGRAM_API_ID),
        Config.TELEGRAM_API_HASH,
    )


# =============================================================================
# TELETHON AUTH
# =============================================================================

def tl_auth_status() -> dict:
    if not _is_telethon():
        return {
            "ok":    False,
            "mode":  _mode(),
            "authed": False,
            "reason": (
                "Set MESSAGING_BACKEND=telethon plus TELEGRAM_API_ID, "
                "TELEGRAM_API_HASH, TELEGRAM_PHONE in .env to enable."
            ),
        }
    return {
        "ok":    True,
        "mode":  "telethon",
        "authed": _is_authed(),
        "phone":  Config.TELEGRAM_PHONE,
    }


def tl_auth_start() -> dict:
    """Send OTP to TELEGRAM_PHONE. Must call tl_auth_verify() next."""
    if not _is_telethon():
        return {"success": False, "message": "Telethon not configured."}

    async def _send_code():
        client = _make_client()
        await client.connect()
        result = await client.send_code_request(Config.TELEGRAM_PHONE)
        await client.disconnect()
        return result.phone_code_hash

    try:
        phone_code_hash = _run(_send_code())
        with open(_PENDING_FILE, "w") as f:
            json.dump({"phone_code_hash": phone_code_hash}, f)
        logger.info("Telegram OTP sent to %s", Config.TELEGRAM_PHONE)
        return {
            "success": True,
            "message": (
                f"OTP sent to {Config.TELEGRAM_PHONE}. "
                "Check your Telegram app and call POST /telegram/auth/verify "
                'with {"code":"XXXXX"}.'
            ),
        }
    except Exception as e:
        logger.error("tl_auth_start error: %s", e)
        return {"success": False, "message": str(e)}


def tl_auth_verify(code: str, password: str = "") -> dict:
    """Complete auth with the OTP code (and 2FA password if enabled)."""
    if not os.path.exists(_PENDING_FILE):
        return {"success": False, "message": "No pending auth. Call /telegram/auth/start first."}

    with open(_PENDING_FILE) as f:
        pending = json.load(f)
    phone_code_hash = pending.get("phone_code_hash", "")

    async def _verify():
        from telethon.errors import SessionPasswordNeededError
        client = _make_client()
        await client.connect()
        try:
            await client.sign_in(Config.TELEGRAM_PHONE, code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            if not password:
                await client.disconnect()
                raise ValueError("2FA_REQUIRED")
            await client.sign_in(password=password)
        await client.disconnect()

    try:
        _run(_verify())
        try:
            os.remove(_PENDING_FILE)
        except Exception:
            pass
        logger.info("Telegram authenticated successfully.")
        return {
            "success": True,
            "message": "Telegram authenticated! You can now send messages to anyone.",
        }
    except ValueError as ve:
        if "2FA_REQUIRED" in str(ve):
            return {
                "success": False,
                "message": "2FA enabled. Re-send with password field.",
                "needs_2fa": True,
            }
        return {"success": False, "message": str(ve)}
    except Exception as e:
        logger.error("tl_auth_verify error: %s", e)
        return {"success": False, "message": str(e)}


# =============================================================================
# CONTACT NAME RESOLVER  (Telethon path)
# =============================================================================

async def _tl_resolve_entity(client, receiver: str):
    """
    Resolve a receiver string to a Telethon entity.

    Priority order:
      1. @username  → resolve directly
      2. +phone     → resolve directly
      3. Plain name → search phone contacts first, then open dialogs
         (substring + case-insensitive, picks closest match)
    Returns the entity or raises ValueError with a helpful message.
    """
    r = receiver.strip()

    # Direct resolution for @username or +phone
    if r.startswith("@") or r.startswith("+"):
        return await client.get_entity(r)

    # ── Search phone contacts saved in Telegram ──────────────────────────────
    query = r.lower()
    best_entity = None
    best_score  = 0          # higher = better match

    try:
        result = await client(
            __import__("telethon.tl.functions.contacts", fromlist=["SearchRequest"])
            .SearchRequest(q=r, limit=10)
        )
        for user in getattr(result, "users", []):
            full_name = (f"{user.first_name or ''} {user.last_name or ''}").strip().lower()
            username  = (user.username or "").lower()
            score = 0
            if query == full_name or query == username:
                score = 3                    # exact match
            elif query in full_name or query in username:
                score = 2                    # substring match
            elif any(word in full_name for word in query.split()):
                score = 1                    # word match
            if score > best_score:
                best_score  = score
                best_entity = user
    except Exception as _e:
        logger.debug("Contact search failed: %s", _e)

    if best_entity:
        return best_entity

    # ── Fall back to open dialogs (recent conversations) ────────────────────
    async for dialog in client.iter_dialogs(limit=200):
        name = (dialog.name or "").lower()
        score = 0
        if query == name:
            score = 3
        elif query in name:
            score = 2
        elif any(word in name for word in query.split()):
            score = 1
        if score > best_score:
            best_score  = score
            best_entity = dialog.entity

    if best_entity:
        return best_entity

    raise ValueError(
        f"Could not find '{receiver}' in your Telegram contacts or conversations. "
        "Try using @username or the phone number (+91XXXXXXXXXX) instead."
    )


# =============================================================================
# SEND  (Telethon path)
# =============================================================================

def _tl_send(receiver: str, message: str) -> dict:
    """
    Send via Telethon user API. receiver can be:
      @username   ->  any Telegram user by username
      +91XXXXXXXX ->  any phone number
      Vaibhav     ->  searches your own Telegram contacts & dialogs by name
    """
    if not _is_authed():
        return {
            "success": False,
            "message": "Not authenticated. Visit POST /telegram/auth/start to set up.",
            "data": None,
        }

    async def _do_send():
        client = _make_client()
        await client.connect()
        entity = await _tl_resolve_entity(client, receiver)
        await client.send_message(entity, message)
        await client.disconnect()
        return entity

    try:
        entity = _run(_do_send())
        # Best display name for the response
        display = receiver
        try:
            fn = getattr(entity, "first_name", "") or ""
            ln = getattr(entity, "last_name",  "") or ""
            display = (f"{fn} {ln}").strip() or getattr(entity, "username", receiver) or receiver
        except Exception:
            pass
        msg = {
            "id":        uuid.uuid4().hex,
            "sender":    "me",
            "receiver":  display,
            "text":      message,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }
        logger.info("Telethon message sent to %s (%s)", display, receiver)
        return {"success": True, "message": f"Message sent to {display} via Telegram.", "data": msg}
    except ValueError as ve:
        return {"success": False, "message": str(ve), "data": None}
    except Exception as e:
        logger.error("Telethon send error: %s", e)
        return {"success": False, "message": f"Telegram error: {e}", "data": None}


def _tl_read(contact: str = None) -> dict:
    """Read latest messages via Telethon. contact can be a name, @username, or +phone."""
    if not _is_authed():
        return {"success": False, "message": "Not authenticated.", "data": None}

    async def _do_read():
        client = _make_client()
        await client.connect()
        msgs = []
        if contact:
            entity      = await _tl_resolve_entity(client, contact)
            entity_name = (
                (f"{getattr(entity,'first_name','') or ''} "
                 f"{getattr(entity,'last_name','') or ''}").strip()
                or getattr(entity, "title", None)
                or contact
            )
            async for msg in client.iter_messages(entity, limit=10):
                if msg.text:
                    msgs.append({
                        "id":        uuid.uuid4().hex,
                        "sender":    entity_name,
                        "receiver":  "me",
                        "text":      msg.text,
                        "timestamp": msg.date.isoformat(timespec="seconds"),
                    })
        else:
            async for dialog in client.iter_dialogs(limit=5):
                msg = dialog.message
                if msg and msg.text:
                    name = dialog.name or str(dialog.id)
                    msgs.append({
                        "id":        uuid.uuid4().hex,
                        "sender":    name,
                        "receiver":  "me",
                        "text":      msg.text,
                        "timestamp": msg.date.isoformat(timespec="seconds"),
                    })
        await client.disconnect()
        return msgs

    try:
        msgs = _run(_do_read())
        if not msgs:
            return {"success": False, "message": "No messages found.", "data": None}
        return {"success": True, "message": "Messages fetched.", "data": msgs[-1], "all": msgs}
    except ValueError as ve:
        return {"success": False, "message": str(ve), "data": None}
    except Exception as e:
        logger.error("Telethon read error: %s", e)
        return {"success": False, "message": f"Read error: {e}", "data": None}


# =============================================================================
# BOT API HELPERS  (fallback when MESSAGING_BACKEND=telegram)
# =============================================================================

def _norm(name: str) -> str:
    return name.strip().lower()


def _load_contacts():
    global _chat_id_map
    try:
        if os.path.exists(_CONTACTS_FILE):
            with open(_CONTACTS_FILE, "r", encoding="utf-8") as f:
                _chat_id_map = json.load(f)
    except Exception:
        _chat_id_map = {}
    raw = getattr(Config, "TELEGRAM_CHAT_IDS_RAW", "") or ""
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" in entry:
            n, cid = entry.split(":", 1)
            _chat_id_map[_norm(n)] = cid.strip()


def _save_contacts():
    try:
        with open(_CONTACTS_FILE, "w", encoding="utf-8") as f:
            json.dump(_chat_id_map, f, indent=2)
    except Exception:
        pass


def _tg_url(method: str) -> str:
    return f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/{method}"


def _load_offset():
    global _last_offset
    try:
        if os.path.exists(_OFFSET_FILE):
            with open(_OFFSET_FILE) as f:
                _last_offset = json.load(f).get("offset", 0)
    except Exception:
        pass


def _save_offset(offset: int):
    global _last_offset
    _last_offset = offset
    try:
        with open(_OFFSET_FILE, "w") as f:
            json.dump({"offset": offset}, f)
    except Exception:
        pass


def _fetch_updates(limit: int = 50, offset: int = None) -> list:
    import requests as _req
    params = {"limit": limit, "timeout": 0}
    if offset:
        params["offset"] = offset
    try:
        r = _req.get(_tg_url("getUpdates"), params=params, timeout=10)
        data = r.json()
        return data.get("result", []) if data.get("ok") else []
    except Exception:
        return []


def _updates_to_msgs(updates: list) -> list:
    import time as _time
    msgs = []
    for upd in updates:
        m = upd.get("message") or upd.get("channel_post")
        if not m or not m.get("text"):
            continue
        chat = m.get("chat", {})
        cid  = str(chat.get("id", ""))
        fn   = chat.get("first_name", "")
        ln   = chat.get("last_name", "")
        un   = chat.get("username", "")
        display = (f"{fn} {ln}".strip() if fn or ln else un) or cid
        key = _norm(display)
        if key not in _chat_id_map:
            _chat_id_map[key] = cid
            _save_contacts()
        msgs.append({
            "id":        uuid.uuid4().hex,
            "sender":    display,
            "receiver":  "me",
            "text":      m["text"],
            "timestamp": datetime.fromtimestamp(m.get("date", _time.time())).isoformat(timespec="seconds"),
            "chat_id":   cid,
        })
    if updates:
        _save_offset(updates[-1]["update_id"] + 1)
    return msgs


def _bot_send(receiver: str, message: str) -> dict:
    import requests as _req
    cid = _chat_id_map.get(_norm(receiver))
    if not cid:
        return {
            "success": False,
            "message": (
                f"No chat_id for '{receiver}'. "
                "Ask them to message your bot first, then visit GET /telegram/discover."
            ),
            "data": None,
        }
    try:
        r = _req.post(_tg_url("sendMessage"), json={"chat_id": cid, "text": message}, timeout=10)
        data = r.json()
        if not data.get("ok"):
            return {"success": False, "message": f"Telegram error: {data.get('description')}", "data": None}
        msg = {
            "id":        uuid.uuid4().hex,
            "sender":    "me",
            "receiver":  receiver,
            "text":      message,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }
        return {"success": True, "message": f"Sent to {receiver} via Telegram.", "data": msg}
    except Exception as e:
        return {"success": False, "message": str(e), "data": None}


# =============================================================================
# SIMULATION FALLBACK
# =============================================================================

def _sim_load():
    global _sim_store
    try:
        if os.path.exists(_SIM_FILE):
            with open(_SIM_FILE, "r", encoding="utf-8") as f:
                _sim_store = json.load(f)
    except Exception:
        _sim_store = {}
    if not _sim_store:
        _sim_seed()


def _sim_save():
    try:
        with open(_SIM_FILE, "w", encoding="utf-8") as f:
            json.dump(_sim_store, f, indent=2)
    except Exception:
        pass


def _sim_add(contact: str, sender: str, receiver: str, text: str) -> dict:
    msg = {
        "id":        uuid.uuid4().hex,
        "sender":    sender,
        "receiver":  receiver,
        "text":      text,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
    _sim_store.setdefault(contact, []).append(msg)
    return msg


def _sim_seed():
    demos = [
        ("Alice", "Alice", "me",    "Hey! Can we reschedule to tomorrow?"),
        ("Bob",   "Bob",   "me",    "Urgent: please share the report ASAP."),
        ("Alice", "me",    "Alice", "Sure, tomorrow works!"),
    ]
    for c, s, r, t in demos:
        _sim_add(c, s, r, t)
    _sim_save()


# =============================================================================
# PUBLIC API
# =============================================================================

def get_telegram_status() -> dict:
    if _is_telethon():
        return {
            "ok":                True,
            "mode":              "telethon",
            "can_send_to_anyone": True,
            "authed":            _is_authed(),
            "phone":             Config.TELEGRAM_PHONE,
        }
    if _is_bot():
        import requests as _req
        try:
            r = _req.get(_tg_url("getMe"), timeout=8)
            d = r.json()
            if d.get("ok"):
                bot = d["result"]
                return {
                    "ok":                True,
                    "mode":              "telegram_bot",
                    "can_send_to_anyone": False,
                    "name":              bot.get("first_name"),
                    "username":          "@" + bot.get("username", ""),
                }
        except Exception:
            pass
    return {
        "ok":                False,
        "mode":              "simulation",
        "can_send_to_anyone": False,
        "reason":            "Configure MESSAGING_BACKEND in .env",
    }


def discover_contacts() -> dict:
    if _is_bot():
        updates = _fetch_updates(100, _last_offset or None)
        msgs    = _updates_to_msgs(updates)
        return {"contacts": dict(_chat_id_map), "new_messages": len(msgs), "mode": "telegram_bot"}
    return {"contacts": {}, "new_messages": 0, "mode": _mode()}


def register_contact(name: str, chat_id: str) -> dict:
    if not name.strip() or not chat_id.strip():
        return {"success": False, "message": "name and chat_id are required."}
    _chat_id_map[_norm(name)] = chat_id.strip()
    _save_contacts()
    return {"success": True, "message": f"Registered {name} -> {chat_id}"}


def send_message(receiver: str, message: str) -> dict:
    if not receiver.strip():
        return {"success": False, "message": "Receiver required.", "data": None}
    if not message.strip():
        return {"success": False, "message": "Message required.", "data": None}
    receiver = receiver.strip()
    message  = message.strip()

    if _is_telethon():
        return _tl_send(receiver, message)
    if _is_bot():
        return _bot_send(receiver, message)

    # simulation
    msg = _sim_add(receiver, "me", receiver, message)
    _sim_save()
    return {"success": True, "message": f"[Simulation] Message stored for {receiver}.", "data": msg}


def read_latest_message(contact: str = None) -> dict:
    if _is_telethon():
        return _tl_read(contact)

    if _is_bot():
        updates = _fetch_updates(50, _last_offset or None)
        msgs    = _updates_to_msgs(updates)
        if contact:
            target = _norm(contact)
            msgs   = [m for m in msgs if target in _norm(m.get("sender", ""))]
        if msgs:
            return {"success": True, "message": "Latest message.", "data": msgs[-1]}
        return {"success": False, "message": "No new messages.", "data": None}

    # simulation
    if contact:
        thread = _sim_store.get(contact.strip(), [])
        if thread:
            return {"success": True, "message": f"Latest from {contact}.", "data": thread[-1]}
        return {"success": False, "message": f"No messages from {contact}.", "data": None}
    all_msgs = [m for t in _sim_store.values() for m in t]
    if not all_msgs:
        return {"success": False, "message": "No messages.", "data": None}
    return {
        "success": True,
        "message": "Latest.",
        "data":    max(all_msgs, key=lambda m: m["timestamp"]),
    }


def get_all_messages(contact: str = None) -> dict:
    if _is_telethon():
        r = _tl_read(contact)
        return {
            "success":  r["success"],
            "messages": r.get("all", [r["data"]] if r.get("data") else []),
            "contacts": [],
            "mode":     "telethon",
        }

    if _is_bot():
        updates = _fetch_updates(100, _last_offset or None)
        msgs    = _updates_to_msgs(updates)
        if contact:
            target = _norm(contact)
            msgs   = [m for m in msgs if target in _norm(m.get("sender", ""))]
        return {"success": True, "messages": msgs, "contacts": list(_chat_id_map.keys()), "mode": "telegram_bot"}

    # simulation
    if contact:
        thread = _sim_store.get(contact.strip(), [])
        return {"success": True, "messages": thread, "contacts": list(_sim_store.keys()), "mode": "simulation"}
    all_msgs = sorted(
        [m for t in _sim_store.values() for m in t],
        key=lambda m: m["timestamp"],
    )
    return {"success": True, "messages": all_msgs, "contacts": list(_sim_store.keys()), "mode": "simulation"}


def tl_list_contacts() -> dict:
    """
    Return all contacts saved in the user's Telegram account.
    Each entry: { name, username, phone, id }
    Also includes recent dialog partners so even unsaved contacts are found.
    """
    if not _is_telethon():
        return {"success": False, "contacts": [],
                "message": "Telethon not configured."}
    if not _is_authed():
        return {"success": False, "contacts": [],
                "message": "Not authenticated. Visit /telegram/auth/start first."}

    async def _fetch():
        from telethon.tl.functions.contacts import GetContactsRequest
        client = _make_client()
        await client.connect()
        contacts = []
        seen_ids = set()

        # ── Phone-book contacts ───────────────────────────────────────────
        try:
            result = await client(GetContactsRequest(hash=0))
            for user in result.users:
                if user.id in seen_ids:
                    continue
                seen_ids.add(user.id)
                contacts.append({
                    "id":       user.id,
                    "name":     (f"{user.first_name or ''} {user.last_name or ''}").strip(),
                    "username": f"@{user.username}" if user.username else "",
                    "phone":    f"+{user.phone}"    if user.phone    else "",
                    "source":   "contacts",
                })
        except Exception as _e:
            logger.debug("GetContacts error: %s", _e)

        # ── Recent dialogs (conversations) ───────────────────────────────
        async for dialog in client.iter_dialogs(limit=100):
            entity = dialog.entity
            uid    = getattr(entity, "id", None)
            if uid in seen_ids:
                continue
            seen_ids.add(uid)
            fn = getattr(entity, "first_name", "") or ""
            ln = getattr(entity, "last_name",  "") or ""
            name = (f"{fn} {ln}").strip() or getattr(entity, "title", "") or dialog.name or ""
            if not name:
                continue
            contacts.append({
                "id":       uid,
                "name":     name,
                "username": "@" + (getattr(entity, "username", "") or ""),
                "phone":    "+" + (getattr(entity, "phone",    "") or ""),
                "source":   "dialog",
            })

        await client.disconnect()
        return sorted(contacts, key=lambda c: c["name"].lower())

    try:
        contacts = _run(_fetch())
        return {"success": True, "contacts": contacts,
                "message": f"{len(contacts)} contacts found."}
    except Exception as e:
        logger.error("tl_list_contacts error: %s", e)
        return {"success": False, "contacts": [], "message": str(e)}


def get_contacts() -> list:
    if _is_telethon():
        result = tl_list_contacts()
        return [c["name"] for c in result.get("contacts", [])]
    if _is_bot():
        return list(_chat_id_map.keys())
    return list(_sim_store.keys())


def delete_message(message_id: str) -> dict:
    for contact, thread in _sim_store.items():
        for i, msg in enumerate(thread):
            if msg["id"] == message_id:
                removed = thread.pop(i)
                _sim_save()
                return {"success": True, "message": "Deleted.", "data": removed}
    return {"success": False, "message": "Message not found.", "data": None}


# =============================================================================
# INIT
# =============================================================================
_load_contacts()
_load_offset()

if _mode() == "simulation":
    _sim_load()
elif _is_telethon():
    logger.info("Telethon mode active: authed=%s", _is_authed())
elif _is_bot():
    logger.info("Bot API mode active: %d contacts loaded.", len(_chat_id_map))
