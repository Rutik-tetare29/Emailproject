"""
Voice Service — Voice Confirmation Security (Feature 4) + Multi-Language TTS.

This module is the single entry-point for everything voice-output related.

confirm_action(action_text, session)
-------------------------------------
  1. Reads back `action_text` via TTS ("You are about to send …")
  2. Asks "Do you want to confirm this action? Please say yes or no."
  3. Returns True if the user later POSTs a "yes" answer, False otherwise.

  Because confirmation happens in two HTTP round-trips (TTS → browser plays
  audio → user speaks → browser POSTs transcription), the logic is:
    • `speak_confirmation(action_text)` → returns audio URL
    • `check_confirmation_answer(transcription)` → returns True / False

  The caller in app.py can wire these two steps together easily.

Multi-language TTS
------------------
  `speak_text_lang(text, lang, out_path)` wraps pyttsx3 and picks the best
  available voice for the requested language. Falls back to English.

  Supported language codes (configurable in config.py):
    "en" — English (default)
    "hi" — Hindi (requires a Hindi SAPI5/eSpeak voice installed)
    "es" — Spanish
    "fr" — French
    "de" — German
    ... any language code that has a matching system voice.
"""

import os
import uuid
import logging
import pyttsx3

from config import Config
from services.tts_engine import speak_to_file  # reuse existing engine

logger = logging.getLogger(__name__)

# ── Language → voice name keyword map ────────────────────────────────────────
# pyttsx3 voice selection is done by matching the voice's `name` or `id` string.
# Add more entries as needed.

_LANG_VOICE_KEYWORDS: dict[str, list[str]] = {
    "en": ["zira", "david", "english", "en-us", "en-gb", "en_"],
    "hi": ["hindi", "hemant", "kalpana", "hi-in", "hi_"],
    "es": ["spanish", "es-", "es_", "sabina", "jorge", "pablo"],
    "fr": ["french", "fr-", "fr_", "hortense", "julie"],
    "de": ["german", "de-", "de_", "hedda", "stefan"],
    "it": ["italian", "it-", "it_", "elsa", "cosimo"],
    "pt": ["portuguese", "pt-", "pt_", "maria", "helia"],
    "ja": ["japanese", "ja-", "ja_", "haruka", "ichiro"],
    "zh": ["chinese", "zh-", "zh_", "huihui", "yaoyao"],
    "ar": ["arabic", "ar-", "ar_", "naayf", "hoda"],
}

# Confirmation YES / NO keyword sets (robust matching)
_YES_WORDS = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm",
              "send", "go", "proceed", "do it", "affirmative", "correct",
              "right", "absolutely", "definitely", "of course", "go ahead"}
_NO_WORDS  = {"no", "nope", "nah", "cancel", "abort", "stop", "dont",
              "don't", "negative", "never", "skip", "quit", "reject",
              "refuse", "hold on", "wait"}


# ── Voice selection ───────────────────────────────────────────────────────────

def _get_voice_for_lang(engine: pyttsx3.Engine, lang: str) -> str | None:
    """
    Return the voice ID for the requested language, or None if not found.
    Falls back to English if the requested language is unavailable.
    """
    lang = lang.lower().strip()
    keywords = _LANG_VOICE_KEYWORDS.get(lang, _LANG_VOICE_KEYWORDS["en"])
    voices = engine.getProperty("voices")

    # Try requested language first
    for voice in voices:
        voice_id_lower   = (voice.id   or "").lower()
        voice_name_lower = (voice.name or "").lower()
        if any(kw in voice_id_lower or kw in voice_name_lower for kw in keywords):
            return voice.id

    # Fall back to English
    if lang != "en":
        logger.warning(
            "No voice found for language '%s'. Falling back to English.", lang
        )
        return _get_voice_for_lang(engine, "en")

    return None  # use system default


