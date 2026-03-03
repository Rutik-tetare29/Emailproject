"""
Summarizer — Smart Text Summarization (Feature 2).

Two modes
---------
1. Simple (always available, no extra dependencies):
   Extracts the first 1-2 sentences as the summary.

2. HuggingFace transformer (optional, requires `transformers` + `torch`):
   Uses the 'facebook/bart-large-cnn' model for abstractive summarization.
   Falls back to simple mode automatically if the library is not installed.

Usage
-----
    from services.summarizer import summarize_text

    summary = summarize_text("Long email body here...")
    summary = summarize_text(text, mode="simple")
    summary = summarize_text(text, mode="transformers")

TTS-ready
---------
The returned string is a plain sentence — no markdown/HTML — so it can be
passed directly to `speak_to_file()` or any other TTS function.
"""

import re
import logging
from collections import Counter

logger = logging.getLogger(__name__)

# Maximum number of characters to feed into the transformer model.
# Bart's token limit is 1 024; ~1 500 chars ≈ 300 tokens — safe headroom.
_MAX_TRANSFORMER_CHARS = 1500

# Common English stop-words excluded from keyword scoring
_STOP_WORDS = {
    "a","an","the","and","or","but","in","on","at","to","for","of","with",
    "by","from","as","is","was","are","were","be","been","being","it","its",
    "this","that","these","those","i","we","you","he","she","they","my","your",
    "our","his","her","their","me","him","us","them","so","if","do","not",
    "have","has","had","will","would","could","should","may","might","can",
    "just","also","about","up","out","into","than","then","when","what",
    "which","who","how","all","any","no","more","very","hi","hello","dear",
    "regards","thanks","thank","sincerely","best","cheers","attached",
    "please","let","know","get","got","make","see","look","here","there",
    "good","well","like","want","need","hope","feel","think","use","used",
}


