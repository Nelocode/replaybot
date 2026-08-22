from __future__ import annotations

import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from billing_entitlement import EntitlementGate, verify_entitlement_token


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _token(private_key, *, now: int, audience: str = "barcelona", allowed=True):
    header = {"alg": "EdDSA", "kid": "test-key", "typ": "ENTITLEMENT"}
    claims = {
        "iss": "chicas-lindas-billing-control-plane",
        "aud": audience,
        "account_id": "chicas-lindas",
        "plan_code": "six-bots-eur-250-monthly",
        "status": "active" if allowed else "suspended",
        "service_allowed": allowed,
        "paid_through": None,
        "grace_until": None,
        "override_until": None,
        "iat": now,
        "exp": now + 6 * 60 * 60,
        "jti": "opaque-test-id",
    }
    first = _b64url(json.dumps(header, sort_keys=True, separators=(",", ":")).encode())
    second = _b64url(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    signature = private_key.sign(f"{first}.{second}".encode("ascii"))
    return f"{first}.{second}.{_b64url(signature)}", claims


def _material():
    private_key = Ed25519PrivateKey.generate()
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_key, {"test-key": serialization.load_pem_public_key(public_pem)}


def test_verified_entitlement_controls_enforced_service(tmp_path):
    private_key, keys = _material()
    now = 2_000_000_000
    token, _claims = _token(private_key, now=now, allowed=True)
    gate = EntitlementGate(
        control_plane_url="https://billing.invalid",
        deployment_id="barcelona",
        deployment_token="d" * 48,
        account_id="chicas-lindas",
        public_keys=keys,
        cache_path=tmp_path / "entitlement.json",
        enforcement=True,
        now_fn=lambda: now,
    )
    gate._request_json = lambda *_args, **_kwargs: {"token": token}

    decision = gate.decision()

    assert decision["observed_allowed"] is True
    assert decision["service_allowed"] is True
    assert (tmp_path / "entitlement.json").is_file()


def test_enforcement_fails_closed_but_shadow_mode_remains_reversible(tmp_path):
    enforced = EntitlementGate(
        control_plane_url="",
        deployment_id="",
        deployment_token="",
        account_id="chicas-lindas",
        public_keys={},
        cache_path=tmp_path / "enforced.json",
        enforcement=True,
    )
    shadow = EntitlementGate(
        control_plane_url="",
        deployment_id="",
        deployment_token="",
        account_id="chicas-lindas",
        public_keys={},
        cache_path=tmp_path / "shadow.json",
        enforcement=False,
    )

    assert enforced.is_service_allowed() is False
    assert shadow.is_service_allowed() is True
    assert shadow.decision()["observed_allowed"] is False


def test_signed_cache_survives_control_plane_outage_until_expiry(tmp_path):
    private_key, keys = _material()
    current = [2_000_000_000]
    token, _claims = _token(private_key, now=current[0], allowed=True)
    cache_path = tmp_path / "entitlement.json"
    online = EntitlementGate(
        control_plane_url="https://billing.invalid",
        deployment_id="barcelona",
        deployment_token="d" * 48,
        account_id="chicas-lindas",
        public_keys=keys,
        cache_path=cache_path,
        enforcement=True,
        now_fn=lambda: current[0],
    )
    online._request_json = lambda *_args, **_kwargs: {"token": token}
    assert online.is_service_allowed() is True

    offline = EntitlementGate(
        control_plane_url="https://billing.invalid",
        deployment_id="barcelona",
        deployment_token="d" * 48,
        account_id="chicas-lindas",
        public_keys=keys,
        cache_path=cache_path,
        enforcement=True,
        now_fn=lambda: current[0],
    )
    offline._request_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError())
    assert offline.is_service_allowed() is True
    current[0] += 6 * 60 * 60 + 31
    assert offline.refresh(force=True) is False
    assert offline.is_service_allowed() is False


def test_token_rejects_wrong_audience_and_tampering():
    private_key, keys = _material()
    now = 2_000_000_000
    token, _claims = _token(private_key, now=now, audience="madrid")
    try:
        verify_entitlement_token(
            token,
            keys=keys,
            deployment_id="barcelona",
            account_id="chicas-lindas",
            now=now,
        )
    except ValueError as exc:
        assert "audience" in str(exc)
    else:
        raise AssertionError("wrong audience was accepted")

    parts = token.split(".")
    changed_signature = bytearray(_b64url_decode(parts[2]))
    changed_signature[0] ^= 0x01
    changed = f"{parts[0]}.{parts[1]}.{_b64url(bytes(changed_signature))}"
    try:
        verify_entitlement_token(
            changed,
            keys=keys,
            deployment_id="madrid",
            account_id="chicas-lindas",
            now=now,
        )
    except Exception:
        pass
    else:
        raise AssertionError("tampered token was accepted")
