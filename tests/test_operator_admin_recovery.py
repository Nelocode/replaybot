import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app as app_module
import operator_admin_recovery as recovery_module
from operator_admin_recovery import (
    MAX_OPERATOR_KEY_BYTES,
    MIN_OPERATOR_KEY_BYTES,
    OperatorAdminRecoveryGuard,
    operator_key_matches,
    valid_configured_operator_key,
)


class MutableClock:
    def __init__(self, value=10_000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class OperatorAdminRecoveryGuardTestCase(unittest.TestCase):
    IDENTITY_SECRET = b"identity-secret-" + (b"s" * 32)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "operator"
        self.clock = MutableClock()
        self.guard = OperatorAdminRecoveryGuard(
            self.directory,
            self.IDENTITY_SECRET,
            window_seconds=120,
            client_attempts=3,
            global_attempts=4,
            lockout_seconds=60,
            clock=self.clock,
        )

    def test_key_policy_and_fixed_size_constant_time_comparison(self):
        configured = "k" * MIN_OPERATOR_KEY_BYTES
        actual_compare = recovery_module.hmac.compare_digest

        self.assertTrue(valid_configured_operator_key(configured))
        self.assertFalse(valid_configured_operator_key("k" * (MIN_OPERATOR_KEY_BYTES - 1)))
        self.assertFalse(valid_configured_operator_key("k" * (MAX_OPERATOR_KEY_BYTES + 1)))
        self.assertFalse(valid_configured_operator_key("k" * 32 + "\n"))
        self.assertTrue(operator_key_matches(configured, configured))
        self.assertFalse(operator_key_matches(configured, "x" * 32))
        self.assertFalse(operator_key_matches(configured, "short"))

        observed = []

        def capture(left, right):
            observed.append((left, right))
            return actual_compare(left, right)

        with patch.object(recovery_module.hmac, "compare_digest", side_effect=capture):
            self.assertFalse(operator_key_matches(configured, "short"))

        self.assertEqual(1, len(observed))
        self.assertEqual(32, len(observed[0][0]))
        self.assertEqual(32, len(observed[0][1]))

    def test_correct_key_bypasses_and_clears_per_client_lockout(self):
        identity = "browser-a\0network-a"

        self.assertEqual(
            "invalid_operator_key",
            self.guard.evaluate(identity, matched=False)["error_code"],
        )
        self.assertEqual(
            "invalid_operator_key",
            self.guard.evaluate(identity, matched=False)["error_code"],
        )
        limited = self.guard.evaluate(identity, matched=False)

        self.assertEqual("rate_limited", limited["error_code"])
        self.assertEqual(60, limited["retry_after"])

        recreated = OperatorAdminRecoveryGuard(
            self.directory,
            self.IDENTITY_SECRET,
            window_seconds=120,
            client_attempts=3,
            global_attempts=4,
            lockout_seconds=60,
            clock=self.clock,
        )
        self.assertTrue(recreated.evaluate(identity, matched=True)["allowed"])
        self.assertEqual(
            "invalid_operator_key",
            recreated.evaluate(identity, matched=False)["error_code"],
        )

    def test_correct_key_bypasses_and_clears_global_lockout(self):
        for index in range(3):
            result = self.guard.evaluate(f"browser-{index}", matched=False)
            self.assertEqual("invalid_operator_key", result["error_code"])

        limited = self.guard.evaluate("browser-3", matched=False)
        recovery = self.guard.evaluate("fresh-browser", matched=True)

        self.assertEqual("rate_limited", limited["error_code"])
        self.assertTrue(recovery["allowed"])
        self.assertEqual(
            "invalid_operator_key",
            self.guard.evaluate("another-browser", matched=False)["error_code"],
        )

    def test_persisted_rate_state_contains_neither_key_nor_raw_identity(self):
        raw_identity = "browser-token-that-must-not-be-stored\0198.51.100.4"
        raw_key = "operator-secret-that-must-never-enter-rate-storage"

        self.guard.evaluate(raw_identity, matched=False)
        stored_text = self.guard.rate_path.read_text(encoding="utf-8")
        stored = json.loads(stored_text)

        self.assertNotIn(raw_identity, stored_text)
        self.assertNotIn(raw_key, stored_text)
        self.assertEqual(64, len(next(iter(stored["clients"]))))

    def test_corrupt_existing_rate_state_fails_closed(self):
        self.guard.rate_path.write_text("{not-json", encoding="utf-8")

        result = self.guard.evaluate("browser", matched=True)

        self.assertFalse(result["allowed"])
        self.assertEqual("busy", result["error_code"])

    def test_telegram_worker_has_no_legacy_saved_messages_delivery_task(self):
        project_root = Path(__file__).resolve().parents[1]
        bot_source = (project_root / "bot.py").read_text(encoding="utf-8")
        app_source = (project_root / "app.py").read_text(encoding="utf-8")
        entrypoint_source = (project_root / "entrypoint.sh").read_text(encoding="utf-8")

        self.assertNotIn("deliver_pending_challenge", bot_source)
        self.assertNotIn("poll_panel_admin_access", bot_source)
        self.assertNotIn("panel_access_task", bot_source)
        self.assertNotIn("PanelAdminAccessStore", app_source)
        self.assertNotIn(".create_challenge(", app_source)
        self.assertGreaterEqual(app_source.count("_channel_worker_environment("), 4)
        self.assertIn("env=_channel_worker_environment()", app_source)
        self.assertNotIn("source /app/data/.env.local", entrypoint_source)
        self.assertIn("TG_API_ID|TG_API_HASH|TG_PHONE|AUTOREPLY_BOT_TOKEN", entrypoint_source)
        self.assertNotIn(
            "TG_API_ID|TG_API_HASH|TG_PHONE|AUTOREPLY_BOT_TOKEN|PANEL_ADMIN_RECOVERY_KEY",
            entrypoint_source,
        )

    def test_channel_workers_do_not_inherit_the_operator_key(self):
        secret = "operator-key-visible-only-to-panel-" + ("K" * 32)
        with patch.dict(
            os.environ,
            {
                app_module.PANEL_ADMIN_RECOVERY_KEY_ENV: secret,
                "CHANNEL_SETTING": "preserved",
            },
        ):
            worker_env = app_module._channel_worker_environment(
                {"EXTRA_SETTING": "present"}
            )

        self.assertNotIn(app_module.PANEL_ADMIN_RECOVERY_KEY_ENV, worker_env)
        self.assertEqual("preserved", worker_env["CHANNEL_SETTING"])
        self.assertEqual("present", worker_env["EXTRA_SETTING"])

        project_root = Path(__file__).resolve().parents[1]
        entrypoint_source = (project_root / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertEqual(3, entrypoint_source.count("env -u PANEL_ADMIN_RECOVERY_KEY"))


class OperatorAdminRecoveryRouteTestCase(unittest.TestCase):
    KEY = "operator-owned-key-" + ("K" * 32)
    CSRF = "r" * 48

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.clock = MutableClock()
        self.guard = OperatorAdminRecoveryGuard(
            Path(self.temporary.name) / "operator",
            b"route-identity-secret-" + (b"z" * 32),
            window_seconds=120,
            client_attempts=3,
            global_attempts=8,
            lockout_seconds=60,
            clock=self.clock,
        )
        guard_patcher = patch.object(
            app_module,
            "_operator_admin_recovery",
            self.guard,
        )
        guard_patcher.start()
        self.addCleanup(guard_patcher.stop)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as browser_session:
            browser_session["channel_csrf"] = self.CSRF

    def post(self, key=None, *, csrf=None):
        payload = {} if key is None else {"key": key}
        return self.client.post(
            "/api/admin_access/operator",
            json=payload,
            headers={"X-Channel-CSRF": csrf or self.CSRF},
        )

    def test_route_requires_csrf_before_reading_or_checking_key(self):
        with (
            patch.object(app_module, "_configured_operator_recovery_key") as configured,
            patch.object(app_module, "operator_key_matches") as compare,
        ):
            response = self.client.post(
                "/api/admin_access/operator",
                json={"key": self.KEY},
            )

        self.assertEqual(403, response.status_code)
        self.assertEqual("csrf_invalid", response.get_json()["error_code"])
        configured.assert_not_called()
        compare.assert_not_called()

    def test_unconfigured_or_invalid_environment_fails_closed(self):
        with (
            patch.dict(os.environ, {app_module.PANEL_ADMIN_RECOVERY_KEY_ENV: "short"}),
            patch.object(app_module, "_read_env_var", return_value=self.KEY) as read_file,
            patch.object(app_module, "operator_key_matches") as compare,
        ):
            response = self.post(self.KEY)

        self.assertEqual(503, response.status_code)
        self.assertEqual(
            "operator_recovery_unconfigured",
            response.get_json()["error_code"],
        )
        # An explicitly configured but invalid environment value shadows the
        # persistent file, preventing an obsolete key from silently reviving.
        read_file.assert_not_called()
        compare.assert_not_called()
        with self.client.session_transaction() as browser_session:
            self.assertFalse(browser_session.get("channel_admin"))

    def test_persistent_file_cannot_supply_the_operator_key(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(app_module.PANEL_ADMIN_RECOVERY_KEY_ENV, None)
            with patch.object(
                app_module,
                "_read_env_var",
                side_effect=lambda name: self.KEY
                if name == app_module.PANEL_ADMIN_RECOVERY_KEY_ENV
                else None,
            ):
                response = self.post(self.KEY)

        self.assertEqual(503, response.status_code)
        self.assertEqual(
            "operator_recovery_unconfigured",
            response.get_json()["error_code"],
        )

    def test_success_grants_admin_rotates_csrf_and_never_touches_channels(self):
        with (
            patch.dict(os.environ, {app_module.PANEL_ADMIN_RECOVERY_KEY_ENV: self.KEY}),
            patch.object(app_module, "_telegram_session_is_authorized") as linked,
            patch.object(app_module, "bot_is_running") as tg_running,
            patch.object(app_module, "wa_is_running") as wa_running,
            patch.object(app_module, "restart_telegram_worker") as restart_tg,
            patch.object(app_module, "restart_wa_bot") as restart_wa,
            patch.object(app_module, "_save_telegram_creds") as save_tg,
            patch.object(app_module, "_telegram_auth") as telegram_auth,
        ):
            response = self.post(self.KEY)

        self.assertEqual(200, response.status_code)
        result = response.get_json()
        self.assertEqual(
            {"ok", "authorized", "state", "csrf"},
            set(result),
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["authorized"])
        self.assertEqual("verified", result["state"])
        self.assertNotEqual(self.CSRF, result["csrf"])
        self.assertNotIn(self.KEY, response.get_data(as_text=True))
        linked.assert_not_called()
        tg_running.assert_not_called()
        wa_running.assert_not_called()
        restart_tg.assert_not_called()
        restart_wa.assert_not_called()
        save_tg.assert_not_called()
        telegram_auth.begin.assert_not_called()
        with self.client.session_transaction() as browser_session:
            self.assertTrue(browser_session.get("channel_admin"))
            self.assertEqual(result["csrf"], browser_session["channel_csrf"])
            self.assertEqual(
                app_module._operator_recovery_key_version(self.KEY),
                browser_session["operator_recovery_key_version"],
            )
            self.assertIsInstance(
                browser_session["operator_recovery_verified_at"],
                float,
            )
            self.assertNotIn(self.KEY, json.dumps(dict(browser_session)))

        with patch.dict(os.environ, {app_module.PANEL_ADMIN_RECOVERY_KEY_ENV: self.KEY}):
            stale_csrf = self.client.post(
                "/api/restart_wa_bot",
                headers={"X-Channel-CSRF": self.CSRF},
            )
        self.assertEqual(403, stale_csrf.status_code)
        self.assertEqual("csrf_invalid", stale_csrf.get_json()["error_code"])

    def test_rotating_or_removing_key_revokes_existing_admin_session(self):
        with patch.dict(os.environ, {app_module.PANEL_ADMIN_RECOVERY_KEY_ENV: self.KEY}):
            granted = self.post(self.KEY)
        self.assertEqual(200, granted.status_code)

        rotated = "rotated-operator-key-" + ("R" * 32)
        with patch.dict(os.environ, {app_module.PANEL_ADMIN_RECOVERY_KEY_ENV: rotated}):
            state = self.client.get("/api/channels")
        self.assertEqual(200, state.status_code)
        self.assertFalse(state.get_json()["can_manage"])
        with self.client.session_transaction() as browser_session:
            self.assertFalse(browser_session.get("channel_admin"))
            self.assertNotIn("operator_recovery_key_version", browser_session)

        with self.client.session_transaction() as browser_session:
            browser_session["channel_admin"] = True
            browser_session["telegram_admin"] = True
            browser_session["wa_admin"] = True
        with patch.dict(os.environ, {}, clear=True):
            state = self.client.get("/api/channels")
        self.assertFalse(state.get_json()["can_manage"])
        with self.client.session_transaction() as browser_session:
            self.assertFalse(browser_session.get("channel_admin"))
            self.assertNotIn("telegram_admin", browser_session)
            self.assertNotIn("wa_admin", browser_session)

    def test_admin_session_expires_independently_of_cookie_lifetime(self):
        with patch.dict(os.environ, {app_module.PANEL_ADMIN_RECOVERY_KEY_ENV: self.KEY}):
            granted = self.post(self.KEY)
            self.assertEqual(200, granted.status_code)
            with self.client.session_transaction() as browser_session:
                browser_session["operator_recovery_verified_at"] = (
                    app_module.time.time()
                    - app_module.OPERATOR_ADMIN_SESSION_TTL_SECONDS
                    - 1
                )
            state = self.client.get("/api/channels")

        self.assertFalse(state.get_json()["can_manage"])
        with self.client.session_transaction() as browser_session:
            self.assertFalse(browser_session.get("channel_admin"))

    def test_wrong_key_is_generic_and_rate_limited_without_secret_disclosure(self):
        wrong = "wrong-operator-key-" + ("W" * 32)
        with patch.dict(os.environ, {app_module.PANEL_ADMIN_RECOVERY_KEY_ENV: self.KEY}):
            first = self.post(wrong)
            second = self.post(wrong)
            limited = self.post(wrong)

        self.assertEqual(401, first.status_code)
        self.assertEqual("invalid_operator_key", first.get_json()["error_code"])
        self.assertEqual(401, second.status_code)
        self.assertEqual(429, limited.status_code)
        self.assertEqual("rate_limited", limited.get_json()["error_code"])
        self.assertEqual("60", limited.headers["Retry-After"])
        for response in (first, second, limited):
            body = response.get_data(as_text=True)
            self.assertNotIn(self.KEY, body)
            self.assertNotIn(wrong, body)
        rate_text = self.guard.rate_path.read_text(encoding="utf-8")
        self.assertNotIn(self.KEY, rate_text)
        self.assertNotIn(wrong, rate_text)
        with self.client.session_transaction() as browser_session:
            self.assertFalse(browser_session.get("channel_admin"))


if __name__ == "__main__":
    unittest.main()
