"""Shared, bounded language evidence for Spanish, English, and French."""

from __future__ import annotations

import json
from pathlib import Path
import re
import unicodedata
from typing import Any


LANGUAGES = ("es", "en", "fr")
_RULES_PATH = Path(__file__).with_name("language_rules.json")
_TOKEN_PATTERN = re.compile(r"[^\W_]+", flags=re.UNICODE)
_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "`": "'", "´": "'"})


def _normalize_unbounded(text: str) -> str:
    compatible = unicodedata.normalize("NFKC", text).lower().translate(_APOSTROPHES)
    decomposed = unicodedata.normalize("NFD", compatible)
    return "".join(
        character
        for character in decomposed
        if not unicodedata.category(character).startswith("M")
    )


def _tokens_unbounded(text: str) -> list[str]:
    return _TOKEN_PATTERN.findall(_normalize_unbounded(text))


def _require_positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _load_rules() -> dict[str, Any]:
    parsed = json.loads(_RULES_PATH.read_text(encoding="utf-8"))
    expected_root_keys = {
        "version",
        "limits",
        "thresholds",
        "neutral_terms",
        "language_names",
        "explicit_context",
        "negation",
        "rules",
    }
    if (
        not isinstance(parsed, dict)
        or set(parsed) != expected_root_keys
        or parsed.get("version") != 1
    ):
        raise ValueError("language rules must use schema version 1")

    limits = parsed.get("limits")
    thresholds = parsed.get("thresholds")
    names = parsed.get("language_names")
    explicit_context = parsed.get("explicit_context")
    negation = parsed.get("negation")
    rules = parsed.get("rules")
    neutral_terms = parsed.get("neutral_terms")
    if not all(
        isinstance(item, dict)
        for item in (limits, thresholds, names, explicit_context, negation, rules)
    ):
        raise ValueError("language rules sections are invalid")
    if (
        set(limits) != {"max_text_chars", "max_tokens"}
        or set(thresholds) != {
            "weak_score", "weak_margin", "strong_score", "strong_margin", "explicit_weight"
        }
        or set(explicit_context) != {
            "prefixes", "courtesy_suffixes", "contrastive_connectors"
        }
        or set(negation) != {"lookback_tokens", "tokens", "sequences"}
        or not isinstance(neutral_terms, list)
        or not isinstance(explicit_context.get("prefixes"), list)
        or not isinstance(explicit_context.get("courtesy_suffixes"), list)
        or not isinstance(explicit_context.get("contrastive_connectors"), list)
        or not isinstance(negation.get("tokens"), list)
        or not isinstance(negation.get("sequences"), list)
    ):
        raise ValueError("neutral_terms must be an array")

    _require_positive_int(limits.get("max_text_chars"), "max_text_chars")
    _require_positive_int(limits.get("max_tokens"), "max_tokens")
    for name in (
        "weak_score", "weak_margin", "strong_score", "strong_margin", "explicit_weight"
    ):
        _require_positive_int(thresholds.get(name), name)
    if thresholds["strong_score"] < thresholds["weak_score"]:
        raise ValueError("strong_score must not be below weak_score")
    if thresholds["strong_margin"] < thresholds["weak_margin"]:
        raise ValueError("strong_margin must not be below weak_margin")
    lookback_tokens = _require_positive_int(
        negation.get("lookback_tokens"), "negation lookback_tokens"
    )
    if lookback_tokens > 8:
        raise ValueError("negation lookback_tokens must be at most 8")

    normalized_neutral: set[str] = set()
    for term in neutral_terms:
        tokens = _tokens_unbounded(term) if isinstance(term, str) else []
        if len(tokens) != 1 or tokens[0] in normalized_neutral:
            raise ValueError("neutral terms must each normalize to one unique token")
        normalized_neutral.add(tokens[0])

    name_targets: dict[str, str] = {}
    compiled_rules: dict[str, list[dict[str, Any]]] = {}
    if set(names) != set(LANGUAGES) or set(rules) != set(LANGUAGES):
        raise ValueError("language rules must define exactly es, en, and fr")
    for language in LANGUAGES:
        raw_names = names[language]
        raw_rules = rules[language]
        if (
            not isinstance(raw_names, list)
            or not raw_names
            or not isinstance(raw_rules, list)
        ):
            raise ValueError(f"rules for {language} must be arrays")
        for name in raw_names:
            tokens = tuple(_tokens_unbounded(name)) if isinstance(name, str) else ()
            if len(tokens) != 1 or tokens[0] in name_targets:
                raise ValueError(
                    f"language names must be unique single tokens: {language}"
                )
            name_targets[tokens[0]] = language

        seen: set[tuple[str, ...]] = set()
        compiled_language_rules: list[dict[str, Any]] = []
        for raw_rule in raw_rules:
            if not isinstance(raw_rule, dict) or set(raw_rule) != {"term", "weight", "decisive"}:
                raise ValueError(f"invalid rule for {language}")
            tokens = tuple(_tokens_unbounded(raw_rule.get("term", "")))
            weight = _require_positive_int(raw_rule.get("weight"), "rule weight")
            decisive = raw_rule.get("decisive")
            if not tokens or not isinstance(decisive, bool) or tokens in seen:
                raise ValueError(f"invalid or duplicate rule for {language}")
            if len(tokens) == 1 and tokens[0] in normalized_neutral:
                raise ValueError(f"neutral token cannot score for {language}: {tokens[0]}")
            seen.add(tokens)
            compiled_language_rules.append(
                {"tokens": tokens, "weight": weight, "decisive": decisive}
            )
        compiled_rules[language] = compiled_language_rules

    negation_tokens: set[str] = set()
    for term in negation["tokens"]:
        tokens = _tokens_unbounded(term) if isinstance(term, str) else []
        if len(tokens) != 1 or tokens[0] in negation_tokens:
            raise ValueError(
                "negation tokens must each normalize to one unique token"
            )
        negation_tokens.add(tokens[0])
    negation_sequences: list[tuple[str, ...]] = []
    seen_negation_sequences: set[tuple[str, ...]] = set()
    for term in negation["sequences"]:
        tokens = tuple(_tokens_unbounded(term)) if isinstance(term, str) else ()
        if (
            len(tokens) < 2
            or len(tokens) > lookback_tokens
            or tokens in seen_negation_sequences
        ):
            raise ValueError(
                "negation sequences must be unique and fit the lookback window"
            )
        seen_negation_sequences.add(tokens)
        negation_sequences.append(tokens)

    def compile_unique_phrases(raw_terms: list[object], label: str) -> tuple[tuple[str, ...], ...]:
        compiled: list[tuple[str, ...]] = []
        seen: set[tuple[str, ...]] = set()
        for term in raw_terms:
            tokens = tuple(_tokens_unbounded(term)) if isinstance(term, str) else ()
            if not tokens or len(tokens) > lookback_tokens or tokens in seen:
                raise ValueError(f"{label} must be unique and fit the bounded context")
            seen.add(tokens)
            compiled.append(tokens)
        if not compiled:
            raise ValueError(f"{label} must not be empty")
        return tuple(compiled)

    prefixes = compile_unique_phrases(
        explicit_context["prefixes"], "explicit prefixes"
    )
    courtesy_suffixes = compile_unique_phrases(
        explicit_context["courtesy_suffixes"], "courtesy suffixes"
    )
    normalized_connectors: set[str] = set()
    for connector in explicit_context["contrastive_connectors"]:
        tokens = _tokens_unbounded(connector) if isinstance(connector, str) else []
        if len(tokens) != 1 or tokens[0] in normalized_connectors:
            raise ValueError("contrastive connectors must be unique single tokens")
        normalized_connectors.add(tokens[0])
    if not normalized_connectors:
        raise ValueError("contrastive connectors must not be empty")

    parsed["neutral_terms"] = frozenset(normalized_neutral)
    parsed["language_names"] = name_targets
    parsed["explicit_context"] = {
        "prefixes": prefixes,
        "courtesy_suffixes": courtesy_suffixes,
        "contrastive_connectors": frozenset(normalized_connectors),
    }
    parsed["negation"] = {
        "lookback_tokens": lookback_tokens,
        "tokens": frozenset(negation_tokens),
        "sequences": tuple(negation_sequences),
    }
    parsed["rules"] = compiled_rules
    return parsed


