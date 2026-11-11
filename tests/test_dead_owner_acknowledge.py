"""An interrupted cleanup run whose owner process is gone no longer blocks every later clean until a person acknowledges it.

The next clean writes the acknowledgment itself, with a full private report built from the run's own receipts. Nothing is deleted
by that report: every path is re-gated by the normal sweep. A live, unknown or unreadable owner, and a deliberate stop, still need
a person. Real flocks and child processes inside a temporary home."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trimreaper import core, host


class DeadOwnerAcknowledge(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="trimreaper-owner-")))
        self.home = self.tmp / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"TRIMREAPER_HOME": str(self.home), "HOME": str(self.home),
                                           "TRIMREAPER_CONFIG": str(self.tmp / "config.json")})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)], check=False)
        self.lock = self.home.joinpath(*host.REAP_OWNER_DIRS) / "disk-cleanup.lock"
        self.children = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for child in self.children:
            child.kill()
            child.wait(timeout=15)

    def journal(self):
        return [json.loads(line) for line in self.lock.read_text().splitlines()]

    def sleeper(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.children.append(child)
        return child

    def dead_pid(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=15)
        return child.pid

    def interrupted_run(self, pid, pid_start=None, at=None):
        """A journal whose last note is an interrupted run owned by `pid`, with receipts: one path deleted, one kept, one planned and never finished."""
        with core.reap_cleanup_owner("seed") as owner:
            run = owner["run"]
            for path, phase, outcome in (("/fixture/deleted", "plan", None), ("/fixture/deleted", "result", "deleted"),
                                         ("/fixture/kept", "plan", None), ("/fixture/kept", "result", "refused"),
                                         ("/fixture/unreached", "plan", None)):
                core.reap_receipt({"phase": phase, "path": path, **({"outcome": outcome} if outcome else {})})
        note = {"run": run, "pid": pid, "scope": "clean", "phase": "interrupted", "previousRun": None, "root": str(self.home),
                "outcome": "interrupted; investigate receipts and revalidate every remaining path", "at": at or core.iso()}
        if pid_start is not None:
            note["pidStart"] = pid_start
        fd = os.open(self.lock, os.O_WRONLY | os.O_APPEND)
        try:
            os.write(fd, (json.dumps(note, sort_keys=True) + "\n").encode())
        finally:
            os.close(fd)
        return run

    def test_interrupted_run_with_a_dead_owner_is_acknowledged_by_the_next_clean_and_it_proceeds(self):
        run = self.interrupted_run(self.dead_pid())
        with core.reap_cleanup_owner("clean") as owner:
            self.assertTrue(owner["run"])
        notes = self.journal()
        auto = [n for n in notes if n.get("auto")]
        self.assertEqual(len(auto), 1)
        self.assertEqual((auto[0]["run"], auto[0]["phase"]), (run, "acknowledged"))
        self.assertEqual(auto[0]["report"], {**auto[0]["report"], "deleted": 1, "retained": 1, "unresolved": 1, "ownerState": "gone"})
        self.assertEqual(notes[-1]["phase"], "finished")
        report = Path(auto[0]["evidence"])
        self.assertEqual(oct(report.stat().st_mode & 0o777), "0o600")
        data = report.read_bytes()
        self.assertIsNone(core.reap_ack_report_problem(run, data), "the manual acknowledgment would have accepted it too")
        self.assertEqual(auto[0]["evidenceSha256"], hashlib.sha256(data).hexdigest())
        text = data.decode()
        self.assertIn("/fixture/deleted", text)
        self.assertIn("/fixture/unreached", text, "an unreached path is preserved and named, never deleted from the report")
        self.assertIn("Unresolved paths: 1", text)

    def test_interrupted_run_with_a_live_owner_still_blocks(self):
        child = self.sleeper()
        start = core.reap_pid_start(child.pid)
        self.assertIsNotNone(start)
        self.interrupted_run(child.pid, pid_start=start)
        self.assertEqual(core.reap_owner_pid_state(self.journal()[-1]), "alive")
        with self.assertRaisesRegex(core.ReapRefused, "cleanup-handoff-incomplete"):
            with core.reap_cleanup_owner("clean"):
                self.fail("entered while the recorded owner is alive")
        self.assertFalse([n for n in self.journal() if n.get("auto")])

    def test_the_start_time_does_not_depend_on_the_callers_time_zone(self):
        import time
        before = time.time()
        child = self.sleeper()
        time.sleep(1.2)
        for zone in ("UTC", "Etc/GMT-14", "Etc/GMT+12"):
            with mock.patch.dict(os.environ, {"TZ": zone}):
                time.tzset()
                try:
                    start = core.reap_pid_start(child.pid)
                finally:
                    pass
            self.assertIsNotNone(start)
            self.assertLess(abs(start - before), 5, f"TZ={zone}: a live pid would read as reused")
        time.tzset()

    def test_a_pid_reused_by_a_process_with_another_start_time_counts_as_dead(self):
        child = self.sleeper()
        start = core.reap_pid_start(child.pid)
        self.interrupted_run(child.pid, pid_start=start - 3600)  # the owner started an hour before this process did
        self.assertEqual(core.reap_owner_pid_state(self.journal()[-1]), "reused")
        with core.reap_cleanup_owner("clean"):
            pass
        auto = [n for n in self.journal() if n.get("auto")]
        self.assertEqual(len(auto), 1)
        self.assertEqual(auto[0]["report"]["ownerState"], "reused")

    def test_a_journal_without_a_recorded_start_is_judged_by_its_note_time(self):
        child = self.sleeper()
        self.interrupted_run(child.pid, at="2020-01-01T00:00:00+0000")  # the owner wrote its note years before this process existed
        self.assertEqual(core.reap_owner_pid_state(self.journal()[-1]), "reused")

    def test_a_journal_with_a_colon_offset_note_time_proves_pid_reuse(self):
        child = self.sleeper()
        self.interrupted_run(child.pid, at="2020-01-01T00:00:00+00:00")  # the host's iso() colon form, no recorded start
        self.assertEqual(core.reap_owner_pid_state(self.journal()[-1]), "reused")

    def test_unknown_owner_state_and_unreadable_receipts_keep_the_block(self):
        self.interrupted_run(self.dead_pid())
        with mock.patch.object(core, "reap_run_receipt_records", return_value=None):
            with self.assertRaisesRegex(core.ReapRefused, "cleanup-handoff-incomplete"):
                with core.reap_cleanup_owner("clean"):
                    self.fail("entered with unreadable receipts")
        with mock.patch.object(core, "reap_owner_pid_state", return_value="unknown"):
            with self.assertRaisesRegex(core.ReapRefused, "cleanup-handoff-incomplete"):
                with core.reap_cleanup_owner("clean"):
                    self.fail("entered with an unknown owner")
        self.assertFalse([n for n in self.journal() if n.get("auto")])

    def test_a_stopped_handoff_still_needs_a_person_even_with_a_dead_owner(self):
        run = self.interrupted_run(self.dead_pid())
        fd = os.open(self.lock, os.O_WRONLY | os.O_APPEND)
        os.write(fd, (json.dumps({"run": run, "pid": self.dead_pid(), "scope": "clean", "phase": "stopped", "at": core.iso()}) + "\n").encode())
        os.close(fd)
        with self.assertRaisesRegex(core.ReapRefused, "cleanup-handoff-incomplete"):
            with core.reap_cleanup_owner("clean"):
                self.fail("a deliberate stop was auto-acknowledged")


class NoteEpochOffsetForms(unittest.TestCase):
    """reap_note_epoch reads the host's colon UTC offset (+ZZ:ZZ) exactly like the engine's legacy +ZZZZ; malformed text stays None."""

    def test_colon_and_legacy_negative_offsets_are_the_same_instant(self):
        colon = core.reap_note_epoch("2026-11-11T04:50:04-03:00")
        self.assertIsNotNone(colon)
        self.assertEqual(colon, core.reap_note_epoch("2026-11-11T04:50:04-0300"))

    def test_colon_and_legacy_positive_half_hour_offsets_are_the_same_instant(self):
        colon = core.reap_note_epoch("2026-11-11T04:50:04+05:30")
        self.assertIsNotNone(colon)
        self.assertEqual(colon, core.reap_note_epoch("2026-11-11T04:50:04+0530"))

    def test_malformed_offsets_still_return_none(self):
        for text in ("2026-11-11T04:50:04-03:0:0",  # a second colon
                     "2026-11-11T04:50:04-0::00",   # a second colon inside a 25-char text
                     "2026-11-11T04:50:04-03:0",    # short minutes
                     "2026-11-11T04:50:04-3:00",    # short hours
                     "2026-11-11T04:50:0403:00",    # no sign
                     "2026-11-11T04:50:04 03:00",   # no sign, colon present
                     "2026-11-11T04:50:04-03:0a"):  # non-digit minutes
            self.assertIsNone(core.reap_note_epoch(text), text)


if __name__ == "__main__":
    unittest.main()
