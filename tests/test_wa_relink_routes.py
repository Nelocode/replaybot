import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app as app_module
from wa_relink_incident import WhatsAppRelinkIncidentStore
from wa_relink_notifier import WhatsAppRelinkConfig


class WhatsAppRelinkRoutesTests(unittest.TestCase):
    SECRET = "r" * 64
    INCIDENT_ID = "a" * 32

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name) / "data"
        self.wa_auth_dir = self.data_dir / "wa_auth"
        self.switch_dir = self.data_dir / "wa_switch"
        self.relink_dir = self.data_dir / "wa_relink"
        self.store = WhatsAppRelinkIncidentStore(
            self.relink_dir,
            self.SECRET,
            id_factory=lambda: self.INCIDENT_ID,
        )
        self.config = WhatsAppRelinkConfig(
            enabled=True,
            public_base_url="https://panel.example",
            telegram_chat_id="-1234",
            telegram_bot_token="dedicated-token",
            service_name="Madrid",
            token_source="dedicated",
        )

        app_module._cancel_wa_switch_expiry()
        replacements = {
            "DATA_DIR": self.data_dir,
            "WA_AUTH_DIR": self.wa_auth_dir,
            "WA_IDENTITY_FILE": self.data_dir / "wa_identity.json",
            "WA_CALL_HEALTH_FILE": self.data_dir / "wa_call_health.json",
            "WA_SWITCH_DIR": self.switch_dir,
            "WA_SWITCH_AUTH_DIR": self.switch_dir / "candidate_auth",
            "WA_SWITCH_QR_FILE": self.switch_dir / "qr.png",
            "WA_SWITCH_HEALTH_FILE": self.switch_dir / "health.json",
            "WA_SWITCH_IDENTITY_FILE": self.switch_dir / "identity.json",
            "WA_SWITCH_PID_FILE": self.switch_dir / "worker.pid",
            "WA_SWITCH_OPERATION_FILE": self.switch_dir / "operation.json",
            "WA_SWITCH_RECOVERY_ROOT": self.data_dir / ".wa_switch_recovery",
            "WA_RELINK_DIR": self.relink_dir,
            "_wa_relink_store": self.store,
            "_wa_switch_expiry_timer": None,
        }
        for name, value in replacements.items():
            patcher = patch.object(app_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        config_patcher = patch.object(app_module, "_wa_relink_config", return_value=self.config)
        config_patcher.start()
        self.addCleanup(config_patcher.stop)
        config_state = patch.dict(
            app_module.app.config,
            {"TESTING": True, "SESSION_COOKIE_SECURE": False},
        )
        config_state.start()
        self.addCleanup(config_state.stop)
        self.client = app_module.app.test_client()

    def seed_incident(self):
        state, created = self.store.ensure_outage("b" * 24, "logged_out")
        self.assertTrue(created)
        return state, self.store.token_for(state)

    def start_candidate(self, token, *, client=None, pid=4321):
        target = client or self.client
        with (
            patch.object(app_module, "_start_wa_process", return_value=pid) as start,
            patch.object(app_module, "_schedule_wa_switch_expiry"),
        ):
            response = target.post("/api/wa-relink/confirm", json={"token": token})
        return response, start

    @staticmethod
    def write_creds(directory: Path, account_id: str | None):
        directory.mkdir(parents=True, exist_ok=True)
        payload = {"me": {"id": account_id}} if account_id is not None else {"me": {}}
        (directory / "creds.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_preview_has_explicit_button_and_gets_do_not_mutate_relink_state(self):
        before, _token = self.seed_incident()
        with patch.object(app_module, "_start_wa_process") as start:
            preview = self.client.get("/wa-relink")
            wrong_method = self.client.get("/api/wa-relink/confirm")

        html = preview.get_data(as_text=True)
        self.assertEqual(200, preview.status_code)
        self.assertIn('id="generate"', html)
        self.assertIn('generate.addEventListener("click"', html)
        self.assertEqual(405, wrong_method.status_code)
        self.assertEqual(before, self.store.snapshot())
        self.assertFalse(self.switch_dir.exists())
        start.assert_not_called()
        with self.client.session_transaction() as browser_session:
            self.assertNotIn("wa_relink_capability", browser_session)
            self.assertNotIn("channel_admin", browser_session)

    def test_token_is_one_use_but_same_browser_retries_after_start_failure(self):
        _state, token = self.seed_incident()
        with (
            patch.object(app_module, "_start_wa_process", side_effect=[None, 2468]) as start,
            patch.object(app_module, "_schedule_wa_switch_expiry"),
        ):
            failed = self.client.post("/api/wa-relink/confirm", json={"token": token})
            with self.client.session_transaction() as browser_session:
                first_capability = dict(browser_session["wa_relink_capability"])
                first_csrf = browser_session["wa_relink_csrf"]

            other_browser = app_module.app.test_client()
            replayed = other_browser.post("/api/wa-relink/confirm", json={"token": token})
            retried = self.client.post("/api/wa-relink/confirm", json={"token": token})

        self.assertEqual(503, failed.status_code)
        self.assertEqual("candidate_failed", failed.get_json()["error_code"])
        self.assertEqual(410, replayed.status_code)
        self.assertEqual("token_consumed", replayed.get_json()["error_code"])
        self.assertEqual(202, retried.status_code)
        self.assertEqual(first_csrf, retried.get_json()["csrf"])
        with self.client.session_transaction() as browser_session:
            self.assertEqual(first_capability, browser_session["wa_relink_capability"])
        self.assertEqual(2, start.call_count)
        self.assertIsNotNone(self.store.snapshot()["token_consumed_at"])
        self.assertEqual("qr_active", self.store.snapshot()["status"])

    def test_owned_candidate_is_idempotent_and_foreign_candidate_conflicts(self):
        _state, token = self.seed_incident()
        with (
            patch.object(app_module, "_start_wa_process", return_value=2468) as start,
            patch.object(app_module, "_schedule_wa_switch_expiry"),
            patch.object(app_module, "_wa_process_running", return_value=True),
        ):
            first = self.client.post("/api/wa-relink/confirm", json={"token": token})
            repeated = self.client.post("/api/wa-relink/confirm", json={"token": token})

        self.assertEqual(202, first.status_code)
        self.assertEqual(first.get_json()["operation_id"], repeated.get_json()["operation_id"])
        start.assert_called_once()

        second_store = WhatsAppRelinkIncidentStore(
            self.data_dir / "other_relink",
            self.SECRET,
            id_factory=lambda: "c" * 32,
        )
        second_state, _ = second_store.ensure_outage("d" * 24, "logged_out")
        second_token = second_store.token_for(second_state)
        with app_module.app.test_request_context():
            app_module._cleanup_wa_switch_candidate()
        app_module._save_wa_switch_operation({
            "version": 1,
            "source": "admin",
            "token_hash": "e" * 64,
            "started_at": time.time(),
            "status": "preparing",
        })
        foreign_client = app_module.app.test_client()
        with (
            patch.object(app_module, "_wa_relink_store", second_store),
            patch.object(app_module, "_start_wa_process") as foreign_start,
        ):
            conflict = foreign_client.post(
                "/api/wa-relink/confirm",
                json={"token": second_token},
            )

        self.assertEqual(409, conflict.status_code)
        self.assertEqual("switch_in_progress", conflict.get_json()["error_code"])
        self.assertIsNone(second_store.snapshot()["token_consumed_at"])
        foreign_start.assert_not_called()

    def test_scoped_csrf_and_full_creds_match_do_not_grant_panel_admin(self):
        _state, token = self.seed_incident()
        started, _start = self.start_candidate(token)
        csrf = started.get_json()["csrf"]
        self.write_creds(self.wa_auth_dir, "573001234567:3@s.whatsapp.net")
        self.write_creds(app_module.WA_SWITCH_AUTH_DIR, "573001234567@lid")

        with patch.object(app_module, "restart_wa_bot") as restart:
            panel_mutation = self.client.post(
                "/api/restart_wa_bot",
                headers={"X-WA-Relink-CSRF": csrf},
            )
        self.assertEqual(403, panel_mutation.status_code)
        self.assertEqual("admin_required", panel_mutation.get_json()["error_code"])
        restart.assert_not_called()

        with (
            patch.object(app_module, "_wa_connection_open", return_value=True),
            patch.object(
                app_module,
                "_promote_wa_candidate",
                return_value=(True, "Cuenta verificada.", {"display_name": "Cuenta"}, True, True),
            ) as promote,
            patch.object(app_module, "_supervise_wa_relink_once"),
        ):
            missing = self.client.post("/api/wa-relink/commit")
            panel_csrf = self.client.post(
                "/api/wa-relink/commit",
                headers={"X-Channel-CSRF": csrf},
            )
            committed = self.client.post(
                "/api/wa-relink/commit",
                headers={"X-WA-Relink-CSRF": csrf},
            )

        self.assertEqual("csrf_invalid", missing.get_json()["error_code"])
        self.assertEqual("csrf_invalid", panel_csrf.get_json()["error_code"])
        self.assertEqual(200, committed.status_code)
        promote.assert_called_once_with(
            ownership_check=app_module._wa_relink_switch_owned,
            grant_channel_admin=False,
        )
        with self.client.session_transaction() as browser_session:
            self.assertNotIn("wa_relink_capability", browser_session)
            self.assertNotIn("wa_relink_csrf", browser_session)
            self.assertNotIn("channel_admin", browser_session)

    def test_commit_rejects_full_creds_mismatch(self):
        _state, token = self.seed_incident()
        started, _start = self.start_candidate(token)
        csrf = started.get_json()["csrf"]
        self.write_creds(self.wa_auth_dir, "573001234567@s.whatsapp.net")
        self.write_creds(app_module.WA_SWITCH_AUTH_DIR, "573009876543:2@s.whatsapp.net")

        with (
            patch.object(app_module, "_wa_connection_open", return_value=True),
            patch.object(app_module, "_promote_wa_candidate") as promote,
        ):
            response = self.client.post(
                "/api/wa-relink/commit",
                headers={"X-WA-Relink-CSRF": csrf},
            )

        self.assertEqual(409, response.status_code)
        self.assertEqual("identity_mismatch", response.get_json()["error_code"])
        promote.assert_not_called()

    def test_commit_fails_closed_when_full_creds_are_unverified(self):
        _state, token = self.seed_incident()
        started, _start = self.start_candidate(token)
        csrf = started.get_json()["csrf"]
        self.write_creds(self.wa_auth_dir, "573001234567@s.whatsapp.net")
        self.write_creds(app_module.WA_SWITCH_AUTH_DIR, None)
        app_module.WA_SWITCH_IDENTITY_FILE.write_text(
            json.dumps({"display_name": "Mismo nombre", "phone_hint": "••••4567"}),
            encoding="utf-8",
        )

        with (
            patch.object(app_module, "_wa_connection_open", return_value=True),
            patch.object(app_module, "_promote_wa_candidate") as promote,
        ):
            response = self.client.post(
                "/api/wa-relink/commit",
                headers={"X-WA-Relink-CSRF": csrf},
            )

        self.assertEqual(409, response.status_code)
        self.assertEqual("identity_unverified", response.get_json()["error_code"])
        promote.assert_not_called()

    def test_qr_is_capability_scoped_and_has_private_security_headers(self):
        _state, token = self.seed_incident()
        started, _start = self.start_candidate(token)
        self.assertEqual(202, started.status_code)
        png = b"\x89PNG\r\n\x1a\nprivate-qr"
        app_module.WA_SWITCH_QR_FILE.write_bytes(png)

        response = self.client.get("/api/wa-relink/qr")
        anonymous = app_module.app.test_client().get("/api/wa-relink/qr")

        self.assertEqual(200, response.status_code)
        self.assertEqual(png, response.data)
        self.assertEqual("image/png", response.content_type)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual("no-cache", response.headers["Pragma"])
        self.assertEqual("no-referrer", response.headers["Referrer-Policy"])
        self.assertEqual("nosniff", response.headers["X-Content-Type-Options"])
        self.assertEqual("DENY", response.headers["X-Frame-Options"])
        self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(403, anonymous.status_code)
        self.assertEqual("capability_invalid", anonymous.get_json()["error_code"])
        response.close()

    def test_cancel_removes_only_candidate_and_preserves_active_auth(self):
        active = self.wa_auth_dir / "creds.json"
        active.parent.mkdir(parents=True)
        active.write_bytes(b"old-auth-must-survive")
        _state, token = self.seed_incident()
        started, _start = self.start_candidate(token)
        csrf = started.get_json()["csrf"]
        (app_module.WA_SWITCH_AUTH_DIR / "creds.json").write_bytes(b"candidate-auth")

        with patch.object(app_module, "_stop_wa_process") as stop:
            cancelled = self.client.post(
                "/api/wa-relink/cancel",
                headers={"X-WA-Relink-CSRF": csrf},
            )

        self.assertEqual(200, cancelled.status_code)
        self.assertEqual(b"old-auth-must-survive", active.read_bytes())
        self.assertFalse(self.switch_dir.exists())
        self.assertEqual("cancelled", self.store.snapshot()["status"])
        stop.assert_called_once_with(app_module.WA_SWITCH_PID_FILE)
        with self.client.session_transaction() as browser_session:
            self.assertNotIn("wa_relink_capability", browser_session)
            self.assertNotIn("channel_admin", browser_session)

    def test_supervisor_sends_alert_and_online_confirmation_once(self):
        self.wa_auth_dir.mkdir(parents=True)
        (self.wa_auth_dir / "creds.json").write_text("present", encoding="utf-8")
        notifier = Mock()
        health = {
            "reauth_required": True,
            "disconnect_reason": "logged_out",
            "worker_revision": "worker-1",
        }

        with (
            patch.object(app_module, "TelegramRelinkNotifier", return_value=notifier),
            patch.object(app_module, "_wa_connection_open", return_value=False),
            patch.object(app_module, "_read_wa_call_health", return_value=health),
        ):
            app_module._supervise_wa_relink_once()
            app_module._supervise_wa_relink_once()

        notifier.send_relink_alert.assert_called_once()
        recovery_url = notifier.send_relink_alert.call_args.args[0]
        self.assertTrue(recovery_url.startswith("https://panel.example/wa-relink#"))
        token = recovery_url.split("#", 1)[1]
        self.assertNotIn(token, self.store.path.read_text(encoding="utf-8"))

        with (
            patch.object(app_module, "TelegramRelinkNotifier", return_value=notifier),
            patch.object(app_module, "_wa_connection_open", return_value=True),
        ):
            app_module._supervise_wa_relink_once()
            app_module._supervise_wa_relink_once()

        notifier.send_online.assert_called_once_with()
        final = self.store.snapshot()
        self.assertFalse(final["outage_open"])
        self.assertEqual(1, final["alert"]["attempts"])
        self.assertEqual(1, final["online"]["attempts"])

    def test_worker_environment_scrubs_panel_only_relink_secret(self):
        sensitive = {
            "FLASK_SECRET": "flask-secret",
            "PANEL_ADMIN_RECOVERY_KEY": "panel-secret",
            "BILLING_CONTROL_PLANE_ADMIN_TOKEN": "billing-secret",
            "WA_RELINK_TELEGRAM_BOT_TOKEN": "relink-secret",
            "AUTOREPLY_BOT_TOKEN": "worker-required-token",
        }
        with patch.dict(os.environ, sensitive, clear=False):
            worker_environment = app_module._channel_worker_environment({"WA_LINK_ONLY": "1"})

        self.assertNotIn("FLASK_SECRET", worker_environment)
        self.assertNotIn("PANEL_ADMIN_RECOVERY_KEY", worker_environment)
        self.assertNotIn("BILLING_CONTROL_PLANE_ADMIN_TOKEN", worker_environment)
        self.assertNotIn("WA_RELINK_TELEGRAM_BOT_TOKEN", worker_environment)
        self.assertNotIn("AUTOREPLY_BOT_TOKEN", worker_environment)
        with patch.dict(os.environ, sensitive, clear=False):
            botfather_environment = app_module._channel_worker_environment(
                keep_autoreply_token=True,
            )
        self.assertEqual(
            "worker-required-token",
            botfather_environment["AUTOREPLY_BOT_TOKEN"],
        )
        self.assertNotIn("FLASK_SECRET", botfather_environment)
        self.assertEqual("1", worker_environment["WA_LINK_ONLY"])


if __name__ == "__main__":
    unittest.main()
