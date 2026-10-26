"""Standalone clean goals and release generations, using only synthetic disk/store fixtures."""
import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from trimreaper import clean, core, host


class HostGoalPolicyTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(host.DEFAULT_POLICY["cleanAtPercent"], 75)
        self.assertEqual(host.DEFAULT_POLICY["cleanToPercent"], 60)
        self.assertEqual(host.DEFAULT_POLICY["diskWarnPercent"], 75)
        host.check_policy({})

    def test_valid_numeric_goals(self):
        for high, low in ((85, 80), (85.5, 80.25), (100, 99.9), (0.2, 0.1), (0, 80), (0.0, 84.9)):
            with self.subTest(high=high, low=low):
                host.check_policy({"cleanAtPercent": high, "cleanToPercent": low})

    def test_invalid_goals(self):
        cases = [(85, 85), (80, 85), (101, 80), (-1, 80), (85, 0), (85, -1),
                 (0, 85), (0, 90), (True, 80), (False, 80), (85, True), ("85", 80),
                 (85, "80"), (None, 80), (85, None), (10 ** 500, 80)]
        for value in (float("nan"), float("inf"), float("-inf")):
            cases.extend(((value, 80), (85, value), (0, value)))
        for high, low in cases:
            with self.subTest(high=high, low=low), self.assertRaises(host.HostError):
                host.check_policy({"cleanAtPercent": high, "cleanToPercent": low})

    def test_load_preserves_off_and_validates_stored_goals(self):
        with patch.object(host, "config", return_value={"policy": {"cleanAtPercent": 0}}):
            policy = host.load_policy()
        self.assertEqual(policy["cleanAtPercent"], 0)
        self.assertEqual(policy["cleanToPercent"], 60)
        with patch.object(host, "config", return_value={"policy": {"cleanToPercent": 90}}):
            with self.assertRaises(host.HostError):
                host.load_policy()

    def test_off_still_allows_manual_clean_with_effective_high_85(self):
        policy = {**host.DEFAULT_POLICY, "cleanAtPercent": 0}
        disk = {"usedBytes": 900, "freeBytes": 100, "usedPercent": 90}
        with patch.object(clean, "clean_disk", return_value=disk), \
                patch.object(clean, "clean_plan", return_value=[]) as plan, \
                patch.object(clean, "clean_size_all"):
            result = clean.clean_pass(True, report=False, policy=policy, escalate=False)
        plan.assert_called_once()
        self.assertEqual((result["cleanAtPercent"], result["cleanToPercent"]), (85, 60))
        self.assertEqual(policy["cleanAtPercent"], 0)

    def test_off_never_runs_automatic_clean_even_after_release(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript(host.SCHEMA)
        policy = {**host.DEFAULT_POLICY, "cleanAtPercent": 0}
        disk = {"usedBytes": 990, "freeBytes": 10, "usedPercent": 99}
        actions, errors = [], []
        with patch.object(clean, "clean_disk", return_value=disk), patch.object(clean, "clean_pass") as manual:
            clean.clean_auto(conn, policy, actions, errors, event=12)
        manual.assert_not_called()
        self.assertEqual((actions, errors), ([], []))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0], 0)


class HostDiskGoalTests(unittest.TestCase):
    def test_apfs_exact_used_bytes_not_total_or_rounded_occupancy(self):
        # APFS can report a shared container total different from used + available.
        text = 'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/synthetic 2000 801 199 81% /synthetic/data\n'
        with patch.object(host, "config", return_value={"diskVolume": "/synthetic/data"}), \
                patch.object(core, "reap_probe", return_value=(0, text)) as measure:
            disk = host.disk_state()
        self.assertEqual(measure.call_args.args[:2], (["df", "-kP", "/synthetic/data"], 5))
        self.assertEqual(disk, {"path": "/synthetic/data", "usedPercent": 81,
                                "usedBytes": 801 * 1024, "freeBytes": 199 * 1024, "measurement": "df-kP"})
        occupancy = clean.clean_occupancy(disk, 80)
        self.assertAlmostEqual(occupancy["usedPercent"], 80.1)
        self.assertEqual(occupancy["remainingBytes"], 1024)
        self.assertFalse(occupancy["goalMet"])

    def test_fallback_disk_measurement_keeps_actual_used_bytes(self):
        text = 'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/synthetic 1000 400 100 80% /\n'
        with patch.object(host, "config", return_value={"diskVolume": None}), \
                patch.object(core, "reap_probe", side_effect=[(1, ''), (0, text)]) as measure, \
                patch.object(core.shutil, "disk_usage", side_effect=OSError()):
            disk = host.disk_state()
        self.assertEqual(measure.call_count, 2)
        self.assertEqual((disk["path"], disk["usedBytes"], disk["usedPercent"]), ("/", 400 * 1024, 80))


class HostReleaseEventTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(host.SCHEMA)

    def event(self, kind, data):
        host.log_event(self.conn, kind, "J-0001", "synthetic-test", data)
        return self.conn.execute("SELECT MAX(id) FROM events").fetchone()[0]

    def token(self):
        return host.clean_release_event_token(self.conn)

    def test_only_real_terminal_transitions_advance_token(self):
        self.assertEqual(self.token(), 0)
        for status in ("done", "failed", "cancelled"):
            expected = self.event("job.status", {"from": "running", "to": status})
            self.assertEqual(self.token(), expected)
        for kind, data in (("job.status", {"from": "done", "to": "running"}),
                           ("job.status", {"from": "done", "to": "done"}),
                           ("job.status", {"from": "done", "to": "failed"}),
                           ("job.status", {"to": "done"}),
                           ("job.added", {"status": "running"}),
                           ("job.reaped", {"deleted": 1}),
                           ("job.reap_blocked", {}), ("tick", {})):
            self.event(kind, data)
            self.assertEqual(self.token(), expected)
        self.assertEqual(self.token(), self.token())

    def test_only_evidenced_scope_release_advances_token(self):
        for data in ({}, {"evidence": ""}, {"evidence": "  "}, {"evidence": False}, {"evidence": 1}):
            self.event("cleanup.scope_released", data)
            self.assertEqual(self.token(), 0)
        expected = self.event("cleanup.scope_released", {"evidence": "successor terminal: J-0002 is done"})
        self.assertEqual(self.token(), expected)
        self.event("cleanup.scope_release_requested", {"evidence": "not a completed release"})
        self.assertEqual(self.token(), expected)

    def test_malformed_event_data_cannot_abort_or_trigger_tick(self):
        for data in ("{", "null", "[]", "42", '"done"', None):
            self.conn.execute("INSERT INTO events(ts,kind,data) VALUES(0,'job.status',?)", (data,))
        self.assertEqual(self.token(), 0)

    def test_changed_release_token_and_sustained_pressure_both_retry(self):
        disk = {"usedBytes": 900, "freeBytes": 100, "usedPercent": 90}
        state = {"at": 0, "freeBytes": 100, "goalMet": False, "event": self.token()}
        self.conn.execute("INSERT INTO meta(key,value) VALUES(?,?)", (clean.CLEAN_AUTO_KEY, json.dumps(state)))
        with patch.object(clean, "now", return_value=1000):
            for _ in range(3):
                self.assertEqual(clean.clean_auto_observe(self.conn, host.DEFAULT_POLICY, disk, event=self.token()), "pressure")
            self.event("job.reap_blocked", {})
            self.assertEqual(clean.clean_auto_observe(self.conn, host.DEFAULT_POLICY, disk, event=self.token()), "pressure")
            self.event("job.status", {"from": "running", "to": "done"})
            self.assertEqual(clean.clean_auto_observe(self.conn, host.DEFAULT_POLICY, disk, event=self.token()), "release")
            state["event"] = self.token()
            self.conn.execute("UPDATE meta SET value=? WHERE key=?", (json.dumps(state), clean.CLEAN_AUTO_KEY))
            self.assertEqual(clean.clean_auto_observe(self.conn, host.DEFAULT_POLICY, disk, event=self.token()), "pressure")


if __name__ == "__main__":
    unittest.main()
