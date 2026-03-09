"""
Offline Speech-to-Text using OpenAI Whisper (local model, no API key).

The browser already sends a 16 kHz mono PCM WAV so we feed it straight
to Whisper without any resampling.

Model is downloaded automatically on first load to  ~/.cache/whisper/
(~145 MB for 'base') and reused on every subsequent run.

Model size guide
----------------
  tiny   ~75 MB   – fastest, lower accuracy
  base   ~145 MB  – recommended: good accuracy ~3-6 s on CPU
  small  ~465 MB  – better accuracy, ~8-15 s on CPU
  medium ~1.5 GB  – near-perfect, needs GPU for real-time use

Set WHISPER_MODEL=base in your .env file (default: base).
"""
import logging
import numpy as np
import soundfile as sf
import whisper

from config import Config

logger = logging.getLogger(__name__)

# ── Load model once (module-level singleton) ──────────────────────────────────
_model_name: str = Config.WHISPER_MODEL
_model = None

try:
    logger.info("Loading Whisper '%s' model …", _model_name)
    _model = whisper.load_model(_model_name)
    logger.info("Whisper '%s' model loaded successfully.", _model_name)
except Exception as exc:
    _model = None
    logger.error("Failed to load Whisper model '%s': %s", _model_name, exc)


# ── Prompt — guides Whisper toward command vocabulary ───────────────────────────
# IMPORTANT: Keep this short and keyword-only.
# Long descriptive sentences (especially with "at", "dot", "com") are hallucinated
# verbatim by Whisper when the mic audio is unclear or near-silent.
# Domain names (gmail, hotmail, etc.) and example addresses must NOT appear
# in the prompt.  Whisper treats the prompt as a prefix and will hallucinate
# the very next word it sees after a contact name, e.g.
#   user says "Pooja" → Whisper outputs "Pooja gmail hotmail"
# because those words immediately follow "Pooja" in the prompt string.
_PROMPT = (
    "read email send email next previous list summarize "
    "send message read messages yes no confirm cancel stop logout help"
)

# ── Substrings that indicate Whisper is echoing its own prompt (hallucination) ──
# If the transcription contains any of these, it is prompt-echo and must be
# discarded (return "") so the intent engine sees silence instead of gibberish.
_HALLUCINATION_SUBSTRINGS: list[str] = [
    "email addresses like",
    "user at gmail",
    "user123 at",
    "gmail dot com",
    "hotmail dot com",
    "voice email and telegram",
    "voice assistant for email",
    "email compose steps",
    "confirmation words",
    "contact names",
    "navigation:",
    "commands:",
    "summarize email, read messages",   # prompt sentence fragment
    "read email, send email, next",     # prompt sentence fragment
    # Prompt-echo patterns caused by having domain names in the old prompt
    "gmail hotmail",
    "hotmail gmail",
    "pooja gmail",
    "pooja hotmail",
    "amit gmail",
    "rahul gmail",
    "neha gmail",
    "priya gmail",
    "rutik gmail",
    "vaibhav gmail",
]


def _is_hallucination(text: str) -> bool:
    """
    Return True if `text` looks like Whisper echoing its own initial_prompt
    rather than transcribing real speech.
    """
    lower = text.lower()
    return any(sub in lower for sub in _HALLUCINATION_SUBSTRINGS)


def _trim_silence(
    audio: np.ndarray,
    sr: int,
    top_db: float = 25.0,
    frame_ms: int = 25,
    hop_ms: int = 10,
) -> np.ndarray:
    """
    Strip leading and trailing silence from `audio` using a simple
    RMS energy threshold (no librosa dependency required).

    top_db  — frames whose energy is more than `top_db` dB below the peak
               frame are treated as silence.  25 dB is conservative — only
               removes near-total silence while preserving soft word onsets.
               (40 dB was too aggressive and chopped off word beginnings.)
    """
    if len(audio) == 0:
        return audio

    frame_len = int(sr * frame_ms / 1000)
    hop_len   = int(sr * hop_ms  / 1000)

    # Compute per-frame RMS energy
    frames = [
        audio[i : i + frame_len]
        for i in range(0, max(1, len(audio) - frame_len + 1), hop_len)
    ]
    rms = np.array([np.sqrt(np.mean(f ** 2)) for f in frames], dtype=np.float32)

    if rms.max() == 0:
        return audio  # all silence — return as-is so caller can handle it

    threshold = rms.max() * (10 ** (-top_db / 20.0))
    voiced    = np.where(rms > threshold)[0]

    if len(voiced) == 0:
        return audio[:100]  # return tiny stub so caller detects silence

    start = max(0,          voiced[0]  * hop_len - frame_len)
    end   = min(len(audio), voiced[-1] * hop_len + frame_len * 2)
    return audio[start:end]


def transcribe(wav_path: str, language: str = "en") -> str:
    """
    Transcribe a 16 kHz mono WAV file and return the recognised text.
    `language` is the BCP-47 code (e.g. "en", "hi", "mr") passed to Whisper
    so it focuses on the right language and alphabet.
    Returns an empty string on failure.
    """
    if _model is None:
        logger.error("Whisper model not loaded — cannot transcribe")
        return ""

    try:
        # Load audio — soundfile handles .wav without requiring ffmpeg
        audio, sr = sf.read(wav_path, dtype="float32")

        # Mix stereo → mono if needed
        if audio.ndim > 1:
            audio = audio.mean(axis=1)

        # Browser sends 16 kHz already.  Guard for edge cases with other rates.
        if sr != 16000:
            target_len = int(len(audio) * 16000 / sr)
            audio = np.interp(
                np.linspace(0, len(audio) - 1, target_len),
                np.arange(len(audio)),
                audio,
            ).astype(np.float32)

        # ── Silence trim: strip leading/trailing quiet (< 1 % of peak RMS) ──
        # This prevents Whisper from wasting time on empty frames and dramatically
        # reduces transcription time when the user pauses before/after speaking.
        audio = _trim_silence(audio, sr)

        # Return immediately if the entire clip is silence
        if len(audio) < 1600:   # < 0.1 s of speech — nothing useful
            logger.info("Audio is silence — skipping transcription.")
            return ""

        result = _model.transcribe(
            audio,
            language=language,
            fp16=False,                          # False for CPU; True speeds up GPU
            initial_prompt=_PROMPT,
            temperature=(0.0, 0.2, 0.4, 0.6),   # fallback temperatures if confidence low
            beam_size=5,                         # beam search — significantly better accuracy
            best_of=5,                           # pick best of 5 candidates at higher temps
            condition_on_previous_text=False,
            no_speech_threshold=0.4,             # was 0.6 — less likely to discard valid speech
            compression_ratio_threshold=2.4,     # skip repetitive / garbage output
            logprob_threshold=-1.0,              # retry with higher temp if avg log-prob too low
        )

        text = result.get("text", "").strip()

        # ── Hallucination guard: reject prompt-echo ────────────────────────────
        # Whisper sometimes copies its initial_prompt into the output when audio
        # is unclear.  Detect and discard these to prevent false intent matches.
        if _is_hallucination(text):
            logger.warning("Whisper hallucination detected — discarding: %r", text)
            return ""

        logger.info("Whisper transcription: %r", text)
        return text

    except Exception as exc:
        logger.error("Whisper transcription error for %s: %s", wav_path, exc)
        return ""

