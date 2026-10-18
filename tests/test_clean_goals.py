"""Synthetic-only goal/skip fixtures: no machine paths, cleaners, or removal calls."""
from __future__ import annotations

import contextlib
import errno
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trimreaper import clean, cli


def disk(used=90, free=10):
    # Deliberately wrong rounded percentage: goals must use the byte measurement.
    return {"usedBytes": used, "freeBytes": free, "usedPercent": 100, "path": "/synthetic"}


def candidate(name, size):
    return {"name": name, "kind": "folder-cache", "path": "/synthetic/" + name,
            "root": "/synthetic", "paths": ["/synthetic/" + name], "tier": "safe", "fixtureSize": size}


class Goals(unittest.TestCase):
    def setUp(self):
        self.entries = [candidate("small", 2), candidate("large", 12), candidate("medium", 5)]
        self.measurement = disk()
        self.acted = []
        patches = [
            mock.patch.object(clean, "clean_plan", side_effect=lambda *a: self.entries),
            mock.patch.object(clean, "disk_state", side_effect=lambda: self.measurement),
            mock.patch.object(clean, "clean_tilde", side_effect=lambda p: p),
            mock.patch.object(clean, "reap_metadata_size", side_effect=self.size),
            mock.patch.object(clean, "reap_cleanup_owner", side_effect=lambda *a: contextlib.nullcontext()),
            mock.patch.object(clean, "clean_act", side_effect=self.act),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def size(self, path, seconds):
        entry = next(e for e in self.entries if path in e["paths"])
        return {"bytes": entry["fixtureSize"], "complete": True, "reason": None}

    def act(self, entry, *args):
        self.acted.append(entry["name"])
        entry["outcome"] = "removed"
        if self.measurement is not None:
            self.measurement = disk(self.measurement["usedBytes"] - entry["fixtureSize"],
                                    self.measurement["freeBytes"] + entry["fixtureSize"])

    def run_pass(self, dry=False, policy=None):
        return clean.clean_pass(dry, report=False, policy=policy or {})

    def test_default_goals_largest_first_stop_on_measured_low(self):
        out = self.run_pass()
        self.assertEqual((out["cleanAtPercent"], out["cleanToPercent"]), (85, 80))
        self.assertEqual(self.acted, ["large"])
        self.assertEqual(out["initialOccupancy"]["usedPercent"], 90)
        self.assertEqual(out["finalOccupancy"]["usedPercent"], 78)
        self.assertEqual(out["remainingBytes"], 0)
        self.assertIs(out["goalMet"], True)
        self.assertEqual(out["summary"]["freedBytes"], 12)

    def test_bounded_when_sizes_do_not_translate_to_df_reclamation(self):
        def no_reclamation(entry, *args):
            self.acted.append(entry["name"])
            entry["outcome"] = "removed"
        clean.clean_act.side_effect = no_reclamation
        out = self.run_pass()
        self.assertEqual(self.acted, ["large", "medium", "small"])
        self.assertFalse(out["goalMet"])
        self.assertEqual(out["remainingBytes"], 10)

    def test_unknown_measurement_is_null_and_each_candidate_tried_once(self):
        self.measurement = None
        out = self.run_pass()
        self.assertIsNone(out["goalMet"])
        self.assertIsNone(out["remainingBytes"])
        self.assertIsNone(out["initialOccupancy"]["usedPercent"])
        self.assertEqual(self.acted, ["large", "medium", "small"])

    def test_rounded_percent_without_bytes_is_not_a_measurement(self):
        self.assertIsNone(clean.clean_occupancy({"usedPercent": 80, "freeBytes": 20}, 80)["goalMet"])
        for invalid in (disk(0, 0), disk(-1, 1), disk(float("nan"), 1)):
            self.assertIsNone(clean.clean_occupancy(invalid, 80)["goalMet"])
        self.assertEqual(clean.clean_occupancy(disk(81, 20), 80)["remainingBytes"], 1)

    def test_unknown_final_does_not_claim_goal_based_on_estimated_sizes(self):
        def unknown_after(entry, *args):
            self.acted.append(entry["name"])
            entry["outcome"] = "removed"
            self.measurement = None
        clean.clean_act.side_effect = unknown_after
        out = self.run_pass()
        self.assertIsNone(out["goalMet"])
        self.assertEqual(len(self.acted), 3)

    def test_no_deletion_when_already_at_goal(self):
        self.measurement = disk(80, 20)
        out = self.run_pass()
        self.assertEqual(self.acted, [])
        clean.reap_cleanup_owner.assert_not_called()
        self.assertTrue(out["goalMet"])

    def test_dry_run_measures_without_predicting_reclamation(self):
        out = self.run_pass(dry=True)
        self.assertEqual(self.acted, [])
        self.assertFalse(out["goalMet"])
        self.assertEqual(out["remainingBytes"], 10)

    def test_candidate_os_errors_skip_independently_without_retry(self):
        def transient(entry, *args):
            self.acted.append(entry["name"])
            if entry["name"] == "large":
                raise FileNotFoundError(errno.ENOENT, "synthetic disappearance")
            if entry["name"] == "medium":
                raise PermissionError(errno.EACCES, "synthetic unreadability")
            entry["outcome"] = "removed"
        clean.clean_act.side_effect = transient
        out = self.run_pass()
        self.assertEqual(self.acted, ["large", "medium", "small"])
        keeps = {e["name"]: e.get("keep") for e in out["items"]}
        self.assertTrue(keeps["large"].startswith("missing:"))
        self.assertTrue(keeps["medium"].startswith("unreadable:"))
        self.assertEqual(out["summary"]["done"], 1)

    def test_size_error_skips_one_candidate_and_continues(self):
        ordinary_size = self.size
        def transient_size(path, seconds):
            if path.endswith("large"):
                raise PermissionError(errno.EACCES, "synthetic unreadability")
            return ordinary_size(path, seconds)
        clean.reap_metadata_size.side_effect = transient_size
        out = self.run_pass()
        self.assertEqual(self.acted, ["medium", "small"])
        self.assertEqual(out["summary"]["keptReasons"], {"unreadable": 1})

    def test_proof_stop_is_not_overridden_by_goal_processing(self):
        def stop(entry, *args):
            self.acted.append(entry["name"])
            entry["stopped"] = "synthetic-proof-failed"
        clean.clean_act.side_effect = stop
        out = self.run_pass()
        self.assertEqual(self.acted, ["large"])
        self.assertEqual(out["stopped"], "synthetic-proof-failed")

    def test_invalid_goal_bounds_refused_before_planning(self):
        for low, high in ((0, 85), (85, 85), (86, 85), (80, 101), (True, 85), (80, -1), (80, float("nan"))):
            with self.subTest(low=low, high=high), self.assertRaises(clean.HostError):
                clean.clean_pass(False, report=False, policy={"cleanToPercent": low, "cleanAtPercent": high})
        clean.clean_plan.assert_not_called()


class Discovery(unittest.TestCase):
    def test_vanished_size_path_is_named_and_independent_path_sized(self):
        with tempfile.TemporaryDirectory(prefix="trimreaper-goals-synthetic-") as root:
            missing, good = Path(root).resolve() / "vanished", Path(root).resolve() / "good"
            missing.mkdir()
            good.mkdir()
            missing.rmdir()  # isolated synthetic fixture only
            entries = [{"paths": [str(p)]} for p in (missing, good)]
            clean.clean_size_all(entries)
            self.assertTrue(entries[0]["keep"].startswith("missing:"))
            self.assertTrue(entries[1]["sizeComplete"])
            self.assertNotIn("keep", entries[1])

    def test_planning_gate_os_errors_do_not_abort_siblings(self):
        with tempfile.TemporaryDirectory(prefix="trimreaper-goals-synthetic-") as root:
            for name in ("a-missing", "b-unreadable", "c-good"):
                Path(root, name).mkdir()
            def gate(path):
                if path.endswith("a-missing"):
                    raise FileNotFoundError(errno.ENOENT, "synthetic vanished gate")
                if path.endswith("b-unreadable"):
                    raise PermissionError(errno.EACCES, "synthetic unreadable gate")
            spec = {"name": "fixture", "busy": (), "roots": lambda: [root]}
            with mock.patch.object(clean, "clean_running", return_value=set()), \
                    mock.patch.object(clean, "CLEAN_FOLDERS", (spec,)), \
                    mock.patch.object(clean, "clean_folder_gates", side_effect=gate), \
                    mock.patch.object(clean, "clean_extra_entries", return_value=[]):
                entries = clean.clean_plan({"fixture"}, False, [], [], 7)
            self.assertEqual(len(entries), 3)
            self.assertTrue(entries[0]["keep"].startswith("missing:"))
            self.assertTrue(entries[1]["keep"].startswith("unreadable:"))
            self.assertNotIn("keep", entries[2])

    def test_bad_discovery_child_does_not_abort_other_children(self):
        bad = mock.Mock(path="/synthetic/missing")
        bad.is_dir.side_effect = FileNotFoundError(errno.ENOENT, "synthetic")
        good = mock.Mock(path="/synthetic/good")
        good.is_dir.return_value = True
        errors = []
        found = [e.path for e in (bad, good) if clean.clean_directory(e, "fixture", errors)]
        self.assertEqual(found, ["/synthetic/good"])
        self.assertTrue(errors[0]["error"].startswith("missing:"))


class AutoGoals(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        self.addCleanup(self.conn.close)
        self.policy = {"cleanAtPercent": 85, "cleanToPercent": 80, "cleanFloorGB": 0}
        patch = mock.patch.object(clean, "now", return_value=10000)
        patch.start()
        self.addCleanup(patch.stop)

    def state(self, **extra):
        value = {"at": 1, "freeBytes": 10, "goalMet": False, "event": "release-1", **extra}
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (clean.CLEAN_AUTO_KEY, json.dumps(value)))

    def observe(self, used, event=None):
        return clean.clean_auto_observe(self.conn, self.policy, disk(used, 100 - used), event)

    def test_high_crossing_and_low_rearm_not_high_band_rearm(self):
        self.assertIsNone(self.observe(84))
        self.assertEqual(self.observe(85), "crossed")
        self.state()
        for used in (90, 84, 85):
            self.assertIsNone(self.observe(used))
            self.assertTrue(clean.clean_auto_state(self.conn))
        self.assertIsNone(self.observe(79))
        self.assertEqual(clean.clean_auto_state(self.conn), {})
        self.assertIsNone(self.observe(81))
        self.assertEqual(self.observe(85), "crossed")

    def test_exact_low_rearms_small_volume_crossing(self):
        self.state(goalMet=True, freeBytes=20)
        self.assertIsNone(self.observe(80))
        self.assertEqual(clean.clean_auto_state(self.conn), {})
        self.assertEqual(self.observe(85), 'crossed')

    def test_release_generation_retries_unresolved_not_unchanged_ticks(self):
        self.state()
        self.assertIsNone(self.observe(90, "release-1"))
        self.assertEqual(self.observe(84, "release-2"), "release")
        self.state(event="release-2", at=10000)
        self.assertIsNone(self.observe(90, "release-3"))
        self.state(event="release-2")
        self.assertIsNone(self.observe(90, "release-2"))
        self.state(goalMet=True)
        self.assertIsNone(self.observe(90, "release-2"))

    def test_elapsed_time_alone_never_retries_and_real_growth_does(self):
        gb = 1024 ** 3
        self.state(freeBytes=20 * gb)
        measured = {"usedBytes": 180 * gb, "freeBytes": 20 * gb}
        for stamp in (10000, 100000, 1000000):
            with mock.patch.object(clean, "now", return_value=stamp):
                self.assertIsNone(clean.clean_auto_observe(self.conn, self.policy, measured))
        self.assertEqual(clean.clean_auto_observe(self.conn, self.policy, disk(185 * gb, 15 * gb)), "growth")
        self.policy["cleanFloorGB"] = 10
        self.assertEqual(clean.clean_auto_observe(self.conn, self.policy, disk(191 * gb, 9 * gb)), "floor")

    def test_successful_auto_records_actual_post_pass_and_release_token(self):
        actions, errors = [], []
        result = {"diskAfter": disk(88, 12), "goalMet": False, "remainingBytes": 8,
                  "summary": {"done": 1, "freedBytes": 999}, "errors": []}
        with mock.patch.object(clean, "disk_state", return_value=disk()), \
                mock.patch.object(clean, "clean_pass", return_value=result):
            clean.clean_auto(self.conn, self.policy, actions, errors, "release-1")
        self.assertEqual(actions[0]["freedBytes"], 2)
        self.assertEqual(actions[0]["remainingBytes"], 8)
        self.assertEqual(clean.clean_auto_state(self.conn)["freeBytes"], 12)
        self.assertFalse(clean.clean_auto_state(self.conn)["goalMet"])
        self.assertEqual(errors, [])

    def test_unknown_disk_never_triggers_or_rearms(self):
        self.state()
        self.assertIsNone(clean.clean_auto_observe(self.conn, self.policy, None, "release-2"))
        self.assertTrue(clean.clean_auto_state(self.conn))

    def test_auto_stamps_event_even_if_pass_fails_without_tick_retry(self):
        actions, errors = [], []
        with mock.patch.object(clean, "disk_state", return_value=disk()), \
                mock.patch.object(clean, "clean_pass", side_effect=clean.ReapRefused("synthetic refusal")):
            clean.clean_auto(self.conn, self.policy, actions, errors, "release-1")
        self.assertEqual(len(errors), 1)
        self.assertEqual(clean.clean_auto_state(self.conn)["event"], "release-1")
        self.assertIsNone(self.observe(90, "release-1"))
        self.conn.execute("UPDATE meta SET value=?", (json.dumps({**clean.clean_auto_state(self.conn), "at": 1}),))
        self.assertIsNone(self.observe(90, "release-1"))


class CLI(unittest.TestCase):
    def test_standalone_goal_overrides(self):
        args = cli.parser().parse_args(["clean", "--clean-at-percent", "91", "--clean-to-percent", "75", "--no-report"])
        with mock.patch.object(cli.host, "load_policy", return_value={"worktreeIdleDays": 7}), \
                mock.patch.object(clean, "clean_pass", return_value={}) as run:
            cli.cmd_clean(args)
        self.assertEqual(run.call_args.kwargs["policy"]["cleanAtPercent"], 91)
        self.assertEqual(run.call_args.kwargs["policy"]["cleanToPercent"], 75)
        clean.clean_goals(run.call_args.kwargs["policy"])


if __name__ == "__main__":
    unittest.main()
