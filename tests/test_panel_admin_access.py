import asyncio
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

import panel_admin_access as panel_access_module

from panel_admin_access import (
    CHALLENGE_FILENAME,
    DELIVERY_STATUS_FILENAME,
    MAX_TRACKED_BROWSERS,
    OUTBOX_FILENAME,
    RATE_FILENAME,
    PanelAdminAccessStore,
    _exclusive_file_lock,
    deliver_pending_challenge,
)


class MutableClock:
    def __init__(self, value=1_000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeTelegramClient:
    def __init__(self, *, failures=0):
        self.failures = failures
        self.calls = []

    async def send_message(self, peer, message):
        self.calls.append((peer, message))
        if len(self.calls) <= self.failures:
            raise RuntimeError("transport failed without echoing message data")
        return object()


class PanelAdminAccessStoreTestCase(unittest.TestCase):
    CODE = "01234567"
    REQUEST_ID = "request-id-with-enough-entropy"
    BROWSER_A = "browser-a-" + "a" * 32
    BROWSER_B = "browser-b-" + "b" * 32
    SECRET = b"s" * 32

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "panel_admin_access"
        self.clock = MutableClock()
        self.store = self.make_store()

    def make_store(self, **overrides):
        settings = {
            "ttl_seconds": 180,
            "max_attempts": 5,
            "cooldown_seconds": 30,
            "clock": self.clock,
            "code_factory": lambda: self.CODE,
            "request_id_factory": lambda: self.REQUEST_ID,
        }
        settings.update(overrides)
        return PanelAdminAccessStore(self.directory, self.SECRET, **settings)

    def read_json(self, filename):
        return json.loads((self.directory / filename).read_text(encoding="utf-8"))

    def deliver(self, client=None, **kwargs):
        client = client or FakeTelegramClient()
        result = asyncio.run(
            deliver_pending_challenge(
                client,
                self.directory,
                clock=self.clock,
                retry_delay_seconds=0,
                **kwargs,
            )
        )
        return client, result

    def test_create_challenge_keeps_raw_code_only_in_private_outbox(self):
        result = self.store.create_challenge(self.BROWSER_A)

        self.assertTrue(result["ok"])
        self.assertEqual("queued", result["state"])
        self.assertEqual(180, result["expires_in"])
        self.assertEqual(5, result["attempts_remaining"])
        self.assertNotIn(self.CODE, json.dumps(result))

        challenge = self.read_json(CHALLENGE_FILENAME)
        outbox = self.read_json(OUTBOX_FILENAME)
        rate = self.read_json(RATE_FILENAME)
        self.assertEqual(self.CODE, outbox["code"])
        self.assertNotIn(self.CODE, json.dumps(challenge))
        self.assertNotIn(self.CODE, json.dumps(rate))
        self.assertNotIn(self.BROWSER_A, json.dumps(challenge))
        self.assertNotIn(self.BROWSER_A, json.dumps(outbox))
        self.assertRegex(challenge["code_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual({}, challenge["browser_attempts"])

        if os.name != "nt":
            self.assertEqual(0, stat.S_IMODE(self.directory.stat().st_mode) & 0o077)
            for filename in (CHALLENGE_FILENAME, OUTBOX_FILENAME, RATE_FILENAME):
                mode = stat.S_IMODE((self.directory / filename).stat().st_mode)
                self.assertEqual(0, mode & 0o077)
        self.assertEqual([], list(self.directory.glob("*.tmp")))

    def test_all_browsers_reuse_one_global_challenge_without_new_delivery(self):
        first = self.store.create_challenge(self.BROWSER_A)
        original_outbox = self.read_json(OUTBOX_FILENAME)
        browser_b_status = self.store.get_status(self.BROWSER_B)
        self.clock.advance(31)
        browser_b_request = self.store.create_challenge(self.BROWSER_B)

        self.assertTrue(first["ok"])
        self.assertTrue(browser_b_status["ok"])
        self.assertEqual("queued", browser_b_status["state"])
        self.assertTrue(browser_b_request["ok"])
        self.assertTrue(browser_b_request["reused"])
        self.assertEqual(first["request_id"], browser_b_request["request_id"])
        self.assertEqual(self.REQUEST_ID, self.read_json(CHALLENGE_FILENAME)["request_id"])
        self.assertEqual(original_outbox, self.read_json(OUTBOX_FILENAME))

        first_client, delivered = self.deliver()
        second_client, repeated_delivery = self.deliver()
        self.assertTrue(delivered["ok"])
        self.assertEqual(1, len(first_client.calls))
        self.assertEqual("no_pending_delivery", repeated_delivery["error_code"])
        self.assertEqual([], second_client.calls)

    def test_same_browser_resumes_active_challenge_without_replacing_it(self):
        first = self.store.create_challenge(self.BROWSER_A)
        self.clock.advance(12)
        resumed = self.store.create_challenge(self.BROWSER_A)

        self.assertEqual(first["request_id"], resumed["request_id"])
        self.assertTrue(resumed["request_in_progress"])
        self.assertEqual(168, resumed["expires_in"])
        self.assertEqual(self.CODE, self.read_json(OUTBOX_FILENAME)["code"])

    def test_cooldown_survives_success_and_store_recreation(self):
        self.store.create_challenge(self.BROWSER_A)
        self.deliver()
        self.assertTrue(self.store.verify(self.BROWSER_A, self.CODE)["ok"])

        recreated = self.make_store()
        limited = recreated.create_challenge(self.BROWSER_B)
        self.assertEqual("rate_limited", limited["error_code"])
        self.assertEqual(30, limited["retry_after"])

        self.clock.advance(31)
        allowed = recreated.create_challenge(self.BROWSER_B)
        self.assertTrue(allowed["ok"])
        self.assertEqual("queued", allowed["state"])

    def test_delivery_then_wrong_attempts_and_successful_single_use_verify(self):
        self.store.create_challenge(self.BROWSER_A)
        client, delivered = self.deliver()

        self.assertTrue(delivered["ok"])
        self.assertEqual("sent", delivered["state"])
        self.assertEqual("me", client.calls[0][0])
        self.assertIn(self.CODE, client.calls[0][1])
        self.assertFalse((self.directory / OUTBOX_FILENAME).exists())
        self.assertNotIn(
            self.CODE,
            (self.directory / DELIVERY_STATUS_FILENAME).read_text(encoding="utf-8"),
        )
        self.assertEqual("sent", self.store.get_status(self.BROWSER_A)["state"])

        for expected_remaining in (4, 3, 2, 1):
            rejected = self.store.verify(self.BROWSER_A, "99999999")
            self.assertEqual("invalid_code", rejected["error_code"])
            self.assertEqual(expected_remaining, rejected["attempts_remaining"])

        verified = self.store.verify(self.BROWSER_A, self.CODE)
        replay = self.store.verify(self.BROWSER_A, self.CODE)
        self.assertEqual(
            {"ok": True, "authorized": True, "state": "verified"},
            verified,
        )
        self.assertEqual("no_active_challenge", replay["error_code"])
        self.assertFalse((self.directory / CHALLENGE_FILENAME).exists())
        self.assertFalse((self.directory / DELIVERY_STATUS_FILENAME).exists())

    def test_fifth_wrong_code_exhausts_only_that_browser(self):
        self.store.create_challenge(self.BROWSER_A)
        self.deliver()

        for _ in range(4):
            self.assertEqual(
                "invalid_code",
                self.store.verify(self.BROWSER_A, "87654321")["error_code"],
            )
        exhausted = self.store.verify(self.BROWSER_A, "87654321")

        self.assertEqual("attempts_exhausted", exhausted["error_code"])
        self.assertEqual(0, exhausted["attempts_remaining"])
        self.assertEqual(
            "attempts_exhausted",
            self.store.verify(self.BROWSER_A, self.CODE)["error_code"],
        )
        self.assertTrue((self.directory / CHALLENGE_FILENAME).exists())
        self.assertEqual(5, self.store.get_status(self.BROWSER_B)["attempts_remaining"])
        self.assertTrue(self.store.verify(self.BROWSER_B, self.CODE)["authorized"])

    def test_verify_waits_until_delivery_is_confirmed(self):
        self.store.create_challenge(self.BROWSER_A)

        result = self.store.verify(self.BROWSER_A, self.CODE)

        self.assertEqual("not_delivered", result["error_code"])
        self.assertEqual(5, self.store.get_status(self.BROWSER_A)["attempts_remaining"])

    def test_expired_challenge_is_removed_with_its_raw_outbox(self):
        self.store.create_challenge(self.BROWSER_A)
        self.clock.advance(181)

        expired = self.store.get_status(self.BROWSER_A)

        self.assertEqual("expired_code", expired["error_code"])
        self.assertFalse((self.directory / CHALLENGE_FILENAME).exists())
        self.assertFalse((self.directory / OUTBOX_FILENAME).exists())
        self.assertTrue((self.directory / RATE_FILENAME).exists())

    def test_cancel_is_local_noop_and_never_invalidates_global_challenge(self):
        self.store.create_challenge(self.BROWSER_A)
        original_challenge = self.read_json(CHALLENGE_FILENAME)
        original_outbox = self.read_json(OUTBOX_FILENAME)

        cancelled = self.store.cancel(self.BROWSER_B)
        self.assertEqual("cancelled", cancelled["state"])
        self.assertTrue(cancelled["global_challenge_preserved"])
        self.assertTrue((self.directory / CHALLENGE_FILENAME).exists())
        self.assertEqual(original_challenge, self.read_json(CHALLENGE_FILENAME))
        self.assertEqual(original_outbox, self.read_json(OUTBOX_FILENAME))
        self.assertTrue(self.store.create_challenge(self.BROWSER_B)["reused"])

    def test_delivery_retries_are_limited_and_status_never_contains_code(self):
        self.store.create_challenge(self.BROWSER_A)
        client = FakeTelegramClient(failures=2)

        _, result = self.deliver(client, max_attempts=3)

        self.assertTrue(result["ok"])
        self.assertEqual(3, result["attempts"])
        self.assertEqual(3, len(client.calls))
        self.assertNotIn(self.CODE, json.dumps(result))
        delivery = self.read_json(DELIVERY_STATUS_FILENAME)
        self.assertEqual("sent", delivery["state"])
        self.assertEqual(3, delivery["attempts"])
        self.assertNotIn(self.CODE, json.dumps(delivery))

    def test_failed_delivery_cannot_exceed_persistent_attempt_limit(self):
        self.store.create_challenge(self.BROWSER_A)
        client = FakeTelegramClient(failures=99)

        _, first = self.deliver(client, max_attempts=2)
        _, second = self.deliver(client, max_attempts=2)

        self.assertEqual("delivery_failed", first["error_code"])
        self.assertFalse(first["already_failed"])
        self.assertEqual("no_pending_delivery", second["error_code"])
        self.assertEqual(2, len(client.calls))
        self.assertFalse((self.directory / OUTBOX_FILENAME).exists())
        self.assertEqual("delivery_failed", self.store.get_status(self.BROWSER_A)["state"])
        self.assertEqual(
            "not_delivered",
            self.store.verify(self.BROWSER_A, self.CODE)["error_code"],
        )
        before_cooldown = self.store.create_challenge(self.BROWSER_B)
        self.assertTrue(before_cooldown["ok"])
        self.assertEqual("delivery_failed", before_cooldown["state"])
        self.assertEqual(30, before_cooldown["retry_after"])
        self.assertTrue(before_cooldown["reused"])
        self.clock.advance(31)
        self.assertEqual(
            "queued",
            self.store.create_challenge(self.BROWSER_A)["state"],
        )

    def test_delivery_is_idle_without_a_matching_challenge(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / OUTBOX_FILENAME).write_text(
            json.dumps(
                {
                    "request_id": self.REQUEST_ID,
                    "code": self.CODE,
                    "expires_at": self.clock() + 180,
                }
            ),
            encoding="utf-8",
        )
        client, result = self.deliver()

        self.assertEqual("no_pending_delivery", result["error_code"])
        self.assertEqual([], client.calls)

    def test_browser_attempt_tracking_is_bounded_without_invalidating_otp(self):
        self.store.create_challenge(self.BROWSER_A)
        self.deliver()
        challenge = self.read_json(CHALLENGE_FILENAME)
        challenge["browser_attempts"] = {
            self.store._browser_digest(f"tracked-browser-{index:03d}-" + "x" * 20): 4
            for index in range(MAX_TRACKED_BROWSERS)
        }
        (self.directory / CHALLENGE_FILENAME).write_text(
            json.dumps(challenge, separators=(",", ":")),
            encoding="utf-8",
        )
        new_browser = "untracked-browser-" + "z" * 32

        rejected = self.store.verify(new_browser, "99999999")

        self.assertEqual("attempts_exhausted", rejected["error_code"])
        self.assertEqual(0, rejected["attempts_remaining"])
        self.assertEqual(
            MAX_TRACKED_BROWSERS,
            len(self.read_json(CHALLENGE_FILENAME)["browser_attempts"]),
        )
        self.assertTrue((self.directory / CHALLENGE_FILENAME).exists())
        self.assertTrue(self.store.verify(new_browser, self.CODE)["authorized"])

    def test_secret_and_factory_validation_fail_closed(self):
        with self.assertRaises(ValueError):
            PanelAdminAccessStore(self.directory, b"short")

        broken = self.make_store(code_factory=lambda: "123")
        with self.assertRaises(ValueError):
            broken.create_challenge(self.BROWSER_A)

    def test_lock_release_never_deletes_persistent_or_substituted_contents(self):
        lock_path = self.directory / ".ownership.lock"
        replacement_owner = "replacement-owner-token"

        with patch.object(Path, "unlink", side_effect=AssertionError("lock unlink")):
            with _exclusive_file_lock(lock_path):
                pass
            lock_path.write_text(replacement_owner, encoding="ascii")
            with _exclusive_file_lock(lock_path):
                pass

        self.assertTrue(lock_path.exists())
        self.assertEqual(replacement_owner, lock_path.read_text(encoding="ascii"))

    def test_advisory_lock_rejects_a_concurrent_owner_and_remains_reusable(self):
        lock_path = self.directory / ".contention.lock"
        outcome = []

        def contender():
            try:
                with _exclusive_file_lock(lock_path, wait_seconds=0.05):
                    outcome.append("acquired")
            except panel_access_module.PanelAccessBusy:
                outcome.append("busy")

        with _exclusive_file_lock(lock_path):
            thread = threading.Thread(target=contender)
            thread.start()
            thread.join(timeout=1)

        self.assertEqual(["busy"], outcome)
        self.assertTrue(lock_path.exists())
        with _exclusive_file_lock(lock_path, wait_seconds=0.05):
            pass

    def test_posix_and_windows_advisory_backends_are_selectable(self):
        lock_file = self.directory / ".backend.lock"
        lock_file.write_bytes(b"0")
        descriptor = os.open(lock_file, os.O_RDWR)

        class FakeFcntl:
            LOCK_EX = 1
            LOCK_NB = 2
            LOCK_UN = 4

            def __init__(self):
                self.operations = []

            def flock(self, supplied_descriptor, operation):
                self.operations.append((supplied_descriptor, operation))

        class FakeMsvcrt:
            LK_NBLCK = 10
            LK_UNLCK = 11

            def __init__(self):
                self.operations = []

            def locking(self, supplied_descriptor, operation, byte_count):
                self.operations.append((supplied_descriptor, operation, byte_count))

        try:
            fake_fcntl = FakeFcntl()
            with (
                patch.object(panel_access_module.os, "name", "posix"),
                patch.object(panel_access_module, "_fcntl", fake_fcntl),
            ):
                self.assertTrue(panel_access_module._try_advisory_lock(descriptor))
                panel_access_module._release_advisory_lock(descriptor)
            self.assertEqual(
                [(descriptor, 3), (descriptor, 4)],
                fake_fcntl.operations,
            )

            fake_msvcrt = FakeMsvcrt()
            with (
                patch.object(panel_access_module.os, "name", "nt"),
                patch.object(panel_access_module, "_msvcrt", fake_msvcrt),
            ):
                self.assertTrue(panel_access_module._try_advisory_lock(descriptor))
                panel_access_module._release_advisory_lock(descriptor)
            self.assertEqual(
                [(descriptor, 10, 1), (descriptor, 11, 1)],
                fake_msvcrt.operations,
            )
        finally:
            os.close(descriptor)


if __name__ == "__main__":
    unittest.main()
