"""Private Telegram delivery for WhatsApp relink incidents."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


class RelinkConfigurationError(RuntimeError):
    pass


class RelinkNotificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class WhatsAppRelinkConfig:
    enabled: bool
    public_base_url: str
    telegram_chat_id: str
    telegram_bot_token: str
    service_name: str
    token_source: str

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None):
        values = environ if environ is not None else os.environ
        enabled = values.get("WA_RELINK_ENABLED") == "1"
        base_url = (values.get("WA_RELINK_PUBLIC_BASE_URL") or "").strip().rstrip("/")
        chat_id = (values.get("WA_RELINK_TELEGRAM_CHAT_ID") or "").strip()
        dedicated = (values.get("WA_RELINK_TELEGRAM_BOT_TOKEN") or "").strip()
        fallback = (values.get("AUTOREPLY_BOT_TOKEN") or "").strip()
        service_name = (values.get("WA_RELINK_SERVICE_NAME") or "WhatsApp").strip()
        token = dedicated or fallback
        token_source = "dedicated" if dedicated else ("autoreply_fallback" if fallback else "missing")
        config = cls(enabled, base_url, chat_id, token, service_name, token_source)
        if enabled:
            config.validate()
        return config

    def validate(self) -> None:
        parsed = urlsplit(self.public_base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise RelinkConfigurationError("invalid_public_base_url")
        if not re.fullmatch(r"-?[0-9]{1,24}", self.telegram_chat_id):
            raise RelinkConfigurationError("invalid_telegram_chat_id")
        if not self.telegram_bot_token or len(self.telegram_bot_token) > 256:
            raise RelinkConfigurationError("missing_telegram_bot_token")
        if not self.service_name or len(self.service_name) > 80 or any(
            character in self.service_name for character in "\r\n\0"
        ):
            raise RelinkConfigurationError("invalid_service_name")

    def recovery_url(self, token: str) -> str:
        return f"{self.public_base_url}/wa-relink#{token}"


class TelegramRelinkNotifier:
    def __init__(self, config: WhatsAppRelinkConfig, *, opener=urlopen, timeout_seconds=10):
        config.validate()
        self.config = config
        self._opener = opener
        self.timeout_seconds = max(1, int(timeout_seconds))

    def _send(self, text: str) -> None:
        payload = json.dumps(
            {
                "chat_id": self.config.telegram_chat_id,
                "text": text,
                "disable_web_page_preview": True,
                "link_preview_options": {"is_disabled": True},
            },
            separators=(",", ":"),
        ).encode("utf-8")
        request = Request(
            "https://api.telegram.org/"
            f"bot{quote(self.config.telegram_bot_token, safe='')}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read(64 * 1024)
            result = json.loads(raw.decode("utf-8"))
        except Exception:
            # Never propagate transport text: URLs can contain the bot token.
            raise RelinkNotificationError("telegram_delivery_failed") from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RelinkNotificationError("telegram_delivery_rejected")

    def send_relink_alert(self, recovery_url: str) -> None:
        self._send(
            f"{self.config.service_name}: WhatsApp necesita volver a vincularse. "
            f"Abre este enlace privado y escanea el QR: {recovery_url}"
        )

    def send_online(self) -> None:
        self._send(f"{self.config.service_name}: WhatsApp está nuevamente en línea.")
