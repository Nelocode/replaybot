import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from wa_incident_health import (
    LEDGER_BASE_KEYS,
    public_wa_incident_health,
    read_wa_incident_history,
)


NOW_MS = 1_700_000_000_000


def incident_payload(*, terminal=True, resolved=False):
    return {
        "incident_id": "incident_1",
        "started_at": NOW_MS - 20_000,
        "last_event_at": NOW_MS - 10_000,
        "resolved_at": NOW_MS - 5_000 if resolved else None,
        "status_code": 440,
        "status_name": "connection_replaced",
        "category": "session_conflict",
        "action": "check_duplicate_session",
        "terminal": terminal,
        "reauth_required": terminal,
        "disconnect_count": 1,
        "reconnect_attempts": 0,
        "precursors": ["worker_lease_conflict", "private-signal"],
        "last_signature": "private-signature",
        "message": "private-message",
    }


def failure_payload(*, terminal=True):
    return {
        "failure_id": "failure_1",
        "incident_id": "incident_1",
        "at": NOW_MS - 10_000,
        "status_code": 440,
        "status_name": "connection_replaced",
        "category": "session_conflict",
        "action": "check_duplicate_session",
        "terminal": terminal,
        "reauth_required": terminal,
        "raw_error": "private-error",
        "jid": "573001234567@s.whatsapp.net",
    }


def snapshot_payload(**overrides):
    payload = {
        "schema_version": 1,
        "worker_revision": "worker_1",
        "updated_at": NOW_MS,
        "heartbeat_at": NOW_MS,
        "connection": "open",
        "connection_since": NOW_MS - 30_000,
        "last_open_at": NOW_MS - 30_000,
        "last_disconnect_at": None,
        "condition": "healthy",
        "failure_likelihood": "low",
        "signals": [],
        "likely_failure_modes": [],
        "active_incident": None,
        "last_incident": None,
        "last_failure": None,
        "ledger_integrity": "verified",
        "ledger_sequence": 0,
    }
    payload.update(overrides)
    return payload


def ledger_record(sequence, previous_hash, **overrides):
    base = {
        "schema_version": 1,
        "sequence": sequence,
        "record_id": f"record_{sequence}",
        "at": NOW_MS + sequence,
        "event": "connection_closed" if sequence % 2 else "health_transition",
        "lifecycle": "opened" if sequence % 2 else "observation",
        "incident_id": "incident_1" if sequence % 2 else None,
        "condition": "critical" if sequence % 2 else "warning",
        "status_code": 440 if sequence % 2 else None,
        "status_name": "connection_replaced" if sequence % 2 else "unknown",
        "category": "session_conflict" if sequence % 2 else "unknown",
        "action": "check_duplicate_session" if sequence % 2 else "inspect_worker",
        "terminal": sequence % 2 == 1,
        "reauth_required": sequence % 2 == 1,
        "reconnect_attempt": sequence - 1,
        "reconnect_delay_ms": (sequence - 1) * 1_000,
        "signals": ["terminal_session_failure"] if sequence % 2 else [],
        "previous_hash": previous_hash,
    }
    base.update(overrides)
    ordered = {key: base[key] for key in LEDGER_BASE_KEYS}
    serialized = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))
    record_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return {**ordered, "record_hash": record_hash}


def write_ledger(path, count):
    records = []
    previous_hash = None
    for sequence in range(1, count + 1):
        record = ledger_record(sequence, previous_hash)
        records.append(record)
        previous_hash = record["record_hash"]
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )
    return records


class WhatsAppIncidentHealthTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.snapshot_path = self.directory / "wa_incident_health.json"
        self.ledger_path = self.directory / "wa_incidents.jsonl"

    def tearDown(self):
        self.temporary.cleanup()

    def write_snapshot(self, **overrides):
        self.snapshot_path.write_text(
            json.dumps(snapshot_payload(**overrides)),
            encoding="utf-8",
        )

    def test_snapshot_is_allowlisted_and_admin_details_are_redacted(self):
        self.write_snapshot(
            active_incident=incident_payload(),
            last_incident=incident_payload(resolved=True),
            last_failure=failure_payload(),
            signals=["terminal_session_failure", "private-signal"],
            likely_failure_modes=["duplicate_session", "private-mode"],
            phone="+573001234567",
            raw_error="private-top-level-error",
        )

        administrator = public_wa_incident_health(
            self.snapshot_path,
            can_manage=True,
            worker_running=False,
            now_ms=NOW_MS,
        )
        anonymous = public_wa_incident_health(
            self.snapshot_path,
            can_manage=False,
            worker_running=False,
            now_ms=NOW_MS,
        )

        self.assertEqual("connection_replaced", administrator["active_incident"]["status_name"])
        self.assertEqual(["worker_lease_conflict"], administrator["active_incident"]["precursors"])
        self.assertIsNone(anonymous["active_incident"])
        self.assertIsNone(anonymous["last_incident"])
        self.assertIsNone(anonymous["last_failure"])
        serialized = json.dumps(administrator)
        for private_value in (
            "573001234567",
            "private-message",
            "private-error",
            "private-signature",
            "private-signal",
            "private-mode",
            "private-top-level-error",
            "raw_error",
            "jid",
            "phone",
        ):
            self.assertNotIn(private_value, serialized)

    def test_missing_corrupt_and_stale_snapshots_never_report_healthy(self):
        missing = public_wa_incident_health(
            self.snapshot_path,
            can_manage=True,
            worker_running=True,
            now_ms=NOW_MS,
        )
        self.assertFalse(missing["available"])
        self.assertFalse(missing["telemetry_fresh"])
        self.assertEqual("unknown", missing["condition"])

        self.snapshot_path.write_text("{not-json", encoding="utf-8")
        corrupt = public_wa_incident_health(
            self.snapshot_path,
            can_manage=True,
            worker_running=True,
            now_ms=NOW_MS,
        )
        self.assertFalse(corrupt["available"])
        self.assertEqual("unknown", corrupt["condition"])

        self.write_snapshot(heartbeat_at=NOW_MS - 90_001)
        stale = public_wa_incident_health(
            self.snapshot_path,
            can_manage=True,
            worker_running=True,
            now_ms=NOW_MS,
            stale_after_ms=90_000,
        )
        self.assertTrue(stale["available"])
        self.assertFalse(stale["telemetry_fresh"])
        self.assertEqual("unknown", stale["condition"])
        self.assertEqual("unknown", stale["failure_likelihood"])

        self.write_snapshot()
        stopped = public_wa_incident_health(
            self.snapshot_path,
            can_manage=True,
            worker_running=False,
            now_ms=NOW_MS,
        )
        self.assertTrue(stopped["telemetry_fresh"])
        self.assertEqual("unknown", stopped["condition"])

    def test_terminal_incident_remains_critical_when_stale_and_stopped(self):
        self.write_snapshot(
            heartbeat_at=NOW_MS - 600_000,
            connection="closed",
            condition="critical",
            failure_likelihood="high",
            signals=["terminal_session_failure"],
            likely_failure_modes=["authentication_loss", "duplicate_session"],
            active_incident=incident_payload(),
            last_failure=failure_payload(),
        )

        health = public_wa_incident_health(
            self.snapshot_path,
            can_manage=True,
            worker_running=False,
            now_ms=NOW_MS,
        )

        self.assertTrue(health["available"])
        self.assertFalse(health["telemetry_fresh"])
        self.assertFalse(health["worker_running"])
        self.assertTrue(health["terminal_incident"])
        self.assertEqual("critical", health["condition"])
        self.assertEqual("high", health["failure_likelihood"])
        self.assertEqual(440, health["active_incident"]["status_code"])

    def test_valid_hash_chain_is_returned_only_to_admin_with_limit(self):
        records = write_ledger(self.ledger_path, 3)

        history = read_wa_incident_history(
            self.ledger_path,
            can_manage=True,
            limit=2,
        )
        anonymous = read_wa_incident_history(
            self.ledger_path,
            can_manage=False,
            limit=2,
        )

        self.assertTrue(history["ok"])
        self.assertEqual("verified", history["integrity"])
        self.assertEqual(3, history["total_records"])
        self.assertEqual([2, 3], [record["sequence"] for record in history["records"]])
        self.assertEqual(records[-1]["record_hash"], history["head"])
        self.assertFalse(anonymous["authorized"])
        self.assertEqual([], anonymous["records"])
        self.assertEqual(0, anonymous["total_records"])

    def test_manipulated_hash_chain_is_invalid_and_returns_no_partial_records(self):
        records = write_ledger(self.ledger_path, 3)
        records[1]["status_name"] = "forbidden"
        self.ledger_path.write_text(
            "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
            encoding="utf-8",
        )

        history = read_wa_incident_history(
            self.ledger_path,
            can_manage=True,
            limit=100,
        )

        self.assertFalse(history["ok"])
        self.assertEqual("invalid", history["integrity"])
        self.assertEqual([], history["records"])
        self.assertEqual(0, history["total_records"])
        self.assertEqual(0, history["sequence"])
        self.assertIsNone(history["head"])

    def test_truncated_or_extra_key_ledger_is_invalid(self):
        write_ledger(self.ledger_path, 2)
        with self.ledger_path.open("a", encoding="utf-8") as ledger:
            ledger.write('{"schema_version":1,"sequence":3')

        truncated = read_wa_incident_history(
            self.ledger_path,
            can_manage=True,
        )
        self.assertEqual("invalid", truncated["integrity"])
        self.assertEqual([], truncated["records"])

        records = write_ledger(self.ledger_path, 1)
        records[0]["raw_error"] = "private"
        self.ledger_path.write_text(json.dumps(records[0]) + "\n", encoding="utf-8")
        extra_key = read_wa_incident_history(
            self.ledger_path,
            can_manage=True,
        )
        self.assertEqual("invalid", extra_key["integrity"])
        self.assertEqual([], extra_key["records"])

    def test_snapshot_anchor_detects_valid_tail_rollback(self):
        records = write_ledger(self.ledger_path, 3)
        self.write_snapshot(ledger_sequence=3, ledger_head=records[-1]["record_hash"])
        verified = read_wa_incident_history(
            self.ledger_path,
            can_manage=True,
            snapshot_path=self.snapshot_path,
        )
        self.assertEqual("verified", verified["integrity"])

        self.ledger_path.write_text(
            "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records[:2]),
            encoding="utf-8",
        )
        rolled_back = read_wa_incident_history(
            self.ledger_path,
            can_manage=True,
            snapshot_path=self.snapshot_path,
        )
        self.assertEqual("invalid", rolled_back["integrity"])
        self.assertEqual([], rolled_back["records"])

    def test_history_limit_is_clamped_between_one_and_one_hundred(self):
        write_ledger(self.ledger_path, 105)

        minimum = read_wa_incident_history(
            self.ledger_path,
            can_manage=True,
            limit=0,
        )
        maximum = read_wa_incident_history(
            self.ledger_path,
            can_manage=True,
            limit=999,
        )

        self.assertEqual(1, minimum["limit"])
        self.assertEqual([105], [record["sequence"] for record in minimum["records"]])
        self.assertEqual(100, maximum["limit"])
        self.assertEqual(100, len(maximum["records"]))
        self.assertEqual(6, maximum["records"][0]["sequence"])
        self.assertEqual(105, maximum["records"][-1]["sequence"])


if __name__ == "__main__":
    unittest.main()
