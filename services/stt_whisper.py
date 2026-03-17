"""Lightweight Speech-to-Text backend.

Keeps the same module interface used by the rest of the app:
  - `transcribe(wav_path, language="en")`
  - `_model` sentinel for readiness checks in other modules

This implementation uses `speech_recognition` + Google Web Speech API,
which avoids shipping heavy local Whisper/Torch models in deployments.
"""

import logging

try:
    import speech_recognition as sr
except Exception:  # pragma: no cover
    sr = None


logger = logging.getLogger(__name__)

# Keep a module-level sentinel so existing checks in voice_processor keep working.
_model = object() if sr is not None else None
_recognizer = sr.Recognizer() if sr is not None else None


_LANGUAGE_MAP = {
    "en": "en-IN",
    "hi": "hi-IN",
    "mr": "mr-IN",
    "es": "es-ES",
    "fr": "fr-FR",
    "de": "de-DE",
    "it": "it-IT",
    "pt": "pt-PT",
    "ja": "ja-JP",
    "zh": "zh-CN",
}


def _to_google_lang(language: str) -> str:
    code = (language or "en").strip().lower()
    return _LANGUAGE_MAP.get(code, "en-IN")


def transcribe(wav_path: str, language: str = "en") -> str:
    """Transcribe WAV audio into text using a lightweight online backend."""
    if _recognizer is None:
        logger.error("speech_recognition is not available")
        return ""

    try:
        with sr.AudioFile(wav_path) as source:
            audio = _recognizer.record(source)

        text = _recognizer.recognize_google(audio, language=_to_google_lang(language))
        text = (text or "").strip()
        logger.info("STT transcription (%s): %r", language, text)
        return text
    except sr.UnknownValueError:
        return ""
    except sr.RequestError as exc:
        logger.error("STT service request failed: %s", exc)
        return ""
    except Exception as exc:
        logger.error("STT transcription error for %s: %s", wav_path, exc)
        return ""