def speak_text_lang(text: str, lang: str = "en", out_path: str = None) -> str:
    """
    Convert `text` to speech in the requested language and save to a WAV file.

    Parameters
    ----------
    text     : str   Text to speak.
    lang     : str   BCP-47 language code prefix, e.g. "en", "hi", "fr".
                     Falls back to English if the language voice is not installed.
    out_path : str   Optional explicit output path. If None, a temp file is
                     created in Config.UPLOAD_FOLDER.

    Returns
    -------
    str  Full path of the WAV file produced.

    Notes
    -----
    For non-English languages on Windows, install the desired voices via
    Settings → Time & Language → Speech → Add voices.
    On Linux, install espeak-ng voices: sudo apt install espeak-ng-data-<lang>
    """
    if lang == "en" or lang == Config.DEFAULT_LANGUAGE:
        # Reuse the existing high-quality engine (handles chunking, stitching)
        if out_path is None:
            out_path = os.path.join(
                Config.UPLOAD_FOLDER, f"tts_{uuid.uuid4().hex}.wav"
            )
        speak_to_file(text, out_path)
        return out_path

    # For other languages, use a fresh pyttsx3 engine with the matching voice
    if out_path is None:
        out_path = os.path.join(
            Config.UPLOAD_FOLDER, f"tts_{lang}_{uuid.uuid4().hex}.wav"
        )

    engine = pyttsx3.init()
    try:
        engine.setProperty("rate", 155)
        engine.setProperty("volume", 0.95)
        voice_id = _get_voice_for_lang(engine, lang)
        if voice_id:
            engine.setProperty("voice", voice_id)
        engine.save_to_file(text, out_path)
        engine.runAndWait()
    finally:
        engine.stop()

    return out_path


# ── Voice confirmation ────────────────────────────────────────────────────────

def speak_confirmation(action_text: str, lang: str = None) -> str:
    """
    Generate a TTS audio file that reads back the action and asks for
    yes/no confirmation.

    Returns
    -------
    str  URL path of the audio file (relative to /static/audio/).
    """
    if lang is None:
        lang = Config.DEFAULT_LANGUAGE

    prompt = (
        f"You are about to: {action_text}. "
        "Do you want to confirm and send? Please say yes or no."
    )
    out_path = os.path.join(
        Config.UPLOAD_FOLDER, f"confirm_{uuid.uuid4().hex}.wav"
    )
    speak_text_lang(prompt, lang=lang, out_path=out_path)

    filename = os.path.basename(out_path)
    return f"/static/audio/{filename}"


def check_confirmation_answer(transcription: str) -> bool:
    """
    Determine whether the user said "yes" or "no" from a voice transcription.

    Parameters
    ----------
    transcription : str  The raw text from STT (e.g. "yes please", "no thanks").

    Returns
    -------
    bool  True = confirmed (send).  False = rejected (cancel).

    Strategy
    --------
    Normalise the text, then check for YES words before NO words.
    If neither is found, default to False (conservative — don't send by mistake).
    """
    text = transcription.lower().strip()
    # Remove punctuation
    text = "".join(c for c in text if c.isalpha() or c.isspace())

    words = set(text.split())

    if words & _YES_WORDS:  # intersection — at least one yes-word present
        return True
    if words & _NO_WORDS:
        return False

    # Full phrase checks for multi-word expressions
    for phrase in ("go ahead", "do it", "of course", "sounds good", "send it"):
        if phrase in text:
            return True
    for phrase in ("don't send", "do not send", "hold on", "wait"):
        if phrase in text:
            return False

    logger.warning(
        "Confirmation answer unclear: '%s'. Defaulting to False (cancel).", transcription
    )
    return False


def confirm_action(action_text: str, transcription: str, lang: str = None) -> dict:
    """
    One-shot helper that combines speak + check into a single call.

    In practice the browser makes two round-trips:
      POST /confirm/start  → server calls speak_confirmation() → audio URL
      POST /confirm/answer → server calls check_confirmation_answer()

    This helper is useful for testing or non-interactive scripts.

    Returns
    -------
    dict  {"confirmed": bool, "audio_url": str, "answer_detected": str}
    """
    audio_url = speak_confirmation(action_text, lang=lang)
    confirmed = check_confirmation_answer(transcription)
    return {
        "confirmed"      : confirmed,
        "audio_url"      : audio_url,
        "answer_detected": "yes" if confirmed else "no",
    }
