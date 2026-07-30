import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import app as app_module
from panel_admin_access import PanelAdminAccessStore


class MutableClock:
    def __init__(self, value=1_000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class AdminAccessRoutesTestCase(unittest.TestCase):
    CODE = "01234567"
    WRONG_CODE = "87654321"
    REQUEST_ID = "deterministic-admin-access-request"
    SECRET = b"route-test-secret-" + (b"s" * 32)
    CSRF = "c" * 48
    TTL_SECONDS = 180
    MAX_ATTEMPTS = 3

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.storage_dir = Path(self.temporary.name) / "panel_admin_access"
        self.clock = MutableClock()
        self.store = PanelAdminAccessStore(
            self.storage_dir,
            self.SECRET,
            ttl_seconds=self.TTL_SECONDS,
            max_attempts=self.MAX_ATTEMPTS,
            cooldown_seconds=0,
            clock=self.clock,
            code_factory=lambda: self.CODE,
            request_id_factory=lambda: self.REQUEST_ID,
        )

        app_module.app.config.update(TESTING=True)
        store_patcher = patch.object(app_module, "_panel_admin_access", self.store)
        store_patcher.start()
        self.addCleanup(store_patcher.stop)

        self.client = app_module.app.test_client()
        self.set_csrf(self.client)

    def set_csrf(self, client, value=None):
        with client.session_transaction() as browser_session:
            browser_session["channel_csrf"] = value or self.CSRF

    def csrf_headers(self, value=None):
        return {"X-Channel-CSRF": value or self.CSRF}

    def request_challenge(self, client=None, csrf=None):
        target = client or self.client
        with (
            patch.object(
                app_module,
                "_telegram_session_is_authorized",
                return_value=True,
            ),
            patch.object(app_module, "bot_is_running", return_value=True),
            patch.object(app_module, "restart_telegram_worker") as restart,
        ):
            response = target.post(
                "/api/admin_access/request",
                headers=self.csrf_headers(csrf),
            )
        restart.assert_not_called()
        self.assertEqual(200, response.status_code)
        return response

    def mark_delivered(self):
        challenge = json.loads(
            self.store.challenge_path.read_text(encoding="utf-8")
        )
        self.store.delivery_status_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "request_id": challenge["request_id"],
                    "state": "sent",
                    "attempts": 1,
                    "delivered_at": self.clock(),
                }
            ),
            encoding="utf-8",
        )

    def verify(self, code, client=None, csrf=None):
        target = client or self.client
        return target.post(
            "/api/admin_access/verify",
            json={"code": code},
            headers=self.csrf_headers(csrf),
        )

    def assert_not_admin(self, client=None):
        target = client or self.client
        with target.session_transaction() as browser_session:
            self.assertFalse(browser_session.get("channel_admin"))

    def test_request_verify_and_cancel_require_csrf(self):
        for route, payload in (
            ("/api/admin_access/request", None),
            ("/api/admin_access/verify", {"code": self.CODE}),
            ("/api/admin_access/cancel", None),
        ):
            with self.subTest(route=route):
                response = self.client.post(route, json=payload)
                self.assertEqual(403, response.status_code)
                self.assertEqual("csrf_invalid", response.get_json()["error_code"])

        self.assertFalse(self.store.challenge_path.exists())
        self.assert_not_admin()

    def test_all_sensitive_panel_mutations_require_admin(self):
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
                    headers=self.csrf_headers(),
                )
                self.assertEqual(403, response.status_code)
                self.assertEqual("admin_required", response.get_json()["error_code"])

    def test_admin_mutation_requires_csrf_and_succeeds_with_it(self):
        with self.client.session_transaction() as browser_session:
            browser_session["channel_admin"] = True

        missing = self.client.post(
            "/api/messages",
            json={"lang": "es", "step": 0, "text": "nuevo", "action": "edit_text"},
        )
        self.assertEqual(403, missing.status_code)
        self.assertEqual("csrf_invalid", missing.get_json()["error_code"])

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
                headers=self.csrf_headers(),
            )

        self.assertEqual(200, allowed.status_code)
        self.assertTrue(allowed.get_json()["ok"])
        self.assertEqual("nuevo", messages["es"]["steps"][0]["text"])
        save_messages.assert_called_once_with(messages)

    def test_initial_telegram_link_requires_csrf_but_not_existing_admin(self):
        response = self.client.post(
            "/api/link_telegram",
            json={"api_id": "invalid", "api_hash": "invalid", "phone": "invalid"},
        )
        self.assertEqual(403, response.status_code)
        self.assertEqual("csrf_invalid", response.get_json()["error_code"])

    def test_request_rejects_unlinked_telegram(self):
        with (
            patch.object(
                app_module,
                "_telegram_session_is_authorized",
                return_value=False,
            ),
            patch.object(app_module, "bot_is_running") as running,
            patch.object(app_module, "restart_telegram_worker") as restart,
        ):
            response = self.client.post(
                "/api/admin_access/request",
                headers=self.csrf_headers(),
            )

        self.assertEqual(409, response.status_code)
        self.assertEqual("telegram_not_linked", response.get_json()["error_code"])
        running.assert_not_called()
        restart.assert_not_called()
        self.assertFalse(self.store.challenge_path.exists())
        self.assert_not_admin()

    def test_online_telegram_creates_private_challenge_without_restart_or_secrets(self):
        with (
            patch.object(
                app_module,
                "_telegram_session_is_authorized",
                return_value=True,
            ),
            patch.object(app_module, "bot_is_running", return_value=True),
            patch.object(app_module, "restart_telegram_worker") as restart,
        ):
            response = self.client.post(
                "/api/admin_access/request",
                headers=self.csrf_headers(),
            )

        self.assertEqual(200, response.status_code)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual("queued", data["state"])
        restart.assert_not_called()

        challenge = json.loads(
            self.store.challenge_path.read_text(encoding="utf-8")
        )
        with self.client.session_transaction() as browser_session:
            browser_token = browser_session["admin_access_browser"]

        body = response.get_data(as_text=True)
        for secret in (
            self.CODE,
            challenge["code_digest"],
            browser_token,
        ):
            self.assertNotIn(secret, body)
        for forbidden_key in (
            "code",
            "otp",
            "code_digest",
            "browser_token",
            "browser_token_digest",
        ):
            self.assertNotIn(forbidden_key, data)

    def test_status_is_bound_to_the_browser_that_requested_the_code(self):
        requested = self.request_challenge()
        self.mark_delivered()

        owner_status = self.client.get("/api/admin_access/status")
        other_client = app_module.app.test_client()
        self.set_csrf(other_client, "o" * 48)
        foreign_status = other_client.get("/api/admin_access/status")

        self.assertEqual(200, requested.status_code)
        self.assertEqual(200, owner_status.status_code)
        self.assertEqual("sent", owner_status.get_json()["state"])
        self.assertEqual(self.REQUEST_ID, owner_status.get_json()["request_id"])
        self.assertEqual(404, foreign_status.status_code)
        self.assertEqual(
            "no_active_challenge",
            foreign_status.get_json()["error_code"],
        )
        self.assertNotIn(self.REQUEST_ID, foreign_status.get_data(as_text=True))

    def test_correct_code_grants_admin_and_rotates_csrf_and_browser_token(self):
        self.request_challenge()
        self.mark_delivered()
        with self.client.session_transaction() as browser_session:
            old_browser_token = browser_session["admin_access_browser"]

        response = self.verify(self.CODE)

        self.assertEqual(200, response.status_code)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["authorized"])
        self.assertEqual("verified", data["state"])
        self.assertNotEqual(self.CSRF, data["csrf"])
        self.assertGreaterEqual(len(data["csrf"]), 32)
        with self.client.session_transaction() as browser_session:
            self.assertTrue(browser_session.get("channel_admin"))
            self.assertEqual(data["csrf"], browser_session["channel_csrf"])
            self.assertNotEqual(
                old_browser_token,
                browser_session["admin_access_browser"],
            )
            self.assertTrue(browser_session.permanent)

    def test_another_browser_cannot_verify_the_code(self):
        self.request_challenge()
        self.mark_delivered()
        other_client = app_module.app.test_client()
        other_csrf = "o" * 48
        self.set_csrf(other_client, other_csrf)

        foreign = self.verify(self.CODE, client=other_client, csrf=other_csrf)

        self.assertEqual(404, foreign.status_code)
        self.assertEqual("no_active_challenge", foreign.get_json()["error_code"])
        self.assert_not_admin(other_client)
        owner = self.verify(self.CODE)
        self.assertEqual(200, owner.status_code)
        self.assertTrue(owner.get_json()["authorized"])

    def test_wrong_codes_exhaust_only_the_requesting_browser(self):
        self.request_challenge()
        self.mark_delivered()

        for expected_remaining in (2, 1):
            response = self.verify(self.WRONG_CODE)
            self.assertEqual(400, response.status_code)
            self.assertEqual("invalid_code", response.get_json()["error_code"])
            self.assertEqual(
                expected_remaining,
                response.get_json()["attempts_remaining"],
            )

        exhausted = self.verify(self.WRONG_CODE)
        after_exhaustion = self.verify(self.CODE)

        self.assertEqual(429, exhausted.status_code)
        self.assertEqual("attempts_exhausted", exhausted.get_json()["error_code"])
        self.assertEqual(0, exhausted.get_json()["attempts_remaining"])
        self.assertEqual(429, after_exhaustion.status_code)
        self.assertEqual(
            "attempts_exhausted",
            after_exhaustion.get_json()["error_code"],
        )
        self.assertTrue(self.store.challenge_path.exists())
        self.assert_not_admin()

        other_client = app_module.app.test_client()
        other_csrf = "o" * 48
        self.set_csrf(other_client, other_csrf)
        reused = self.request_challenge(other_client, csrf=other_csrf)
        accepted = self.verify(
            self.CODE,
            client=other_client,
            csrf=other_csrf,
        )

        self.assertTrue(reused.get_json()["reused"])
        self.assertTrue(reused.get_json()["request_in_progress"])
        self.assertEqual(200, accepted.status_code)
        self.assertTrue(accepted.get_json()["authorized"])
        self.assertFalse(self.store.challenge_path.exists())

    def test_expired_code_is_rejected(self):
        self.request_challenge()
        self.mark_delivered()
        self.clock.advance(self.TTL_SECONDS + 1)

        response = self.verify(self.CODE)

        self.assertEqual(410, response.status_code)
        self.assertEqual("expired_code", response.get_json()["error_code"])
        self.assert_not_admin()

    def test_successful_code_is_single_use(self):
        self.request_challenge()
        self.mark_delivered()

        first = self.verify(self.CODE)
        rotated_csrf = first.get_json()["csrf"]
        replay = self.verify(self.CODE, csrf=rotated_csrf)

        self.assertEqual(200, first.status_code)
        self.assertTrue(first.get_json()["authorized"])
        # Once authorized, the route returns the already-authorized state and
        # never evaluates the consumed code a second time.
        self.assertEqual(404, replay.status_code)
        self.assertEqual("no_active_challenge", replay.get_json()["error_code"])
        self.assertFalse(self.store.challenge_path.exists())

    def test_offline_worker_is_not_restarted_by_an_unauthenticated_request(self):
        manager = Mock()
        with (
            patch.object(
                app_module,
                "_telegram_session_is_authorized",
                return_value=True,
            ),
            patch.object(app_module, "bot_is_running", return_value=False),
            patch.object(app_module, "restart_telegram_worker") as restart,
            patch.object(app_module, "_telegram_auth", manager),
            patch.object(app_module, "_save_telegram_creds") as save_creds,
            patch.object(
                app_module,
                "_write_telegram_authorized_marker",
            ) as write_marker,
        ):
            response = self.client.post(
                "/api/admin_access/request",
                headers=self.csrf_headers(),
            )

        self.assertEqual(503, response.status_code)
        self.assertEqual("telegram_offline", response.get_json()["error_code"])
        restart.assert_not_called()
        manager.begin.assert_not_called()
        manager.verify_code.assert_not_called()
        save_creds.assert_not_called()
        write_marker.assert_not_called()

    def test_linked_telegram_recovery_rejects_invalid_input_before_comparison(self):
        manager = Mock()
        with (
            patch.object(
                app_module,
                "_telegram_session_is_authorized",
                return_value=True,
            ),
            patch.object(
                app_module,
                "_valid_telegram_credentials",
                return_value=False,
            ) as validate,
            patch.object(app_module, "restart_telegram_worker") as restart,
            patch.object(app_module, "_save_telegram_creds") as save_creds,
            patch.object(
                app_module,
                "_write_telegram_authorized_marker",
            ) as write_marker,
            patch.object(app_module, "_telegram_auth", manager),
        ):
            response = self.client.post(
                "/api/link_telegram",
                json={
                    "api_id": "invalid-and-should-not-be-read",
                    "api_hash": "not-a-valid-hash",
                    "phone": "not-a-phone",
                },
                headers=self.csrf_headers(),
            )

        self.assertEqual(400, response.status_code)
        self.assertEqual("invalid_input", response.get_json()["error_code"])
        validate.assert_called_once()
        manager.begin.assert_not_called()
        restart.assert_not_called()
        save_creds.assert_not_called()
        write_marker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
