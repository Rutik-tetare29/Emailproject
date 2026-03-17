"""
Voice command processor.

Pipeline:
    1. Save uploaded audio to disk as WAV
    2. Run Whisper STT → get transcription text
    3. Match an intent from the text
    4. Execute intent → produce response text
    5. Run pyttsx3 TTS → save response audio
    6. Return JSON payload
"""
import os
import re
import uuid
import logging
import difflib
from werkzeug.datastructures import FileStorage

from services.stt_whisper import transcribe, _model as _whisper_model
from services.tts_engine import speak_to_file
from services.email_service import fetch_emails, send_email
from services.messaging_service import send_message as tg_send, read_latest_message as tg_read, get_all_messages as tg_all
from services.security_admin import log_activity, resolve_role, verify_pin, normalize_pin_input
from services.profile_service import add_saved_contact, add_saved_email
from config import Config

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Intent keyword tables
# Each list contains the canonical word PLUS every common Whisper mis-transcription
# for that word when spoken clearly by an Indian-English speaker.
# ─────────────────────────────────────────────────────────────────────────────

_INTENTS = {
    # ── Email navigation — listed FIRST so they override read/send keywords ──
    "list_emails": [
        "list emails", "list email", "list my emails", "list my email",
        "show emails", "show email", "show inbox", "show my emails",
        "what emails", "how many emails", "emails in inbox",
        "check inbox", "check my inbox", "what is in my inbox",
        "whats in my inbox", "inbox summary",
    ],
    "next_email": [
        # ── single-word shortcuts (must appear before multi-word so substring
        #    check hits correctly when user says just "next") ──────────────────
        "next email", "next mail", "read next", "read next email",
        "the next one", "next one", "go next", "move next",
        "forward", "forwards",
        "email 2", "email two", "second email",
        "email 3", "email three", "third email",
        "email 4", "email four", "fourth email",
        "email 5", "email five", "fifth email",
        "next",                   # bare "next"
    ],
    "prev_email": [
        "previous email", "previous mail", "read previous",
        "go back", "the previous one", "email before",
        "earlier email", "before that", "read previous email",
        "previous", "prev",       # bare single-word forms
        "back",
    ],
    "read_more": [
        "read more", "continue reading", "more of this", "keep reading",
        "rest of the email", "rest of email", "keep going",
        "read the rest", "what else", "more please",
        "continue",               # bare single-word form
        "more",                   # bare "more"
        "next part", "next chunk",
    ],
    # ── Telegram messaging — listed BEFORE send_email so "send message"
    #    doesn't get swallowed by the bare "send" keyword in send_email ───────
    "send_message": [
        "send message", "send a message", "send telegram",
        "message to", "telegram to", "text to",
        "whatsapp", "send chat", "telegram message",
        "send msg", "send sms",
    ],
    "read_messages": [
        "read messages", "read message", "show messages",
        "check messages", "any messages", "new messages",
        "telegram inbox", "read telegram", "messages from",
        "what messages", "got any messages",
    ],
    # send_email must be listed AFTER send_message so multi-word Telegram
    # phrases are matched before the bare "send" / "cent" keywords below.
    "send_email": [
        "send", "cent", "sent", "sand", "ends",
        "compose", "composed",
        "write", "right", "wrote",
        "new email", "new mail",
    ],
    "read_email": [
        "read", "reed", "red", "raid", "rid",
        "check", "czech", "checked",
        "inbox", "in box",
        "emails", "email", "e-mail", "e mail", "mails", "mail",
        "show", "open", "get", "fetch", "list",
    ],
    "logout": [
        "logout", "log out", "log-out",
        "sign out", "sign-out",
        "bye", "by", "buy", "bi",
        "exit", "exist", "quite", "quit",
        "goodbye", "good bye",
    ],
    "help": [
        "help", "held", "heap", "hell",
        "what can", "commands", "command",
    ],
    # ── Email summarization (email-mode only) ───────────────────────────────
    "summarize_email": [
        "summarize", "summarize email", "summarize this email", "summarize the email",
        "summarize email 1", "summarize email 2", "summarize email 3",
        "summarize email 4", "summarize email 5",
        "summarize email one", "summarize email two", "summarize email three",
        "summarize email four", "summarize email five",
        "summarize all", "summarize all emails", "summarize each email",
        "summarize inbox", "summary of all emails", "give me summaries",
        "give me a summary", "email summary", "brief summary",
        "tldr", "tl dr", "what's the summary", "what is the summary",
        "short version", "make it short", "shorten this", "shorten the email",
        "quick summary", "summarise", "summarise email",
        "summarise all", "summarise all emails",
        # Natural spoken variants (Whisper often transcribes "summarize" as these)
        "email summary", "mail summary", "summary email",
        "email to summary", "email two summary", "email for summary",
        "summary of this email", "summary of the email",
        "this email summary", "the email summary",
    ],
    # ── Message summarization (telegram-mode only) ───────────────────────────
    "summarize_message": [
        "summarize messages", "summarize message", "summarize telegram",
        "message summary", "messages summary", "summarise messages",
        "brief messages", "quick message summary",
    ],
    # ── Telegram messaging (moved to top of dict — see comment there) ────────
    "switch_service": [
        "switch service", "change service", "switch mode", "change mode",
        "back to menu", "main menu", "restart service",
        "different service", "start over",
    ],
    # ── Language switching (global — works in any service mode) ──────────────
    "set_language": [
        # Hindi
        "hindi", "switch to hindi", "hindi language", "hindi mode",
        "change to hindi", "speak hindi", "use hindi",
        # Marathi
        "marathi", "switch to marathi", "marathi language", "marathi mode",
        "change to marathi", "speak marathi", "use marathi",
        # English (revert to default)
        "switch to english", "english language", "english mode",
        "change to english", "speak english", "use english",
        # Spanish
        "spanish", "switch to spanish", "spanish language",
        "change to spanish", "speak spanish", "use spanish",
        # French
        "french", "switch to french", "french language",
        "change to french", "speak french", "use french",
        # German
        "german", "switch to german", "german language",
        "change to german", "speak german", "use german",
        # Italian
        "italian", "switch to italian", "italian language",
        "change to italian", "speak italian", "use italian",
        # Portuguese
        "portuguese", "switch to portuguese", "portuguese language",
        "change to portuguese", "speak portuguese", "use portuguese",
        # Generic
        "change language", "switch language", "language change",
        "change the language", "set language",
    ],
}

# ── Phonetic romanized command variants ──────────────────────────────────────
# Whisper sometimes transcribes Hindi/Marathi speech using Roman characters
# instead of Devanagari (especially with the 'base' model and 'en' fallback).
# These entries are added to the standard intent keyword tables so intent
# detection catches them via normal substring matching.
# Format: exact lowercase romanized transcription → intent key
_PHONETIC_COMMANDS: dict[str, str] = {
    # ── Marathi (romanized Whisper outputs) ────────────────────────────────
    # संदेश पाठवा  → send message
    "sandesh pathava": "send_message",
    "sendesh patava":  "send_message",
    "sandesh patava":  "send_message",
    "sandesh pahava":  "send_message",
    "sandesh patha":   "send_message",
    # संदेश वाचा → read messages
    "sandesh vacha":   "read_messages",
    "sendesh vacha":   "read_messages",
    "sandesh wacha":   "read_messages",
    # ईमेल वाचा → read email
    "email vacha":     "read_email",
    "email wachaa":    "read_email",
    # ईमेल पाठवा → send email
    "email pathava":   "send_email",
    "email patava":    "send_email",
    # पुढील → next
    "pudhil":          "next_email",
    "pudil":           "next_email",
    "pude":            "next_email",
    # मागील → previous
    "magil":           "prev_email",
    "mageel":          "prev_email",
    "maagil":          "prev_email",
    # सारांश → summarize
    "saransh":         "summarize_email",
    "saaransh":        "summarize_email",
    # थांबा → stop
    "thamba":          "stop_reading",
    "thaamb":          "stop_reading",
    # ── Hindi (romanized Whisper outputs) ──────────────────────────────────
    # संदेश भेजो → send message
    "sandesh bhejo":   "send_message",
    "sandesh bhejo":   "send_message",
    "message bhejo":   "send_message",
    # संदेश पढ़ो → read messages
    "sandesh padho":   "read_messages",
    "sandesh paro":    "read_messages",
    # ईमेल पढ़ो → read email
    "email padho":     "read_email",
    "email paro":      "read_email",
    # ईमेल भेजो → send email
    "email bhejo":     "send_email",
    # अगला → next
    "agla":            "next_email",
    "agala":           "next_email",
    # पिछला → previous
    "pichla":          "prev_email",
    "pichala":         "prev_email",
    # रुको → stop
    "ruko":            "stop_reading",
    "rukho":           "stop_reading",
}


# "stop" is often heard as: top, stock, shop, cop, drop, prop, stuff, step,
#  stoop, store, storm, sport, spot,ktop, scop, stab, stub, stomp, stoppe
_STOP_EXACT = {
    "stop", "top", "stock", "shop", "cop", "drop", "prop",
    "stuff", "step", "stoop", "store", "stopped", "stopping",
    "stab", "stub", "spot", "stomp", "pause", "paws", "halt",
    "quiet", "quite", "silence", "silent",
    "enough", "that's enough", "that is enough",
    "shut up", "be quiet", "stop it", "stop reading",
    "pause reading", "stop the email", "no more",
    # Hindi romanized (Whisper output for रुको / बंद / चुप)
    "ruko", "rukho", "ruk", "band karo", "bandh karo", "chup", "bas",
    # Marathi romanized (थांब / बंद कर)
    "thamba", "thaamb", "thab", "band kar",
    # Spanish / French / German shortcuts
    "para", "parar", "detente",
    "arrêt", "stopp",
}