def _clean_body(text: str) -> str:
    """Strip HTML, links, quoted text, signatures and extra whitespace."""
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Remove URLs
    text = re.sub(r"https?://\S+", " ", text)
    # Remove email-style quoted lines (lines starting with >)
    text = re.sub(r"(?m)^>.*$", "", text)
    # Remove common email signature markers
    text = re.sub(
        r"(?i)(-{2,}\s*(original message|forwarded message|from:|sent:|to:|cc:|subject:).*)",
        "", text, flags=re.DOTALL
    )
    # Remove lines that look like footers / disclaimers (very short lines with
    # common signature words)
    text = re.sub(
        r"(?im)^(--|best regards?|kind regards?|regards,?|sincerely,?|thanks?,?|warm regards?)[^\n]*$",
        "", text
    )
    # Collapse whitespace
    text = re.sub(r"[\r\n]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extractive_oneline(text: str) -> str:
    """
    Return a single sentence that best captures the main purpose of the email.

    Algorithm
    ---------
    1. Clean and strip boilerplate (HTML, signatures, quoted text).
    2. Split into sentences.
    3. Build a TF-scored word-frequency table (stop-words excluded).
    4. Score each sentence by mean normalised word frequency, then apply:
       - Greeting pattern penalty: "Hi", "Hello", "Dear", "Hope you are" etc.
         are reduced to near-zero so they never win.
       - Position bonus: sentences 1-5 (skipping the opening greeting) in the
         first half of the email get +30% (they typically state the purpose).
       - Last-quarter penalty: closing pleasantries penalised by -30%.
       - Action-word boost: sentences containing keywords like "please",
         "deadline", "payment", "request", "meeting" etc. get +25%.
       - Length penalty: very short (<25 chars) or very long (>250 chars).
    5. Return the highest-scoring sentence, capped at 220 chars for TTS.
    """
    clean = _clean_body(text)
    if not clean:
        return "No readable content found."

    # Split into sentences
    sentences = re.split(r"(?<=[.!?])\s+", clean)
    sentences = [s.strip() for s in sentences if len(s.strip()) >= 12]

    if not sentences:
        return clean[:200] + ("…" if len(clean) > 200 else "")

    if len(sentences) == 1:
        s = sentences[0]
        return (s[:220] + "…" if len(s) > 220 else s)

    # ── Greeting prefix stripper ──────────────────────────────────────────────
    # Strips ONLY the salutation opener (e.g. "Dear Rutik, " / "Hi, ")
    # from a sentence so the substantive remainder can be scored fairly.
    _OPENER_RE = re.compile(
        r"^(?:hi\s*[,!]?\s*|hello\s*[,!]?\s*|hey\s*[,!]?\s*"
        r"|dear\s+\S+[\s,!]*|greetings\s*[,!]?\s*"
        r"|good\s+(?:morning|afternoon|evening)\s*[,!]?\s*)",
        re.IGNORECASE,
    )

    # ── Whole-sentence pleasantry detector ───────────────────────────────────
    # Matches sentences that are ENTIRELY a pleasantry with no real content.
    _PLEASANTRY_RE = re.compile(
        r"^(i\s+hope\s+(you|this)|hope\s+you\s+are|how\s+are\s+you"
        r"|trust\s+this\s+finds|looking\s+forward|thank\s+you\s+for\s+your"
        r"|please\s+(do\s+not\s+hesitate|feel\s+free)|let\s+me\s+know\s+if)",
        re.IGNORECASE,
    )

    # ── High-signal action / purpose words ───────────────────────────────────
    _ACTION_WORDS = {
        "please","request","require","inform","deadline","due","submit",
        "deliver","attend","meeting","interview","schedule","payment",
        "invoice","confirm","urgent","important","action","follow","update",
        "complete","review","approve","attached","regarding","enquiry",
        "inquiry","offer","joining","report","issue","problem","concern",
        "respond","reply","remind","notice","invitation","congratulations",
        "selected","rejected","approved","postponed","cancelled","reschedule",
        "alert","assignment","task","project","proposal","quotation","need",
    }

    # Word frequency (TF-style) across ALL sentences
    all_words = re.findall(r"[a-zA-Z]{3,}", clean.lower())
    content_words = [w for w in all_words if w not in _STOP_WORDS]
    freq = Counter(content_words)
    max_freq = max(freq.values()) if freq else 1
    norm_freq = {w: freq[w] / max_freq for w in freq}

    n = len(sentences)
    best_score = -1.0
    best_sent  = sentences[0]

    for i, sent in enumerate(sentences):
        # Strip greeting opener for scoring (keep original for output)
        score_text = _OPENER_RE.sub("", sent).strip()
        if not score_text:
            score_text = sent

        lower_sent = score_text.lower()
        words = re.findall(r"[a-zA-Z]{3,}", lower_sent)
        if not words:
            continue

        # Whole-sentence pleasantry → near-zero score
        if _PLEASANTRY_RE.match(sent.strip()):
            score = 0.02
        else:
            # Base: mean normalised frequency of content words in score_text
            content_sc = [norm_freq.get(w, 0.0) for w in words if w not in _STOP_WORDS]
            score = (sum(content_sc) / len(content_sc)) if content_sc else 0.0

        # Position weighting
        had_opener = (score_text != sent)   # True if opener was stripped
        pos_ratio  = i / n
        if (0 < i <= 5 or (i == 0 and had_opener and len(score_text) >= 30)) \
                and pos_ratio < 0.50:
            score *= 1.30   # early purpose sentences (incl. opener-prefixed i=0)
        elif i == 0 and not had_opener:
            score *= 1.10   # clean first sentence (no greeting prefix)
        elif pos_ratio > 0.75:
            score *= 0.70   # closing remarks

        # Action-word boost
        if any(w in _ACTION_WORDS for w in words):
            score *= 1.25

        # Length heuristic (based on score_text length)
        length = len(score_text)
        if length < 25:
            score *= 0.50
        elif length > 250:
            score *= 0.80

        if score > best_score:
            best_score = score
            best_sent  = sent

    # Cap for TTS comfort; also strip any opener prefix for clean TTS delivery
    best_sent = _OPENER_RE.sub("", best_sent).strip()
    if not best_sent:
        best_sent = sentences[0]
    if len(best_sent) > 220:
        best_sent = best_sent[:220].rsplit(" ", 1)[0] + "…"
    return best_sent


# ── Simple summarizer (kept for backward-compat; used by non-email callers) ───

def _simple_summarize(text: str, max_sentences: int = 2) -> str:
    """Return the first `max_sentences` sentences (fast, no scoring)."""
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"\s+", " ", clean).strip()
    if not clean:
        return "No content to summarize."
    sentences = re.split(r"(?<=[.!?])\s+", clean)
    sentences = [s.strip() for s in sentences if len(s.strip()) >= 8]
    if not sentences:
        return clean[:160] + ("…" if len(clean) > 160 else "")
    summary = " ".join(sentences[:max_sentences])
    if len(summary) > 300:
        summary = summary[:300].rsplit(" ", 1)[0] + "…"
    return summary


