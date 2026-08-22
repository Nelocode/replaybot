"""Allowlisted WhatsApp incident health and forensic-ledger readers.

The Node worker owns both persisted files.  This module only reads them and
returns bounded, low-cardinality operational data.  Free-form provider errors,
message data, account identifiers and credentials are never exposed.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path


STATE_SCHEMA = 1
LEDGER_SCHEMA = 1
DEFAULT_STALE_AFTER_MS = 90_000
DEFAULT_HISTORY_LIMIT = 20
MAX_HISTORY_LIMIT = 100

CONDITIONS = frozenset({
    "starting", "healthy", "warning", "degraded", "critical", "unknown",
})
CONNECTIONS = frozenset({"starting", "connecting", "open", "closed", "unknown"})
LIKELIHOODS = frozenset({"low", "elevated", "high", "unknown"})
SIGNALS = frozenset({
    "auth_unregistered",
    "auth_write_failure",
    "backoff_active",
    "circuit_open",
    "connection_timeout",
    "connection_unstable",
    "delivery_failures",
    "frequent_reconnects",
    "ledger_integrity_invalid",
    "local_rate_limit",
    "operator_paused",
    "provider_forbidden",
    "provider_rate_limited",
    "slow_connect",
    "startup_failure",
    "terminal_session_failure",
    "worker_lease_conflict",
})
FAILURE_MODES = frozenset({
    "authentication_loss",
    "delivery_suspension",
    "duplicate_session",
    "local_protection",
    "provider_restriction",
    "rate_limit",
    "session_mismatch",
    "transport_instability",
    "unknown",
})
LEDGER_EVENTS = frozenset({
    "connection_closed",
    "connection_open",
    "health_transition",
    "operational_signal",
    "reconnect_scheduled",
    "worker_shutdown",
    "worker_started",
})
LIFECYCLES = frozenset({"opened", "updated", "resolved", "signal", "observation"})
STATUS_NAMES = frozenset({
    "auth_unregistered",
    "auth_write_failure",
    "bad_session",
    "connection_closed",
    "connection_lost_or_timeout",
    "connection_replaced",
    "connection_timeout",
    "delivery_failure",
    "delivery_timeout",
    "forbidden",
    "local_rate_limit",
    "logged_out",
    "multidevice_mismatch",
    "provider_forbidden",
    "provider_rate_limited",
    "queue_full",
    "queue_timeout",
    "restart_required",
    "service_unavailable",
    "slow_connect",
    "startup_failure",
    "unknown",
    "worker_lease_conflict",
    "worker_shutdown",
})
CATEGORIES = frozenset({
    "authorization",
    "delivery",
    "local_protection",
    "process",
    "provider_availability",
    "provider_restriction",
    "session_conflict",
    "session_mismatch",
    "transport",
    "unknown",
})
ACTIONS = frozenset({
    "automatic_reconnect",
    "check_duplicate_session",
    "inspect_delivery",
    "inspect_worker",
    "none",
    "relink",
    "relink_cleanly",
    "retry_backoff",
    "review_provider",
})
LEDGER_INTEGRITIES = frozenset({"verified", "invalid", "unavailable", "unknown"})

LEDGER_BASE_KEYS = (
    "schema_version",
    "sequence",
    "record_id",
    "at",
    "event",
    "lifecycle",
    "incident_id",
    "condition",
    "status_code",
    "status_name",
    "category",
    "action",
    "terminal",
    "reauth_required",
    "reconnect_attempt",
    "reconnect_delay_ms",
    "signals",
    "previous_hash",
)
LEDGER_RECORD_KEYS = frozenset((*LEDGER_BASE_KEYS, "record_hash"))
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_-]{1,96}\Z", re.ASCII)
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


def _enum(value, choices: frozenset[str], fallback: str) -> str:
    return value if isinstance(value, str) and value in choices else fallback


def _timestamp(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return int(value)


def _status_code(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float) and math.isfinite(value):
        parsed = int(value)
    elif isinstance(value, str):
        match = re.match(r"^[+-]?\d+", value)
        if not match:
            return None
        parsed = int(match.group(0))
    else:
        return None
    return parsed if 100 <= parsed <= 599 else None


def _bounded_integer(value, fallback: int = 0, maximum: int = 1_000_000) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return min(max(parsed, 0), maximum)


def _identifier(value) -> str | None:
    return value if isinstance(value, str) and _IDENTIFIER_RE.fullmatch(value) else None


def _hash_value(value) -> str | None:
    return value if isinstance(value, str) and _HASH_RE.fullmatch(value) else None


def _signal_list(values) -> list[str]:
    if not isinstance(values, list):
        return []
    return sorted({value for value in values if isinstance(value, str) and value in SIGNALS})


def _failure_mode_list(values) -> list[str]:
    if not isinstance(values, list):
        return []
    return sorted({value for value in values if isinstance(value, str) and value in FAILURE_MODES})


def _sanitize_failure(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    at = _timestamp(raw.get("at"))
    if at is None:
        return None
    return {
        "failure_id": _identifier(raw.get("failure_id")),
        "incident_id": _identifier(raw.get("incident_id")),
        "at": at,
        "status_code": _status_code(raw.get("status_code")),
        "status_name": _enum(raw.get("status_name"), STATUS_NAMES, "unknown"),
        "category": _enum(raw.get("category"), CATEGORIES, "unknown"),
        "action": _enum(raw.get("action"), ACTIONS, "inspect_worker"),
        "terminal": raw.get("terminal") is True,
        "reauth_required": raw.get("reauth_required") is True,
    }


def _sanitize_incident(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    incident_id = _identifier(raw.get("incident_id"))
    started_at = _timestamp(raw.get("started_at"))
    if incident_id is None or started_at is None:
        return None
    return {
        "incident_id": incident_id,
        "started_at": started_at,
        "last_event_at": _timestamp(raw.get("last_event_at")) or started_at,
        "resolved_at": _timestamp(raw.get("resolved_at")),
        "status_code": _status_code(raw.get("status_code")),
        "status_name": _enum(raw.get("status_name"), STATUS_NAMES, "unknown"),
        "category": _enum(raw.get("category"), CATEGORIES, "unknown"),
        "action": _enum(raw.get("action"), ACTIONS, "inspect_worker"),
        "terminal": raw.get("terminal") is True,
        "reauth_required": raw.get("reauth_required") is True,
        "disconnect_count": _bounded_integer(raw.get("disconnect_count"), 1, 10_000),
        "reconnect_attempts": _bounded_integer(raw.get("reconnect_attempts"), 0, 10_000),
        "precursors": _signal_list(raw.get("precursors")),
    }


def _empty_snapshot() -> dict:
    return {
        "schema_version": STATE_SCHEMA,
        "worker_revision": None,
        "updated_at": None,
        "heartbeat_at": None,
        "connection": "unknown",
        "connection_since": None,
        "last_open_at": None,
        "last_disconnect_at": None,
        "condition": "unknown",
        "failure_likelihood": "unknown",
        "signals": [],
        "likely_failure_modes": [],
        "active_incident": None,
        "last_incident": None,
        "last_failure": None,
        "ledger_integrity": "unknown",
        "ledger_sequence": 0,
        "ledger_head": None,
    }


def load_wa_incident_snapshot(path: Path) -> tuple[dict, bool]:
    """Load a worker snapshot while copying only explicitly allowed fields."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raw = None
    available = bool(
        isinstance(raw, dict)
        and not isinstance(raw.get("schema_version"), bool)
        and raw.get("schema_version") == STATE_SCHEMA
    )
    state = _empty_snapshot()
    if not available:
        return state, False

    state.update({
        "worker_revision": _identifier(raw.get("worker_revision")),
        "updated_at": _timestamp(raw.get("updated_at")),
        "heartbeat_at": _timestamp(raw.get("heartbeat_at")),
        "connection": _enum(raw.get("connection"), CONNECTIONS, "unknown"),
        "connection_since": _timestamp(raw.get("connection_since")),
        "last_open_at": _timestamp(raw.get("last_open_at")),
        "last_disconnect_at": _timestamp(raw.get("last_disconnect_at")),
        "condition": _enum(raw.get("condition"), CONDITIONS, "unknown"),
        "failure_likelihood": _enum(
            raw.get("failure_likelihood"), LIKELIHOODS, "unknown"
        ),
        "signals": _signal_list(raw.get("signals")),
        "likely_failure_modes": _failure_mode_list(raw.get("likely_failure_modes")),
        "active_incident": _sanitize_incident(raw.get("active_incident")),
        "last_incident": _sanitize_incident(raw.get("last_incident")),
        "last_failure": _sanitize_failure(raw.get("last_failure")),
        "ledger_integrity": _enum(
            raw.get("ledger_integrity"), LEDGER_INTEGRITIES, "unknown"
        ),
        "ledger_sequence": _bounded_integer(raw.get("ledger_sequence")),
        "ledger_head": _hash_value(raw.get("ledger_head")),
    })
    return state, True


