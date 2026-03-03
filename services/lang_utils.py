"""
Multi-language utilities for VoiceMail.

Provides two public helpers used throughout the voice pipeline:

  translate_text(text, target_lang)
      Translate an English response string into the target language.
      Uses deep-translator → Google Translate (needs internet).
      Falls back to the original English text on any error.

  speak_multilang(text, lang)
      Generate a speech audio file for the given text and language.
      • English  → pyttsx3 offline WAV  (no internet required)
      • Hindi/Marathi/other → gTTS MP3 via Google (needs internet)
      Falls back to English pyttsx3 if gTTS fails.
      Returns the absolute file path (WAV or MP3), or "" on failure.

Supported session language codes
----------------------------------
  "en"  English  (default, offline)
  "hi"  Hindi    (gTTS + Google Translate)
  "mr"  Marathi  (gTTS + Google Translate)
"""

import os
import uuid
import logging

from config import Config

logger = logging.getLogger(__name__)

# ── Human-readable display names ──────────────────────────────────────────────
LANG_NAMES: dict[str, str] = {
    "en": "English",
    "hi": "Hindi",
    "mr": "Marathi",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
}

# ── BCP-47 codes for Web Speech API (used in voice.js) ────────────────────────
LANG_BCP47: dict[str, str] = {
    "en": "en-US",
    "hi": "hi-IN",
    "mr": "mr-IN",
    "es": "es-ES",
    "fr": "fr-FR",
    "de": "de-DE",
    "it": "it-IT",
    "pt": "pt-PT",
}

# ── TTS switch confirmation phrases — already in the target language ──────────
# These are spoken WHEN the user asks to switch language, so they must
# NOT pass through translate_text (they're already in the right language).
SWITCH_PHRASES: dict[str, str] = {
    "en": "Language switched to English. I will now speak in English.",
    "hi": "भाषा हिंदी में बदल दी गई है। अब मैं हिंदी में बोलूँगा।",
    "mr": "भाषा मराठीत बदलली आहे. आता मी मराठीत बोलेन.",
    "es": "Idioma cambiado a español. Ahora hablaré en español.",
    "fr": "Langue changée en français. Je parlerai maintenant en français.",
    "de": "Sprache auf Deutsch umgestellt. Ich werde jetzt auf Deutsch sprechen.",
    "it": "Lingua cambiata in italiano. Ora parlerò in italiano.",
    "pt": "Idioma alterado para português. Agora falarei em português.",
}

