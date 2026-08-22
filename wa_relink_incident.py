"""Persistent, fail-closed state for one WhatsApp relink incident.

The recovery URL token is derived deterministically from the incident id and
the Flask application secret.  Only its SHA-256 digest and non-sensitive
metadata are persisted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Callable


SCHEMA_VERSION = 1
DEFAULT_LINK_TTL_SECONDS = 15 * 60
DEFAULT_CAPABILITY_TTL_SECONDS = 10 * 60
DEFAULT_MAX_NOTIFICATION_ATTEMPTS = 3
_INCIDENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_CAPABILITY_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_NOTIFICATION_CLAIM_RE = re.compile(r"^[0-9a-f]{32}$")
_ALLOWED_REASONS = frozenset({"logged_out", "session_invalid"})
_RETRY_DELAYS_SECONDS = (5, 30, 120)


class RelinkStateError(RuntimeError):
    """Base error with a stable, non-sensitive code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class RelinkStateCorrupt(RelinkStateError):
    pass


class RelinkTokenError(RelinkStateError):
    pass


def _urlsafe(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def relink_token(incident_id: str, app_secret: str) -> str:
    """Return the deterministic bearer token for one incident."""

    if not _INCIDENT_ID_RE.fullmatch(incident_id or ""):
        raise ValueError("invalid_incident_id")
    if not isinstance(app_secret, str) or len(app_secret) < 32:
        raise ValueError("invalid_app_secret")
    signature = hmac.new(
        app_secret.encode("utf-8"),
        b"wa-relink:v1\0" + incident_id.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{incident_id}.{_urlsafe(signature)}"


def relink_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def relink_capability_digest(capability: str) -> str:
    return hashlib.sha256(capability.encode("utf-8")).hexdigest()


def relink_outage_fingerprint(worker_revision: str | None, reason: str) -> str:
    """Build a low-cardinality outage marker without contact identifiers."""

    safe_reason = reason if reason in _ALLOWED_REASONS else "session_invalid"
    revision = worker_revision if isinstance(worker_revision, str) else "unknown"
    return hashlib.sha256(f"{revision}\0{safe_reason}".encode("utf-8")).hexdigest()[:24]


class WhatsAppRelinkIncidentStore:
    """Atomic single-incident store used by the supervisor and scoped routes."""

    def __init__(
        self,
        directory: Path,
        app_secret: str,
        *,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
        link_ttl_seconds: int = DEFAULT_LINK_TTL_SECONDS,
        capability_ttl_seconds: int = DEFAULT_CAPABILITY_TTL_SECONDS,
        max_notification_attempts: int = DEFAULT_MAX_NOTIFICATION_ATTEMPTS,
    ):
        self.directory = Path(directory)
        self.path = self.directory / "incident.json"
        self.app_secret = app_secret
        self.clock = clock
        self.id_factory = id_factory or (lambda: secrets.token_hex(16))
        self.link_ttl_seconds = max(60, int(link_ttl_seconds))
        self.capability_ttl_seconds = max(60, int(capability_ttl_seconds))
        self.max_notification_attempts = max(1, int(max_notification_attempts))
        self._lock = threading.RLock()

    @staticmethod
    def _copy(state: dict | None) -> dict | None:
        return json.loads(json.dumps(state)) if state is not None else None

    def _secure_directory(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass

    def _validate(self, state: object) -> dict:
        if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
            raise RelinkStateCorrupt("state_corrupt")
        incident_id = state.get("incident_id")
        if not isinstance(incident_id, str) or not _INCIDENT_ID_RE.fullmatch(incident_id):
            raise RelinkStateCorrupt("state_corrupt")
        if not isinstance(state.get("token_hash"), str) or not _CAPABILITY_DIGEST_RE.fullmatch(
            state["token_hash"]
        ):
            raise RelinkStateCorrupt("state_corrupt")
        if not isinstance(state.get("outage_open"), bool):
            raise RelinkStateCorrupt("state_corrupt")
        if not isinstance(state.get("status"), str):
            raise RelinkStateCorrupt("state_corrupt")
        if not isinstance(state.get("created_at"), (int, float)) or not isinstance(
            state.get("link_expires_at"), (int, float)
        ):
            raise RelinkStateCorrupt("state_corrupt")
        for kind in ("alert", "online"):
            delivery = state.get(kind)
            if not isinstance(delivery, dict):
                raise RelinkStateCorrupt("state_corrupt")
            attempts = delivery.get("attempts")
            if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
                raise RelinkStateCorrupt("state_corrupt")
            for timestamp_key in ("sent_at", "next_retry_at", "claim_expires_at"):
                timestamp = delivery.get(timestamp_key)
                if timestamp is not None and (
                    not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool)
                ):
                    raise RelinkStateCorrupt("state_corrupt")
            claim_id = delivery.get("claim_id")
            if claim_id is not None and (
                not isinstance(claim_id, str) or not _NOTIFICATION_CLAIM_RE.fullmatch(claim_id)
            ):
                raise RelinkStateCorrupt("state_corrupt")
            if (claim_id is None) != (delivery.get("claim_expires_at") is None):
                raise RelinkStateCorrupt("state_corrupt")
        capability_hash = state.get("capability_hash")
        if capability_hash is not None and (
            not isinstance(capability_hash, str)
            or not _CAPABILITY_DIGEST_RE.fullmatch(capability_hash)
        ):
            raise RelinkStateCorrupt("state_corrupt")
        return state

    def _read_unlocked(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RelinkStateCorrupt("state_corrupt") from exc
        return self._validate(state)

    def _write_unlocked(self, state: dict) -> None:
        self._validate(state)
        self._secure_directory()
        temporary = self.path.with_suffix(f".{os.getpid()}.tmp")
        try:
            temporary.write_text(
                json.dumps(state, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise RelinkStateError("state_unavailable") from exc

    def snapshot(self) -> dict | None:
        with self._lock:
            return self._copy(self._read_unlocked())

    def ensure_outage(self, fingerprint: str, reason: str) -> tuple[dict, bool]:
        """Create one incident, or return the existing continuous outage."""

        if not re.fullmatch(r"^[0-9a-f]{24}$", fingerprint or ""):
            raise ValueError("invalid_outage_fingerprint")
        safe_reason = reason if reason in _ALLOWED_REASONS else "session_invalid"
        with self._lock:
            current = self._read_unlocked()
            if current and current["outage_open"]:
                return self._copy(current), False

            now = float(self.clock())
            incident_id = self.id_factory()
            token = relink_token(incident_id, self.app_secret)
            state = {
                "schema_version": SCHEMA_VERSION,
                "incident_id": incident_id,
                "outage_fingerprint": fingerprint,
                "reason": safe_reason,
                "status": "detected",
                "outage_open": True,
                "created_at": now,
                "link_expires_at": now + self.link_ttl_seconds,
                "token_hash": relink_token_digest(token),
                "token_consumed_at": None,
                "capability_hash": None,
                "capability_expires_at": None,
                "qr_started_at": None,
                "promoted_at": None,
                "resolved_at": None,
                "cancelled_at": None,
                "alert": {
                    "attempts": 0,
                    "sent_at": None,
                    "next_retry_at": now,
                    "claim_id": None,
                    "claim_expires_at": None,
                },
                "online": {
                    "attempts": 0,
                    "sent_at": None,
                    "next_retry_at": now,
                    "claim_id": None,
                    "claim_expires_at": None,
                },
            }
            self._write_unlocked(state)
            return self._copy(state), True

    def token_for(self, state: dict) -> str:
        incident_id = state.get("incident_id") if isinstance(state, dict) else None
        token = relink_token(incident_id, self.app_secret)
        if not hmac.compare_digest(relink_token_digest(token), state.get("token_hash", "")):
            raise RelinkStateCorrupt("state_corrupt")
        return token

    def consume_token(self, token: str, capability_hash: str) -> dict:
        if not isinstance(token, str) or len(token) > 160:
            raise RelinkTokenError("token_invalid")
        if not _CAPABILITY_DIGEST_RE.fullmatch(capability_hash or ""):
            raise ValueError("invalid_capability_hash")
        with self._lock:
            state = self._read_unlocked()
            if state is None:
                raise RelinkTokenError("token_invalid")
            now = float(self.clock())
            expected = self.token_for(state)
            if not hmac.compare_digest(token, expected):
                raise RelinkTokenError("token_invalid")
            if not state["outage_open"] or state.get("cancelled_at") is not None:
                raise RelinkTokenError("token_inactive")
            if now >= float(state["link_expires_at"]):
                state["status"] = "expired"
                self._write_unlocked(state)
                raise RelinkTokenError("token_expired")
            if state.get("token_consumed_at") is not None:
                raise RelinkTokenError("token_consumed")
            state["token_consumed_at"] = now
            state["capability_hash"] = capability_hash
            state["capability_expires_at"] = now + self.capability_ttl_seconds
            state["status"] = "confirmed"
            self._write_unlocked(state)
            return self._copy(state)

    def capability_state(self, incident_id: str, capability_hash: str) -> dict:
        with self._lock:
            state = self._read_unlocked()
            if state is None or not hmac.compare_digest(
                str(state.get("incident_id", "")), str(incident_id or "")
            ):
                raise RelinkTokenError("capability_invalid")
            expected = state.get("capability_hash")
            if not isinstance(expected, str) or not hmac.compare_digest(expected, capability_hash):
                raise RelinkTokenError("capability_invalid")
            if float(self.clock()) >= float(state.get("capability_expires_at") or 0):
                raise RelinkTokenError("capability_expired")
            if not state["outage_open"] or state.get("cancelled_at") is not None:
                raise RelinkTokenError("capability_inactive")
            return self._copy(state)

    def mark_qr_started(self, incident_id: str) -> dict:
        with self._lock:
            state = self._read_unlocked()
            if state is None or state["incident_id"] != incident_id or not state["outage_open"]:
                raise RelinkStateError("incident_inactive")
            if state.get("qr_started_at") is None:
                state["qr_started_at"] = float(self.clock())
            state["status"] = "qr_active"
            self._write_unlocked(state)
            return self._copy(state)

    def mark_promoted(self, incident_id: str) -> dict:
        with self._lock:
            state = self._read_unlocked()
            if state is None or state["incident_id"] != incident_id:
                raise RelinkStateError("incident_inactive")
            if state.get("promoted_at") is None:
                state["promoted_at"] = float(self.clock())
            state["status"] = "promoted"
            self._write_unlocked(state)
            return self._copy(state)

    def cancel(self, incident_id: str) -> dict:
        with self._lock:
            state = self._read_unlocked()
            if state is None or state["incident_id"] != incident_id:
                raise RelinkStateError("incident_inactive")
            if state.get("cancelled_at") is None:
                state["cancelled_at"] = float(self.clock())
            state["status"] = "cancelled"
            self._write_unlocked(state)
            return self._copy(state)

    def resolve(self) -> tuple[dict | None, bool]:
        with self._lock:
            state = self._read_unlocked()
            if state is None:
                return None, False
            changed = bool(state["outage_open"])
            if changed:
                state["outage_open"] = False
                state["status"] = "resolved"
                state["resolved_at"] = float(self.clock())
                state["capability_hash"] = None
                state["capability_expires_at"] = None
                self._write_unlocked(state)
            return self._copy(state), changed

    @staticmethod
    def _notification_lifecycle_allows(state: dict, kind: str, now: float) -> bool:
        if kind == "alert":
            return bool(
                state["outage_open"]
                and state.get("token_consumed_at") is None
                and state.get("cancelled_at") is None
                and now < float(state["link_expires_at"])
            )
        return bool(not state["outage_open"] and state.get("status") == "resolved")

    def claim_notification(
        self,
        kind: str,
        *,
        lease_seconds: int = 60,
    ) -> tuple[dict | None, str | None]:
        """Atomically reserve one bounded notification attempt before network I/O."""

        if kind not in {"alert", "online"}:
            raise ValueError("invalid_notification_kind")
        with self._lock:
            state = self._read_unlocked()
            if state is None:
                return None, None
            delivery = state[kind]
            now = float(self.clock())
            if not self._notification_lifecycle_allows(state, kind, now):
                return self._copy(state), None
            if delivery.get("sent_at") is not None:
                return self._copy(state), None
            if int(delivery.get("attempts", 0)) >= self.max_notification_attempts:
                return self._copy(state), None
            if now < float(delivery.get("next_retry_at") or 0):
                return self._copy(state), None
            if (
                isinstance(delivery.get("claim_id"), str)
                and now < float(delivery.get("claim_expires_at") or 0)
            ):
                return self._copy(state), None

            claim_id = secrets.token_hex(16)
            delivery["attempts"] = int(delivery.get("attempts", 0)) + 1
            delivery["claim_id"] = claim_id
            delivery["claim_expires_at"] = now + max(10, int(lease_seconds))
            self._write_unlocked(state)
            return self._copy(state), claim_id

    def finish_notification(
        self,
        incident_id: str,
        kind: str,
        claim_id: str,
        delivered: bool,
    ) -> dict:
        """Record only the result belonging to the currently active claim."""

        if kind not in {"alert", "online"}:
            raise ValueError("invalid_notification_kind")
        if not isinstance(claim_id, str) or not _NOTIFICATION_CLAIM_RE.fullmatch(claim_id):
            raise ValueError("invalid_notification_claim")
        with self._lock:
            state = self._read_unlocked()
            if state is None or state["incident_id"] != incident_id:
                raise RelinkStateError("incident_inactive")
            delivery = state[kind]
            if delivery.get("sent_at") is not None:
                return self._copy(state)
            active_claim = delivery.get("claim_id")
            if not isinstance(active_claim, str) or not hmac.compare_digest(
                active_claim,
                claim_id,
            ):
                return self._copy(state)
            now = float(self.clock())
            delivery["claim_id"] = None
            delivery["claim_expires_at"] = None
            if delivered:
                delivery["sent_at"] = now
                delivery["next_retry_at"] = None
                if kind == "alert" and state["status"] == "detected":
                    state["status"] = "notified"
            else:
                index = min(delivery["attempts"] - 1, len(_RETRY_DELAYS_SECONDS) - 1)
                delivery["next_retry_at"] = now + _RETRY_DELAYS_SECONDS[index]
            self._write_unlocked(state)
            return self._copy(state)
