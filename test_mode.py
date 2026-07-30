"""Safe helpers for the panel's reversible conversation test mode."""

from __future__ import annotations

import json
import os
from pathlib import Path
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

    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{channel}_interaction_state.previous.json"
    backup_temp = backup_path.with_suffix(backup_path.suffix + ".tmp")
    shutil.copyfile(state_path, backup_temp)
    try:
        os.chmod(backup_temp, 0o600)
    except OSError:
        pass
    os.replace(backup_temp, backup_path)

    previous = contacts[latest_key] if isinstance(contacts[latest_key], dict) else {}
    contacts[latest_key] = {
        "phase": 0,
        "language": language,
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
