import time
import unittest
from unittest.mock import Mock, patch

import app as app_module


class AdminAccessRoutesTestCase(unittest.TestCase):
    CSRF = "c" * 48
    OPERATOR_KEY = "route-test-operator-key-" + ("K" * 32)

    def setUp(self):
        operator_key_patcher = patch.object(
            app_module,
            "_configured_operator_recovery_key",
            return_value=self.OPERATOR_KEY,
        )
        operator_key_patcher.start()
        self.addCleanup(operator_key_patcher.stop)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as browser_session:
            browser_session["channel_csrf"] = self.CSRF

    def headers(self, value=None):
        return {"X-Channel-CSRF": value or self.CSRF}

    def assert_not_admin(self):
        with self.client.session_transaction() as browser_session:
            self.assertFalse(browser_session.get("channel_admin"))

    def authorize_browser(self):
        with self.client.session_transaction() as browser_session:
            browser_session["channel_admin"] = True
            browser_session["operator_recovery_key_version"] = (
                app_module._operator_recovery_key_version(self.OPERATOR_KEY)
            )
            browser_session["operator_recovery_verified_at"] = time.time()

    def test_legacy_telegram_otp_mutations_still_require_csrf(self):
        for route, payload in (
            ("/api/admin_access/request", None),
            ("/api/admin_access/verify", {"code": "01234567"}),
            ("/api/admin_access/cancel", None),
        ):
            with self.subTest(route=route):
                response = self.client.post(route, json=payload)
                self.assertEqual(403, response.status_code)
                self.assertEqual("csrf_invalid", response.get_json()["error_code"])
        self.assert_not_admin()

    def test_all_legacy_telegram_otp_routes_are_inert_410_stubs(self):
        legacy_store = Mock()
        with (
            patch.object(app_module, "_panel_admin_access", legacy_store, create=True),
            patch.object(app_module, "_telegram_session_is_authorized") as linked,
            patch.object(app_module, "bot_is_running") as running,
            patch.object(app_module, "restart_telegram_worker") as restart_tg,
            patch.object(app_module, "restart_wa_bot") as restart_wa,
            patch.object(app_module, "_save_telegram_creds") as save_tg,
        ):
            responses = (
                self.client.post(
                    "/api/admin_access/request",
                    headers=self.headers(),
                ),
                self.client.get("/api/admin_access/status"),
                self.client.post(
                    "/api/admin_access/verify",
                    json={"code": "01234567"},
                    headers=self.headers(),
                ),
                self.client.post(
                    "/api/admin_access/cancel",
                    headers=self.headers(),
                ),
            )

        for response in responses:
            with self.subTest(path=response.request.path):
                self.assertEqual(410, response.status_code)
                data = response.get_json()
                self.assertFalse(data["ok"])
                self.assertFalse(data["authorized"])
                self.assertEqual(
                    "operator_recovery_required",
                    data["error_code"],
                )

        legacy_store.create_challenge.assert_not_called()
        legacy_store.get_status.assert_not_called()
        legacy_store.verify.assert_not_called()
        legacy_store.cancel.assert_not_called()
        linked.assert_not_called()
        running.assert_not_called()
        restart_tg.assert_not_called()
        restart_wa.assert_not_called()
        save_tg.assert_not_called()
        self.assert_not_admin()

    def test_legacy_stubs_do_not_authorize_an_already_linked_browser(self):
        with (
            patch.object(
                app_module,
                "_telegram_session_is_authorized",
                return_value=True,
            ),
            patch.object(app_module, "bot_is_running", return_value=True),
        ):
            response = self.client.post(
                "/api/admin_access/request",
                headers=self.headers(),
            )

        self.assertEqual(410, response.status_code)
        self.assertEqual(
            "operator_recovery_required",
            response.get_json()["error_code"],
        )
        self.assert_not_admin()

    def test_sensitive_panel_mutations_still_require_admin_and_csrf(self):
        for route in (
            "/api/messages",
            "/api/upload_call_audio",
            "/api/upload_audio",
            "/api/upload_chunk",
            "/api/upload_assemble",
            "/api/link_botfather",
            "/api/start_botfather",
        ):
            with self.subTest(route=route):
                response = self.client.post(
                    route,
                    json={},
                    headers=self.headers(),
                )
                self.assertEqual(403, response.status_code)
                self.assertEqual("admin_required", response.get_json()["error_code"])

        self.authorize_browser()

        missing_csrf = self.client.post(
            "/api/messages",
            json={"lang": "es", "step": 0, "text": "nuevo", "action": "edit_text"},
        )
        self.assertEqual(403, missing_csrf.status_code)
        self.assertEqual("csrf_invalid", missing_csrf.get_json()["error_code"])

        messages = {
            "es": {
                "steps": [{"text": "anterior", "audio": "es.mp3", "loop": True}],
                "call": {"text": "llamada", "audio": "call.mp3"},
            }
        }
        with (
            patch.object(app_module, "load_messages_json", return_value=messages),
            patch.object(app_module, "save_messages_json") as save_messages,
        ):
            allowed = self.client.post(
                "/api/messages",
                json={"lang": "es", "step": 0, "text": "nuevo", "action": "edit_text"},
                headers=self.headers(),
            )

        self.assertEqual(200, allowed.status_code)
        self.assertTrue(allowed.get_json()["ok"])
        self.assertEqual("nuevo", messages["es"]["steps"][0]["text"])
        save_messages.assert_called_once_with(messages)

    def test_telegram_auth_mutations_require_operator_admin_and_csrf(self):
        manager = Mock()
        with (
            patch.object(
                app_module,
                "_telegram_session_is_authorized",
                return_value=False,
            ) as linked,
            patch.object(app_module, "_valid_telegram_credentials") as validate,
            patch.object(app_module, "restart_telegram_worker") as restart,
            patch.object(app_module, "_save_telegram_creds") as save_creds,
            patch.object(app_module, "_telegram_auth", manager),
        ):
            for route, payload in (
                (
                    "/api/link_telegram",
                    {"api_id": 123456, "api_hash": "a" * 32, "phone": "+570000000000"},
                ),
                ("/api/verify_telegram_code", {"code": "12345"}),
                ("/api/verify_telegram_password", {"password": "secret"}),
                ("/api/cancel_telegram_auth", {}),
            ):
                with self.subTest(route=route):
                    denied = self.client.post(
                        route,
                        json=payload,
                        headers=self.headers(),
                    )
                    self.assertEqual(403, denied.status_code)
                    self.assertEqual(
                        "admin_required",
                        denied.get_json()["error_code"],
                    )

            linked.assert_not_called()
            validate.assert_not_called()
            manager.begin.assert_not_called()
            manager.verify_code.assert_not_called()
            manager.verify_password.assert_not_called()
            manager.cancel.assert_not_called()
            restart.assert_not_called()
            save_creds.assert_not_called()

            self.authorize_browser()

            missing_csrf = self.client.post(
                "/api/link_telegram",
                json={"api_id": 123456, "api_hash": "a" * 32, "phone": "+570000000000"},
                headers={"X-Channel-CSRF": "wrong"},
            )
            self.assertEqual(403, missing_csrf.status_code)
            self.assertEqual("csrf_invalid", missing_csrf.get_json()["error_code"])

            validate.return_value = False
            invalid = self.client.post(
                "/api/link_telegram",
                json={
                    "api_id": "invalid-and-should-not-be-read",
                    "api_hash": "not-a-valid-hash",
                    "phone": "not-a-phone",
                },
                headers=self.headers(),
            )

        self.assertEqual(400, invalid.status_code)
        self.assertEqual("invalid_input", invalid.get_json()["error_code"])
        validate.assert_called_once()
        manager.begin.assert_not_called()
        restart.assert_not_called()
        save_creds.assert_not_called()


if __name__ == "__main__":
    unittest.main()
