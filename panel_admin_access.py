"""One-time access codes for authorizing a browser to manage the panel.

The web process owns challenge creation and verification.  The already-running
Telegram user bot only consumes ``outbox.json`` and sends its message to
``Saved Messages``.  Keeping those responsibilities separate means the web
process never opens the live Telethon session and the Telegram worker never
needs the Flask/session secret.

All public results deliberately omit the OTP, its digest and the browser-token
digest.  The raw OTP exists on disk only in ``outbox.json`` and is removed as
soon as Telegram confirms delivery.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import contextmanager
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any

try:  # POSIX advisory locks
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - selected only on Windows
    _fcntl = None

try:  # Windows advisory locks
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - selected only on POSIX
    _msvcrt = None


DEFAULT_TTL_SECONDS = 180
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_COOLDOWN_SECONDS = 60
DEFAULT_DELIVERY_ATTEMPTS = 3
DEFAULT_DELIVERY_TIMEOUT_SECONDS = 10.0
DEFAULT_DELIVERY_RETRY_DELAY_SECONDS = 1.0
MAX_TRACKED_BROWSERS = 128

CHALLENGE_FILENAME = "challenge.json"
OUTBOX_FILENAME = "outbox.json"
DELIVERY_STATUS_FILENAME = "delivery_status.json"
RATE_FILENAME = "rate.json"

_OTP_PATTERN = re.compile(r"^[0-9]{8}$")
_HEX_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class PanelAccessBusy(RuntimeError):
    """A short-lived filesystem lock is currently owned by another process."""


def _secure_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write one private JSON document and atomically replace its destination."""

    _secure_directory(path.parent)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = None
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _request_matches(payload: dict[str, Any] | None, request_id: str) -> bool:
    return bool(
        isinstance(payload, dict)
        and isinstance(payload.get("request_id"), str)
        and secrets.compare_digest(payload["request_id"], request_id)
    )


def _unlink_matching(path: Path, request_id: str) -> None:
    if _request_matches(_read_json(path), request_id):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def _exclusive_file_lock(
    lock_path: Path,
    *,
    wait_seconds: float = 1.0,
):
    """Hold a kernel advisory lock without deleting its shared lock file.

    The descriptor remains open throughout the critical section.  A crashed
    process releases the kernel lock automatically, eliminating stale-lease
    cleanup and the release-time race where an owner could unlink a lock that
    had already been replaced by another process.
    """

    _secure_directory(lock_path.parent)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
            os.fsync(descriptor)

        deadline = time.monotonic() + max(0.0, wait_seconds)
        while not locked:
            locked = _try_advisory_lock(descriptor)
            if locked:
                break
            if time.monotonic() >= deadline:
                raise PanelAccessBusy("panel access state is busy")
            time.sleep(0.01)
        yield
    finally:
        if locked:
            _release_advisory_lock(descriptor)
        os.close(descriptor)


