"""Provisional language-state helpers for the Telegram Bot API worker."""

from __future__ import annotations

from language_detection import detect_supported_language


VALID_LANGUAGES = frozenset({"es", "en", "fr"})


def apply_language_evidence(
    state: dict,
    *,
    detected_language: str | None,
    provisional_language: str = "es",
) -> str:
    """Apply text evidence without replacing an already confirmed language."""

    detected = detected_language if detected_language in VALID_LANGUAGES else None
    provisional = provisional_language if provisional_language in VALID_LANGUAGES else "es"
    current = state.get("lang")
    is_provisional = state.get("language_provisional") is True
    if detected and (current not in VALID_LANGUAGES or is_provisional):
        state["lang"] = detected
        state["language_provisional"] = False
    elif current not in VALID_LANGUAGES:
        state["lang"] = provisional
        state["language_provisional"] = True
    elif "language_provisional" not in state:
        # Legacy state with a language predates provisional hints and was
        # selected from customer text, so keep treating it as confirmed.
        state["language_provisional"] = False
    return state["lang"]


def detected_language_from_message(message: object) -> str | None:
    """Read textual evidence from either normal text or a media caption."""

    text = (
        getattr(message, "text", None)
        or getattr(message, "caption", None)
        or ""
    )
    return detect_supported_language(text.strip()) if isinstance(text, str) else None


def apply_message_language_evidence(state: dict, message: object) -> str:
    """Apply Telegram text/caption evidence, otherwise keep Spanish provisional."""

    return apply_language_evidence(
        state,
        detected_language=detected_language_from_message(message),
    )
