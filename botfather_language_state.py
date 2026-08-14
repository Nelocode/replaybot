"""Language evidence and adaptation for the Telegram Bot API worker."""

from __future__ import annotations

from collections.abc import Mapping

from language_adaptation import reduce_language_state
from language_detection import detect_language_evidence, detect_supported_language


def apply_language_evidence(
    state: dict,
    *,
    detected_language: str | None,
    language_evidence: Mapping[str, object] | None = None,
    provisional_language: str = "es",
) -> str:
    """Apply the same strong/weak transition used by the Telegram user bot."""

    reduced = reduce_language_state(
        {
            "language": state.get("lang"),
            "language_provisional": state.get("language_provisional"),
            "language_source": state.get("language_source"),
            "language_candidate": state.get("language_candidate"),
            "language_candidate_streak": state.get("language_candidate_streak"),
        },
        detected_language=detected_language,
        language_evidence=language_evidence,
        provisional_language=provisional_language,
    )
    state["lang"] = reduced["language"] or "es"
    for key in (
        "language_provisional",
        "language_source",
        "language_candidate",
        "language_candidate_streak",
    ):
        state[key] = reduced[key]
    return state["lang"]


def _message_text(message: object) -> str:
    text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
    return text.strip() if isinstance(text, str) else ""


def language_evidence_from_message(message: object) -> dict[str, object] | None:
    """Return a full observation for text/captions, and None for non-text."""

    text = _message_text(message)
    return detect_language_evidence(text) if text else None


def detected_language_from_message(message: object) -> str | None:
    """Backward-compatible wrapper returning only the detected language."""

    evidence = language_evidence_from_message(message)
    return evidence["language"] if evidence else None  # type: ignore[return-value]


def apply_message_language_evidence(state: dict, message: object) -> str:
    """Apply Telegram text/caption evidence, otherwise keep Spanish provisional."""

    evidence = language_evidence_from_message(message)
    return apply_language_evidence(
        state,
        detected_language=evidence["language"] if evidence else None,  # type: ignore[arg-type]
        language_evidence=evidence,
    )