def public_wa_incident_health(
    path: Path,
    *,
    can_manage: bool,
    worker_running: bool,
    now_ms: int | None = None,
    stale_after_ms: int = DEFAULT_STALE_AFTER_MS,
) -> dict:
    """Return the aggregate live state plus admin-only forensic details."""

    current = int(time.time() * 1000) if now_ms is None else int(now_ms)
    stale_window = max(1, int(stale_after_ms))
    state, available = load_wa_incident_snapshot(path)
    heartbeat_at = state["heartbeat_at"]
    telemetry_fresh = bool(
        available
        and heartbeat_at is not None
        and -60_000 <= current - heartbeat_at <= stale_window
    )
    worker_is_running = worker_running is True
    active_incident = state["active_incident"]
    last_failure = state["last_failure"]
    terminal_evidence = bool(
        active_incident and active_incident.get("terminal") is True
    ) or bool(
        state["connection"] == "closed"
        and last_failure
        and last_failure.get("terminal") is True
        and "terminal_session_failure" in state["signals"]
    )

    if terminal_evidence:
        condition = "critical"
        failure_likelihood = "high"
    elif not (available and telemetry_fresh and worker_is_running):
        condition = "unknown"
        failure_likelihood = "unknown"
    else:
        condition = state["condition"]
        failure_likelihood = state["failure_likelihood"]

    administrator = can_manage is True
    return {
        "ok": True,
        "schema_version": STATE_SCHEMA,
        "available": available,
        "can_manage": administrator,
        "worker_running": worker_is_running,
        "telemetry_fresh": telemetry_fresh,
        "terminal_incident": terminal_evidence,
        "incident_active": active_incident is not None,
        "condition": condition,
        "failure_likelihood": failure_likelihood,
        "connection": state["connection"],
        "updated_at": state["updated_at"],
        "heartbeat_at": heartbeat_at,
        "connection_since": state["connection_since"],
        "last_open_at": state["last_open_at"],
        "last_disconnect_at": state["last_disconnect_at"],
        "signals": state["signals"],
        "likely_failure_modes": state["likely_failure_modes"],
        "ledger_integrity": state["ledger_integrity"],
        "ledger_sequence": state["ledger_sequence"],
        "ledger_head": state["ledger_head"] if administrator else None,
        "active_incident": active_incident if administrator else None,
        "last_incident": state["last_incident"] if administrator else None,
        "last_failure": last_failure if administrator else None,
    }