# ── Transformer summarizer (optional) ────────────────────────────────────────

def _transformer_summarize(text: str) -> str:
    """
    Abstractive summarization using facebook/bart-large-cnn via HuggingFace.

    If the `transformers` package is NOT installed, this falls back to the
    simple summarizer automatically and logs a warning.

    The model is downloaded (~1.6 GB) on first use and cached in
    ~/.cache/huggingface/  — subsequent calls are instant.
    """
    try:
        # Lazy import so the app starts normally even without transformers
        from transformers import pipeline as hf_pipeline  # type: ignore
    except ImportError:
        logger.warning(
            "transformers library not installed. "
            "Run: pip install transformers torch  "
            "Falling back to simple summarizer."
        )
        return _simple_summarize(text)

    try:
        # Use a lightweight model for speed; swap to 'facebook/bart-large-cnn'
        # for higher quality at the cost of download size.
        summarizer = hf_pipeline(
            "summarization",
            model="sshleifer/distilbart-cnn-12-6",  # ~300 MB — faster to download
            tokenizer="sshleifer/distilbart-cnn-12-6",
        )
        # Truncate to avoid token-limit errors
        input_text = text[:_MAX_TRANSFORMER_CHARS]
        result = summarizer(
            input_text,
            max_length=80,
            min_length=20,
            do_sample=False,
        )
        return result[0]["summary_text"].strip()
    except Exception as e:
        logger.error("Transformer summarization failed: %s. Using simple mode.", e)
        return _simple_summarize(text)


# ── Public API ────────────────────────────────────────────────────────────────

def summarize_text(text: str, mode: str = "simple", max_sentences: int = 2) -> str:
    """
    Summarize the given text.

    Parameters
    ----------
    text          : str   The text to summarize (email body, message, etc.)
    mode          : str   "extractive" — single best sentence (default for emails)
                          "simple"     — first N sentences (fast)
                          "transformers" — HuggingFace abstractive model
    max_sentences : int   Used only in "simple" mode. Default: 2.

    Returns
    -------
    str  A short, TTS-ready single-line summary.
    """
    if not text or not text.strip():
        return "No content to summarize."

    mode = mode.lower().strip()

    if mode == "transformers":
        return _transformer_summarize(text)
    elif mode == "extractive":
        return _extractive_oneline(text)
    else:
        return _simple_summarize(text, max_sentences=max_sentences)


def summarize_email(email_dict: dict, mode: str = "simple") -> str:
    """
    Convenience wrapper for email dicts returned by email_service.

    Combines subject + body for a richer summary.
    """
    subject = email_dict.get("subject", "")
    body    = email_dict.get("body", email_dict.get("snippet", ""))
    combined = f"{subject}. {body}".strip()
    summary  = summarize_text(combined, mode=mode)
    return f"Email from {email_dict.get('sender', 'unknown')}: {summary}"


def summarize_message(msg_dict: dict, mode: str = "simple") -> str:
    """
    Convenience wrapper for message dicts from messaging_service.
    """
    sender  = msg_dict.get("sender", "unknown")
    text    = msg_dict.get("text", "")
    summary = summarize_text(text, mode=mode, max_sentences=1)
    return f"Message from {sender}: {summary}"
