import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from wa_relink_incident import (
    RelinkStateCorrupt,
    WhatsAppRelinkIncidentStore,
    relink_capability_digest,
)
from wa_relink_notifier import (
    RelinkConfigurationError,
    RelinkNotificationError,
    TelegramRelinkNotifier,
    WhatsAppRelinkConfig,
)


class Clock:
    def __init__(self):
        self.value = 1_000.0

    def __call__(self):
        return self.value


class WhatsAppRelinkIncidentTests(unittest.TestCase):
    SECRET = "s" * 64
    INCIDENT_ID = "a" * 32

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.clock = Clock()
        self.store = WhatsAppRelinkIncidentStore(
            Path(self.temporary.name) / "relink",
            self.SECRET,
            clock=self.clock,
            id_factory=lambda: self.INCIDENT_ID,
        )

    def test_incident_is_deduplicated_and_disk_is_hash_only(self):
        first, created = self.store.ensure_outage("b" * 24, "logged_out")
        second, repeated = self.store.ensure_outage("c" * 24, "session_invalid")
        token = self.store.token_for(first)
        persisted = self.store.path.read_text(encoding="utf-8")

        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(first["incident_id"], second["incident_id"])
        self.assertNotIn(token, persisted)
        self.assertNotIn("secret", persisted)
        self.assertEqual(64, len(json.loads(persisted)["token_hash"]))

    def test_corrupt_state_blocks_without_replacement(self):
        self.store.directory.mkdir(parents=True)
        self.store.path.write_text("{broken", encoding="utf-8")
        before = self.store.path.read_bytes()

        with self.assertRaises(RelinkStateCorrupt):
            self.store.ensure_outage("b" * 24, "logged_out")

        self.assertEqual(before, self.store.path.read_bytes())

    def test_retry_uses_same_link_and_lifecycle_gates_delivery(self):
        state, _ = self.store.ensure_outage("b" * 24, "logged_out")
        token = self.store.token_for(state)
        claimed, first_claim = self.store.claim_notification("alert")
        self.assertIsNotNone(first_claim)
        self.assertIsNone(self.store.claim_notification("alert")[1])
        self.store.finish_notification(
            state["incident_id"],
            "alert",
            first_claim,
            False,
        )
        self.assertIsNone(self.store.claim_notification("alert")[1])
        self.clock.value += 5
        retried, retry_claim = self.store.claim_notification("alert")
        self.assertIsNotNone(retry_claim)
        self.assertEqual(token, self.store.token_for(retried))

        self.store.consume_token(token, relink_capability_digest("capability"))
        self.assertIsNone(self.store.claim_notification("alert")[1])
        self.assertIsNone(self.store.claim_notification("online")[1])
        self.store.resolve()
        self.assertIsNotNone(self.store.claim_notification("online")[1])

    def test_notification_claim_is_atomic_across_threads(self):
        self.store.ensure_outage("b" * 24, "logged_out")
        barrier = threading.Barrier(3)

        def claim():
            barrier.wait()
            return self.store.claim_notification("alert")[1]

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(claim) for _ in range(2)]
            barrier.wait()
            claims = [future.result() for future in futures]

        self.assertEqual(1, sum(claim_id is not None for claim_id in claims))

    def test_config_requires_https_and_explicit_destination_with_fallback(self):
        config = WhatsAppRelinkConfig.from_environment({
            "WA_RELINK_ENABLED": "1",
            "WA_RELINK_PUBLIC_BASE_URL": "https://panel.example",
            "WA_RELINK_TELEGRAM_CHAT_ID": "-1234",
            "AUTOREPLY_BOT_TOKEN": "fallback-token",
            "WA_RELINK_SERVICE_NAME": "Madrid",
        })
        self.assertEqual("autoreply_fallback", config.token_source)
        self.assertTrue(config.recovery_url("token").endswith("/wa-relink#token"))

        with self.assertRaises(RelinkConfigurationError):
            WhatsAppRelinkConfig.from_environment({
                "WA_RELINK_ENABLED": "1",
                "WA_RELINK_PUBLIC_BASE_URL": "http://panel.example",
                "WA_RELINK_TELEGRAM_CHAT_ID": "-1234",
                "AUTOREPLY_BOT_TOKEN": "fallback-token",
            })

    def test_notifier_payload_is_private_and_transport_error_is_scrubbed(self):
        config = WhatsAppRelinkConfig(
            enabled=True,
            public_base_url="https://panel.example",
            telegram_chat_id="-1234",
            telegram_bot_token="123456:dedicated-secret",
            service_name="Madrid",
            token_source="dedicated",
        )
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return b'{"ok":true}'

        def opener(request, *, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

        notifier = TelegramRelinkNotifier(config, opener=opener, timeout_seconds=7)
        notifier.send_relink_alert("https://panel.example/wa-relink#one-use-token")
        payload = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual("POST", captured["request"].method)
        self.assertEqual(7, captured["timeout"])
        self.assertEqual("-1234", payload["chat_id"])
        self.assertIn("#one-use-token", payload["text"])
        self.assertTrue(payload["disable_web_page_preview"])
        self.assertTrue(payload["link_preview_options"]["is_disabled"])

        def exploding_opener(_request, *, timeout):
            raise OSError(
                f"transport failed for bot {config.telegram_bot_token} after {timeout}s"
            )

        failing = TelegramRelinkNotifier(config, opener=exploding_opener)
        with self.assertRaises(RelinkNotificationError) as raised:
            failing.send_online()
        self.assertEqual("telegram_delivery_failed", str(raised.exception))
        self.assertNotIn(config.telegram_bot_token, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
