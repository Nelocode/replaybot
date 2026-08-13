"""Conservative detection for the three languages supported by the bots."""

from __future__ import annotations

import re


LANGUAGES = ("es", "en", "fr")
AMBIGUOUS = {"ok", "no", "si", "hey"}
LANG_KEYWORDS = {
    "es": re.compile(
        r"\b(hola|gracias|por\s*favor|buenos\s*días|quiero|necesito|ayuda|habla|"
        r"precio|precios|tarifa|tarifas|reserva|reservas|foto|fotos|vídeo|vídeos|video|videos|"
        r"buenas|amigo|claro|vale|dale|listo|entiendo|puedes|hacer|"
        r"dónde|cuándo|cómo|cuál|quién|eso|esto|algo|nada|todo|más|menos|"
        r"está|estoy|estamos|están|tengo|tiene|tenemos|soy|eres|somos|son)\b",
        re.IGNORECASE,
    ),
    "en": re.compile(
        r"\b(hello|hi|thanks|thank\s*you|please|help|want|need|can\s*i|"
        r"price|prices|rate|rates|book|booking|photo|photos|video|videos|"
        r"yes|sure|fine|good|great|hey|would|could|should|"
        r"where|when|how|what|who|that|this|there|here|"
        r"is|are|am|have|has|do|does|did|will|may|might)\b",
        re.IGNORECASE,
    ),
    "fr": re.compile(
        r"\b(bonjour|merci|s'il\s*vous\s*plaît|aide|besoin|vouloir|"
        r"prix|tarif|tarifs|réservation|réserver|photo|photos|vidéo|vidéos|"
        r"oui|d'accord|bien|tres|peux|peut|où|quand|comment|quoi|qui|que|"
        r"est|suis|sommes|êtes|sont|ai|as|a|avons|avez|ont|"
        r"je|tu|il|elle|nous|vous|ils|elles|"
        r"ce|cet|cette|ces|mon|ton|son|ma|ta|sa)\b",
        re.IGNORECASE,
    ),
}
LANG_MARKERS = {
    "es": re.compile(r"\b(español|castellano|hablo español|hablo espanol)\b", re.IGNORECASE),
    "en": re.compile(r"\b(english|speak english)\b", re.IGNORECASE),
    "fr": re.compile(r"\b(français|francais|parle français|parle francais)\b", re.IGNORECASE),
}


def detect_supported_language(text: str) -> str | None:
    """Return evidence only when exactly one language has the top score."""

    scores = {language: 0 for language in LANGUAGES}
    for language, pattern in LANG_KEYWORDS.items():
        for match in pattern.findall(text or ""):
            if match.lower() not in AMBIGUOUS:
                scores[language] += 1
    for language, marker in LANG_MARKERS.items():
        if marker.search(text or ""):
            scores[language] += 20

    top_score = max(scores.values(), default=0)
    if top_score < 1:
        return None
    winners = [language for language, score in scores.items() if score == top_score]
    return winners[0] if len(winners) == 1 else None