_RULES = _load_rules()


def normalize_language_text(text: str) -> str:
    """Normalize a bounded prefix without ASCII-only word-boundary rules."""

    if not isinstance(text, str):
        return ""
    return _normalize_unbounded(text[: _RULES["limits"]["max_text_chars"]])


def tokenize_language_text(text: str) -> list[str]:
    """Tokenize Unicode letters/numbers and cap work per inbound message."""

    return _TOKEN_PATTERN.findall(normalize_language_text(text))[
        : _RULES["limits"]["max_tokens"]
    ]


def _tokenize_language_clauses(text: str) -> list[list[str]]:
    """Preserve bounded punctuation scope without retaining message content."""

    normalized = normalize_language_text(text)
    raw_clauses: list[str] = []
    current: list[str] = []
    for character in normalized:
        if character == "'" or character.isspace() or character.isalnum():
            current.append(character)
        elif current:
            raw_clauses.append("".join(current))
            current = []
    if current:
        raw_clauses.append("".join(current))

    clauses: list[list[str]] = []
    remaining = _RULES["limits"]["max_tokens"]
    for clause in raw_clauses:
        if remaining <= 0:
            break
        tokens = _TOKEN_PATTERN.findall(clause)[:remaining]
        if tokens:
            clauses.append(tokens)
            remaining -= len(tokens)
    return clauses


def _contains_sequence(tokens: list[str], phrase: tuple[str, ...]) -> bool:
    width = len(phrase)
    if width > len(tokens):
        return False
    return any(tuple(tokens[index : index + width]) == phrase for index in range(len(tokens) - width + 1))


def _ends_with_sequence(tokens: list[str], end: int, phrase: tuple[str, ...]) -> bool:
    start = end - len(phrase)
    return start >= 0 and tuple(tokens[start:end]) == phrase