def _try_advisory_lock(descriptor: int) -> bool:
    """Try once using the native lock backend selected for this platform."""

    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        if _msvcrt is None:  # pragma: no cover - unsupported Python runtime
            raise RuntimeError("Windows advisory locking is unavailable")
        try:
            _msvcrt.locking(descriptor, _msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    if _fcntl is None:  # pragma: no cover - unsupported Python runtime
        raise RuntimeError("POSIX advisory locking is unavailable")
    try:
        _fcntl.flock(descriptor, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        return True
    except (BlockingIOError, OSError):
        return False


def _release_advisory_lock(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        if _msvcrt is not None:
            try:
                _msvcrt.locking(descriptor, _msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        return
    if _fcntl is not None:
        try:
            _fcntl.flock(descriptor, _fcntl.LOCK_UN)
        except OSError:
            pass


def _remaining_seconds(expires_at: Any, now: float) -> int:
    try:
        return max(0, int(math.ceil(float(expires_at) - now)))
    except (TypeError, ValueError):
        return 0


def _valid_browser_token(browser_token: Any) -> bool:
    return isinstance(browser_token, str) and 16 <= len(browser_token) <= 512


def _safe_request_id(payload: dict[str, Any] | None) -> str | None:
    request_id = payload.get("request_id") if isinstance(payload, dict) else None
    if not isinstance(request_id, str) or not 16 <= len(request_id) <= 256:
        return None
    return request_id


class PanelAdminAccessStore:
    """Create one global OTP with independent attempt budgets per browser."""

    def __init__(
        self,
        storage_dir: str | Path,
        secret: str | bytes,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.time,
        code_factory: Callable[[], str] | None = None,
        request_id_factory: Callable[[], str] | None = None,
    ) -> None:
        secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
        if not isinstance(secret_bytes, bytes) or len(secret_bytes) < 32:
            raise ValueError("secret must contain at least 32 bytes")
        if ttl_seconds <= 0 or max_attempts <= 0 or cooldown_seconds < 0:
            raise ValueError("invalid panel access timing or attempt limits")

        self.storage_dir = Path(storage_dir)
        self._secret = secret_bytes
        self.ttl_seconds = int(ttl_seconds)
        self.max_attempts = int(max_attempts)
        self.cooldown_seconds = int(cooldown_seconds)
        self._clock = clock
        self._code_factory = code_factory or (
            lambda: f"{secrets.randbelow(100_000_000):08d}"
        )
        self._request_id_factory = request_id_factory or (
            lambda: secrets.token_urlsafe(24)
        )
        self._thread_lock = threading.RLock()
        _secure_directory(self.storage_dir)

    @property
    def challenge_path(self) -> Path:
        return self.storage_dir / CHALLENGE_FILENAME

    @property
    def outbox_path(self) -> Path:
        return self.storage_dir / OUTBOX_FILENAME

    @property
    def delivery_status_path(self) -> Path:
        return self.storage_dir / DELIVERY_STATUS_FILENAME

    @property
    def rate_path(self) -> Path:
        return self.storage_dir / RATE_FILENAME

    @property
    def _state_lock_path(self) -> Path:
        return self.storage_dir / ".state.lock"

    def _browser_digest(self, browser_token: str) -> str:
        return hmac.new(
            self._secret,
            b"panel-browser-token:v1\0" + browser_token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _code_digest(
        self,
        request_id: str,
        code: str,
    ) -> str:
        material = (
            "panel-admin-code:v1\0"
            + request_id
            + "\0"
            + code
        ).encode("utf-8")
        return hmac.new(self._secret, material, hashlib.sha256).hexdigest()

    def _active_challenge(
        self,
        *,
        now: float,
    ) -> tuple[dict[str, Any] | None, str]:
        challenge = _read_json(self.challenge_path)
        request_id = _safe_request_id(challenge)
        if not challenge or not request_id:
            return None, "missing"
        if _remaining_seconds(challenge.get("expires_at"), now) <= 0:
            self._cleanup_request(request_id)
            return None, "expired"
        return challenge, "active"

    def _browser_attempts_remaining(
        self,
        challenge: dict[str, Any],
        browser_token: str,
    ) -> int:
        attempts = self._normalized_browser_attempts(challenge)
        browser_digest = self._browser_digest(browser_token)
        try:
            return max(
                0,
                min(self.max_attempts, int(attempts.get(browser_digest, self.max_attempts))),
            )
        except (TypeError, ValueError):
            return self.max_attempts

    def _normalized_browser_attempts(
        self,
        challenge: dict[str, Any],
    ) -> dict[str, int]:
        raw_attempts = challenge.get("browser_attempts")
        if not isinstance(raw_attempts, dict):
            return {}
        normalized: dict[str, int] = {}
        for digest, remaining in raw_attempts.items():
            if len(normalized) >= MAX_TRACKED_BROWSERS:
                break
            if not isinstance(digest, str) or not _HEX_DIGEST_PATTERN.fullmatch(digest):
                continue
            try:
                normalized[digest] = max(
                    0,
                    min(self.max_attempts, int(remaining)),
                )
            except (TypeError, ValueError):
                continue
        return normalized

    def _cleanup_request(self, request_id: str) -> None:
        _unlink_matching(self.outbox_path, request_id)
        _unlink_matching(self.delivery_status_path, request_id)
        _unlink_matching(self.challenge_path, request_id)

    def _cleanup_orphan_delivery_files(self) -> None:
        if _safe_request_id(_read_json(self.challenge_path)) is not None:
            return
        for path in (self.outbox_path, self.delivery_status_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _public_status(
        self,
        challenge: dict[str, Any],
        now: float,
        browser_token: str,
    ) -> dict[str, Any]:
        request_id = challenge["request_id"]
        delivery = _read_json(self.delivery_status_path)
        delivery_state = (
            delivery.get("state")
            if _request_matches(delivery, request_id)
            else None
        )
        if delivery_state == "sent":
            state = "sent"
        elif delivery_state == "failed":
            state = "delivery_failed"
        elif delivery_state == "retrying":
            state = "delivery_retrying"
        else:
            state = "queued"
        return {
            "ok": True,
            "state": state,
            "request_id": request_id,
            "delivered": delivery_state == "sent",
            "expires_in": _remaining_seconds(challenge.get("expires_at"), now),
            "attempts_remaining": self._browser_attempts_remaining(
                challenge,
                browser_token,
            ),
        }

    def create_challenge(self, browser_token: str) -> dict[str, Any]:
        """Create one account-level OTP or reuse the existing global OTP."""

        if not _valid_browser_token(browser_token):
            return {
                "ok": False,
                "state": "invalid",
                "error_code": "invalid_browser_token",
                "error": "No fue posible identificar de forma segura este navegador.",
            }

        try:
            with self._thread_lock, _exclusive_file_lock(self._state_lock_path):
                now = float(self._clock())
                rate = _read_json(self.rate_path) or {}
                retry_after = _remaining_seconds(rate.get("cooldown_until"), now)
                existing = _read_json(self.challenge_path)
                existing_id = _safe_request_id(existing)
                if existing and existing_id:
                    if _remaining_seconds(existing.get("expires_at"), now) <= 0:
                        self._cleanup_request(existing_id)
                    else:
                        result = self._public_status(existing, now, browser_token)
                        if result["state"] == "delivery_failed" and retry_after <= 0:
                            # This OTP never reached Telegram and therefore cannot
                            # authorize anyone. It is the sole safe replacement
                            # exception to the global active-challenge rule.
                            self._cleanup_request(existing_id)
                        else:
                            if result["state"] == "delivery_failed":
                                result["retry_after"] = retry_after
                            result["request_in_progress"] = True
                            result["reused"] = True
                            return result
                else:
                    self._cleanup_orphan_delivery_files()

                if retry_after > 0:
                    return {
                        "ok": False,
                        "state": "rate_limited",
                        "error_code": "rate_limited",
                        "retry_after": retry_after,
                        "error": "Espera antes de solicitar otro código.",
                    }

                code = self._code_factory()
                request_id = self._request_id_factory()
                if not isinstance(code, str) or not _OTP_PATTERN.fullmatch(code):
                    raise ValueError("code_factory must return exactly eight digits")
                if not isinstance(request_id, str) or not 16 <= len(request_id) <= 256:
                    raise ValueError("request_id_factory returned an invalid identifier")

                expires_at = now + self.ttl_seconds
                challenge = {
                    "version": 1,
                    "request_id": request_id,
                    "code_digest": self._code_digest(request_id, code),
                    "created_at": now,
                    "expires_at": expires_at,
                    "browser_attempts": {},
                }
                outbox = {
                    "version": 1,
                    "request_id": request_id,
                    "code": code,
                    "created_at": now,
                    "expires_at": expires_at,
                }
                rate_payload = {
                    "version": 1,
                    "last_request_at": now,
                    "cooldown_until": now + self.cooldown_seconds,
                }

                try:
                    _atomic_write_json(self.challenge_path, challenge)
                    _atomic_write_json(self.outbox_path, outbox)
                    _atomic_write_json(self.rate_path, rate_payload)
                except OSError:
                    self._cleanup_request(request_id)
                    return {
                        "ok": False,
                        "state": "storage_error",
                        "error_code": "storage_error",
                        "error": "No fue posible preparar el código de acceso.",
                    }

                return {
                    "ok": True,
                    "state": "queued",
                    "request_id": request_id,
                    "delivered": False,
                    "expires_in": self.ttl_seconds,
                    "attempts_remaining": self.max_attempts,
                    "request_in_progress": False,
                    "reused": False,
                }
        except PanelAccessBusy:
            return {
                "ok": False,
                "state": "busy",
                "error_code": "busy",
                "retry_after": 1,
                "error": "La autorización está ocupada. Intenta nuevamente.",
            }

    def get_status(self, browser_token: str) -> dict[str, Any]:
        """Return global delivery state plus this browser's attempt budget."""

        if not _valid_browser_token(browser_token):
            return {
                "ok": False,
                "state": "missing",
                "error_code": "no_active_challenge",
            }
        try:
            with self._thread_lock, _exclusive_file_lock(self._state_lock_path):
                now = float(self._clock())
                challenge, challenge_state = self._active_challenge(now=now)
                if not challenge:
                    return {
                        "ok": False,
                        "state": (
                            challenge_state
                            if challenge_state == "expired"
                            else "missing"
                        ),
                        "error_code": (
                            "expired_code"
                            if challenge_state == "expired"
                            else "no_active_challenge"
                        ),
                    }
                return self._public_status(challenge, now, browser_token)
        except PanelAccessBusy:
            return {
                "ok": False,
                "state": "busy",
                "error_code": "busy",
                "retry_after": 1,
            }

    def verify(self, browser_token: str, code: Any) -> dict[str, Any]:
        """Verify the global OTP with a failure budget local to this browser."""

        if not _valid_browser_token(browser_token):
            return {
                "ok": False,
                "authorized": False,
                "state": "missing",
                "error_code": "no_active_challenge",
            }
        try:
            with self._thread_lock, _exclusive_file_lock(self._state_lock_path):
                now = float(self._clock())
                challenge, challenge_state = self._active_challenge(now=now)
                if not challenge:
                    return {
                        "ok": False,
                        "authorized": False,
                        "state": (
                            challenge_state
                            if challenge_state == "expired"
                            else "missing"
                        ),
                        "error_code": (
                            "expired_code"
                            if challenge_state == "expired"
                            else "no_active_challenge"
                        ),
                    }

                request_id = challenge["request_id"]
                browser_digest = self._browser_digest(browser_token)
                attempts = self._normalized_browser_attempts(challenge)
                remaining = self._browser_attempts_remaining(
                    challenge,
                    browser_token,
                )
                if remaining <= 0:
                    return {
                        "ok": False,
                        "authorized": False,
                        "state": "locked",
                        "error_code": "attempts_exhausted",
                        "attempts_remaining": 0,
                        "error": "Este navegador agotó sus intentos para el código actual.",
                    }

                delivery = _read_json(self.delivery_status_path)
                if not (
                    _request_matches(delivery, request_id)
                    and delivery.get("state") == "sent"
                ):
                    return {
                        "ok": False,
                        "authorized": False,
                        "state": "not_delivered",
                        "error_code": "not_delivered",
                        "error": "El código todavía no ha sido entregado por Telegram.",
                    }

                supplied_code = code if isinstance(code, str) else ""
                valid_format = bool(_OTP_PATTERN.fullmatch(supplied_code))
                # Never feed attacker-controlled, unbounded input into the HMAC.
                # A fixed dummy preserves the same comparison path for malformed
                # and well-formed-but-incorrect codes.
                digest_input = supplied_code if valid_format else "00000000"
                supplied_digest = self._code_digest(request_id, digest_input)
                expected_digest = challenge.get("code_digest") or ""
                valid_digest = bool(
                    isinstance(expected_digest, str)
                    and _HEX_DIGEST_PATTERN.fullmatch(expected_digest)
                    and secrets.compare_digest(expected_digest, supplied_digest)
                )
                if not (valid_format and valid_digest):
                    if (
                        browser_digest not in attempts
                        and len(attempts) >= MAX_TRACKED_BROWSERS
                    ):
                        return {
                            "ok": False,
                            "authorized": False,
                            "state": "locked",
                            "error_code": "attempts_exhausted",
                            "attempts_remaining": 0,
                            "error": "No se admiten más intentos desde navegadores nuevos para el código actual.",
                        }
                    remaining = max(0, remaining - 1)
                    attempts[browser_digest] = remaining
                    challenge["browser_attempts"] = attempts
                    _atomic_write_json(self.challenge_path, challenge)
                    if remaining <= 0:
                        return {
                            "ok": False,
                            "authorized": False,
                            "state": "locked",
                            "error_code": "attempts_exhausted",
                            "attempts_remaining": 0,
                            "error": "Este navegador agotó sus intentos para el código actual.",
                        }
                    return {
                        "ok": False,
                        "authorized": False,
                        "state": "invalid_code",
                        "error_code": "invalid_code",
                        "attempts_remaining": remaining,
                        "error": "El código no es válido.",
                    }

                consumed = self.storage_dir / (
                    f".challenge.{request_id}.{secrets.token_hex(6)}.consumed"
                )
                try:
                    os.replace(self.challenge_path, consumed)
                except OSError:
                    return {
                        "ok": False,
                        "authorized": False,
                        "state": "missing",
                        "error_code": "no_active_challenge",
                    }
                try:
                    _unlink_matching(self.outbox_path, request_id)
                    _unlink_matching(self.delivery_status_path, request_id)
                finally:
                    try:
                        consumed.unlink(missing_ok=True)
                    except OSError:
                        pass
                return {
                    "ok": True,
                    "authorized": True,
                    "state": "verified",
                }
        except (OSError, PanelAccessBusy):
            return {
                "ok": False,
                "authorized": False,
                "state": "busy",
                "error_code": "busy",
                "retry_after": 1,
                "error": "No fue posible verificar el código en este momento.",
            }

    def cancel(self, browser_token: str) -> dict[str, Any]:
        """Cancel this browser's UI flow without invalidating the global OTP."""

        if not _valid_browser_token(browser_token):
            return {
                "ok": False,
                "state": "missing",
                "error_code": "no_active_challenge",
            }
        try:
            with self._thread_lock, _exclusive_file_lock(self._state_lock_path):
                challenge, challenge_state = self._active_challenge(
                    now=float(self._clock()),
                )
                if not challenge:
                    return {
                        "ok": False,
                        "state": (
                            challenge_state
                            if challenge_state == "expired"
                            else "missing"
                        ),
                        "error_code": (
                            "expired_code"
                            if challenge_state == "expired"
                            else "no_active_challenge"
                        ),
                    }
                return {
                    "ok": True,
                    "state": "cancelled",
                    "global_challenge_preserved": True,
                }
        except PanelAccessBusy:
            return {
                "ok": False,
                "state": "busy",
                "error_code": "busy",
                "retry_after": 1,
            }


def _default_message(code: str, expires_in: int) -> str:
    minutes = max(1, int(math.ceil(expires_in / 60)))
    return (
        f"🔐 Código de acceso al panel Bot AutoReply: {code}\n\n"
        f"Vence en {minutes} minutos. No lo compartas. "
        "Si no lo solicitaste, ignora este mensaje."
    )


async def deliver_pending_challenge(
    client: Any,
    store_or_directory: PanelAdminAccessStore | str | Path,
    *,
    max_attempts: int = DEFAULT_DELIVERY_ATTEMPTS,
    send_timeout_seconds: float = DEFAULT_DELIVERY_TIMEOUT_SECONDS,
    retry_delay_seconds: float = DEFAULT_DELIVERY_RETRY_DELAY_SECONDS,
    clock: Callable[[], float] = time.time,
    message_builder: Callable[[str, int], str] | None = None,
) -> dict[str, Any]:
    """Deliver one queued OTP through the already-connected Telegram client.

    ``client`` is expected to be the live Telethon client owned by ``bot.py``.
    This helper never creates or disconnects a Telegram client.  Its result and
    delivery-status file contain no message text, OTP or exception details.
    """

    directory = (
        store_or_directory.storage_dir
        if isinstance(store_or_directory, PanelAdminAccessStore)
        else Path(store_or_directory)
    )
    _secure_directory(directory)
    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")
    lock_path = directory / ".delivery.lock"
    outbox_path = directory / OUTBOX_FILENAME
    challenge_path = directory / CHALLENGE_FILENAME
    delivery_path = directory / DELIVERY_STATUS_FILENAME

    try:
        lock_context = _exclusive_file_lock(
            lock_path,
            wait_seconds=0.0,
        )
        with lock_context:
            now = float(clock())
            outbox = _read_json(outbox_path)
            challenge = _read_json(challenge_path)
            request_id = _safe_request_id(outbox)
            if (
                not outbox
                or not request_id
                or not _request_matches(challenge, request_id)
            ):
                return {
                    "ok": False,
                    "state": "idle",
                    "error_code": "no_pending_delivery",
                }
            if _remaining_seconds(outbox.get("expires_at"), now) <= 0:
                _unlink_matching(outbox_path, request_id)
                return {
                    "ok": False,
                    "state": "expired",
                    "error_code": "expired_code",
                }

            code = outbox.get("code")
            if not isinstance(code, str) or not _OTP_PATTERN.fullmatch(code):
                _unlink_matching(outbox_path, request_id)
                return {
                    "ok": False,
                    "state": "invalid_outbox",
                    "error_code": "invalid_outbox",
                }

            previous = _read_json(delivery_path)
            attempts_so_far = 0
            if _request_matches(previous, request_id):
                if previous.get("state") == "sent":
                    _unlink_matching(outbox_path, request_id)
                    return {
                        "ok": True,
                        "state": "sent",
                        "already_delivered": True,
                        "attempts": int(previous.get("attempts", 0) or 0),
                    }
                attempts_so_far = max(0, int(previous.get("attempts", 0) or 0))
            if attempts_so_far >= max_attempts:
                _unlink_matching(outbox_path, request_id)
                return {
                    "ok": False,
                    "state": "delivery_failed",
                    "error_code": "delivery_failed",
                    "attempts": attempts_so_far,
                    "already_failed": True,
                }

            expires_in = _remaining_seconds(outbox.get("expires_at"), now)
            builder = message_builder or _default_message
            message = builder(code, expires_in)
            if not isinstance(message, str) or not message.strip():
                return {
                    "ok": False,
                    "state": "delivery_failed",
                    "error_code": "delivery_failed",
                    "attempts": attempts_so_far,
                }

            last_attempt = attempts_so_far
            while last_attempt < max_attempts:
                last_attempt += 1
                try:
                    await asyncio.wait_for(
                        client.send_message("me", message),
                        timeout=send_timeout_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    state = "failed" if last_attempt >= max_attempts else "retrying"
                    _atomic_write_json(
                        delivery_path,
                        {
                            "version": 1,
                            "request_id": request_id,
                            "state": state,
                            "attempts": last_attempt,
                            "updated_at": float(clock()),
                        },
                    )
                    if state == "failed":
                        _unlink_matching(outbox_path, request_id)
                    if last_attempt < max_attempts:
                        await asyncio.sleep(max(0.0, retry_delay_seconds))
                    continue

                _atomic_write_json(
                    delivery_path,
                    {
                        "version": 1,
                        "request_id": request_id,
                        "state": "sent",
                        "attempts": last_attempt,
                        "delivered_at": float(clock()),
                    },
                )
                _unlink_matching(outbox_path, request_id)
                return {
                    "ok": True,
                    "state": "sent",
                    "already_delivered": False,
                    "attempts": last_attempt,
                }

            return {
                "ok": False,
                "state": "delivery_failed",
                "error_code": "delivery_failed",
                "attempts": last_attempt,
                "already_failed": False,
            }
    except PanelAccessBusy:
        return {
            "ok": False,
            "state": "busy",
            "error_code": "delivery_busy",
            "retry_after": 1,
        }


__all__ = [
    "CHALLENGE_FILENAME",
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_DELIVERY_ATTEMPTS",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_TTL_SECONDS",
    "DELIVERY_STATUS_FILENAME",
    "OUTBOX_FILENAME",
    "PanelAdminAccessStore",
    "RATE_FILENAME",
    "deliver_pending_challenge",
]
