"""Brute-force protection for operator-owned panel recovery.

The operator key itself never enters this store.  Only a keyed digest of the
requesting browser/network identity and failure timestamps are persisted.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from pathlib import Path
import time
from typing import Any, Callable

from panel_admin_access import (
    PanelAccessBusy,
    _atomic_write_json,
    _exclusive_file_lock,
    _read_json,
    _secure_directory,
)


MIN_OPERATOR_KEY_BYTES = 32
MAX_OPERATOR_KEY_BYTES = 512
DEFAULT_WINDOW_SECONDS = 15 * 60
DEFAULT_CLIENT_ATTEMPTS = 5
DEFAULT_GLOBAL_ATTEMPTS = 20
DEFAULT_LOCKOUT_SECONDS = 15 * 60
MAX_TRACKED_CLIENTS = 256
RATE_FILENAME = "operator_rate.json"


def _key_bytes(value: Any) -> bytes | None:
    if not isinstance(value, str):
        return None
    if len(value) > MAX_OPERATOR_KEY_BYTES:
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        return None
    if not MIN_OPERATOR_KEY_BYTES <= len(encoded) <= MAX_OPERATOR_KEY_BYTES:
        return None
    if any(character in value for character in ("\x00", "\r", "\n")):
        return None
    return encoded


def valid_configured_operator_key(value: Any) -> bool:
    """Return whether an operator key is safe to enable in production."""

    return _key_bytes(value) is not None


def operator_key_matches(configured: Any, supplied: Any) -> bool:
    """Compare fixed-size digests so equality uses constant-time comparison."""

    configured_bytes = _key_bytes(configured)
    supplied_bytes = _key_bytes(supplied)
    if configured_bytes is None:
        return False

    # Invalid input still participates in the same fixed-size comparison.  It
    # cannot accidentally match because of the distinct invalid-input domain.
    configured_digest = hashlib.sha256(
        b"panel-operator-key:v1\0" + configured_bytes
    ).digest()
    if supplied_bytes is None:
        supplied_digest = hashlib.sha256(
            b"panel-operator-key:invalid:v1\0"
        ).digest()
    else:
        supplied_digest = hashlib.sha256(
            b"panel-operator-key:v1\0" + supplied_bytes
        ).digest()
    return hmac.compare_digest(configured_digest, supplied_digest)


def _remaining_seconds(value: Any, now: float) -> int:
    try:
        return max(0, int(math.ceil(float(value) - now)))
    except (TypeError, ValueError):
        return 0


def _recent_failures(value: Any, now: float, window_seconds: int) -> list[float]:
    if not isinstance(value, list):
        return []
    cutoff = now - window_seconds
    recent: list[float] = []
    for item in value:
        try:
            timestamp = float(item)
        except (TypeError, ValueError):
            continue
        if cutoff < timestamp <= now + 1:
            recent.append(timestamp)
    return recent


class OperatorAdminRecoveryGuard:
    """Persist per-client and global attempt budgets without storing keys."""

    def __init__(
        self,
        storage_dir: str | Path,
        identity_secret: str | bytes,
        *,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        client_attempts: int = DEFAULT_CLIENT_ATTEMPTS,
        global_attempts: int = DEFAULT_GLOBAL_ATTEMPTS,
        lockout_seconds: int = DEFAULT_LOCKOUT_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        secret_bytes = (
            identity_secret.encode("utf-8")
            if isinstance(identity_secret, str)
            else identity_secret
        )
        if not isinstance(secret_bytes, bytes) or len(secret_bytes) < 32:
            raise ValueError("identity_secret must contain at least 32 bytes")
        if (
            window_seconds <= 0
            or client_attempts <= 0
            or global_attempts <= 0
            or lockout_seconds <= 0
        ):
            raise ValueError("invalid operator recovery rate limits")

        self.storage_dir = Path(storage_dir)
        self._identity_secret = secret_bytes
        self.window_seconds = int(window_seconds)
        self.client_attempts = int(client_attempts)
        self.global_attempts = int(global_attempts)
        self.lockout_seconds = int(lockout_seconds)
        self._clock = clock
        _secure_directory(self.storage_dir)

    @property
    def rate_path(self) -> Path:
        return self.storage_dir / RATE_FILENAME

    @property
    def lock_path(self) -> Path:
        return self.storage_dir / ".operator-rate.lock"

    def _identity_digest(self, identity: str) -> str:
        return hmac.new(
            self._identity_secret,
            b"panel-operator-client:v1\0" + identity.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _normalize_state(self, raw: Any, now: float) -> dict[str, Any]:
        source = raw if isinstance(raw, dict) else {}
        clients: dict[str, Any] = {}
        raw_clients = source.get("clients")
        if isinstance(raw_clients, dict):
            def updated_at(item) -> float:
                try:
                    return float(
                        item[1].get("updated_at", 0)
                        if isinstance(item[1], dict)
                        else 0
                    )
                except (TypeError, ValueError):
                    return 0.0

            ordered_clients = sorted(
                raw_clients.items(),
                key=updated_at,
                reverse=True,
            )
            for digest, entry in ordered_clients:
                if len(clients) >= MAX_TRACKED_CLIENTS:
                    break
                if (
                    not isinstance(digest, str)
                    or len(digest) != 64
                    or not isinstance(entry, dict)
                ):
                    continue
                failures = _recent_failures(
                    entry.get("failures"), now, self.window_seconds
                )
                blocked_until = (
                    float(entry.get("blocked_until", 0) or 0)
                    if _remaining_seconds(entry.get("blocked_until"), now) > 0
                    else 0.0
                )
                if failures or blocked_until:
                    clients[digest] = {
                        "failures": failures,
                        "blocked_until": blocked_until,
                        "updated_at": float(entry.get("updated_at", now) or now),
                    }
        return {
            "version": 1,
            "failures": _recent_failures(
                source.get("failures"), now, self.window_seconds
            ),
            "blocked_until": (
                float(source.get("blocked_until", 0) or 0)
                if _remaining_seconds(source.get("blocked_until"), now) > 0
                else 0.0
            ),
            "clients": clients,
        }

    def evaluate(self, identity: str, *, matched: bool) -> dict[str, Any]:
        """Atomically enforce budgets and record one authentication result."""

        if not isinstance(identity, str) or not 1 <= len(identity) <= 1024:
            return {
                "allowed": False,
                "error_code": "invalid_client",
            }
        digest = self._identity_digest(identity)
        try:
            with _exclusive_file_lock(self.lock_path):
                now = float(self._clock())
                raw_state = _read_json(self.rate_path)
                if self.rate_path.exists() and raw_state is None:
                    # A truncated/tampered limiter file must never reset the
                    # attempt budget.  Recovery stays unavailable until the
                    # operator inspects or removes the damaged state.
                    return {
                        "allowed": False,
                        "error_code": "busy",
                        "retry_after": 1,
                    }
                state = self._normalize_state(raw_state, now)
                client = state["clients"].get(
                    digest,
                    {"failures": [], "blocked_until": 0.0, "updated_at": now},
                )
                if matched:
                    # Possessing the high-entropy operator key is authoritative.
                    # Clear anonymous failure counters instead of allowing them
                    # to become a denial-of-service switch against the operator.
                    _atomic_write_json(
                        self.rate_path,
                        {
                            "version": 1,
                            "failures": [],
                            "blocked_until": 0.0,
                            "clients": {},
                        },
                    )
                    return {"allowed": True}

                retry_after = max(
                    _remaining_seconds(state.get("blocked_until"), now),
                    _remaining_seconds(client.get("blocked_until"), now),
                )
                if retry_after > 0:
                    _atomic_write_json(self.rate_path, state)
                    return {
                        "allowed": False,
                        "error_code": "rate_limited",
                        "retry_after": retry_after,
                    }

                state["failures"].append(now)
                client["failures"].append(now)
                client["updated_at"] = now
                if len(client["failures"]) >= self.client_attempts:
                    client["blocked_until"] = now + self.lockout_seconds
                if len(state["failures"]) >= self.global_attempts:
                    state["blocked_until"] = now + self.lockout_seconds
                state["clients"][digest] = client
                _atomic_write_json(self.rate_path, state)
                retry_after = max(
                    _remaining_seconds(state.get("blocked_until"), now),
                    _remaining_seconds(client.get("blocked_until"), now),
                )
                if retry_after > 0:
                    return {
                        "allowed": False,
                        "error_code": "rate_limited",
                        "retry_after": retry_after,
                    }
                return {
                    "allowed": False,
                    "error_code": "invalid_operator_key",
                }
        except (OSError, PanelAccessBusy, ValueError, TypeError):
            return {
                "allowed": False,
                "error_code": "busy",
                "retry_after": 1,
            }


__all__ = [
    "DEFAULT_CLIENT_ATTEMPTS",
    "DEFAULT_GLOBAL_ATTEMPTS",
    "DEFAULT_LOCKOUT_SECONDS",
    "DEFAULT_WINDOW_SECONDS",
    "MAX_OPERATOR_KEY_BYTES",
    "MIN_OPERATOR_KEY_BYTES",
    "OperatorAdminRecoveryGuard",
    "RATE_FILENAME",
    "operator_key_matches",
    "valid_configured_operator_key",
]
