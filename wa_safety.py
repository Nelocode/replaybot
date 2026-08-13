"""Allowlisted WhatsApp operational-health state for the admin panel.

The persisted event log contains only a category and millisecond timestamp.
No JID, phone number, message text, raw provider error or credential is read or
returned by this module.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


EVENT_TYPES = frozenset({
    "outgoing",
    "delivery_failure",
    "rate_limited",
    "forbidden",
    "local_rate_limit",
    "reconnect",
})
REASON_TYPES = frozenset({
    "forbidden_response",
    "rate_limited_response",
    "delivery_failures",
    "frequent_reconnects",
    "circuit_open",
    "elevated_activity",
    "local_rate_limit",
    "backoff_active",
    "operator_paused",
})
RETENTION_MS = 60 * 60 * 1000
MAX_EVENTS_PER_TYPE = 500


def _timestamp(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _empty_state() -> dict:
    return {
        "schema_version": 1,
        "updated_at": None,
        "operator_paused": False,
        "pause_updated_at": None,
        "backoff_until": None,
        "circuit_open_until": None,
        "events": [],
    }


def load_wa_safety_state(
    path: Path,
    *,
    control_path: Path | None = None,
    now_ms: int | None = None,
) -> tuple[dict, bool]:
    current = int(time.time() * 1000) if now_ms is None else int(now_ms)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raw = None
    available = isinstance(raw, dict) and raw.get("schema_version") == 1
    state = _empty_state()
    if available:
        floor = current - RETENTION_MS
        events_by_type = {event_type: [] for event_type in EVENT_TYPES}
        for event in raw.get("events", []):
            if not isinstance(event, dict) or event.get("type") not in EVENT_TYPES:
                continue
            at = _timestamp(event.get("at"))
            if at is None or at < floor or at > current + 60_000:
                continue
            events_by_type[event["type"]].append({"type": event["type"], "at": at})

        events = sorted(
            (
                event
                for values in events_by_type.values()
                for event in values[-MAX_EVENTS_PER_TYPE:]
            ),
            key=lambda event: event["at"],
        )
        state.update({
            "updated_at": _timestamp(raw.get("updated_at")),
            "operator_paused": raw.get("operator_paused") is True,
            "pause_updated_at": _timestamp(raw.get("pause_updated_at")),
            "backoff_until": _timestamp(raw.get("backoff_until")),
            "circuit_open_until": _timestamp(raw.get("circuit_open_until")),
            "events": events,
        })
    if control_path is not None:
        try:
            control = json.loads(control_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            control = None
        if isinstance(control, dict) and control.get("schema_version") == 1:
            state["operator_paused"] = control.get("operator_paused") is True
            state["pause_updated_at"] = _timestamp(control.get("pause_updated_at"))
    return state, available


def _count(events: list[dict], event_type: str, since: int) -> int:
    return sum(1 for event in events if event["type"] == event_type and event["at"] >= since)


def public_wa_safety_health(
    path: Path,
    *,
    control_path: Path | None = None,
    now_ms: int | None = None,
) -> dict:
    current = int(time.time() * 1000) if now_ms is None else int(now_ms)
    state, available = load_wa_safety_state(
        path,
        control_path=control_path,
        now_ms=current,
    )
    events = state["events"]
    counts = {
        "outgoing_1m": _count(events, "outgoing", current - 60_000),
        "delivery_failures_15m": _count(events, "delivery_failure", current - 15 * 60_000),
        "reconnects_15m": _count(events, "reconnect", current - 15 * 60_000),
        "rate_limits_60m": _count(events, "rate_limited", current - 60 * 60_000),
        "forbidden_60m": _count(events, "forbidden", current - 60 * 60_000),
        "local_rate_limits_15m": _count(events, "local_rate_limit", current - 15 * 60_000),
    }
    backoff_until = state["backoff_until"] if (state["backoff_until"] or 0) > current else None
    circuit_open_until = (
        state["circuit_open_until"]
        if (state["circuit_open_until"] or 0) > current
        else None
    )
    high_reasons = []
    if counts["forbidden_60m"]:
        high_reasons.append("forbidden_response")
    if counts["rate_limits_60m"]:
        high_reasons.append("rate_limited_response")
    if counts["delivery_failures_15m"] >= 5:
        high_reasons.append("delivery_failures")
    if counts["reconnects_15m"] >= 5:
        high_reasons.append("frequent_reconnects")
    if circuit_open_until:
        high_reasons.append("circuit_open")

    level = "high" if high_reasons else "low"
    reasons = high_reasons
    if level == "low":
        if counts["delivery_failures_15m"] >= 2:
            reasons.append("delivery_failures")
        if counts["reconnects_15m"] >= 3:
            reasons.append("frequent_reconnects")
        if counts["outgoing_1m"] >= 15:
            reasons.append("elevated_activity")
        if counts["local_rate_limits_15m"]:
            reasons.append("local_rate_limit")
        if backoff_until:
            reasons.append("backoff_active")
        if state["operator_paused"]:
            reasons.append("operator_paused")
        if reasons:
            level = "moderate"

    return {
        "ok": True,
        "schema_version": 1,
        "available": available,
        "level": level if available else "unknown",
        "reasons": [reason for reason in reasons if reason in REASON_TYPES],
        "counts": counts,
        "operator_paused": state["operator_paused"],
        "backoff_until": backoff_until,
        "circuit_open_until": circuit_open_until,
        "delivery_blocked": bool(state["operator_paused"] or backoff_until or circuit_open_until),
        "updated_at": state["updated_at"],
    }


def set_wa_operator_paused(
    health_path: Path,
    paused: bool,
    *,
    control_path: Path | None = None,
    now_ms: int | None = None,
) -> dict:
    current = int(time.time() * 1000) if now_ms is None else int(now_ms)
    destination = control_path or health_path.with_name("wa_safety_control.json")
    control = {
        "schema_version": 1,
        "operator_paused": paused is True,
        "pause_updated_at": current,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(control, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, destination)
    return public_wa_safety_health(
        health_path,
        control_path=destination,
        now_ms=current,
    )
