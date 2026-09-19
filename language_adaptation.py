"""Pure, privacy-safe language-state adaptation for Python runtimes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


VALID_LANGUAGES = frozenset({"es", "en", "fr"})
VALID_LANGUAGE_SOURCES = frozenset({"detected", "provisional", "operator_seed", "legacy"})
EVIDENCE_KEYS = frozenset({"language", "strong", "explicit", "score", "margin"})


def sanitize_language_state(state: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return only validated routing fields; never retain text or tokens."""

    raw = state if isinstance(state, Mapping) else {}
    language = raw.get("language")
    if language not in VALID_LANGUAGES:
        language = None
    provisional = bool(language and raw.get("language_provisional") is True)
    source = raw.get("language_source")
    if source not in VALID_LANGUAGE_SOURCES:
        source = "provisional" if provisional else ("legacy" if language else None)
    if provisional:
        source = "provisional"

    candidate = raw.get("language_candidate")
    streak = raw.get("language_candidate_streak")
    candidate_allowed = bool(
        language in VALID_LANGUAGES
        and candidate in VALID_LANGUAGES
        and isinstance(streak, int)
        and not isinstance(streak, bool)
        and streak == 1
        and (provisional or candidate != language)
    )
    if not candidate_allowed:
        candidate = None
        streak = 0
    return {
        "language": language,
        "language_provisional": provisional,
        "language_source": source,
        "language_candidate": candidate,
        "language_candidate_streak": streak,
    }


def _normalised_observation(
    detected_language: str | None,
    language_evidence: Mapping[str, object] | None,
) -> tuple[str | None, bool, bool]:
    if isinstance(language_evidence, Mapping):
        if set(language_evidence) != EVIDENCE_KEYS:
            return None, False, True
        language = language_evidence.get("language")
        strong = language_evidence.get("strong")
        explicit = language_evidence.get("explicit")
        score = language_evidence.get("score")
        margin = language_evidence.get("margin")
        valid = bool(
            language in VALID_LANGUAGES | {None}
            and isinstance(strong, bool)
            and isinstance(explicit, bool)
            and isinstance(score, int)
            and not isinstance(score, bool)
            and score >= 0
            and isinstance(margin, int)
            and not isinstance(margin, bool)
            and margin >= 0
            and not (language is None and (strong or explicit))
            and not (explicit and not strong)
        )
        if not valid:
            return None, False, True
        return language, bool(language and strong), True
    language = detected_language if detected_language in VALID_LANGUAGES else None
    # Legacy language-only observations remain compatible but deliberately
    # weak, so an old call site cannot immediately replace a confirmed language.
    return language, False, bool(language)


def reduce_language_state(
    state: Mapping[str, Any] | None,
    *,
    detected_language: str | None = None,
    language_evidence: Mapping[str, object] | None = None,
    provisional_language: str | None = None,
) -> dict[str, Any]:
    """Reduce one non-duplicate observation without retaining content.

    Strong evidence changes immediately. Weak evidence can replace a provisional
    language immediately, while remaining provisional until confirmed. Replacing
    a confirmed language requires two consecutive weak observations. With no
    current language, the first weak observation may be used provisionally.
    Non-text observations leave a candidate untouched; ambiguous text clears
    it. An operator seed never blocks a later strong observation.
    """

    next_state = sanitize_language_state(state)
    provisional = provisional_language if provisional_language in VALID_LANGUAGES else None
    detected, strong, observed_text = _normalised_observation(
        detected_language,
        language_evidence,
    )
    current = next_state["language"]

    def clear_candidate() -> None:
        next_state["language_candidate"] = None
        next_state["language_candidate_streak"] = 0

    def set_detected(language: str) -> None:
        next_state["language"] = language
        next_state["language_provisional"] = False
        next_state["language_source"] = "detected"
        clear_candidate()

    def record_candidate(language: str) -> bool:
        streak = (
            next_state["language_candidate_streak"] + 1
            if next_state["language_candidate"] == language
            else 1
        )
        if streak >= 2:
            set_detected(language)
            return True
        next_state["language_candidate"] = language
        next_state["language_candidate_streak"] = streak
        return False

    if current not in VALID_LANGUAGES:
        if detected and strong:
            set_detected(detected)
        elif detected:
            next_state["language"] = detected
            next_state["language_provisional"] = True
            next_state["language_source"] = "provisional"
            next_state["language_candidate"] = detected
            next_state["language_candidate_streak"] = 1
        elif provisional:
            next_state["language"] = provisional
            next_state["language_provisional"] = True
            next_state["language_source"] = "provisional"
            clear_candidate()
        return next_state

    if not observed_text:
        return next_state
    if not detected:
        clear_candidate()
        return next_state
    if strong:
        set_detected(detected)
        return next_state
    if next_state["language_provisional"] and detected != current:
        # A phone/default hint or one weak observation must not outweigh the
        # client's next identifiable text. One weak signal is still provisional.
        next_state["language"] = detected
        next_state["language_source"] = "provisional"
        # Preserve a concordant candidate saved under the previous policy.
        record_candidate(detected)
        return next_state
    if detected == current:
        if next_state["language_provisional"]:
            record_candidate(detected)
        else:
            clear_candidate()
        return next_state
    record_candidate(detected)
    return next_state