# ── Cancel signals ────────────────────────────────────────────────────────────
# "cancel" → council, console, consul, cancel, camel, counsel, counsel
_CANCEL_EXACT = {
    "cancel", "council", "console", "consul", "camel", "counsel",
    "cancelled", "cancelling",
    "abort", "a board", "aboard",
    "never mind", "nevermind", "never mine",
    "forget it", "forget", "forget that",
    "don't send", "do not send", "don't do it",
    "no", "nope", "nah", "not",
    "stop sending", "cancel email", "cancel sending", "cancel it",
}

# ── Explicit cancel signals ───────────────────────────────────────────────────
# Used at CONFIRM / TO_CONFIRM steps where the user is expected to say
# yes or no.  Fuzzy matching is acceptable there because the user is not
# dictating free-form content.
_EXPLICIT_CANCEL = {
    "cancel", "cancel email", "cancel message", "cancel sending",
    "cancel it", "cancel compose",
    "cancelled", "cancelling",
    "abort", "a board", "aboard",
    "never mind", "nevermind", "never mine",
    "forget it", "forget that",
    "don't send", "do not send", "don't do it",
    "stop sending", "exit compose", "stop composing", "quit compose",
    "stop", "ruko", "rukho", "ruk",          # Hindi: रुको
    "thamba", "thaamb", "thab",              # Marathi: थांब
    "bando", "bandh", "band karo",           # Hindi: बंद करो
}

# ── Content-step cancel signals (EXACT-ONLY — NO fuzzy matching) ──────────────
# Used when capturing free-form content: contact name, message body,
# email address, subject, email body.
# IMPORTANT: do NOT include short 3–4 char words here — they fuzzy-match
# Indian names (e.g. "ruk" matches "Rutik" at ratio 0.75).
_CONTENT_CANCEL = {
    # Multi-word phrases are unambiguous — safe to keep
    "cancel message", "cancel email", "cancel sending",
    "cancel it", "cancel compose",
    "stop sending", "exit compose", "stop composing", "quit compose",
    "never mind", "nevermind", "never mine",
    "forget it", "forget that",
    "don't send", "do not send", "don't do it",
    "band karo", "bandh karo",   # Hindi multi-word: बंद करो
    # Single words that are ALWAYS cancel and can NEVER be a name or name fragment
    "cancel", "cancelled", "cancelling",
    "abort",
    # NOTE: "stop", "ruko", "rukho", "thamba", "thaamb" are intentionally
    # EXCLUDED here.  When Whisper mishears the name "Rutik" in Hindi mode it
    # outputs "रुक" which translates to "stop" — keeping "stop" here would
    # silently cancel every attempt to send a message to Rutik.
    # To cancel during name/text capture the user must say "cancel" or a
    # multi-word phrase above.
}


def _is_cancel_content(text: str) -> bool:
    """
    Exact-only cancel detection for content-capture steps.

    Unlike _any_token_matches (which uses fuzzy similarity), this function
    ONLY accepts exact string equality or exact substring for multi-word
    phrases.  This prevents names like 'Rutik' from matching 'ruk'
    (SequenceMatcher ratio 0.75 ≥ cutoff 0.72) and triggering a false cancel.
    """
    lower = text.lower().strip()
    # 1. Exact full-phrase match (covers all single-word entries too)
    if lower in _CONTENT_CANCEL:
        return True
    # 2. Multi-word phrases: safe to match as substrings
    for phrase in _CONTENT_CANCEL:
        if " " in phrase and phrase in lower:
            return True
    return False

# ── Confirm signals ───────────────────────────────────────────────────────────
# "yes" → yet, yes, yep, yeah, ya, yah, yea, jest, chest
# "confirm" → conform, conformed, confirmed
# "ok" → oak, okay, ok, o.k.
_CONFIRM_EXACT = {
    "yes", "yet", "yep", "yeah", "ya", "yah", "yea", "jest",
    # Romanized Hindi/Marathi 'yes' (Whisper output when mic lang is hi/mr)
    "ha", "haa", "haan", "han", "ho", "hoy", "hoji",
    "confirm", "confirmed", "conform", "conformed",
    "ok", "okay", "o.k.", "oak",
    "send it", "do it", "go ahead", "go", "proceed",
    "yes please", "please send", "absolutely", "sure", "correct",
}


# ── Number-word → digit table (covers 0-19 and tens up to 90) ─────────────────
_NUM_WORDS = {
    "zero":"0","one":"1","two":"2","three":"3","four":"4",
    "five":"5","six":"6","seven":"7","eight":"8","nine":"9",
    "ten":"10","eleven":"11","twelve":"12","thirteen":"13",
    "fourteen":"14","fifteen":"15","sixteen":"16","seventeen":"17",
    "eighteen":"18","nineteen":"19",
    "twenty":"20","thirty":"30","forty":"40","fifty":"50",
    "sixty":"60","seventy":"70","eighty":"80","ninety":"90",
}
# compound tens: "twenty one" … "ninety nine"
_TENS = ["twenty","thirty","forty","fifty","sixty","seventy","eighty","ninety"]
_ONES = ["one","two","three","four","five","six","seven","eight","nine"]


def _replace_number_words(t: str) -> str:
    """Replace spoken number words with digits, e.g. 'twenty nine' → '29'."""
    # compound tens first ("twenty one" → "21")
    for ten in _TENS:
        for one in _ONES:
            t = re.sub(rf'\b{ten}\s+{one}\b',
                       str(int(_NUM_WORDS[ten]) + int(_NUM_WORDS[one])), t)
    # single words
    for word, digit in _NUM_WORDS.items():
        t = re.sub(rf'\b{word}\b', digit, t)
    return t


# ── Known domain spoken-form fixes ────────────────────────────────────────────
_DOMAIN_FIXES = [
    # Gmail variants
    (r'\bg\s*mail\b',       'gmail'),
    (r'\bgemail\b',         'gmail'),
    (r'\bg-mail\b',         'gmail'),
    # Hotmail / Outlook
    (r'\bhot\s*mail\b',     'hotmail'),
    (r'\bout\s*look\b',     'outlook'),
    # Yahoo
    (r'\byah+oo\b',         'yahoo'),
    # TLD: "com" mis-heard
    (r'\b(?:calm|come|comma|khan|con|gom|cam)\b', 'com'),
    # TLD: "in" short form
    (r'\b(?:inn|an|and)$',  'in'),
    # TLD: "net"
    (r'\b(?:naet|neat|met)\b', 'net'),
    # TLD: "org"
    (r'\b(?:org|aura|alba)\b', 'org'),
    # TLD: "edu"
    (r'\b(?:edu|eddo|ado)\b', 'edu'),
]


def _normalize_email_address(text: str) -> str:
    """
    Convert a spoken email address to a valid format.
    Handles the many ways Whisper mis-transcribes email components.

    Pronunciation guide spoken to the user:
        name  [at / at the rate / @ / add / hat]  domain  [dot / period / full stop]  tld
    """
    t = text.lower().strip()

    # ── 0. Number words → digits ──────────────────────────────────────────────
    t = _replace_number_words(t)

    # ── 1. Domain spoken-form fixes (before @ replacement so 'add' isn't
    #        accidentally turned into a digit) ─────────────────────────────────
    for pattern, replacement in _DOMAIN_FIXES:
        t = re.sub(pattern, replacement, t)

    # ── 2. @ substitutes ─────────────────────────────────────────────────────
    # "at the rate (of)?"
    t = re.sub(r'\bat\s+the\s+rate\s+(?:of\s+)?', '@', t)
    # "at sign" / "at symbol" / "@ symbol"
    t = re.sub(r'\bat\s+(?:sign|symbol|mark)\b', '@', t)
    # "commercial at"
    t = re.sub(r'\bcommercial\s+at\b', '@', t)
    # Whisper sometimes mis-hears "at" as "add" / "hat" / "that" / "had" / "rat" / "bat"
    t = re.sub(r'\b(?:add|hat|that|had|rat|bat|cat|fat|sat|@)\b', '@', t)
    # Plain "at" between two non-space sequences
    t = re.sub(r'(?<=\S)\s+at\s+(?=\S)', '@', t)
    # Remove stray "at" at start if it survived
    t = re.sub(r'^at\s+', '@', t)

    # ── 3. Dot substitutes ────────────────────────────────────────────────────
    # "full stop", "period", "point", "dot"
    t = re.sub(r'\s*\b(?:dot|period|full\s+stop|point|por)\b\s*', '.', t)

    # ── 4. Special character names ────────────────────────────────────────────
    t = re.sub(r'\s*\bunderscore\b\s*', '_', t)
    # Use a null-byte placeholder for intentional dashes (spoken as "dash"/"hyphen")
    # so they survive the Whisper-separator removal in step 4b below.
    t = re.sub(r'\s*\b(?:dash|hyphen|minus)\b\s*', '\x00', t)
    t = re.sub(r'\s*\bplus\b\s*', '+', t)

    # ── 4b. Strip Whisper-inserted letter-separator hyphens ───────────────────
    # When the user spells out their email letter-by-letter, Whisper inserts
    # hyphens between the letters: "rutikte-t-e-t-k-r-e" → "rutiktetekre".
    # Intentional dashes were protected as '\x00' in step 4, so we can safely
    # strip all remaining bare hyphens from the local part only.
    # (Domain hyphens like "x-y.com" come from the spoken domain text and are
    # rarely user-spoken letter-by-letter, but to be safe we only strip the local.)
    if '@' in t:
        _lp, _rest = t.split('@', 1)
        t = _lp.replace('-', '') + '@' + _rest
    else:
        t = t.replace('-', '')

    # ── 5. Strip filler words that creep in ───────────────────────────────────
    # "my email is", "send to", "the address is", etc.
    t = re.sub(r'^(?:my\s+)?(?:email\s+(?:is\s+|address\s+is\s+)?'  \
               r'|address\s+is\s+|send\s+(?:it\s+)?to\s+|to\s+)?', '', t)

    # ── 6. Collapse whitespace inside the address ─────────────────────────────
    # At this point anything left that is a space inside the email is wrong
    t = re.sub(r'\s+', '', t)

    # ── 7. Cleanup double punctuation / leading-trailing junk ─────────────────
    t = re.sub(r'\.{2,}', '.', t)   # ".." → "."
    t = re.sub(r'@{2,}', '@', t)    # "@@" → "@"
    t = t.strip('.@_-\x00')

    # ── 8. Restore intentional dashes (spoken as "dash"/"hyphen") ─────────────
    t = t.replace('\x00', '-')

    return t