def _reject_json_constant(value: str):
    raise ValueError(f"non-standard JSON constant: {value}")


def _strict_integer(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _ledger_hash(base: dict) -> str:
    serialized = json.dumps(
        base,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _audit_ledger(path: Path) -> tuple[str, bool, list[dict], int, str | None]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "verified", False, [], 0, None
    except UnicodeError:
        return "invalid", True, [], 0, None
    except OSError:
        return "unavailable", False, [], 0, None

    expected_hash = None
    expected_sequence = 0
    records: list[dict] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line, parse_constant=_reject_json_constant)
        except (ValueError, TypeError):
            return "invalid", True, [], 0, None
        if not isinstance(parsed, dict) or set(parsed) != LEDGER_RECORD_KEYS:
            return "invalid", True, [], 0, None
        schema = _strict_integer(parsed.get("schema_version"))
        sequence = _strict_integer(parsed.get("sequence"))
        if (
            schema != LEDGER_SCHEMA
            or sequence != expected_sequence + 1
            or parsed.get("previous_hash") != expected_hash
        ):
            return "invalid", True, [], 0, None
        base = {key: parsed.get(key) for key in LEDGER_BASE_KEYS}
        try:
            expected_record_hash = _ledger_hash(base)
        except (TypeError, ValueError, OverflowError):
            return "invalid", True, [], 0, None
        if parsed.get("record_hash") != expected_record_hash:
            return "invalid", True, [], 0, None
        expected_hash = expected_record_hash
        expected_sequence = sequence
        records.append(parsed)
    return "verified", True, records, expected_sequence, expected_hash


def _sanitize_ledger_record(raw: dict) -> dict:
    return {
        "schema_version": LEDGER_SCHEMA,
        "sequence": _bounded_integer(raw.get("sequence")),
        "record_id": _identifier(raw.get("record_id")),
        "at": _timestamp(raw.get("at")),
        "event": _enum(raw.get("event"), LEDGER_EVENTS, "health_transition"),
        "lifecycle": _enum(raw.get("lifecycle"), LIFECYCLES, "observation"),
        "incident_id": _identifier(raw.get("incident_id")),
        "condition": _enum(raw.get("condition"), CONDITIONS, "unknown"),
        "status_code": _status_code(raw.get("status_code")),
        "status_name": _enum(raw.get("status_name"), STATUS_NAMES, "unknown"),
        "category": _enum(raw.get("category"), CATEGORIES, "unknown"),
        "action": _enum(raw.get("action"), ACTIONS, "inspect_worker"),
        "terminal": raw.get("terminal") is True,
        "reauth_required": raw.get("reauth_required") is True,
        "reconnect_attempt": _bounded_integer(raw.get("reconnect_attempt"), 0, 10_000),
        "reconnect_delay_ms": _bounded_integer(
            raw.get("reconnect_delay_ms"), 0, 24 * 60 * 60_000
        ),
        "signals": _signal_list(raw.get("signals")),
        "previous_hash": _hash_value(raw.get("previous_hash")),
        "record_hash": _hash_value(raw.get("record_hash")),
    }


def _history_limit(value) -> int:
    if isinstance(value, bool):
        return DEFAULT_HISTORY_LIMIT
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_HISTORY_LIMIT
    return min(max(parsed, 1), MAX_HISTORY_LIMIT)


def read_wa_incident_history(
    path: Path,
    *,
    can_manage: bool,
    limit: int = DEFAULT_HISTORY_LIMIT,
    snapshot_path: Path | None = None,
) -> dict:
    """Read a bounded ledger only after the complete SHA-256 chain verifies."""

    selected_limit = _history_limit(limit)
    if can_manage is not True:
        return {
            "ok": False,
            "authorized": False,
            "available": False,
            "integrity": "unavailable",
            "limit": selected_limit,
            "total_records": 0,
            "sequence": 0,
            "head": None,
            "records": [],
        }

    integrity, available, records, sequence, head = _audit_ledger(path)
    if integrity == "verified" and snapshot_path is not None:
        anchor, anchor_available = load_wa_incident_snapshot(snapshot_path)
        anchor_sequence = anchor["ledger_sequence"]
        anchor_head = anchor["ledger_head"]
        if anchor_available and anchor_sequence > 0 and anchor_head is not None:
            if (
                anchor_sequence > sequence
                or records[anchor_sequence - 1].get("record_hash") != anchor_head
            ):
                integrity, records, sequence, head = "invalid", [], 0, None
    verified = integrity == "verified"
    safe_records = (
        [_sanitize_ledger_record(record) for record in records[-selected_limit:]]
        if verified
        else []
    )
    return {
        "ok": verified,
        "authorized": True,
        "available": available,
        "integrity": integrity,
        "limit": selected_limit,
        "total_records": len(records) if verified else 0,
        "sequence": sequence if verified else 0,
        "head": _hash_value(head) if verified else None,
        "records": safe_records,
    }
