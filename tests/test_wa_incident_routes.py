import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
from tests.admin_session import grant_operator_admin, install_operator_key
from wa_incident_health import LEDGER_BASE_KEYS


class WhatsAppIncidentRoutesTestCase(unittest.TestCase):
    CSRF = "i" * 48

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        directory = Path(self.temporary.name)
        self.snapshot_path = directory / "wa_incident_health.json"
        self.ledger_path = directory / "wa_incidents.jsonl"
        self.path_patches = [
            patch.object(app_module, "WA_INCIDENT_HEALTH_FILE", self.snapshot_path),
            patch.object(app_module, "WA_INCIDENT_LEDGER_FILE", self.ledger_path),
            patch.object(app_module, "_wa_worker_running", return_value=True),
        ]
        for path_patch in self.path_patches:
            path_patch.start()
        self.operator_key = install_operator_key(self)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def tearDown(self):
        for path_patch in reversed(self.path_patches):
            path_patch.stop()
        self.temporary.cleanup()

    def authorize(self):
        grant_operator_admin(self.client, self.operator_key, csrf=self.CSRF)

    def write_snapshot(self):
        now_ms = int(time.time() * 1000)
        incident = {
            "incident_id": "incident_1",
            "started_at": now_ms - 1000,
            "last_event_at": now_ms,
            "resolved_at": None,
            "status_code": 440,
            "status_name": "connection_replaced",
            "category": "session_conflict",
            "action": "check_duplicate_session",
            "terminal": True,
            "reauth_required": True,
            "disconnect_count": 1,
            "reconnect_attempts": 0,
            "precursors": ["worker_lease_conflict"],
            "jid": "573001234567@s.whatsapp.net",
        }
        payload = {
            "schema_version": 1,
            "worker_revision": "worker_1",
            "updated_at": now_ms,
            "heartbeat_at": now_ms,
            "connection": "closed",
            "connection_since": now_ms,
            "last_open_at": None,
            "last_disconnect_at": now_ms,
            "condition": "critical",
            "failure_likelihood": "high",
            "signals": ["terminal_session_failure", "worker_lease_conflict"],
            "likely_failure_modes": ["authentication_loss", "duplicate_session"],
            "active_incident": incident,
            "last_incident": None,
            "last_failure": {
                "failure_id": "failure_1",
                "incident_id": "incident_1",
                "at": now_ms,
                "status_code": 440,
                "status_name": "connection_replaced",
                "category": "session_conflict",
                "action": "check_duplicate_session",
                "terminal": True,
                "reauth_required": True,
                "raw_error": "token-secreto",
            },
            "ledger_integrity": "verified",
            "ledger_sequence": 1,
        }
        self.snapshot_path.write_text(json.dumps(payload), encoding="utf-8")
        return now_ms

    def write_ledger(self, at):
        base = {
            "schema_version": 1,
            "sequence": 1,
            "record_id": "record_1",
            "at": at,
            "event": "connection_closed",
            "lifecycle": "opened",
            "incident_id": "incident_1",
            "condition": "critical",
            "status_code": 440,
            "status_name": "connection_replaced",
            "category": "session_conflict",
            "action": "check_duplicate_session",
            "terminal": True,
            "reauth_required": True,
            "reconnect_attempt": 0,
            "reconnect_delay_ms": 0,
            "signals": ["terminal_session_failure"],
            "previous_hash": None,
        }
        serialized = json.dumps(
            {key: base[key] for key in LEDGER_BASE_KEYS},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        record = {**base, "record_hash": hashlib.sha256(serialized.encode()).hexdigest()}
        self.ledger_path.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")

    def test_public_summary_is_redacted_but_admin_sees_exact_cause(self):
        self.write_snapshot()
        public = self.client.get("/api/wa_incident_health")
        self.assertEqual(200, public.status_code)
        self.assertIsNone(public.get_json()["last_failure"])
        self.assertIsNone(public.get_json()["active_incident"])
        self.assertNotIn("573001234567", public.get_data(as_text=True))
        self.assertNotIn("token-secreto", public.get_data(as_text=True))
        self.assertEqual("no-store, private", public.headers["Cache-Control"])
        self.assertEqual("nosniff", public.headers["X-Content-Type-Options"])
        self.assertEqual("no-referrer", public.headers["Referrer-Policy"])

        self.authorize()
        admin = self.client.get("/api/wa_incident_health").get_json()
        self.assertEqual("connection_replaced", admin["last_failure"]["status_name"])
        self.assertEqual(440, admin["last_failure"]["status_code"])

    def test_history_requires_admin_and_returns_only_verified_records(self):
        at = self.write_snapshot()
        self.write_ledger(at)
        denied = self.client.get("/api/wa_incidents?limit=20")
        self.assertEqual(403, denied.status_code)
        self.assertEqual("no-store, private", denied.headers["Cache-Control"])

        self.authorize()
        accepted = self.client.get("/api/wa_incidents?limit=999")
        self.assertEqual(200, accepted.status_code)
        body = accepted.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(100, body["limit"])
        self.assertEqual("connection_replaced", body["records"][0]["status_name"])
        self.assertNotIn("jid", accepted.get_data(as_text=True))

    def test_panel_keeps_warning_visible_and_polls_health(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="wa-incident-card"', html)
        self.assertIn("loadWaIncidentHealth();", html)
        self.assertIn("setInterval(loadWaIncidentHealth, 10000)", html)
        self.assertIn("textContent", html)
        self.assertIn("Hay señales preventivas", html)


if __name__ == "__main__":
    unittest.main()