def _is_valid_email(addr: str) -> bool:
    """Basic sanity check — must have exactly one @ and at least one dot after it."""
    return bool(re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', addr))


def _fuzzy_match(word: str, targets: set, cutoff: float = 0.72) -> bool:
    """
    Return True if `word` is close enough to any string in `targets`.
    Uses SequenceMatcher ratio — tolerates 1-2 character substitutions.
    """
    word = word.strip()
    if word in targets:
        return True
    matches = difflib.get_close_matches(word, targets, n=1, cutoff=cutoff)
    return bool(matches)


def _any_token_matches(text: str, targets: set, cutoff: float = 0.72) -> bool:
    """
    Check every individual word in `text` AND the full phrase against `targets`.
    Short utterances (≤3 words) get a slightly more lenient cutoff, but
    never below 0.78 — prevents common words ('send','read') fuzzy-matching
    into the stop-word set.
    """
    words = text.lower().split()
    # short utterance → be more lenient, but floor at 0.78
    if len(words) <= 3:
        cutoff = min(cutoff, 0.78)

    # full phrase check
    if _fuzzy_match(text.lower(), targets, cutoff):
        return True
    # per-word check
    for w in words:
        if _fuzzy_match(w, targets, cutoff):
            return True
    return False


# ── Navigation abort phrases ─────────────────────────────────────────────────
# Multi-word phrases that cannot be a person's name and clearly signal the user
# wants to switch away from an active compose flow.
_NAV_ABORT_PHRASES = [
    "read email", "read emails", "read mail", "read my email", "read my emails",
    "list email", "list emails", "list my emails", "show email", "show emails",
    "check email", "check emails", "check inbox", "check my inbox", "show inbox",
    "open inbox", "open email", "get email", "fetch email",
    "read messages", "check messages", "show messages", "read telegram",
    "send email", "compose email", "new email", "write email",
    "next email", "previous email", "read next", "read next email",
    "summarize email", "email summary", "switch service", "switch to email",
    "switch to telegram", "email mode", "telegram mode",
]


def _is_nav_abort(text: str) -> bool:
    """Return True if text contains a multi-word navigation phrase that cannot
    be a valid recipient name and signals the user wants to pivot away from
    the current compose flow."""
    lower = text.lower().strip()
    return any(phrase in lower for phrase in _NAV_ABORT_PHRASES)


def _detect_intent(text: str, session: dict) -> str:
    lower = text.lower().strip()
    if not lower:
        return "unknown"

    # ── Is the user currently dictating free-form content? ────────────────────
    # During contact-name capture ("to"), message body ("text"), email subject
    # ("subject"), email body ("body"), and email-address capture ("to" in email
    # compose) common English words like "shop", "top", "drop", "step", "store",
    # "cop" are VALID content — they must NOT trigger stop_reading.
    _content_step = (
        (session.get("msg_compose")   or {}).get("step") in ("to", "text")
        or
        (session.get("email_compose") or {}).get("step") in ("to", "subject", "body")
    )

    # ── GLOBAL PRIORITY: stop/pause wins — but NOT during free-form capture ───
    # When capturing a Telegram contact name / message body or an email address /
    # subject / body, we skip the broad stop-word set so the user can freely
    # say names and sentences.  _EXPLICIT_CANCEL in each branch handles bail-out.
    if not _content_step and _any_token_matches(lower, _STOP_EXACT):
        return "stop_reading"

    # ── While message compose is active ──────────────────────────────────────
    # Each step has different cancel sensitivity to avoid false positives.
    if session.get("msg_compose"):
        step = session["msg_compose"].get("step")

        if step == "to":
            # Capturing contact name — use EXACT-ONLY cancel check so names
            # like ‘Rutik’ don’t fuzzy-match ‘ruk’ and trigger a false cancel.
            if _is_cancel_content(lower):
                return "cancel_message"
            # Navigation override: multi-word phrase that can't be a person's name
            if _is_nav_abort(lower):
                session.pop("msg_compose", None)
                session.modified = True
                return _detect_intent(lower, session)
            return "send_message"  # any utterance = contact name

        elif step == "to_confirm":
            # User is confirming the name or providing a correction.
            # Explicit cancel → cancel. Confirm words → advance. Anything else = correction.
            if _any_token_matches(lower, _EXPLICIT_CANCEL):
                return "cancel_message"
            if _any_token_matches(lower, _CONFIRM_EXACT):
                return "send_message"  # confirmed
            # Navigation override: user pivoted to a different service/action
            if _is_nav_abort(lower):
                session.pop("msg_compose", None)
                session.modified = True
                return _detect_intent(lower, session)
            return "send_message"  # treat as corrected name

        elif step == "text":
            # Capturing message body — use EXACT-ONLY cancel check.
            if _is_cancel_content(lower):
                return "cancel_message"
            return "send_message"  # any utterance = message content

        elif step == "confirm":
            # Final confirm/cancel step: yes/no both fully valid here.
            if _any_token_matches(lower, _CONFIRM_EXACT):
                return "send_message"
            if _any_token_matches(lower, _CANCEL_EXACT):
                return "cancel_message"
            return "send_message"  # let handler reprompt instead of hard-cancel

        else:
            # Unknown step fallback
            if _is_cancel_content(lower):
                return "cancel_message"
            return "send_message"

    # ── While email compose is active ────────────────────────────────────────
    if session.get("email_compose"):
        step = session["email_compose"].get("step")

        if step == "to":
            # Capturing recipient address — exact-only cancel.
            if _is_cancel_content(lower):
                return "cancel_email"
            # Navigation override: phrase can't be an email address
            if _is_nav_abort(lower):
                session.pop("email_compose", None)
                session.modified = True
                return _detect_intent(lower, session)
            return "send_email"

        elif step == "subject":
            # Capturing subject — exact-only cancel.
            if _is_cancel_content(lower):
                return "cancel_email"
            # Navigation override: user pivoted away from compose
            if _is_nav_abort(lower):
                session.pop("email_compose", None)
                session.modified = True
                return _detect_intent(lower, session)
            return "send_email"

        elif step == "body":
            # Capturing body — exact-only cancel.
            if _is_cancel_content(lower):
                return "cancel_email"
            return "send_email"

        elif step == "confirm":
            # Final confirm/cancel step: all cancel words valid here.
            if _any_token_matches(lower, _CONFIRM_EXACT):
                return "send_email"
            if _any_token_matches(lower, _CANCEL_EXACT):
                return "cancel_email"
            return "send_email"  # let handler reprompt instead of hard-cancel

        else:
            if _is_cancel_content(lower):
                return "cancel_email"
            return "send_email"

    # ── Stop reading (checked before general intents) ─────────────────────────
    # Note: also checked at the very top of this function to catch stop inside
    # compose flows — this second check handles code paths that bypass the top.
    if _any_token_matches(lower, _STOP_EXACT):
        return "stop_reading"

    # ── Native-language command matching (offline fallback) ───────────────────
    # When translate_to_english is unavailable or returned unchanged text,
    # try matching against the per-language command table directly.
    # This runs BEFORE the English intent loop so native commands always work.
    _session_lang = session.get("language", "en")
    if _session_lang != "en":
        try:
            from services.lang_utils import NATIVE_COMMANDS
            _native = NATIVE_COMMANDS.get(_session_lang, {})
            for _intent, _phrases in _native.items():
                if _intent.startswith("_"):
                    continue  # skip _confirm / _cancel meta-keys
                if any(p in lower for p in _phrases):
                    return _intent
        except Exception:
            pass  # fail silently — English path still follows

    # ── Phonetic romanized command matching ───────────────────────────────────
    # Catches Whisper's Roman-character output when it can't render Devanagari
    # (e.g. "sendesh patava" → send_message).  Checked for ALL languages since
    # even 'en' sessions can receive phonetic spillover from native speakers.
    for _phrase, _intent in _PHONETIC_COMMANDS.items():
        if _phrase in lower:
            # Respect service filter so telegram-only intents don't fire in email mode
            _es_active = session.get("active_service")
            _EMAIL_ONLY   = {"list_emails", "read_email", "next_email", "prev_email",
                             "read_more", "send_email", "cancel_email", "summarize_email"}
            _TELE_ONLY    = {"send_message", "read_messages", "cancel_message", "summarize_message"}
            if _es_active == "email"    and _intent in _TELE_ONLY:   continue
            if _es_active == "telegram" and _intent in _EMAIL_ONLY:  continue
            return _intent

    # ── Service-aware routing: skip intents that don't belong to active service
    active_service = session.get("active_service")  # 'email' | 'telegram' | None
    _EMAIL_ONLY_INTENTS    = {"list_emails", "read_email", "next_email", "prev_email",
                               "read_more", "send_email", "cancel_email", "summarize_email"}
    _TELEGRAM_ONLY_INTENTS = {"send_message", "read_messages", "cancel_message", "summarize_message"}

    def _service_allowed(intent: str) -> bool:
        if active_service == "email"    and intent in _TELEGRAM_ONLY_INTENTS:
            return False
        if active_service == "telegram" and intent in _EMAIL_ONLY_INTENTS:
            return False
        return True

    # ── "summarize email N" / "summarize all" ────────────────────────────────
    _sum_num_map = {
        "one": 0, "1": 0, "two": 1, "2": 1, "three": 2, "3": 2,
        "four": 3, "4": 3, "five": 4, "5": 4,
    }
    if _service_allowed("summarize_email"):
        sum_all = re.search(
            r'\b(?:summarize|summarise)\s+(?:all|each|every|inbox)\b', lower
        )
        if sum_all:
            session["_summarize_all"] = True
            session.pop("_summarize_idx", None)
            session.modified = True
            return "summarize_email"

        sum_num = re.search(
            r'\b(?:summarize|summarise)\s+(?:email|mail)?\s*'
            r'(one|two|three|four|five|1|2|3|4|5)\b',
            lower
        )
        if sum_num:
            session["_summarize_idx"] = _sum_num_map.get(sum_num.group(1), 0)
            session.pop("_summarize_all", None)
            session.modified = True
            return "summarize_email"

        # Bare "summarize [email/mail/this/the/...]" — must be caught here
        # BEFORE the standard intent loop where read_email's "email" keyword
        # would otherwise match first.
        if re.search(r'\b(?:summarize|summarise)\b', lower):
            session.pop("_summarize_idx", None)
            session.pop("_summarize_all", None)
            return "summarize_email"

    # ── "email [N] summary" / "email to/two summary" — must come BEFORE the ──
    # navigation regex so "email two summary" is not misread as "go to email 2".
    if _service_allowed("summarize_email"):
        if re.search(r'\bemail\b.*\bsummar|\bsummar.*\bemail\b', lower):
            _sn = re.search(
                r'\bemail\b.*?\b(one|two|three|four|five|1|2|3|4|5)\b', lower
            )
            if _sn:
                session["_summarize_idx"] = _sum_num_map.get(_sn.group(1), 0)
                session.pop("_summarize_all", None)
                session.modified = True
            else:
                session.pop("_summarize_idx", None)
                session.pop("_summarize_all", None)
            return "summarize_email"

    # ── "read email N" / "email number N" — positional navigation ──────────
    _num_map = {
        "one": 1, "1": 1, "two": 2, "2": 2, "three": 3, "3": 3,
        "four": 4, "4": 4, "five": 5, "5": 5,
    }
    m = re.search(
        r'(?:read|open|show|play)\s+(?:email|mail|message|number|no\.?)\s*'
        r'(one|two|three|four|five|1|2|3|4|5)\b',
        lower
    )
    if not m:
        m = re.search(r'(?:email|message)\s+(?:number\s+)?(one|two|three|four|five|1|2|3|4|5)\b', lower)
    if m:
        if _service_allowed("next_email"):
            session["_goto_email_idx"] = _num_map.get(m.group(1), 1) - 1
            session.modified = True
            return "next_email"

    # ── Language switching — highest priority, not filtered by active service ──
    _LANG_KW = {
        "hi": ["hindi", "switch to hindi", "hindi language", "hindi mode",
               "change to hindi", "speak hindi", "use hindi"],
        "mr": ["marathi", "switch to marathi", "marathi language", "marathi mode",
               "change to marathi", "speak marathi", "use marathi"],
        "en": ["switch to english", "english language", "english mode",
               "change to english", "speak english", "use english"],
        "es": ["spanish", "español", "switch to spanish", "spanish language",
               "change to spanish", "speak spanish", "use spanish"],
        "fr": ["french", "français", "francais", "switch to french", "french language",
               "change to french", "speak french", "use french"],
        "de": ["german", "deutsch", "switch to german", "german language",
               "change to german", "speak german", "use german"],
        "it": ["italian", "italiano", "switch to italian", "italian language",
               "change to italian", "speak italian", "use italian"],
        "pt": ["portuguese", "português", "portugues", "switch to portuguese",
               "portuguese language", "change to portuguese", "speak portuguese"],
    }
    for _lc, _triggers in _LANG_KW.items():
        if any(t in lower for t in _triggers):
            session["_set_lang_code"] = _lc
            session.modified = True
            return "set_language"

    # ── Standard intent matching ──────────────────────────────────────────────
    for intent, keywords in _INTENTS.items():
        if not _service_allowed(intent):
            continue
        if any(kw in lower for kw in keywords):
            return intent

    # Fuzzy fallback
    words = lower.split()
    for intent, keywords in _INTENTS.items():
        if not _service_allowed(intent):
            continue
        kw_set = set(keywords)
        for w in words:
            if _fuzzy_match(w, kw_set, cutoff=0.70):
                return intent

    return "unknown"


# ── Email reading helpers ──────────────────────────────────────────────────────
_CHUNK_SIZE = 400   # chars per spoken navigation chunk (~30 s at 165 WPM)

# Server-side cache to avoid Flask session-cookie overflow (4 KB limit).
# Keyed by user email address — survives the request lifetime of the process.
_EMAIL_STORE: dict[str, list] = {}


def _store_key(session: dict) -> str:
    """Return a cache key unique to the logged-in user."""
    # Primary: session["user"]["email"] (set by both GoogleUser and AppPasswordUser)
    user_dict = session.get("user") or {}
    email = user_dict.get("email") if isinstance(user_dict, dict) else None
    return email or session.get("user_email") or session.get("email") or "anon"


def _cache_emails(session: dict, limit: int = 5) -> list:
    """Fetch emails and store in _EMAIL_STORE (NOT in the cookie-based session)."""
    emails = fetch_emails(session, limit=limit)
    key = _store_key(session)
    _EMAIL_STORE[key] = emails
    # Only store lightweight navigation pointers in the session cookie
    session["_email_cache_key"]  = key
    session["_email_read_idx"]   = 0
    session["_email_read_chunk"] = 0
    session.modified = True
    return emails


def _get_cached_emails(session: dict) -> list | None:
    """Return the cached email list for this user, or None if not cached."""
    key = session.get("_email_cache_key") or _store_key(session)
    return _EMAIL_STORE.get(key)


# ── TTS-safe text helpers ──────────────────────────────────────────────────────

def _clean_sender(from_str: str) -> str:
    """
    Convert a raw RFC-2822 From header into a short, TTS-safe spoken form.

    Examples
    --------
    '"Do not reply" <no-reply@iirs.gov.in>'  →  'no-reply at iirs.gov.in'
    'Rutik Tetare <rutik@gmail.com>'          →  'Rutik Tetare'
    'rutik@gmail.com'                         →  'rutik at gmail.com'
    """
    s = from_str.strip()

    # 1. Extract display name and address parts
    m = re.match(r'^(.*?)<([^>]+)>', s)
    if m:
        display = m.group(1).strip().strip('"').strip("'").strip()
        addr    = m.group(2).strip()
        # If display name is meaningful (not empty / not equal to addr), use it
        if display and display.lower() != addr.lower() and len(display) > 1:
            # Limit to first 60 chars to avoid absurdly long TTS intros
            display = display[:60].rstrip()
            return _tts_safe(display)
        # Otherwise speak the address in a readable way
        return _tts_safe(addr.replace("@", " at ").replace(".", " dot "))
    # No angle brackets — might be a plain address or plain name
    if "@" in s:
        return _tts_safe(s.replace("@", " at ").replace(".", " dot "))
    return _tts_safe(s[:80])


def _tts_safe(text: str) -> str:
    """
    Strip characters that confuse pyttsx3/SAPI5 SSML parser and clean up
    whitespace so the engine produces reliable untruncated audio.

    SAPI5 interprets < > as XML/SSML tags; encountering a malformed tag
    silently aborts audio generation — hence the 'stops at sender' bug.
    """
    # Remove SSML/XML angle-bracket constructs entirely
    text = re.sub(r'<[^>]*>', ' ', text)
    # Replace remaining stray < > & with safe equivalents
    text = text.replace('&', ' and ').replace('<', ' ').replace('>', ' ')
    # Strip markdown-style formatting
    text = re.sub(r'[*_`#~]', '', text)
    # Collapse repeated punctuation
    text = re.sub(r'[.]{2,}', '.', text)
    text = re.sub(r'[-]{2,}', '-', text)
    # URLs are unreadable — replace with "link"
    text = re.sub(r'https?://\S+', 'link', text)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _normalize_contact_name(raw: str) -> str:
    """
    Fix Whisper spelling-out artefact: "R-U-T-I-K." or "R U T I K" → "Rutik".
    Strips trailing punctuation first (Whisper often adds a period/comma),
    then collapses separated single letters into one capitalised word.
    """
    text = raw.strip()
    # Strip trailing punctuation that Whisper appends (e.g. "R-U-T-I-K.")
    text = text.rstrip('.,!?;:')
    # Pattern 1: letters separated by hyphens e.g. "R-U-T-I-K" or "r-u-t-i-k"
    if re.fullmatch(r'[A-Za-z](?:-[A-Za-z])+', text):
        return text.replace('-', '').capitalize()
    # Pattern 2: single letters separated by spaces e.g. "R U T I K"
    parts = text.split()
    if len(parts) >= 2 and all(len(p) == 1 and p.isalpha() for p in parts):
        return ''.join(parts).capitalize()
    # Pattern 3: letters separated by dots e.g. "R.U.T.I.K"
    if re.fullmatch(r'[A-Za-z](?:\.[A-Za-z])+', text):
        return text.replace('.', '').capitalize()
    return text


def _read_email_at(email: dict, idx: int, total: int, chunk: int = 0) -> str:
    """Speak one email, paginating the body into chunks."""
    sender  = _clean_sender(email.get("from", "Unknown"))
    subject = _tts_safe(email.get("subject", "No subject"))
    body    = _tts_safe(
        (email.get("body") or email.get("snippet") or "No content").strip()
    )

    start     = chunk * _CHUNK_SIZE
    body_part = body[start : start + _CHUNK_SIZE]
    has_more  = len(body) > start + _CHUNK_SIZE

    ordinals = ["first", "second", "third", "fourth", "fifth"]
    label    = ordinals[idx] if idx < len(ordinals) else f"email {idx + 1}"

    if chunk == 0:
        result = (
            f"Reading your {label} email. "
            f"From: {sender}. "
            f"Subject: {subject}. "
            f"Message: {body_part}"
        )
    else:
        result = f"Continuing email {idx + 1}. {body_part}"

    if has_more:
        result += " Say 'read more' to continue."
    else:
        if idx < total - 1:
            result += f" End of message. Say 'next' for email {idx + 2}."
        else:
            result += " That was your last email."
    return result


# ── Intent handlers ────────────────────────────────────────────────────────────
def _handle_list_emails(session: dict) -> str:
    """List subjects + senders so user knows what's in inbox before reading."""
    emails = _cache_emails(session, limit=5)
    if not emails:
        return "Your inbox is empty or I could not retrieve your emails."
    lines = []
    for i, e in enumerate(emails, 1):
        lines.append(
            f"Email {i}: from {_clean_sender(e.get('from', 'Unknown'))}. "
            f"Subject: {_tts_safe(e.get('subject', 'No subject'))}."
        )
    return (
        f"You have {len(emails)} email{'s' if len(emails) > 1 else ''} loaded. "
        + " ".join(lines)
        + " Say 'read email 1' or 'next' to read them."
    )


def _handle_read_email(session: dict) -> str:
    """Read the first (latest) email and cache all 5 for navigation."""
    emails = _cache_emails(session, limit=5)
    if not emails:
        return "Your inbox is empty or I could not retrieve your emails."
    return _read_email_at(emails[0], 0, len(emails), chunk=0)


def _handle_next_email(session: dict) -> str:
    """Read the next email, or a specific one if _goto_email_idx was set."""
    emails = _get_cached_emails(session)
    if not emails:
        emails = _cache_emails(session, limit=5)

    # Honour positional jump ("read email 3")
    goto = session.pop("_goto_email_idx", None)
    if goto is not None:
        idx = int(goto)
    else:
        idx = session.get("_email_read_idx", 0) + 1

    if idx >= len(emails):
        return (
            f"You've reached the end. There are only {len(emails)} emails loaded. "
            "Say 'list emails' to hear the subjects again."
        )

    session["_email_read_idx"]   = idx
    session["_email_read_chunk"] = 0
    session.modified = True
    return _read_email_at(emails[idx], idx, len(emails), chunk=0)


def _handle_prev_email(session: dict) -> str:
    """Go back to the previous email."""
    emails = _get_cached_emails(session)
    if not emails:
        return "No emails loaded yet. Say 'read emails' to load your inbox first."

    idx = session.get("_email_read_idx", 0) - 1
    if idx < 0:
        return "You're already at the first email."

    session["_email_read_idx"]   = idx
    session["_email_read_chunk"] = 0
    session.modified = True
    return _read_email_at(emails[idx], idx, len(emails), chunk=0)


def _handle_read_more(session: dict) -> str:
    """Read the next chunk of the current email body."""
    emails = _get_cached_emails(session)
    idx    = session.get("_email_read_idx", 0)
    chunk  = session.get("_email_read_chunk", 0) + 1

    if not emails or idx >= len(emails):
        return "No email is currently being read. Say 'read emails' to start."

    body  = (emails[idx].get("body") or emails[idx].get("snippet") or "").strip()
    start = chunk * _CHUNK_SIZE
    if start >= len(body):
        nxt = idx + 1
        if nxt < len(emails):
            return f"That's the end of this email. Say 'next' for email {nxt + 1}."
        return "That's the end of this email and your last loaded message."

    session["_email_read_chunk"] = chunk
    session.modified = True
    return _read_email_at(emails[idx], idx, len(emails), chunk=chunk)



def _handle_stop_reading(session: dict = None) -> str:
    # Return empty string — frontend stops audio instantly, no TTS needed.
    # Generating TTS for "stop" adds ~500 ms latency for no benefit.
    # Also clear any active compose flow so the next mic tap starts fresh
    # rather than resuming a dead (interrupted) workflow.
    if session:
        cleared = session.pop("msg_compose", None) or session.pop("email_compose", None)
        if cleared:
            session.modified = True
    return ""


def _session_user_email(session: dict) -> str:
    user = session.get("user", {}) if isinstance(session, dict) else {}
    return str(user.get("email", "")).strip().lower()


def _session_user_role(session: dict) -> str:
    user = session.get("user", {}) if isinstance(session, dict) else {}
    role = str(user.get("role", "")).strip().lower()
    if role:
        return role
    return resolve_role(_session_user_email(session))


def _log_compose_activity(session: dict, action: str, status: str = "success", details: dict | None = None) -> None:
    try:
        log_activity(
            user_email=_session_user_email(session) or "anonymous",
            role=_session_user_role(session),
            action=action,
            status=status,
            details=details,
            ip="",
        )
    except Exception as exc:
        logger.warning("Could not log compose activity '%s': %s", action, exc)


def _remember_saved_contact(session: dict, recipient: str) -> None:
    """Best-effort persistence of successful Telegram recipients for profile reuse."""
    try:
        user_email = _session_user_email(session)
        if user_email and recipient:
            add_saved_contact(user_email, recipient)
    except Exception as exc:
        logger.warning("Could not save Telegram recipient to profile: %s", exc)


def _remember_saved_email(session: dict, recipient_email: str) -> None:
    """Best-effort persistence of successful email recipients for profile reuse."""
    try:
        user_email = _session_user_email(session)
        if user_email and recipient_email:
            add_saved_email(user_email, recipient_email)
    except Exception as exc:
        logger.warning("Could not save email recipient to profile: %s", exc)


def _looks_like_pin_input(text: str) -> bool:
    digits = normalize_pin_input(text or "")
    return 4 <= len(digits) <= 8


def _handle_cancel_email(session: dict) -> str:
    session.pop("email_compose", None)
    session.modified = True
    return "Email cancelled. What else can I help you with?"


def _handle_summarize_email(session: dict) -> str:
    """
    Summarize emails by voice:
      - "summarize email"     → full content summary of currently loaded email
      - "summarize email 2"   → full content summary of email #2 in cache
      - "summarize all emails"→ one-line subject+sender per email (full body
                                 of each would be too long to read aloud)
    """
    from services.summarizer import summarize_email as _summarize_email, summarize_text

    emails = _get_cached_emails(session)
    if not emails:
        return (
            "No emails are loaded yet. "
            "Please say 'read emails' first to load your inbox."
        )

    def _full_summary(email: dict, number: int) -> str:
        """Return a full content summary for a single email dict."""
        body   = (email.get("body") or email.get("snippet") or "").strip()
        sender = _clean_sender(email.get("from", "Unknown"))
        if not body:
            subject = _tts_safe(email.get("subject", "No subject"))
            return f"Email {number} from {sender}: {subject}. No body content."
        # Build a dict that matches summarize_email's expected shape
        email_dict = {
            "sender":  sender,
            "subject": email.get("subject", ""),
            "body":    body,
        }
        return f"Email {number}: " + _summarize_email(email_dict, mode="full")

    def _one_line(email: dict, number: int) -> str:
        """Return a short subject+sender line (used for 'summarize all')."""
        sender  = _clean_sender(email.get("from", "Unknown"))
        subject = _tts_safe(email.get("subject", "No subject"))
        return f"Email {number} from {sender}: {subject}."

    # ── Summarize all loaded emails ────────────────────────────────────────
    if session.pop("_summarize_all", False):
        session.modified = True
        lines = [_one_line(e, i + 1) for i, e in enumerate(emails)]
        intro = f"Here are summaries of your {len(emails)} emails. "
        return intro + " ".join(lines)

    # ── Summarize a specific email by number ───────────────────────────────
    specific = session.pop("_summarize_idx", None)
    if specific is not None:
        session.modified = True
        if specific >= len(emails):
            return (
                f"You only have {len(emails)} email"
                f"{'s' if len(emails) != 1 else ''} loaded. "
                "Please say a valid email number."
            )
        session["_email_read_idx"]   = specific
        session["_email_read_chunk"] = 0
        session.modified = True
        return _full_summary(emails[specific], specific + 1)

    # ── Summarize currently loaded email ─────────────────────────────────
    idx = session.get("_email_read_idx", 0)
    if idx >= len(emails):
        idx = 0
    return _full_summary(emails[idx], idx + 1)


def _handle_summarize_message(session: dict) -> str:
    """Summarize the most recent Telegram messages."""
    from services.summarizer import summarize_text
    result = tg_all()
    msgs   = result.get("messages", [])
    if not msgs:
        return "You have no Telegram messages to summarize."
    latest = msgs[-5:]
    parts  = []
    for m in latest:
        sender = _tts_safe(m.get("sender", "Unknown"))
        text   = _tts_safe(m.get("text",   ""))
        if text.strip():
            parts.append(f"From {sender}: {text}")
    if not parts:
        return "No message content found to summarize."
    combined = ". ".join(parts)
    summary  = summarize_text(combined, mode="simple", max_sentences=3)
    return f"Summary of your recent Telegram messages: {summary}"


def _handle_send_email(session: dict, transcription: str, eng_text: str = "") -> str:
    """
    Multi-step voice-guided email compose with cancel support at every step.

    `transcription` — raw Whisper output in the user's language (used to
                      capture recipient, subject, body as the user spoke them).
    `eng_text`      — English-normalised version of the same utterance (used
                      for confirm/cancel keyword matching so native-language
                      'yes'/'no'/etc. are recognised correctly).
    """
    # Use english-normalised text for control words; fall back to transcription.
    ctrl_lower = (eng_text or transcription).lower()
    lower      = transcription.lower()
    compose  = session.get("email_compose")

    # Helper: check confirm/cancel in the session language as an offline fallback.
    def _is_native_confirm() -> bool:
        try:
            from services.lang_utils import NATIVE_COMMANDS
            lang = session.get("language", "en")
            return any(p in ctrl_lower for p in NATIVE_COMMANDS.get(lang, {}).get("_confirm", []))
        except Exception:
            return False

    def _is_native_cancel() -> bool:
        try:
            from services.lang_utils import NATIVE_COMMANDS
            lang = session.get("language", "en")
            return any(p in ctrl_lower for p in NATIVE_COMMANDS.get(lang, {}).get("_cancel", []))
        except Exception:
            return False

    # ── Step 0: start the flow ────────────────────────────────────────────────
    if compose is None:
        session["email_compose"] = {"step": "to", "to": "", "subject": "", "body": ""}
        return (
            "Sure! Let's compose an email. "
            "Who would you like to send it to? Please say the recipient's email address."
        )

    step = compose["step"]

    # ── Step 1: recipient ─────────────────────────────────────────────────────
    if step == "to":
        raw     = transcription.strip()
        to_addr = _normalize_email_address(raw)
        logger.info("Compose 'to': raw=%r  normalised=%r", raw, to_addr)

        retries = compose.get("to_retries", 0)

        if not _is_valid_email(to_addr):
            # Keep step as "to", increment retry counter
            new_retries = retries + 1
            session["email_compose"] = dict(compose, to_retries=new_retries)
            session.modified = True
            if new_retries >= 2:
                # After 2 failures, suggest typing
                return (
                    f"I heard: {raw!r} — that doesn't look like a valid email address. "
                    "Please type the address in the text box that appeared below the mic, "
                    "then say continue."
                )
            return (
                f"I heard: {raw!r} — that doesn't look like a valid email address. "
                "Please say it again clearly. For example: "
                "r u t i k at gmail dot com."
            )

        session["email_compose"] = dict(compose, to=to_addr, step="subject", to_retries=0)
        session.modified = True
        readable = to_addr.replace("@", " at ").replace(".", " dot ")
        return f"Got it — sending to {readable}. What is the subject?"

    # ── Step 2: subject ───────────────────────────────────────────────────────
    elif step == "subject":
        subject = transcription.strip()
        session["email_compose"] = dict(compose, subject=subject, step="body")
        session.modified = True
        return f"Subject: {subject}. What is your message?"

    # ── Step 3: body ─────────────────────────────────────────────────────────
    elif step == "body":
        body    = transcription.strip()
        session["email_compose"] = dict(compose, body=body, step="confirm", pin_attempts=0)
        session.modified = True
        to      = compose["to"]
        subject = compose["subject"]
        readable_to = to.replace("@", " at ").replace(".", " dot ")
        return (
            f"Ready to send. "
            f"To: {readable_to}. Subject: {subject}. "
            f"Message: {body}. "
            f"Say yes or confirm to continue to PIN verification, or say cancel to stop."
        )

    # ── Step 4: confirm ───────────────────────────────────────────────────────
    elif step == "confirm":
        # Check against ctrl_lower (English-normalised) so native-language
        # confirmations like "हाँ", "oui", "sí" etc. are recognised.
        # Also check native phrases directly as offline fallback.
        if _any_token_matches(ctrl_lower, _CONFIRM_EXACT) or _is_native_confirm():
            session["email_compose"] = dict(compose, step="pin")
            session.modified = True
            return "Security check: please say your PIN digits now."
        if _any_token_matches(ctrl_lower, _CANCEL_EXACT) or _is_native_cancel():
            session.pop("email_compose", None)
            session.modified = True
            return "Email cancelled."

        # If user speaks PIN at confirm step, treat it as direct secure confirmation.
        if _looks_like_pin_input(ctrl_lower):
            pin_candidate = (eng_text or transcription).strip()
            user_email = _session_user_email(session)
            if verify_pin(pin_candidate, user_email=user_email):
                to      = compose["to"]
                subject = compose["subject"]
                body    = compose["body"]
                session.pop("email_compose", None)
                session.modified = True
                try:
                    success, message = send_email(session, to, subject, body)
                    if success:
                        _remember_saved_email(session, to)
                        _log_compose_activity(
                            session,
                            "email_sent",
                            details={"to": to, "subject": subject[:80]},
                        )
                        readable_to = to.replace("@", " at ").replace(".", " dot ")
                        return f"Email sent successfully to {readable_to}!"
                    _log_compose_activity(
                        session,
                        "email_send_failed",
                        status="error",
                        details={"to": to, "subject": subject[:80], "reason": message},
                    )
                    logger.error("Send email returned failure: %s", message)
                    return f"Failed to send email. {message}. Please try again."
                except Exception as exc:
                    _log_compose_activity(
                        session,
                        "email_send_failed",
                        status="error",
                        details={"to": to, "subject": subject[:80], "reason": str(exc)},
                    )
                    logger.error("Send email exception: %s", exc)
                    return "Sorry, I could not send the email. Please check your settings and try again."

            attempts = int(compose.get("pin_attempts", 0)) + 1
            if attempts >= Config.PIN_MAX_ATTEMPTS:
                session.pop("email_compose", None)
                session.modified = True
                return "PIN verification failed too many times. Email cancelled."

            session["email_compose"] = dict(compose, pin_attempts=attempts, step="pin")
            session.modified = True
            return "That PIN is not correct. Please say your PIN again."

        return "Please say yes to continue, or say cancel to stop. You can also speak your PIN digits now."

    elif step == "pin":
        pin_candidate = (eng_text or transcription).strip()
        user_email = _session_user_email(session)
        if verify_pin(pin_candidate, user_email=user_email):
            to      = compose["to"]
            subject = compose["subject"]
            body    = compose["body"]
            session.pop("email_compose", None)
            session.modified = True
            try:
                success, message = send_email(session, to, subject, body)
                if success:
                    _remember_saved_email(session, to)
                    _log_compose_activity(
                        session,
                        "email_sent",
                        details={"to": to, "subject": subject[:80]},
                    )
                    readable_to = to.replace("@", " at ").replace(".", " dot ")
                    return f"Email sent successfully to {readable_to}!"
                _log_compose_activity(
                    session,
                    "email_send_failed",
                    status="error",
                    details={"to": to, "subject": subject[:80], "reason": message},
                )
                logger.error("Send email returned failure: %s", message)
                return f"Failed to send email. {message}. Please try again."
            except Exception as exc:
                _log_compose_activity(
                    session,
                    "email_send_failed",
                    status="error",
                    details={"to": to, "subject": subject[:80], "reason": str(exc)},
                )
                logger.error("Send email exception: %s", exc)
                return "Sorry, I could not send the email. Please check your settings and try again."

        attempts = int(compose.get("pin_attempts", 0)) + 1
        if attempts >= Config.PIN_MAX_ATTEMPTS:
            session.pop("email_compose", None)
            session.modified = True
            return "PIN verification failed too many times. Email cancelled."

        session["email_compose"] = dict(compose, pin_attempts=attempts, step="pin")
        session.modified = True
        return "That PIN is not correct. Please say your PIN again."

    # fallback
    session.pop("email_compose", None)
    return "Something went wrong. Email compose reset. Please try again."


# ── Telegram messaging handlers ───────────────────────────────────────────────

def _handle_send_message(session: dict, transcription: str, eng_text: str = "") -> str:
    """
    Multi-step voice-guided Telegram message compose.

    `transcription` — raw Whisper output in the user's language (used to
                      capture recipient name and message body).
    `eng_text`      — English-normalised version (used for confirm/cancel
                      matching so native-language yes/no is recognised).
    """
    # Use English-normalised text for control words; fall back to transcription.
    ctrl_lower = (eng_text or transcription).lower().strip()
    lower      = transcription.lower().strip()
    compose = session.get("msg_compose")

    # Helper: check confirm/cancel in the session language as an offline fallback.
    def _is_native_confirm() -> bool:
        try:
            from services.lang_utils import NATIVE_COMMANDS
            lang = session.get("language", "en")
            return any(p in ctrl_lower for p in NATIVE_COMMANDS.get(lang, {}).get("_confirm", []))
        except Exception:
            return False

    def _is_native_cancel() -> bool:
        try:
            from services.lang_utils import NATIVE_COMMANDS
            lang = session.get("language", "en")
            return any(p in ctrl_lower for p in NATIVE_COMMANDS.get(lang, {}).get("_cancel", []))
        except Exception:
            return False

    # Step 0 — start
    if compose is None:
        session["msg_compose"] = {"step": "to", "to": "", "text": ""}
        session.modified = True
        return (
            "Sure! Let's send a Telegram message. "
            "Who would you like to send it to? Say their name."
        )

    step = compose["step"]

    # Step 1 — capture contact name → ask to confirm
    if step == "to":
        name = _normalize_contact_name(transcription)
        session["msg_compose"] = dict(compose, to=name, step="to_confirm")
        session.modified = True
        return (
            f"I heard {name}. "
            f"Is that the correct recipient? Say yes to confirm, "
            f"or say the correct name."
        )

    # Step 1b — confirm recipient
    elif step == "to_confirm":
        # Use ctrl_lower (English-normalised) so native confirmations work.
        # Also check native phrases directly as offline fallback.
        if _any_token_matches(ctrl_lower, _CONFIRM_EXACT) or _is_native_confirm():
            # Confirmed — advance to message text
            session["msg_compose"] = dict(compose, step="text")
            session.modified = True
            return (
                f"Great! Sending to {compose['to']}. "
                f"Now what would you like to say?"
            )
        else:
            # User said a different name — treat this utterance as the corrected name
            new_name = _normalize_contact_name(transcription)
            session["msg_compose"] = dict(compose, to=new_name, step="to_confirm")
            session.modified = True
            return (
                f"Got it. I heard {new_name}. "
                f"Is that the correct recipient? Say yes to confirm or say the correct name."
            )

    # Step 2 — message text
    elif step == "text":
        text = transcription.strip()
        session["msg_compose"] = dict(compose, text=text, step="confirm", pin_attempts=0)
        session.modified = True
        return (
            f"Ready to send to {compose['to']}. "
            f"Message: {text}. "
            f"Say yes to continue to PIN verification, or say cancel to stop."
        )

    # Step 3 — confirm
    elif step == "confirm":
        # Use ctrl_lower (English-normalised) so native confirmations work.
        # Also check native phrases directly as offline fallback.
        if _any_token_matches(ctrl_lower, _CONFIRM_EXACT) or _is_native_confirm():
            session["msg_compose"] = dict(compose, step="pin")
            session.modified = True
            return "Security check: please say your PIN digits now."
        if _any_token_matches(ctrl_lower, _CANCEL_EXACT) or _is_native_cancel():
            session.pop("msg_compose", None)
            session.modified = True
            return "Message cancelled. What else can I help you with?"

        # If user speaks PIN at confirm step, treat it as direct secure confirmation.
        if _looks_like_pin_input(ctrl_lower):
            pin_candidate = (eng_text or transcription).strip()
            user_email = _session_user_email(session)
            if verify_pin(pin_candidate, user_email=user_email):
                to = compose["to"]
                text = compose["text"]
                session.pop("msg_compose", None)
                session.modified = True
                result = tg_send(to, text)
                if result.get("success"):
                    _remember_saved_contact(session, to)
                    _log_compose_activity(session, "message_sent", details={"receiver": to})
                    return f"Message sent to {to} via Telegram!"
                _log_compose_activity(
                    session,
                    "message_send_failed",
                    status="error",
                    details={"receiver": to, "reason": result.get("message", "")},
                )
                return f"Could not send message. {result.get('message', '')}"

            attempts = int(compose.get("pin_attempts", 0)) + 1
            if attempts >= Config.PIN_MAX_ATTEMPTS:
                session.pop("msg_compose", None)
                session.modified = True
                return "PIN verification failed too many times. Message cancelled."

            session["msg_compose"] = dict(compose, pin_attempts=attempts, step="pin")
            session.modified = True
            return "That PIN is not correct. Please say your PIN again."

        return "Please say yes to continue, or say cancel to stop. You can also speak your PIN digits now."

    elif step == "pin":
        pin_candidate = (eng_text or transcription).strip()
        user_email = _session_user_email(session)
        if verify_pin(pin_candidate, user_email=user_email):
            to = compose["to"]
            text = compose["text"]
            session.pop("msg_compose", None)
            session.modified = True
            result = tg_send(to, text)
            if result.get("success"):
                _remember_saved_contact(session, to)
                _log_compose_activity(session, "message_sent", details={"receiver": to})
                return f"Message sent to {to} via Telegram!"
            _log_compose_activity(
                session,
                "message_send_failed",
                status="error",
                details={"receiver": to, "reason": result.get("message", "")},
            )
            return f"Could not send message. {result.get('message', '')}"

        attempts = int(compose.get("pin_attempts", 0)) + 1
        if attempts >= Config.PIN_MAX_ATTEMPTS:
            session.pop("msg_compose", None)
            session.modified = True
            return "PIN verification failed too many times. Message cancelled."

        session["msg_compose"] = dict(compose, pin_attempts=attempts, step="pin")
        session.modified = True
        return "That PIN is not correct. Please say your PIN again."

    session.pop("msg_compose", None)
    return "Something went wrong. Message compose reset. Please try again."


def _handle_cancel_message(session: dict) -> str:
    session.pop("msg_compose", None)
    session.modified = True
    return "Message cancelled. What else can I help you with?"


def _handle_read_messages(session: dict) -> str:
    """Read the latest Telegram messages by voice."""
    result = tg_all()
    msgs = result.get("messages", [])
    if not msgs:
        return "You have no new Telegram messages."
    lines = []
    for m in msgs[-5:]:   # speak up to 5 most recent
        sender  = _tts_safe(m.get("sender", "Unknown"))
        text    = _tts_safe(m.get("text",   ""))
        lines.append(f"From {sender}: {text}.")
    intro = f"You have {len(msgs)} message{'s' if len(msgs) > 1 else ''}. "
    return intro + " ".join(lines)


def _handle_logout() -> str:
    return "You have been logged out. Goodbye!"



def _handle_help() -> str:
    return (
        "You can say: "
        "Read emails — to hear your inbox. "
        "Summarize email — to get a quick summary of the current email. "
        "Next — to move to the next email. "
        "Previous — to go back. "
        "Read more — to hear more of a long email. "
        "Read email two — to jump to a specific email. "
        "Send email — to compose a new email. "
        "Stop — to interrupt me while I am speaking. "
        "Send message — to compose a Telegram message. "
        "Read messages — to hear your latest Telegram messages. "
        "Summarize messages — to get a summary of recent Telegram messages. "
        "Switch service — to toggle between Email and Telegram mode. "
        "Logout — to sign out."
    )


def _handle_unknown(text: str, session: dict | None = None) -> str:
    if not text:
        return ""
    # Give a hint that matches the active service so the user knows what to say
    active = (session or {}).get("active_service")
    if active == "telegram":
        hint = "Try saying: send message, read messages, or summarize messages."
    elif active == "email":
        hint = "Try saying: read emails, send email, next, previous, or summarize email."
    else:
        hint = "Try saying: Email to check your inbox, or Telegram for chat messages."
    return f"I heard: {text}. I am not sure what you want. {hint}"


# ── Main entry point ───────────────────────────────────────────────────────────
def _handle_service_choice(eng_text: str, orig_transcription: str, session: dict) -> dict:
    """
    Called when the user is responding to the 'which service — Email or Telegram?' prompt.
    
    `eng_text`          — English-normalised Whisper transcription (for keyword matching)
    `orig_transcription`— original Whisper output in the user's language (shown in UI)
    Returns the same dict shape as process_voice_command.
    """
    t = eng_text.lower()
    if any(w in t for w in ["telegram", "chat", "whatsapp", "msg", "message", "messaging", "text"]):
        service = "telegram"
        resp = (
            "Telegram mode activated. "
            "Say send message to compose, or read messages to hear your latest messages."
        )
    elif any(w in t for w in ["email", "mail", "gmail", "inbox", "e-mail"]):
        service = "email"
        resp = (
            "Email mode activated. "
            "Say read emails to check your inbox, or send email to compose a new one."
        )
    else:
        service = None
        resp = "Sorry, I didn't catch that. Please say Email for your inbox, or Telegram for chat."

    session["active_service"] = service
    session.modified = True

    # ── TTS: translate response to user's language, then speak with multilang engine ──
    audio_url = None
    from services.lang_utils import translate_text, speak_multilang
    tts_lang = session.get("language", "en")
    tts_text = translate_text(resp, tts_lang)
    tts_path = speak_multilang(tts_text, tts_lang)
    if tts_path:
        audio_url = f"/static/audio/{os.path.basename(tts_path)}"

    return {
        "transcription":  orig_transcription,   # show original language to user
        "intent":         "service_selected" if service else "choosing_service",
        "service":        service,
        "response_text":  tts_text or resp,     # show translated text in UI
        "audio_url":      audio_url,
        "email_step":     None,
        "msg_step":       None,
        "voice_lang":     tts_lang,
    }


def _handle_switch_service(session: dict) -> str:
    """Clears the active service so the user is asked again on next mic tap."""
    session.pop("active_service", None)
    session.pop("email_compose", None)
    session.pop("msg_compose", None)
    session.modified = True
    return "Service reset. Tap the microphone and say Email or Telegram to choose again."


def _handle_set_language(session: dict) -> str:
    """
    Switch the active voice language.
    The language code to apply is already in session["_set_lang_code"]
    (set by _detect_intent before routing here).
    Returns the confirmation phrase in the TARGET language so it is spoken
    correctly — do NOT run this return value through translate_text.
    """
    from services.lang_utils import SWITCH_PHRASES
    lang = session.pop("_set_lang_code", "en") or "en"
    session["language"] = lang
    session.modified = True
    return SWITCH_PHRASES.get(lang, f"Language switched to {lang}.")


def _cleanup_old_audio_files(max_age_seconds: int = 600) -> None:
    """Delete TTS output files (wav/mp3) in UPLOAD_FOLDER older than max_age_seconds."""
    import time
    try:
        now = time.time()
        for fname in os.listdir(Config.UPLOAD_FOLDER):
            if not (fname.endswith(".wav") or fname.endswith(".mp3")):
                continue
            fpath = os.path.join(Config.UPLOAD_FOLDER, fname)
            try:
                if now - os.path.getmtime(fpath) > max_age_seconds:
                    os.remove(fpath)
            except OSError:
                pass
    except Exception:
        pass


def process_voice_command(audio_file: FileStorage, session: dict, choosing_service: bool = False) -> dict:
    """
    Accepts a Werkzeug FileStorage WAV upload, transcribes it,
    detects intent, generates a spoken response, and returns a dict:
        {
            "transcription": str,
            "intent": str,
            "response_text": str,
            "audio_url": str | None
        }
    """
    # 0 — Opportunistic cleanup of stale TTS files (> 10 min old)
    _cleanup_old_audio_files()

    # 1 — Save raw upload
    temp_path = os.path.join(Config.UPLOAD_FOLDER, f"input_{uuid.uuid4().hex}.wav")
    audio_file.save(temp_path)

    # Early exit if STT backend is not available
    if _whisper_model is None:
        tts_path = speak_to_file("Speech recognition backend is not available. Please check server dependencies.")
        audio_url = f"/static/audio/{os.path.basename(tts_path)}" if tts_path else None
        try: os.remove(temp_path)
        except OSError: pass
        return {
            "transcription": "",
            "intent": "error",
            "response_text": "Speech recognition backend is not available. Check deployment dependencies.",
            "audio_url": audio_url,
        }

    # 2 — Transcribe (pass session language so Whisper focuses on the right script)
    stt_lang = session.get("language", "en")
    transcription = transcribe(temp_path, language=stt_lang)
    logger.info("Transcription (%s): %r", stt_lang, transcription)

    # 2.1 — Normalise to English for intent detection.
    # Whisper transcribes in the chosen language (Hindi → Devanagari, etc.).
    # Translating to English first lets all existing keyword/regex tables work
    # without change, regardless of which language the user speaks.
    if stt_lang != "en" and transcription.strip():
        from services.lang_utils import translate_to_english
        eng_text = translate_to_english(transcription, stt_lang)
        logger.info("English normalised text: %r", eng_text)
    else:
        eng_text = transcription

    # 2.5 — Service-selection shortcut (user is answering "Email or Telegram?")
    if choosing_service:
        result = _handle_service_choice(eng_text, transcription, session)
        try:
            os.remove(temp_path)
        except OSError:
            pass
        return result

    # 3 — Detect intent using English-normalised text (works for all languages)
    intent = _detect_intent(eng_text, session)

    # 4 — Execute intent
    intent_map = {
        "list_emails":       lambda: _handle_list_emails(session),
        "read_email":        lambda: _handle_read_email(session),
        "next_email":        lambda: _handle_next_email(session),
        "prev_email":        lambda: _handle_prev_email(session),
        "read_more":         lambda: _handle_read_more(session),
        "send_email":        lambda: _handle_send_email(session, transcription, eng_text),
        "stop_reading":      lambda: _handle_stop_reading(session),
        "cancel_email":      lambda: _handle_cancel_email(session),
        "summarize_email":   lambda: _handle_summarize_email(session),
        "send_message":      lambda: _handle_send_message(session, transcription, eng_text),
        "read_messages":     lambda: _handle_read_messages(session),
        "cancel_message":    lambda: _handle_cancel_message(session),
        "summarize_message": lambda: _handle_summarize_message(session),
        "switch_service":    lambda: _handle_switch_service(session),
        "set_language":      lambda: _handle_set_language(session),
        "logout":            _handle_logout,
        "help":              _handle_help,
        "unknown":           lambda: _handle_unknown(transcription, session),
    }
    response_text = intent_map.get(intent, lambda: _handle_unknown(transcription))()

    # 5 — TTS (skip if response is empty, e.g. stop_reading)
    # For set_language the response is already in the target language (no translation needed).
    # For all other intents, translate the English response into the session language.
    audio_url = None
    tts_text = ""
    if response_text:
        from services.lang_utils import translate_text, speak_multilang
        tts_lang = session.get("language", "en")
        if intent == "set_language":
            tts_text = response_text          # already in target language
        else:
            tts_text = translate_text(response_text, tts_lang)
        tts_path = speak_multilang(tts_text, tts_lang)
        if tts_path:
            audio_url = f"/static/audio/{os.path.basename(tts_path)}"

    # Clean up input file
    try:
        os.remove(temp_path)
    except OSError:
        pass

    return {
        "transcription": transcription,
        "intent":        intent,
        "response_text": tts_text or response_text,   # show translated text in UI
        "audio_url":     audio_url,
        "service":       session.get("active_service"),
        "voice_lang":    session.get("language", "en"),   # active language code for JS
        # Tells the frontend which compose step we are on (or null)
        "email_step":    (session.get("email_compose") or {}).get("step"),
        "msg_step":      (session.get("msg_compose")   or {}).get("step"),
    }


# ── Text-input path for compose fields (bypasses STT) ─────────────────────────
def process_text_compose_input(field: str, value: str, session: dict) -> dict:
    """
    Accepts a typed value for one compose field and advances the flow.
    Returns the same dict shape as process_voice_command.
    """
    compose = session.get("email_compose")

    # Safety: if no compose session is active, start one
    if compose is None:
        session["email_compose"] = {"step": "to", "to": "", "subject": "", "body": ""}
        session.modified = True
        compose = session["email_compose"]

    response_text = ""

    if field == "to":
        # Validate the typed address (basic sanity, not full RFC)
        if not _is_valid_email(value):
            response_text = (
                f"'{value}' doesn't look like a valid email address. "
                "Please check and try again."
            )
        else:
            session["email_compose"] = dict(compose, to=value, step="subject", to_retries=0)
            session.modified = True
            readable = value.replace("@", " at ").replace(".", " dot ")
            response_text = f"Got it — sending to {readable}. Now say the subject."

    elif field == "subject":
        session["email_compose"] = dict(compose, subject=value, step="body")
        session.modified = True
        response_text = f"Subject: {value}. Now say your message."

    elif field == "body":
        to      = compose.get("to", "")
        subject = compose.get("subject", "")
        session["email_compose"] = dict(compose, body=value, step="confirm", pin_attempts=0)
        session.modified = True
        readable_to = to.replace("@", " at ").replace(".", " dot ")
        response_text = (
            f"Ready to send. To: {readable_to}. Subject: {subject}. "
            f"Message: {value}. Say yes to continue to PIN verification, or say cancel to stop."
        )

    elif field == "confirm":
        session["email_compose"] = dict(compose, step="pin")
        session.modified = True
        response_text = "Security check: type or say your PIN digits now."

    elif field == "pin":
        user_email = _session_user_email(session)
        if verify_pin(value, user_email=user_email):
            to      = compose.get("to", "")
            subject = compose.get("subject", "")
            body_v  = compose.get("body", "")
            session.pop("email_compose", None)
            session.modified = True
            try:
                success, message = send_email(session, to, subject, body_v)
                if success:
                    _remember_saved_email(session, to)
                    _log_compose_activity(
                        session,
                        "email_sent",
                        details={"to": to, "subject": subject[:80]},
                    )
                    readable_to = to.replace("@", " at ").replace(".", " dot ")
                    response_text = f"Email sent successfully to {readable_to}!"
                else:
                    _log_compose_activity(
                        session,
                        "email_send_failed",
                        status="error",
                        details={"to": to, "subject": subject[:80], "reason": message},
                    )
                    response_text = f"Failed to send email. {message}. Please try again."
            except Exception as exc:
                _log_compose_activity(
                    session,
                    "email_send_failed",
                    status="error",
                    details={"to": to, "subject": subject[:80], "reason": str(exc)},
                )
                logger.error("Text compose send error: %s", exc)
                response_text = "Sorry, I could not send the email. Please check your settings."
        else:
            attempts = int(compose.get("pin_attempts", 0)) + 1
            if attempts >= Config.PIN_MAX_ATTEMPTS:
                session.pop("email_compose", None)
                session.modified = True
                response_text = "PIN verification failed too many times. Email cancelled."
            else:
                session["email_compose"] = dict(compose, pin_attempts=attempts, step="pin")
                session.modified = True
                response_text = "Incorrect PIN. Please enter PIN again."

    else:
        response_text = "Unknown field."

    audio_url = None
    if response_text:
        tts_path = speak_to_file(response_text)
        if tts_path:
            audio_url = f"/static/audio/{os.path.basename(tts_path)}"

    return {
        "transcription": f"[typed] {value}",
        "intent":        "send_email",
        "response_text": response_text,
        "audio_url":     audio_url,
        "email_step":    (session.get("email_compose") or {}).get("step"),
    }


def process_text_msg_input(field: str, value: str, session: dict) -> dict:
    """
    Accepts a typed value for the active Telegram message compose step.
    Returns the same dict shape as process_voice_command.
    """
    compose = session.get("msg_compose")
    if compose is None:
        session["msg_compose"] = {"step": "to", "to": "", "text": ""}
        session.modified = True
        compose = session["msg_compose"]

    response_text = ""

    if field == "to":
        name = _normalize_contact_name(value)
        session["msg_compose"] = dict(compose, to=name, step="to_confirm")
        session.modified = True
        response_text = (
            f"I have {name} as the recipient. "
            f"Type YES to confirm or type a different name."
        )

    elif field == "to_confirm":
        if re.match(r'^(yes|y|confirm|ok|okay|correct|right|yep|yeah)$', value.strip(), re.I):
            session["msg_compose"] = dict(compose, step="text")
            session.modified = True
            response_text = (
                f"Recipient confirmed: {compose.get('to', '')}. "
                f"Now type your message."
            )
        else:
            new_name = _normalize_contact_name(value)
            session["msg_compose"] = dict(compose, to=new_name, step="to_confirm")
            session.modified = True
            response_text = (
                f"Updated to {new_name}. "
                f"Type YES to confirm or type a different name."
            )

    elif field == "text":
        text = value.strip()
        to   = compose.get("to", "")
        session["msg_compose"] = dict(compose, text=text, step="confirm", pin_attempts=0)
        session.modified = True
        response_text = (
            f"Ready to send to {to}. "
            f"Message: {text}. Say yes or type YES to continue to PIN verification, or say cancel to stop."
        )

    elif field == "confirm":
        session["msg_compose"] = dict(compose, step="pin")
        session.modified = True
        response_text = "Security check: type or say your PIN digits now."

    elif field == "pin":
        user_email = _session_user_email(session)
        if verify_pin(value, user_email=user_email):
            to = compose.get("to", "")
            text = compose.get("text", "")
            session.pop("msg_compose", None)
            session.modified = True
            try:
                result = tg_send(to, text)
                if result.get("success"):
                    _remember_saved_contact(session, to)
                    _log_compose_activity(session, "message_sent", details={"receiver": to})
                    response_text = f"Telegram message sent to {to}!"
                else:
                    _log_compose_activity(
                        session,
                        "message_send_failed",
                        status="error",
                        details={"receiver": to, "reason": result.get("message", "")},
                    )
                    response_text = f"Could not send. {result.get('message', '')}. Please try again."
            except Exception as exc:
                _log_compose_activity(
                    session,
                    "message_send_failed",
                    status="error",
                    details={"receiver": to, "reason": str(exc)},
                )
                logger.error("Text msg send error: %s", exc)
                response_text = "Sorry, I could not send the message. Please try again."
        else:
            attempts = int(compose.get("pin_attempts", 0)) + 1
            if attempts >= Config.PIN_MAX_ATTEMPTS:
                session.pop("msg_compose", None)
                session.modified = True
                response_text = "PIN verification failed too many times. Message cancelled."
            else:
                session["msg_compose"] = dict(compose, pin_attempts=attempts, step="pin")
                session.modified = True
                response_text = "Incorrect PIN. Please enter PIN again."

    else:
        response_text = "Unknown field."

    audio_url = None
    if response_text:
        tts_path = speak_to_file(response_text)
        if tts_path:
            audio_url = f"/static/audio/{os.path.basename(tts_path)}"

    return {
        "transcription": f"[typed] {value}",
        "intent":        "send_message",
        "response_text": response_text,
        "audio_url":     audio_url,
        "msg_step":      (session.get("msg_compose") or {}).get("step"),
    }