# ── Native-language command keywords ──────────────────────────────────────────
# Maps each LANG_CODE → { INTENT → [native spoken variants] }
# Used as a direct-match shortcut in _detect_intent BEFORE (or when) the
# translate_to_english call is unavailable (e.g., offline).
# Intents must match the keys used in _INTENTS in voice_processor.py.
NATIVE_COMMANDS: dict[str, dict[str, list[str]]] = {
    # ── Hindi ──────────────────────────────────────────────────────────────
    "hi": {
        "read_email":    ["ईमेल पढ़ो", "ईमेल पढ़ें", "इनबॉक्स खोलो", "मेरे ईमेल"],
        "list_emails":   ["ईमेल दिखाओ", "ईमेल सूची", "सभी ईमेल"],
        "next_email":    ["अगला", "अगला ईमेल", "आगे"],
        "prev_email":    ["पिछला", "पिछला ईमेल", "वापस"],
        "read_more":     ["और पढ़ो", "आगे पढ़ो", "जारी रखो"],
        "send_email":    ["ईमेल भेजो", "ईमेल लिखो", "नया ईमेल"],
        "send_message":  ["संदेश भेजो", "मैसेज भेजो", "टेलीग्राम"],
        "read_messages": ["संदेश पढ़ो", "मैसेज पढ़ो", "नए संदेश"],
        "logout":        ["लॉगआउट", "बाहर निकलो", "अलविदा"],
        "help":          ["मदद", "सहायता", "क्या करूं"],
        "stop_reading":  ["रुको", "बंद करो", "चुप हो जाओ"],
        "summarize_email": ["सारांश", "संक्षेप", "ईमेल का सारांश"],
        # confirm / cancel (used in compose flows)
        "_confirm":      ["हाँ", "हां", "ठीक है", "भेजो", "हाँ भेजो", "जी हाँ", "बिल्कुल",
                          "ha", "haa", "haan", "han", "haa.", "ha."],
        "_cancel":       ["रद्द करो", "नहीं", "बंद करो", "मत भेजो", "छोड़ो"],
    },
    # ── Marathi ────────────────────────────────────────────────────────────
    "mr": {
        "read_email":    ["ईमेल वाचा", "ईमेल उघडा", "माझे ईमेल"],
        "list_emails":   ["ईमेल दाखवा", "ईमेल यादी", "सर्व ईमेल"],
        "next_email":    ["पुढील", "पुढील ईमेल", "पुढे जा"],
        "prev_email":    ["मागील", "मागील ईमेल", "मागे जा"],
        "read_more":     ["अजून वाचा", "पुढे वाचा", "सुरू ठेवा"],
        "send_email":    ["ईमेल पाठवा", "ईमेल लिहा", "नवीन ईमेल"],
        "send_message":  ["संदेश पाठवा", "मेसेज पाठवा", "टेलीग्राम"],
        "read_messages": ["संदेश वाचा", "मेसेज वाचा", "नवीन संदेश"],
        "logout":        ["लॉगआउट", "बाहेर पडा", "निरोप"],
        "help":          ["मदत", "सहाय्य", "काय करू"],
        "stop_reading":  ["थांबा", "बंद करा", "शांत रहा"],
        "summarize_email": ["सारांश", "थोडक्यात सांगा", "ईमेल सारांश"],
        "_confirm":      ["हो", "होय", "ठीक आहे", "पाठवा", "नक्कीच",
                          "ha", "haa", "ho", "hoy", "ho.", "hoy.", "ha."],
        "_cancel":       ["रद्द करा", "नाही", "बंद करा", "पाठवू नका"],
    },
    # ── Spanish ────────────────────────────────────────────────────────────
    "es": {
        "read_email":    ["leer correo", "leer email", "abrir bandeja"],
        "list_emails":   ["mostrar correos", "listar emails", "ver bandeja"],
        "next_email":    ["siguiente", "siguiente correo", "adelante"],
        "prev_email":    ["anterior", "correo anterior", "atrás"],
        "read_more":     ["leer más", "continuar", "seguir leyendo"],
        "send_email":    ["enviar correo", "escribir correo", "nuevo email"],
        "send_message":  ["enviar mensaje", "mensaje telegram", "mandar mensaje"],
        "read_messages": ["leer mensajes", "ver mensajes", "nuevos mensajes"],
        "logout":        ["cerrar sesión", "salir", "adiós"],
        "help":          ["ayuda", "socorro", "qué puedo decir"],
        "stop_reading":  ["parar", "para", "silencio", "suficiente"],
        "summarize_email": ["resumir", "resumen", "resumir correo"],
        "_confirm":      ["sí", "si", "ok", "confirmar", "enviar", "de acuerdo", "claro"],
        "_cancel":       ["cancelar", "no", "abortar", "no enviar"],
    },
    # ── French ─────────────────────────────────────────────────────────────
    "fr": {
        "read_email":    ["lire email", "lire courriel", "ouvrir boîte de réception"],
        "list_emails":   ["afficher emails", "liste des emails", "voir boîte"],
        "next_email":    ["suivant", "email suivant", "prochain"],
        "prev_email":    ["précédent", "email précédent", "retour"],
        "read_more":     ["lire plus", "continuer", "suite"],
        "send_email":    ["envoyer email", "écrire email", "nouveau message"],
        "send_message":  ["envoyer message", "message telegram", "envoyer"],
        "read_messages": ["lire messages", "voir messages", "nouveaux messages"],
        "logout":        ["déconnexion", "quitter", "au revoir"],
        "help":          ["aide", "secours", "que puis-je dire"],
        "stop_reading":  ["arrêter", "stop", "silence", "assez"],
        "summarize_email": ["résumer", "résumé", "résumer email"],
        "_confirm":      ["oui", "ok", "confirmer", "envoyer", "d'accord", "bien sûr"],
        "_cancel":       ["annuler", "non", "abandonner", "ne pas envoyer"],
    },
    # ── German ─────────────────────────────────────────────────────────────
    "de": {
        "read_email":    ["email lesen", "e-mail lesen", "posteingang öffnen"],
        "list_emails":   ["emails anzeigen", "emails auflisten", "postfach anzeigen"],
        "next_email":    ["nächste", "nächste email", "weiter"],
        "prev_email":    ["vorherige", "vorherige email", "zurück"],
        "read_more":     ["mehr lesen", "weiterlesen", "weiter"],
        "send_email":    ["email senden", "email schreiben", "neue email"],
        "send_message":  ["nachricht senden", "telegram nachricht", "senden"],
        "read_messages": ["nachrichten lesen", "nachrichten anzeigen", "neue nachrichten"],
        "logout":        ["abmelden", "ausloggen", "auf wiedersehen"],
        "help":          ["hilfe", "was kann ich sagen", "kommandos"],
        "stop_reading":  ["stopp", "anhalten", "ruhe", "genug"],
        "summarize_email": ["zusammenfassen", "zusammenfassung", "email zusammenfassen"],
        "_confirm":      ["ja", "ok", "bestätigen", "senden", "natürlich", "klar"],
        "_cancel":       ["abbrechen", "nein", "stornieren", "nicht senden"],
    },
    # ── Italian ────────────────────────────────────────────────────────────
    "it": {
        "read_email":    ["leggi email", "apri posta", "leggi posta"],
        "list_emails":   ["mostra email", "elenca email", "vedi casella"],
        "next_email":    ["prossima", "prossima email", "avanti"],
        "prev_email":    ["precedente", "email precedente", "indietro"],
        "read_more":     ["leggi di più", "continua", "continua a leggere"],
        "send_email":    ["invia email", "scrivi email", "nuova email"],
        "send_message":  ["invia messaggio", "messaggio telegram", "manda"],
        "read_messages": ["leggi messaggi", "vedi messaggi", "nuovi messaggi"],
        "logout":        ["esci", "disconnetti", "arrivederci"],
        "help":          ["aiuto", "cosa posso dire", "comandi"],
        "stop_reading":  ["fermati", "basta", "silenzio", "stop"],
        "summarize_email": ["riassumi", "riassunto", "riassumi email"],
        "_confirm":      ["sì", "si", "ok", "conferma", "invia", "certo", "d'accordo"],
        "_cancel":       ["annulla", "no", "cancella", "non inviare"],
    },
    # ── Portuguese ─────────────────────────────────────────────────────────
    "pt": {
        "read_email":    ["ler email", "abrir caixa de entrada", "ler correio"],
        "list_emails":   ["mostrar emails", "listar emails", "ver caixa"],
        "next_email":    ["próximo", "próximo email", "avançar"],
        "prev_email":    ["anterior", "email anterior", "voltar"],
        "read_more":     ["ler mais", "continuar", "continuar lendo"],
        "send_email":    ["enviar email", "escrever email", "novo email"],
        "send_message":  ["enviar mensagem", "mensagem telegram", "mandar"],
        "read_messages": ["ler mensagens", "ver mensagens", "novas mensagens"],
        "logout":        ["sair", "desconectar", "adeus"],
        "help":          ["ajuda", "socorro", "o que posso dizer"],
        "stop_reading":  ["parar", "pare", "silêncio", "chega"],
        "summarize_email": ["resumir", "resumo", "resumir email"],
        "_confirm":      ["sim", "ok", "confirmar", "enviar", "claro", "com certeza"],
        "_cancel":       ["cancelar", "não", "abortar", "não enviar"],
    },
}


