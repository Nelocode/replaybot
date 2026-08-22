from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


SAFE_STATUSES = {"trialing", "active", "grace", "suspended", "canceled"}
EXPECTED_ISSUER = "chicas-lindas-billing-control-plane"


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _public_keys(raw: str | None) -> dict[str, Ed25519PublicKey]:
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("BILLING_SIGNING_PUBLIC_KEYS_JSON must be an object")
    result: dict[str, Ed25519PublicKey] = {}
    for key_id, encoded_pem in parsed.items():
        if not isinstance(key_id, str) or not isinstance(encoded_pem, str):
            raise ValueError("billing public key entries must be strings")
        loaded = serialization.load_pem_public_key(base64.b64decode(encoded_pem))
        if not isinstance(loaded, Ed25519PublicKey):
            raise ValueError("billing public keys must be Ed25519")
        result[key_id] = loaded
    return result


def verify_entitlement_token(
    token: str,
    *,
    keys: dict[str, Ed25519PublicKey],
    deployment_id: str,
    account_id: str,
    now: float | None = None,
) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("invalid token shape")
    header = json.loads(_b64url_decode(parts[0]))
    claims = json.loads(_b64url_decode(parts[1]))
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise ValueError("invalid token json")
    if header.get("alg") != "EdDSA" or header.get("typ") != "ENTITLEMENT":
        raise ValueError("invalid token header")
    key = keys.get(str(header.get("kid") or ""))
    if key is None:
        raise ValueError("unknown signing key")
    key.verify(
        _b64url_decode(parts[2]), f"{parts[0]}.{parts[1]}".encode("ascii")
    )
    current = time.time() if now is None else now
    if claims.get("iss") != EXPECTED_ISSUER:
        raise ValueError("invalid issuer")
    if claims.get("aud") != deployment_id or claims.get("account_id") != account_id:
        raise ValueError("invalid entitlement audience")
    if claims.get("status") not in SAFE_STATUSES:
        raise ValueError("invalid entitlement status")
    if not isinstance(claims.get("service_allowed"), bool):
        raise ValueError("invalid service decision")
    try:
        issued_at = int(claims["iat"])
        expires_at = int(claims["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid token time") from exc
    if issued_at > current + 60 or expires_at <= current - 30 or expires_at <= issued_at:
        raise ValueError("expired or future entitlement")
    return claims


class EntitlementGate:
    def __init__(
        self,
        *,
        control_plane_url: str,
        deployment_id: str,
        deployment_token: str,
        account_id: str,
        public_keys: dict[str, Ed25519PublicKey],
        cache_path: Path,
        enforcement: bool,
        allow_insecure_http: bool = False,
        refresh_seconds: int = 60,
        timeout_seconds: float = 3.0,
        now_fn=time.time,
    ):
        self.control_plane_url = control_plane_url.rstrip("/")
        self.deployment_id = deployment_id
        self.deployment_token = deployment_token
        self.account_id = account_id
        self.public_keys = public_keys
        self.cache_path = cache_path
        self.enforcement = enforcement
        self.allow_insecure_http = allow_insecure_http
        self.refresh_seconds = max(10, min(int(refresh_seconds), 300))
        self.timeout_seconds = max(0.5, min(float(timeout_seconds), 10.0))
        self.now_fn = now_fn
        self._lock = threading.Lock()
        self._claims: dict | None = None
        self._last_attempt = 0.0
        self._last_success = 0.0
        self._error_code: str | None = None
        self._load_cache()

    @classmethod
    def from_env(cls) -> "EntitlementGate":
        data_dir = Path(os.environ.get("BOT_DATA_DIR", Path(__file__).parent / "data"))
        deployment_id = os.environ.get("BILLING_DEPLOYMENT_ID", "").strip()
        cache_name = f"billing_entitlement_{deployment_id or 'unconfigured'}.json"
        try:
            keys = _public_keys(os.environ.get("BILLING_SIGNING_PUBLIC_KEYS_JSON"))
        except Exception:
            logging.error("Billing public key configuration is invalid")
            keys = {}
        return cls(
            control_plane_url=os.environ.get("BILLING_CONTROL_PLANE_URL", ""),
            deployment_id=deployment_id,
            deployment_token=os.environ.get("BILLING_DEPLOYMENT_TOKEN", ""),
            account_id=os.environ.get("BILLING_ACCOUNT_ID", "chicas-lindas"),
            public_keys=keys,
            cache_path=data_dir / cache_name,
            enforcement=_as_bool(os.environ.get("BILLING_ENFORCEMENT")),
            allow_insecure_http=_as_bool(
                os.environ.get("BILLING_ALLOW_INSECURE_HTTP")
            ),
            refresh_seconds=int(os.environ.get("BILLING_REFRESH_SECONDS", "60")),
            timeout_seconds=float(os.environ.get("BILLING_TIMEOUT_SECONDS", "3")),
        )

    @property
    def configured(self) -> bool:
        return bool(
            self.control_plane_url
            and self.deployment_id
            and len(self.deployment_token) >= 32
            and self.public_keys
        )

    def _validated_url(self, path: str) -> str:
        url = f"{self.control_plane_url}{path}"
        parsed = urllib.parse.urlparse(url)
        local_http = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (local_http or self.allow_insecure_http):
            raise ValueError("billing control plane must use HTTPS")
        return url

    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        token: str,
        actor: str | None = None,
        body: dict | None = None,
    ) -> dict:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "X-Deployment-ID": self.deployment_id,
        }
        if actor:
            headers["X-Billing-Admin-Actor"] = actor
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._validated_url(path), data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError("billing_http_error")
                parsed = json.loads(response.read(256 * 1024))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"billing_http_{exc.code}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("billing response must be an object")
        return parsed

    def _load_cache(self) -> None:
        try:
            parsed = json.loads(self.cache_path.read_text(encoding="utf-8"))
            claims = verify_entitlement_token(
                parsed["token"],
                keys=self.public_keys,
                deployment_id=self.deployment_id,
                account_id=self.account_id,
                now=self.now_fn(),
            )
            self._claims = claims
        except Exception:
            self._claims = None

    def _write_cache(self, token: str) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"token": token}, separators=(",", ":")), encoding="utf-8"
        )
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, self.cache_path)

    def refresh(self, *, force: bool = False) -> bool:
        current = self.now_fn()
        with self._lock:
            if not force and current - self._last_attempt < self.refresh_seconds:
                return self._claims is not None
            self._last_attempt = current
            if not self.configured:
                self._error_code = "not_configured"
                return False
            try:
                response = self._request_json(
                    "/v1/entitlement",
                    token=self.deployment_token,
                )
                token = str(response.get("token") or "")
                claims = verify_entitlement_token(
                    token,
                    keys=self.public_keys,
                    deployment_id=self.deployment_id,
                    account_id=self.account_id,
                    now=current,
                )
                self._write_cache(token)
                self._claims = claims
                self._last_success = current
                self._error_code = None
                return True
            except Exception as exc:
                self._error_code = type(exc).__name__.lower()
                try:
                    if self._claims:
                        cached = json.loads(self.cache_path.read_text(encoding="utf-8"))
                        verify_entitlement_token(
                            cached["token"],
                            keys=self.public_keys,
                            deployment_id=self.deployment_id,
                            account_id=self.account_id,
                            now=current,
                        )
                except Exception:
                    self._claims = None
                return self._claims is not None

    def decision(self) -> dict:
        self.refresh()
        current = self.now_fn()
        claims = self._claims
        observed_allowed = bool(
            claims
            and isinstance(claims.get("exp"), int)
            and claims["exp"] > current - 30
            and claims.get("service_allowed") is True
        )
        return {
            "configured": self.configured,
            "enforcement": self.enforcement,
            "observed_allowed": observed_allowed,
            "service_allowed": observed_allowed if self.enforcement else True,
            "status": claims.get("status") if claims else "unavailable",
            "paid_through": claims.get("paid_through") if claims else None,
            "grace_until": claims.get("grace_until") if claims else None,
            "override_until": claims.get("override_until") if claims else None,
            "token_expires_at": claims.get("exp") if claims else None,
            "last_refresh_at": int(self._last_success) if self._last_success else None,
            "error_code": self._error_code,
        }

    def is_service_allowed(self) -> bool:
        return self.decision()["service_allowed"] is True

    async def is_service_allowed_async(self) -> bool:
        return await asyncio.to_thread(self.is_service_allowed)

    def _admin_request(self, path: str, *, body: dict | None = None) -> dict:
        admin_token = os.environ.get("BILLING_CONTROL_PLANE_ADMIN_TOKEN", "")
        if len(admin_token) < 32:
            raise RuntimeError("billing_admin_not_configured")
        return self._request_json(
            path,
            method="POST",
            token=admin_token,
            actor=f"{self.deployment_id}-panel",
            body=body,
        )

    def billing_status(self) -> dict:
        admin_token = os.environ.get("BILLING_CONTROL_PLANE_ADMIN_TOKEN", "")
        if len(admin_token) < 32:
            return {"ok": False, "error_code": "billing_admin_not_configured"}
        try:
            return self._request_json("/v1/billing/status", token=admin_token)
        except Exception as exc:
            return {"ok": False, "error_code": type(exc).__name__.lower()}

    def create_checkout(
        self, *, method_id: str = "", payer: dict | None = None
    ) -> dict:
        body: dict = {}
        if method_id:
            body["method_id"] = method_id
        if payer:
            body["payer"] = payer
        return self._admin_request("/v1/checkout-sessions", body=body)

    def create_portal(self) -> dict:
        return self._admin_request("/v1/customer-portal-sessions")

    def create_override(self, *, reason: str, duration_seconds: int) -> dict:
        return self._admin_request(
            "/v1/admin/overrides",
            body={"reason": reason, "duration_seconds": duration_seconds},
        )


billing_gate = EntitlementGate.from_env()
