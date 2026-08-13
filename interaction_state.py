"""Persistent, privacy-conscious interaction routing for Telegram.

The state machine is deliberately saturated for content: after the first
interaction every text or media interaction receives step 2 forever.  Every
distinct call attempt receives the special call response, regardless of the
current phase, while still advancing the contact's interaction phase.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import time
from typing import Callable


VALID_LANGUAGES = frozenset({"es", "en", "fr"})
VALID_KINDS = frozenset({"call", "content"})


@dataclass(frozen=True)
class InteractionDecision:
    duplicate: bool
    phase: int
    response_key: str | None
    language: str
    persisted: bool = True


class PersistentInteractionState:
    """Claims unique inbound events and persists a per-contact phase.

    Raw contact and event identifiers are never written to disk.  Their hashes
    are sufficient for lookup and deduplication while avoiding a readable list
    of customer identifiers in the state file.
    """

    def __init__(
        self,
        file_path: str | Path,
        *,
        default_language: str = "es",
        max_recent_events: int = 256,
        now: Callable[[], float] = time.time,
        logger: logging.Logger | None = None,
    ) -> None:
        if default_language not in VALID_LANGUAGES:
            raise ValueError("default_language must be es, en, or fr")
        if max_recent_events < 1:
            raise ValueError("max_recent_events must be positive")

        self.file_path = Path(file_path)
        self.default_language = default_language
        self.max_recent_events = max_recent_events
        self.now = now
        self.logger = logger or logging.getLogger(__name__)
        self._contacts: dict[str, dict] = {}
        self._load()

    @staticmethod
    def _fingerprint(namespace: str, value: object) -> str:
        material = f"{namespace}\0{value}".encode("utf-8", errors="strict")
        return hashlib.sha256(material).hexdigest()

    def _load(self) -> None:
        if not self.file_path.exists():
            return
        try:
            parsed = json.loads(self.file_path.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("state root is not an object")
            contacts = parsed.get("contacts", {})
            if not isinstance(contacts, dict):
                raise ValueError("contacts is not an object")

            clean: dict[str, dict] = {}
            for contact_key, raw in contacts.items():
                if not isinstance(contact_key, str) or not isinstance(raw, dict):
                    continue
                phase = raw.get("phase", 0)
                language = raw.get("language")
                events = raw.get("recent_events", [])
                if not isinstance(phase, int) or phase not in (0, 1, 2):
                    continue
                if language not in VALID_LANGUAGES:
                    language = None
                if not isinstance(events, list):
                    events = []
                events = [item for item in events if isinstance(item, str)]
                clean[contact_key] = {
                    "phase": phase,
                    "language": language,
                    "language_provisional": bool(
                        language and raw.get("language_provisional") is True
                    ),
                    "recent_events": events[-self.max_recent_events :],
                    "updated_at": raw.get("updated_at", 0),
                }
            self._contacts = clean
        except (OSError, ValueError, json.JSONDecodeError):
            self.logger.error("Interaction state could not be loaded; starting empty")
            self._contacts = {}

    def _save(self) -> bool:
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.file_path.with_suffix(self.file_path.suffix + ".tmp")
            payload = {"version": 1, "contacts": self._contacts}
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temp_path, self.file_path)
            return True
        except OSError:
            self.logger.error("Interaction state could not be persisted")
            return False

    def register(
        self,
        *,
        contact_id: object,
        event_id: object,
        kind: str,
        detected_language: str | None = None,
        provisional_language: str | None = None,
    ) -> InteractionDecision:
        """Atomically claims an event before any outbound delivery occurs."""

        if contact_id in (None, ""):
            raise ValueError("contact_id is required")
        if event_id in (None, ""):
            raise ValueError("event_id is required")
        if kind not in VALID_KINDS:
            raise ValueError("kind must be call or content")
        if detected_language not in VALID_LANGUAGES:
            detected_language = None
        if provisional_language not in VALID_LANGUAGES:
            provisional_language = None

        contact_key = self._fingerprint("contact", contact_id)
        event_key = self._fingerprint("event", event_id)
        state = self._contacts.get(contact_key)
        if state is None:
            state = {
                "phase": 0,
                "language": None,
                "language_provisional": False,
                "recent_events": [],
                "updated_at": 0,
            }
            self._contacts[contact_key] = state

        recent_events = state["recent_events"]
        if event_key in recent_events:
            return InteractionDecision(
                duplicate=True,
                phase=state["phase"],
                response_key=None,
                language=state.get("language") or self.default_language,
            )

        language = state.get("language")
        language_is_provisional = state.get("language_provisional") is True
        if detected_language and (
            language not in VALID_LANGUAGES or language_is_provisional
        ):
            language = detected_language
            state["language"] = detected_language
            state["language_provisional"] = False
        elif language not in VALID_LANGUAGES and provisional_language:
            language = provisional_language
            state["language"] = provisional_language
            state["language_provisional"] = True
        effective_language = language or self.default_language

        previous_phase = state["phase"]
        if kind == "call":
            response_key = "call"
            next_phase = 1 if previous_phase == 0 else 2
        elif previous_phase == 0:
            response_key = "step1"
            next_phase = 1
        else:
            # Every later content interaction uses step 2.
            response_key = "step2"
            next_phase = 2

        recent_events.append(event_key)
        if len(recent_events) > self.max_recent_events:
            del recent_events[: -self.max_recent_events]
        state["phase"] = next_phase
        state["updated_at"] = self.now()
        persisted = self._save()

        return InteractionDecision(
            duplicate=False,
            phase=next_phase,
            response_key=response_key,
            language=effective_language,
            persisted=persisted,
        )

    def preview(
        self,
        *,
        contact_id: object,
        event_id: object,
        kind: str,
        detected_language: str | None = None,
        provisional_language: str | None = None,
    ) -> InteractionDecision:
        """Calcula la respuesta sin consumir la interacción todavía.

        El dispatcher la confirma con ``register`` sólo después de completar
        la entrega, de modo que un fallo de Telegram pueda reintentarse.
        """

        if contact_id in (None, ""):
            raise ValueError("contact_id is required")
        if event_id in (None, ""):
            raise ValueError("event_id is required")
        if kind not in VALID_KINDS:
            raise ValueError("kind must be call or content")
        if detected_language not in VALID_LANGUAGES:
            detected_language = None
        if provisional_language not in VALID_LANGUAGES:
            provisional_language = None

        contact_key = self._fingerprint("contact", contact_id)
        event_key = self._fingerprint("event", event_id)
        state = self._contacts.get(contact_key)
        phase = state["phase"] if state else 0
        language = state.get("language") if state else None
        language_is_provisional = bool(
            state and state.get("language_provisional") is True
        )
        recent_events = state.get("recent_events", []) if state else []
        if event_key in recent_events:
            return InteractionDecision(
                duplicate=True,
                phase=phase,
                response_key=None,
                language=language or self.default_language,
            )

        if detected_language and (
            language not in VALID_LANGUAGES or language_is_provisional
        ):
            effective_language = detected_language
        else:
            effective_language = (
                language or provisional_language or self.default_language
            )
        if kind == "call":
            response_key = "call"
        else:
            response_key = "step1" if phase == 0 else "step2"
        return InteractionDecision(
            duplicate=False,
            phase=1 if phase == 0 else 2,
            response_key=response_key,
            language=effective_language,
        )
