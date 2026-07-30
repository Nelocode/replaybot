import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app as app_module
from tests.admin_session import grant_operator_admin, install_operator_key
from telegram_auth import AuthOutcome


class TelegramSwitchRoutesTestCase(unittest.TestCase):
    API_ID = "123456"
    API_HASH = "0123456789abcdef0123456789abcdef"
    OLD_PHONE = "+573001111111"
    NEW_PHONE = "+573002222222"
    CSRF = "t" * 48

    def setUp(self):
        self.operator_key = install_operator_key(self)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        data_dir = Path(self.temporary.name) / "data"
        replacements = {
            "DATA_DIR": data_dir,
            "TG_SESSION_BASE": data_dir / "tg_session",
            "TG_SWITCH_SESSION_BASE": data_dir / "tg_switch_session",
            "TG_SWITCH_ROLLBACK_DIR": data_dir / ".tg_switch_rollback",
        }
        for name, value in replacements.items():
            patcher = patch.object(app_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        grant_operator_admin(
            self.client,
            self.operator_key,
            csrf=self.CSRF,
        )

    def headers(self):
        return {"X-Channel-CSRF": self.CSRF}

    def env(self):
        return patch.dict(
            os.environ,
            {
                "TG_API_ID": self.API_ID,
                "TG_API_HASH": self.API_HASH,
                "TG_PHONE": self.OLD_PHONE,
            },
            clear=True,
        )

    def test_start_reuses_saved_api_credentials_and_keeps_them_private(self):
        manager = Mock()
        manager.has_pending.return_value = False
        manager.begin.return_value = AuthOutcome(
            {"ok": True, "needs_code": True, "delivery": "Telegram"},
            attempt_token="browser-attempt-token",
        )
        with (
            self.env(),
            patch.object(app_module, "_telegram_session_is_authorized", return_value=True),
            patch.object(app_module, "_telegram_switch_auth", manager),
        ):
            response = self.client.post(
                "/api/switch_telegram",
                json={"phone": self.NEW_PHONE},
                headers=self.headers(),
            )

        self.assertEqual(200, response.status_code)
        data = response.get_json()
        self.assertTrue(data["needs_code"])
        manager.begin.assert_called_once_with(
            int(self.API_ID), self.API_HASH, self.NEW_PHONE, None
        )
        encoded = json.dumps(data)
        self.assertNotIn(self.API_HASH, encoded)
        self.assertNotIn(self.NEW_PHONE, encoded)

    def test_verified_candidate_is_promoted_once(self):
        manager = Mock()
        manager.verify_code.return_value = AuthOutcome(
            {"ok": True},
            (int(self.API_ID), self.API_HASH, self.NEW_PHONE),
        )
        with (
            self.env(),
            patch.object(app_module, "_telegram_switch_auth", manager),
            patch.object(
                app_module,
                "_promote_telegram_candidate",
                return_value=(True, "cambiada", True, True),
            ) as promote,
        ):
            response = self.client.post(
                "/api/switch_telegram/code",
                json={"code": "12345", "auth_attempt": "attempt"},
                headers=self.headers(),
            )

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.get_json()["switched"])
        promote.assert_called_once_with(
            int(self.API_ID), self.API_HASH, self.NEW_PHONE
        )

    def test_cancel_removes_only_candidate_session(self):
        manager = Mock()
        manager.cancel.return_value = AuthOutcome({"ok": True})
        primary = Path(f"{app_module.TG_SESSION_BASE}.session")
        candidate = Path(f"{app_module.TG_SWITCH_SESSION_BASE}.session")
        primary.parent.mkdir(parents=True, exist_ok=True)
        primary.write_text("old", encoding="utf-8")
        candidate.write_text("candidate", encoding="utf-8")
        with patch.object(app_module, "_telegram_switch_auth", manager):
            response = self.client.post(
                "/api/switch_telegram/cancel",
                json={"auth_attempt": "attempt"},
                headers=self.headers(),
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual("old", primary.read_text(encoding="utf-8"))
        self.assertFalse(candidate.exists())

    def test_pending_recovery_blocks_another_telegram_switch(self):
        app_module.TG_SWITCH_ROLLBACK_DIR.mkdir(parents=True)
        backup = app_module.TG_SWITCH_ROLLBACK_DIR / "tg_session.session"
        backup.write_text("preserved", encoding="utf-8")
        manager = Mock()

        with (
            self.env(),
            patch.object(app_module, "_telegram_session_is_authorized", return_value=True),
            patch.object(app_module, "_telegram_switch_auth", manager),
        ):
            response = self.client.post(
                "/api/switch_telegram",
                json={"phone": self.NEW_PHONE},
                headers=self.headers(),
            )

        self.assertEqual(409, response.status_code)
        self.assertEqual("recovery_required", response.get_json()["error_code"])
        manager.begin.assert_not_called()
        self.assertEqual("preserved", backup.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
