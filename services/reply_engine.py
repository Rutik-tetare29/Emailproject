"""
Reply Engine — AI-Based Suggested Replies (Feature 3).

Phase 1: Rule-based keyword matching (works offline, no dependencies).
Phase 2 (future): Plug in an LLM (e.g. OpenAI GPT or a local Ollama model)
          by replacing / extending `_ai_suggest()`.

How it works
------------
1. `suggest_reply(message)` checks the incoming text against a keyword table.
2. Each matched keyword maps to a category.
3. The category is mapped to one or more polished reply templates.
4. Returns a list of 1-3 suggestion strings.

Voice confirmation flow (handled in app.py + voice_service.py):
   suggest_reply(msg)  →  read back suggestions via TTS
   → ask "Do you want to send reply 1?"
   → user says YES → send;  NO → cancel.
"""

import re
import logging
import random

logger = logging.getLogger(__name__)

# ── Keyword → category mapping ────────────────────────────────────────────────
# Each tuple is (regex_pattern, category_name).
# Patterns are checked in order — first match wins.
# Add more rows to extend the rule-base without touching anything else.

_RULES: list[tuple[str, str]] = [
    # Meetings / scheduling
    (r"\b(meeting|schedule|reschedule|call|zoom|teams|conference|sync)\b", "meeting"),
    # Gratitude
    (r"\b(thanks|thank you|thankyou|appreciate|grateful|gratitude)\b", "thanks"),
    # Urgency
    (r"\b(urgent|asap|immediately|emergency|critical|priority|important)\b", "urgent"),
    # Greetings
    (r"\b(hello|hi|hey|good morning|good afternoon|good evening|howdy)\b", "greeting"),
    # Apology
    (r"\b(sorry|apologies|apologize|my mistake|my bad|regret)\b", "apology"),
    # Questions / help
    (r"\b(can you|could you|would you|please help|need help|question|query|asking)\b", "question"),
    # Confirmation / approval
    (r"\b(confirm|approved|accept|agree|sounds good|looks good|perfect)\b", "confirmation"),
    # Rejection / denial
    (r"\b(reject|decline|deny|not available|cannot|can't|won't|unavailable)\b", "rejection"),
    # Deadline / due date
    (r"\b(deadline|due|by friday|by monday|end of day|eod|eow|by tomorrow)\b", "deadline"),
    # Follow-up
    (r"\b(follow up|following up|checking in|any update|update me|status)\b", "followup"),
    # Introduction
    (r"\b(introduce|introduction|meet|new to|joining|on-boarding|onboarding)\b", "introduction"),
]

# ── Reply templates per category ─────────────────────────────────────────────
# Multiple options per category so the engine can vary responses.

_TEMPLATES: dict[str, list[str]] = {
    "meeting": [
        "Sure, the meeting time works for me. I'll be there!",
        "I can join the meeting. Please share the invite link.",
        "Happy to schedule the meeting. What time works best for you?",
    ],
    "thanks": [
        "You're most welcome! Happy to help anytime.",
        "Glad I could assist. Feel free to reach out anytime!",
        "My pleasure! Let me know if you need anything else.",
    ],
    "urgent": [
        "Got it! I'll take care of this right away.",
        "Understood. I'm on it and will respond as soon as possible.",
        "This is urgent — I'll prioritize it immediately.",
    ],
    "greeting": [
        "Hello! Hope you're having a great day.",
        "Hi there! How can I help you today?",
        "Good to hear from you! What's on your mind?",
    ],
    "apology": [
        "No worries at all! These things happen.",
        "It's completely fine. No need to apologize.",
        "I understand. Let's move forward together.",
    ],
    "question": [
        "Sure, I'd be happy to help! Could you give me a bit more detail?",
        "Great question! Let me look into this and get back to you shortly.",
        "Of course! I'll do my best to assist you.",
    ],
    "confirmation": [
        "Confirmed! Everything looks good on my end.",
        "Great, I confirm the details. We're all set!",
        "Perfect, consider this confirmed. Thank you for the update.",
    ],
    "rejection": [
        "I understand. No worries, perhaps we can revisit this later.",
        "That's okay. Let me know if there's anything else I can help with.",
        "Noted. We can explore other options when you're ready.",
    ],
    "deadline": [
        "Understood. I'll make sure everything is ready before the deadline.",
        "Got it! I'll prioritize this to meet the due date.",
        "I'll wrap this up on time. You can count on me.",
    ],
    "followup": [
        "Thanks for following up! I'll get back to you with an update shortly.",
        "Appreciate the check-in. I'm making progress and will update you soon.",
        "Sorry for the delay. Here's a quick status update…",
    ],
    "introduction": [
        "Nice to meet you! Looking forward to working together.",
        "Welcome! Feel free to reach out if you have any questions.",
        "Great to connect! I'm happy to help you settle in.",
    ],
    "default": [
        "Thank you for your message. I'll get back to you soon.",
        "Got your message! I'll respond as quickly as possible.",
        "Thanks for reaching out. I'll look into this and reply shortly.",
    ],
}


# ── Core logic ────────────────────────────────────────────────────────────────

def _detect_category(text: str) -> str:
    """
    Return the best-matching category for the input text, or "default".
    Matching is case-insensitive.
    """
    lower_text = text.lower()
    for pattern, category in _RULES:
        if re.search(pattern, lower_text):
            return category
    return "default"


def suggest_reply(message: str, num_suggestions: int = 3) -> dict:
    """
    Generate context-aware reply suggestions for the given message.

    Parameters
    ----------
    message         : str   The original message to reply to.
    num_suggestions : int   How many different options to return (1-3).

    Returns
    -------
    dict:
        {
            "category"    : str,          # detected intent category
            "suggestions" : list[str],    # reply options (TTS-ready)
            "primary"     : str,          # best single suggestion
        }

    Examples
    --------
    >>> result = suggest_reply("Can we have a meeting tomorrow at 3 pm?")
    >>> result["primary"]
    "Sure, the meeting time works for me. I'll be there!"
    """
    if not message or not message.strip():
        return {
            "category": "default",
            "suggestions": _TEMPLATES["default"][:num_suggestions],
            "primary": _TEMPLATES["default"][0],
        }

    category   = _detect_category(message)
    templates  = _TEMPLATES.get(category, _TEMPLATES["default"])

    # Shuffle for variety on repeated calls, but keep deterministic for tests
    suggestions = templates[:num_suggestions]

    return {
        "category"   : category,
        "suggestions": suggestions,
        "primary"    : suggestions[0],
    }


def get_all_categories() -> list[str]:
    """Return all supported intent category names (useful for the UI)."""
    return list(_TEMPLATES.keys())


# ── Future: AI / LLM integration point ───────────────────────────────────────

def _ai_suggest(message: str) -> str:
    """
    Placeholder for LLM-based reply generation.

    To activate:
      1. pip install openai
      2. Set OPENAI_API_KEY in your .env
      3. Replace the body below with real API call.

    Example (OpenAI):
        import openai
        client = openai.OpenAI()
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You generate short email replies."},
                {"role": "user",   "content": f"Reply to: {message}"},
            ],
            max_tokens=100,
        )
        return resp.choices[0].message.content.strip()
    """
    raise NotImplementedError(
        "AI suggest not yet configured. "
        "Add OPENAI_API_KEY to .env and implement _ai_suggest()."
    )
