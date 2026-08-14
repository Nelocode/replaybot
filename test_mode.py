"""Safe helpers for the panel's reversible conversation test mode."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any


def _read_object(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_object_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def load_test_mode(config_path: Path) -> bool:
    return _read_object(config_path).get("enabled") is True


def save_test_mode(config_path: Path, enabled: bool) -> None:
    _write_object_atomic(config_path, {"version": 1, "enabled": bool(enabled)})


def interaction_state_summary(state_path: Path) -> dict[str, int | float | None]:
    contacts = _read_object(state_path).get("contacts", {})
    if not isinstance(contacts, dict):
        contacts = {}
    updated_values = []
    for state in contacts.values():
        if not isinstance(state, dict):
            continue
        updated_at = state.get("updated_at")
        if isinstance(updated_at, (int, float)) and not isinstance(updated_at, bool):
            updated_values.append(float(updated_at))
    return {
        "conversation_count": len(contacts),
        "latest_updated_at": max(updated_values, default=None),
    }


def normalize_whatsapp_phone(value: object) -> str | None:
    """Return E.164 digits without retaining the operator's raw input."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > 32 or any(ord(char) < 32 for char in candidate):
        return None
    if not re.fullmatch(r"\+[1-9][0-9 ().-]*", candidate, flags=re.ASCII):
        return None
    digits = re.sub(r"[ ().-]", "", candidate[1:])
    if not 7 <= len(digits) <= 15:
        return None
    return digits


def _contact_fingerprint(value: str) -> str:
    return hashlib.sha256(f"contact\0{value}".encode("utf-8")).hexdigest()


def _backup_state(state_path: Path, *, channel: str, backup_dir: Path) -> Path | None:
    if not state_path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{channel}_interaction_state.previous.json"
    backup_temp = backup_path.with_suffix(backup_path.suffix + ".tmp")
    shutil.copyfile(state_path, backup_temp)
    try:
        os.chmod(backup_temp, 0o600)
    except OSError:
        pass
    os.replace(backup_temp, backup_path)
    return backup_path


def reset_latest_interaction(
    state_path: Path,
    *,
    channel: str,
    backup_dir: Path,
    language: str | None = None,
) -> dict[str, Any]:
    """Return the latest contact to phase zero and keep one rollback copy."""

    if channel not in {"telegram", "whatsapp"}:
        raise ValueError("channel must be telegram or whatsapp")
    if language not in {None, "es", "en", "fr"}:
        raise ValueError("language must be es, en, fr, or None")

    payload = _read_object(state_path)
    contacts = payload.get("contacts", {})
    if not isinstance(contacts, dict) or not contacts:
        return {"reset": False, "remaining": 0, "backup": None}

    def updated_at(item: tuple[str, Any]) -> float:
        state = item[1]
        value = state.get("updated_at", 0) if isinstance(state, dict) else 0
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return 0.0

    latest_key, _ = max(contacts.items(), key=updated_at)

    backup_path = _backup_state(state_path, channel=channel, backup_dir=backup_dir)

    previous = contacts[latest_key] if isinstance(contacts[latest_key], dict) else {}
    contacts[latest_key] = {
        "phase": 0,
        "language": language,
        "language_source": "operator_seed" if language else None,
        "language_candidate": None,
        "language_candidate_streak": 0,
        "recent_events": [],
        "updated_at": previous.get("updated_at", 0),
    }
    payload["contacts"] = contacts
    _write_object_atomic(state_path, payload)
    return {
        "reset": True,
        "remaining": len(contacts),
        "language": language,
        "backup": str(backup_path),
    }


def reset_whatsapp_interaction_by_number(
    state_path: Path,
    *,
    backup_dir: Path,
    phone: object,
    language: str | None = None,
) -> dict[str, Any]:
    """Prepare one WhatsApp client by hashed PN, without persisting the number.

    A valid number deliberately produces the same result whether its state was
    found or had to be preconfigured.  This prevents the panel endpoint from
    becoming a customer-number enumeration oracle.
    """

    if language not in {None, "es", "en", "fr"}:
        raise ValueError("language must be es, en, fr, or None")
    normalized_phone = normalize_whatsapp_phone(phone)
    if normalized_phone is None:
        raise ValueError("invalid WhatsApp phone")

    if state_path.exists():
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise OSError("interaction state could not be read") from error
        if not isinstance(payload, dict):
            raise OSError("interaction state root is invalid")
    else:
        payload = {}

    contacts = payload.get("contacts", {})
    aliases = payload.get("aliases", {})
    if not isinstance(contacts, dict) or not isinstance(aliases, dict):
        raise OSError("interaction state structure is invalid")

    identity_keys = [
        _contact_fingerprint(f"{normalized_phone}@s.whatsapp.net"),
        _contact_fingerprint(f"{normalized_phone}@hosted"),
    ]
    resolved_keys: list[str] = []
    for identity_key in identity_keys:
        target = aliases.get(identity_key, identity_key)
        if isinstance(target, str) and target in contacts and target not in resolved_keys:
            resolved_keys.append(target)
        if identity_key in contacts and identity_key not in resolved_keys:
            resolved_keys.append(identity_key)

    canonical_key = resolved_keys[0] if resolved_keys else identity_keys[0]
    duplicate_keys = set(resolved_keys[1:])
    for alias_key, target_key in list(aliases.items()):
        if target_key in duplicate_keys:
            aliases[alias_key] = canonical_key
    for duplicate_key in duplicate_keys:
        contacts.pop(duplicate_key, None)
    for identity_key in identity_keys:
        aliases[identity_key] = canonical_key

    backup_path = _backup_state(
        state_path,
        channel="whatsapp",
        backup_dir=backup_dir,
    )
    contacts[canonical_key] = {
        "phase": 0,
        "language": language,
        "language_source": "operator_seed" if language else None,
        "language_candidate": None,
        "language_candidate_streak": 0,
        "recent_events": [],
        "updated_at": 0,
        # The number can initially be unknown to a LID-only history.  This
        # bounded marker lets the JS state store make this reset win exactly
        # once when WhatsApp later presents PN and LID together.  It contains
        # no raw identifier and is removed by the first inbound interaction.
        "reset_pending": True,
    }
    payload["version"] = 2
    payload["contacts"] = contacts
    payload["aliases"] = aliases
    _write_object_atomic(state_path, payload)
    return {
        "reset": True,
        "language": language,
        "backup": str(backup_path) if backup_path else None,
    }