def _starts_with_sequence(tokens: list[str], start: int, phrase: tuple[str, ...]) -> bool:
    end = start + len(phrase)
    return end <= len(tokens) and tuple(tokens[start:end]) == phrase


def _is_language_request(tokens: list[str], name_index: int) -> bool:
    context = _RULES["explicit_context"]
    non_names = [
        token
        for index, token in enumerate(tokens)
        if index != name_index and token not in _RULES["language_names"]
    ]
    if not non_names or any(
        tuple(non_names) == courtesy for courtesy in context["courtesy_suffixes"]
    ):
        return True
    if any(
        _ends_with_sequence(tokens, name_index, prefix)
        for prefix in context["prefixes"]
    ):
        return True
    return any(
        _starts_with_sequence(tokens, name_index + 1, courtesy)
        for courtesy in context["courtesy_suffixes"]
    )


def _is_negated_language_name(tokens: list[str], name_index: int) -> bool:
    negation = _RULES["negation"]
    start = max(0, name_index - negation["lookback_tokens"])
    for index in range(name_index - 1, start - 1, -1):
        if tokens[index] in _RULES["language_names"]:
            start = index + 1
            break
    window = tokens[start:name_index]
    return bool(
        any(token in negation["tokens"] for token in window)
        or any(
            _contains_sequence(window, sequence)
            for sequence in negation["sequences"]
        )
    )


def _directive_segments(clauses: list[list[str]]) -> list[list[str]]:
    segments: list[list[str]] = []
    connectors = _RULES["explicit_context"]["contrastive_connectors"]
    for clause in clauses:
        current: list[str] = []
        for token in clause:
            if token in connectors:
                if current:
                    segments.append(current)
                    current = []
            else:
                current.append(token)
        if current:
            segments.append(current)
    return segments


def detect_language_evidence(text: str) -> dict[str, object]:
    """Return the exact public evidence contract for one bounded message."""

    clauses = _tokenize_language_clauses(text)
    tokens = [token for clause in clauses for token in clause]
    scores = {language: 0 for language in LANGUAGES}
    decisive = {language: False for language in LANGUAGES}
    last_directive: dict[str, str] = {}

    last_positive_group: set[str] = set()
    for segment in _directive_segments(clauses):
        segment_positive: set[str] = set()
        negative_name_indices: set[int] = set()
        previous_name_index: int | None = None
        for index, token in enumerate(segment):
            target = _RULES["language_names"].get(token)
            if not target:
                continue
            if _is_negated_language_name(segment, index):
                last_directive[target] = "negative"
                negative_name_indices.add(index)
            else:
                request = _is_language_request(segment, index)
                if not request and previous_name_index in negative_name_indices:
                    tail = segment[previous_name_index + 1 :]
                    request = _is_language_request(
                        tail, index - previous_name_index - 1
                    )
                if request:
                    last_directive[target] = "positive"
                    segment_positive.add(target)
            previous_name_index = index
        if segment_positive:
            last_positive_group = segment_positive

    explicit_hits = {
        language for language in last_positive_group
        if last_directive.get(language) == "positive"
    }
    negated_targets = {
        language for language, directive in last_directive.items()
        if directive == "negative"
    }

    for language in LANGUAGES:
        if language in explicit_hits:
            scores[language] += _RULES["thresholds"]["explicit_weight"]
        for rule in _RULES["rules"][language]:
            if _contains_sequence(tokens, rule["tokens"]):
                scores[language] += rule["weight"]
                decisive[language] = decisive[language] or rule["decisive"]

    for language in negated_targets:
        scores[language] = 0
        decisive[language] = False

    ordered_scores = sorted(scores.values(), reverse=True)
    top_score = ordered_scores[0]
    margin = top_score - ordered_scores[1]
    winners = [language for language, score in scores.items() if score == top_score]
    language = winners[0] if top_score > 0 and len(winners) == 1 else None

    # Conflicting explicit requests are code-switch evidence, not permission to
    # select whichever language happened to collect an extra common keyword.
    if len(explicit_hits) > 1:
        language = None
        margin = 0

    thresholds = _RULES["thresholds"]
    valid = bool(
        language
        and top_score >= thresholds["weak_score"]
        and margin >= thresholds["weak_margin"]
    )
    if not valid:
        language = None
    explicit = bool(language and explicit_hits == {language})
    strong = bool(
        language
        and (
            explicit
            or (
                top_score >= thresholds["strong_score"]
                and margin >= thresholds["strong_margin"]
                and decisive[language]
            )
        )
    )
    return {
        "language": language,
        "strong": strong,
        "explicit": explicit,
        "score": top_score,
        "margin": margin,
    }


def detect_supported_language(text: str) -> str | None:
    """Backward-compatible wrapper returning only the supported language."""

    return detect_language_evidence(text)["language"]  # type: ignore[return-value]


def detect_language(text: str) -> str | None:
    """Barcelona compatibility name for the shared detector."""

    return detect_supported_language(text)
