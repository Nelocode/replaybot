import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
from tests.admin_session import grant_operator_admin, install_operator_key
from interaction_state import PersistentInteractionState
from test_mode import (
    interaction_state_summary,
    normalize_whatsapp_phone,
    reset_latest_interaction,
    reset_whatsapp_interaction_by_number,
)


class TestModeStateTests(unittest.TestCase):
    def test_whatsapp_phone_normalization_is_strict_and_ephemeral(self):
        self.assertEqual("573001234567", normalize_whatsapp_phone("+57 300-123-4567"))
        for value in (
            None, "", "+123", "+01234567", "573001234567", "00573001234567",
            "+57abc1234567", "+57\n3001234567", "+573001234567@s.whatsapp.net",
        ):
            with self.subTest(value=value):
                self.assertIsNone(normalize_whatsapp_phone(value))

    def test_specific_whatsapp_number_resolves_v2_alias_without_storing_number(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "wa_interaction_state.json"
            phone = "+573001234567"
            pn_key = PersistentInteractionState._fingerprint(
                "contact", "573001234567@s.whatsapp.net"
            )
            canonical_key = "canonical-lid-hash"
            original = {
                "version": 2,
                "contacts": {
                    canonical_key: {
                        "phase": 2,
                        "language": "es",
                        "recent_events": ["event-hash"],
                        "updated_at": 50,
                    },
                    "unrelated": {"phase": 2, "updated_at": 60},
                },
                "aliases": {pn_key: canonical_key},
            }
            state_path.write_text(json.dumps(original), encoding="utf-8")

            result = reset_whatsapp_interaction_by_number(
                state_path,
                backup_dir=root / "backups",
                phone=phone,
                language="en",
            )

            serialized = state_path.read_text(encoding="utf-8")
            current = json.loads(serialized)
            self.assertEqual(0, current["contacts"][canonical_key]["phase"])
            self.assertEqual("en", current["contacts"][canonical_key]["language"])
            self.assertEqual(
                "operator_seed",
                current["contacts"][canonical_key]["language_source"],
            )
            self.assertIsNone(current["contacts"][canonical_key]["language_candidate"])
            self.assertEqual([], current["contacts"][canonical_key]["recent_events"])
            self.assertNotIn("573001234567", serialized)
            self.assertTrue(result["reset"])

    def test_specific_new_whatsapp_number_is_preconfigured_by_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "wa_interaction_state.json"

            result = reset_whatsapp_interaction_by_number(
                state_path,
                backup_dir=root / "backups",
                phone="+573001234567",
                language=None,
            )

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(2, payload["version"])
            self.assertEqual(1, len(payload["contacts"]))
            state = next(iter(payload["contacts"].values()))
            self.assertEqual(
                {
                    "phase": 0,
                    "language": None,
                    "language_source": None,
                    "language_candidate": None,
                    "language_candidate_streak": 0,
                    "recent_events": [],
                    "updated_at": 0,
                    "reset_pending": True,
                },
                state,
            )
            self.assertEqual(2, len(payload["aliases"]))
            self.assertIsNone(result["backup"])
            self.assertNotIn("573001234567", state_path.read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("node"), "Node.js no está disponible")
    def test_lid_only_history_honors_number_reset_once_when_pn_is_observed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "wa_interaction_state.json"
            lid = "123456789@lid"
            pn = "573001234567@s.whatsapp.net"
            lid_key = PersistentInteractionState._fingerprint("contact", lid)
            state_path.write_text(
                json.dumps({
                    "version": 2,
                    "contacts": {
                        lid_key: {
                            "phase": 2,
                            "language": "es",
                            "recent_events": ["old-event-hash"],
                            "updated_at": 50,
                        },
                    },
                    "aliases": {lid_key: lid_key},
                }),
                encoding="utf-8",
            )

            reset_whatsapp_interaction_by_number(
                state_path,
                backup_dir=root / "backups",
                phone="+573001234567",
                language="en",
            )

            module_uri = (
                Path(__file__).resolve().parents[1] / "interaction_state.mjs"
            ).as_uri()
            script = """
const { PersistentInteractionState } = await import(process.argv[1]);
const store = new PersistentInteractionState({
  filePath: process.argv[2],
  logger: { error() {} },
});
const contactAliases = [process.argv[3], process.argv[4]];
const first = store.register({
  contactId: process.argv[4], contactAliases,
  eventId: 'after-reset', kind: 'content', detectedLanguage: 'fr',
});
const second = store.register({
  contactId: process.argv[3], contactAliases,
  eventId: 'after-reset-2', kind: 'content', detectedLanguage: 'fr',
});
process.stdout.write(JSON.stringify({ first, second }));
"""
            completed = subprocess.run(
                [
                    shutil.which("node"), "--input-type=module", "-e", script,
                    module_uri, str(state_path), lid, pn,
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            decisions = json.loads(completed.stdout)
            self.assertEqual("step1", decisions["first"]["responseKey"])
            self.assertEqual("en", decisions["first"]["language"])
            self.assertEqual("step2", decisions["second"]["responseKey"])
            self.assertEqual("fr", decisions["second"]["language"])

            serialized = state_path.read_text(encoding="utf-8")
            payload = json.loads(serialized)
            self.assertEqual(1, len(payload["contacts"]))
            self.assertNotIn("reset_pending", serialized)
            self.assertNotIn("573001234567", serialized)
            self.assertNotIn("123456789", serialized)

    def test_reset_latest_whatsapp_keeps_aliases_and_creates_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "wa_interaction_state.json"
            original = {
                "version": 2,
                "contacts": {
                    "older": {"phase": 2, "updated_at": 10},
                    "latest": {"phase": 2, "updated_at": 20},
                },
                "aliases": {
                    "latest-phone": "latest",
                    "latest-lid": "latest",
                },
            }
            state_path.write_text(json.dumps(original), encoding="utf-8")

            result = reset_latest_interaction(
                state_path,
                channel="whatsapp",
                backup_dir=root / "backups",
                language="fr",
            )

            current = json.loads(state_path.read_text(encoding="utf-8"))
            backup = json.loads(Path(result["backup"]).read_text(encoding="utf-8"))
            self.assertTrue(result["reset"])
            self.assertEqual(0, current["contacts"]["latest"]["phase"])
            self.assertEqual("fr", current["contacts"]["latest"]["language"])
            self.assertEqual(
                "operator_seed",
                current["contacts"]["latest"]["language_source"],
            )
            self.assertIsNone(current["contacts"]["latest"]["language_candidate"])
            self.assertEqual([], current["contacts"]["latest"]["recent_events"])
            self.assertEqual(original["aliases"], current["aliases"])
            self.assertEqual(original, backup)

    def test_empty_or_missing_state_is_safe_noop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = reset_latest_interaction(
                root / "missing.json",
                channel="telegram",
                backup_dir=root / "backups",
            )
            self.assertEqual({"reset": False, "remaining": 0, "backup": None}, result)
            self.assertEqual(
                {"conversation_count": 0, "latest_updated_at": None},
                interaction_state_summary(root / "missing.json"),
            )

    def test_reset_makes_same_contact_receive_step_one_in_selected_language(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "tg_interaction_state.json"
            state = PersistentInteractionState(state_path)
            state.register(contact_id=451, event_id="first", kind="content", detected_language="es")
            state.register(contact_id=451, event_id="second", kind="content", detected_language="es")

            reset_latest_interaction(
                state_path,
                channel="telegram",
                backup_dir=root / "backups",
                language="en",
            )
            decision = PersistentInteractionState(state_path).register(
                contact_id=451,
                event_id="after-reset",
                kind="content",
                detected_language="fr",
            )

            self.assertEqual("step1", decision.response_key)
            self.assertEqual("en", decision.language)


class TestModeRoutesTests(unittest.TestCase):
    CSRF = "t" * 48

    def setUp(self):
        self.operator_key = install_operator_key(self)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name) / "data"
        replacements = {
            "DATA_DIR": self.data_dir,
            "TG_INTERACTION_STATE_FILE": self.data_dir / "tg_interaction_state.json",
            "WA_INTERACTION_STATE_FILE": self.data_dir / "wa_interaction_state.json",
            "TEST_MODE_FILE": self.data_dir / "test_mode.json",
            "TEST_MODE_BACKUP_DIR": self.data_dir / "test_mode_backups",
        }
        for name, value in replacements.items():
            patcher = patch.object(app_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def authorize(self):
        grant_operator_admin(
            self.client,
            self.operator_key,
            csrf=self.CSRF,
        )

    def headers(self):
        return {"X-Channel-CSRF": self.CSRF}

    def seed_state(self, path: Path, version: int):
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": version,
            "contacts": {"tester-hash": {"phase": 2, "updated_at": 50}},
        }
        if version == 2:
            payload["aliases"] = {"tester-phone-hash": "tester-hash"}
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_state_and_enable_require_verified_browser_and_csrf(self):
        anonymous_get = self.client.get("/api/test_mode")
        self.assertEqual(403, anonymous_get.status_code)
        self.assertEqual("admin_required", anonymous_get.get_json()["error_code"])
        self.assertNotIn("telegram", anonymous_get.get_json())

        anonymous_post = self.client.post(
            "/api/test_mode",
            json={"enabled": True},
            headers=self.headers(),
        )
        self.assertEqual(403, anonymous_post.status_code)

        self.authorize()
        missing_csrf = self.client.post("/api/test_mode", json={"enabled": True})
        self.assertEqual(403, missing_csrf.status_code)
        self.assertEqual("csrf_invalid", missing_csrf.get_json()["error_code"])

        enabled = self.client.post(
            "/api/test_mode",
            json={"enabled": True},
            headers=self.headers(),
        )
        self.assertEqual(200, enabled.status_code)
        self.assertTrue(enabled.get_json()["enabled"])
        self.assertNotIn("telegram", enabled.get_json())
        self.assertNotIn("whatsapp", enabled.get_json())

    def test_reset_is_blocked_until_test_mode_is_enabled(self):
        self.authorize()
        response = self.client.post(
            "/api/test_mode/reset",
            json={"channel": "both", "confirm": True},
            headers=self.headers(),
        )
        self.assertEqual(409, response.status_code)
        self.assertEqual("test_mode_disabled", response.get_json()["error_code"])

    def test_specific_whatsapp_number_requires_admin_csrf_and_valid_input(self):
        self.authorize()
        self.client.post(
            "/api/test_mode", json={"enabled": True}, headers=self.headers()
        )
        payload = {
            "channel": "whatsapp",
            "target": "number",
            "whatsapp_number": "+573001234567",
            "language": "en",
            "confirm": True,
        }
        missing_csrf = self.client.post("/api/test_mode/reset", json=payload)
        self.assertEqual(403, missing_csrf.status_code)

        invalid = self.client.post(
            "/api/test_mode/reset",
            json={**payload, "whatsapp_number": "+57oops"},
            headers=self.headers(),
        )
        self.assertEqual(400, invalid.status_code)
        self.assertEqual("invalid_whatsapp_number", invalid.get_json()["error_code"])
        self.assertEqual("no-store, private", invalid.headers["Cache-Control"])

        wrong_channel = self.client.post(
            "/api/test_mode/reset",
            json={**payload, "channel": "telegram"},
            headers=self.headers(),
        )
        self.assertEqual(400, wrong_channel.status_code)

    def test_specific_whatsapp_existing_and_new_responses_are_non_enumerable(self):
        self.authorize()
        self.client.post(
            "/api/test_mode", json={"enabled": True}, headers=self.headers()
        )
        self.seed_state(app_module.WA_INTERACTION_STATE_FILE, 2)

        def reset(phone):
            with (
                patch.object(app_module, "_test_mode_switch_conflict", return_value=None),
                patch.object(app_module, "_wa_process_running", return_value=False),
            ):
                return self.client.post(
                    "/api/test_mode/reset",
                    json={
                        "channel": "whatsapp",
                        "target": "number",
                        "whatsapp_number": phone,
                        "language": "en",
                        "confirm": True,
                    },
                    headers=self.headers(),
                )

        first = reset("+573001234567")
        second = reset("+573009876543")
        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)
        first_result = first.get_json()["results"]["whatsapp"]
        second_result = second.get_json()["results"]["whatsapp"]
        self.assertEqual(first.get_json(), second.get_json())
        self.assertEqual(first_result, second_result)
        self.assertEqual({"reset", "language"}, set(first_result))
        for response in (first, second):
            body = response.get_data(as_text=True)
            self.assertNotRegex(body, r"57300|@s\.whatsapp\.net|@hosted")
            self.assertEqual("no-store, private", response.headers["Cache-Control"])

    def test_enable_reports_persistence_failure_without_crashing(self):
        self.authorize()
        with patch.object(
            app_module,
            "save_test_mode",
            side_effect=OSError("simulated write failure"),
        ):
            response = self.client.post(
                "/api/test_mode",
                json={"enabled": True},
                headers=self.headers(),
            )

        self.assertEqual(500, response.status_code)
        self.assertFalse(response.get_json()["ok"])
        self.assertEqual(
            "test_mode_persist_failed",
            response.get_json()["error_code"],
        )

    def test_account_switch_conflict_prevents_reset_and_worker_stop(self):
        self.authorize()
        self.client.post(
            "/api/test_mode",
            json={"enabled": True},
            headers=self.headers(),
        )
        self.seed_state(app_module.TG_INTERACTION_STATE_FILE, 1)
        with (
            patch.object(
                app_module,
                "_test_mode_switch_conflict",
                return_value="Termina primero el cambio de cuenta.",
            ),
            patch.object(app_module, "_stop_telegram_worker") as stop_telegram,
        ):
            response = self.client.post(
                "/api/test_mode/reset",
                json={"channel": "telegram", "confirm": True},
                headers=self.headers(),
            )
        self.assertEqual(409, response.status_code)
        self.assertEqual("account_switch_in_progress", response.get_json()["error_code"])
        stop_telegram.assert_not_called()

    def test_reset_both_keeps_backups_and_restarts_only_active_workers(self):
        self.authorize()
        self.client.post(
            "/api/test_mode",
            json={"enabled": True},
            headers=self.headers(),
        )
        self.seed_state(app_module.TG_INTERACTION_STATE_FILE, 1)
        self.seed_state(app_module.WA_INTERACTION_STATE_FILE, 2)

        with (
            patch.object(app_module, "_test_mode_switch_conflict", return_value=None),
            patch.object(app_module, "_tracked_telegram_pid", return_value=101),
            patch.object(app_module, "_is_telegram_worker_pid", return_value=True),
            patch.object(app_module, "_stop_telegram_worker") as stop_telegram,
            patch.object(app_module, "restart_telegram_worker", return_value=(True, "ok")) as restart_telegram,
            patch.object(app_module, "_wa_process_running", return_value=True),
            patch.object(app_module, "_stop_wa_process") as stop_whatsapp,
            patch.object(app_module, "restart_wa_bot", return_value=202) as restart_whatsapp,
        ):
            response = self.client.post(
                "/api/test_mode/reset",
                json={"channel": "both", "language": "en", "confirm": True},
                headers=self.headers(),
            )

        self.assertEqual(200, response.status_code)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["results"]["telegram"]["backup_created"])
        self.assertTrue(data["results"]["whatsapp"]["backup_created"])
        telegram_state = json.loads(app_module.TG_INTERACTION_STATE_FILE.read_text(encoding="utf-8"))
        whatsapp_state = json.loads(app_module.WA_INTERACTION_STATE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(0, telegram_state["contacts"]["tester-hash"]["phase"])
        self.assertEqual("en", telegram_state["contacts"]["tester-hash"]["language"])
        self.assertEqual(0, whatsapp_state["contacts"]["tester-hash"]["phase"])
        self.assertEqual("en", whatsapp_state["contacts"]["tester-hash"]["language"])
        stop_telegram.assert_called_once()
        restart_telegram.assert_called_once()
        stop_whatsapp.assert_called_once_with(self.data_dir / "wa_bot.pid")
        restart_whatsapp.assert_called_once()

    def test_telegram_restart_exception_does_not_skip_whatsapp_restart(self):
        self.authorize()
        self.client.post(
            "/api/test_mode",
            json={"enabled": True},
            headers=self.headers(),
        )
        self.seed_state(app_module.TG_INTERACTION_STATE_FILE, 1)
        self.seed_state(app_module.WA_INTERACTION_STATE_FILE, 2)

        with (
            patch.object(app_module, "_test_mode_switch_conflict", return_value=None),
            patch.object(app_module, "_tracked_telegram_pid", return_value=101),
            patch.object(app_module, "_is_telegram_worker_pid", return_value=True),
            patch.object(app_module, "_stop_telegram_worker"),
            patch.object(app_module, "restart_telegram_worker", side_effect=OSError("boom")),
            patch.object(app_module, "_wa_process_running", return_value=True),
            patch.object(app_module, "_stop_wa_process"),
            patch.object(app_module, "restart_wa_bot", return_value=202) as restart_whatsapp,
        ):
            response = self.client.post(
                "/api/test_mode/reset",
                json={"channel": "both", "language": "auto", "confirm": True},
                headers=self.headers(),
            )

        self.assertEqual(503, response.status_code)
        self.assertFalse(response.get_json()["ok"])
        self.assertIn(
            "Telegram no pudo reiniciarse automáticamente.",
            response.get_json()["restart_warnings"],
        )
        restart_whatsapp.assert_called_once()

    def test_second_channel_failure_rolls_back_first_channel_atomically(self):
        self.authorize()
        self.client.post(
            "/api/test_mode",
            json={"enabled": True},
            headers=self.headers(),
        )
        self.seed_state(app_module.TG_INTERACTION_STATE_FILE, 1)
        self.seed_state(app_module.WA_INTERACTION_STATE_FILE, 2)
        telegram_before = app_module.TG_INTERACTION_STATE_FILE.read_bytes()
        whatsapp_before = app_module.WA_INTERACTION_STATE_FILE.read_bytes()
        real_reset = app_module.reset_latest_interaction

        def fail_on_whatsapp(state_path, *, channel, backup_dir, language=None):
            if channel == "whatsapp":
                raise OSError("simulated second-channel failure")
            return real_reset(
                state_path,
                channel=channel,
                backup_dir=backup_dir,
                language=language,
            )

        with (
            patch.object(app_module, "_test_mode_switch_conflict", return_value=None),
            patch.object(app_module, "_tracked_telegram_pid", return_value=None),
            patch.object(app_module, "_wa_process_running", return_value=False),
            patch.object(
                app_module,
                "reset_latest_interaction",
                side_effect=fail_on_whatsapp,
            ),
        ):
            response = self.client.post(
                "/api/test_mode/reset",
                json={"channel": "both", "language": "fr", "confirm": True},
                headers=self.headers(),
            )

        self.assertEqual(500, response.status_code)
        self.assertFalse(response.get_json()["ok"])
        self.assertTrue(response.get_json()["rollback_complete"])
        self.assertEqual({}, response.get_json()["results"])
        self.assertEqual(telegram_before, app_module.TG_INTERACTION_STATE_FILE.read_bytes())
        self.assertEqual(whatsapp_before, app_module.WA_INTERACTION_STATE_FILE.read_bytes())


if __name__ == "__main__":
    unittest.main()