# ── Text translation ───────────────────────────────────────────────────────────

def translate_text(text: str, target_lang: str) -> str:
    """
    Translate `text` (English) into `target_lang`.

    Returns the original `text` unchanged if:
    • target_lang is "en"
    • text is empty
    • deep-translator is not installed or the API call fails
    """
    if not text or not text.strip():
        return text
    if target_lang == "en":
        return text

    try:
        from deep_translator import GoogleTranslator  # type: ignore
        translated = GoogleTranslator(source="en", target=target_lang).translate(text)
        return translated or text
    except Exception as exc:
        logger.warning(
            "Translation to '%s' failed (%s) — using original English text.",
            target_lang, exc,
        )
        return text


def translate_to_english(text: str, src_lang: str = "auto") -> str:
    """
    Translate `text` from `src_lang` into English.

    Used to normalise non-English Whisper transcriptions so the
    English intent-keyword tables always work regardless of the
    active session language.

    Returns the original `text` unchanged if:
    • src_lang is already "en"
    • text is empty
    • deep-translator is not installed or the API call fails
    """
    if not text or not text.strip():
        return text
    if src_lang == "en":
        return text
    try:
        from deep_translator import GoogleTranslator  # type: ignore
        translated = GoogleTranslator(source=src_lang, target="en").translate(text)
        return translated or text
    except Exception as exc:
        logger.warning(
            "translate_to_english from '%s' failed (%s) — using original text.",
            src_lang, exc,
        )
        return text


# ── Multi-language TTS ─────────────────────────────────────────────────────────

def speak_multilang(text: str, lang: str = "en") -> str:
    """
    Convert `text` to speech in `lang` and save the audio to a file.

    Returns the absolute file path (WAV for English, MP3 for others),
    or an empty string if speech generation fails completely.
    """
    if not text or not text.strip():
        return ""

    # ── English: reuse the existing offline pyttsx3 engine ───────────────────
    if lang == "en":
        from services.tts_engine import speak_to_file
        return speak_to_file(text)

    # ── Hindi / Marathi / others: Google TTS (gTTS) → MP3 ────────────────────
    try:
        from gtts import gTTS  # type: ignore
        fname    = f"tts_{lang}_{uuid.uuid4().hex}.mp3"
        out_path = os.path.join(Config.UPLOAD_FOLDER, fname)
        tts      = gTTS(text=text, lang=lang, slow=False)
        tts.save(out_path)
        logger.info("gTTS (%s) saved: %s", lang, out_path)
        return out_path
    except Exception as exc:
        logger.warning(
            "gTTS for lang '%s' failed (%s) — falling back to English pyttsx3.",
            lang, exc,
        )
        from services.tts_engine import speak_to_file
        return speak_to_file(text)
