"""A folder the filesystem will not let go is kept with its cause, once, instead of an unnamed error on every tick."""
from __future__ import annotations

import contextlib
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import DAY
from test_clean import Home

from trimreaper import clean, core

DARWIN = sys.platform == "darwin"


class Leftovers(Home):
    """Temp folders under a fixture temp root: `buildtmp` (three days) stands for the per-user temp folder."""

    def setUp(self):
        super().setUp()
        for patch in (mock.patch.object(clean, "clean_running", return_value=set()),
                      mock.patch.object(clean, "reap_process_table", return_value={"files": [], "commands": []})):
            patch.start()
            self.addCleanup(patch.stop)

    def leftover(self, name: str = "synthetic-usap-trust-a1b2", days: float = 5) -> Path:
        folder = self.buildtmp / name
        self.put(folder, "home/.claude/plugins/by-pid/.keep", b"")
        self.age(folder, days)
        return folder

    def locked(self, folder: Path) -> Path:
        """The shape of the 60 folders that were retried on every tick: an entry whose ACL denies delete."""
        target = folder / "home/.claude/plugins/by-pid"
        subprocess.run(["chmod", "+a", "everyone deny delete", str(target)], check=True)
        self.addCleanup(lambda: subprocess.run(["chmod", "-RN", str(folder)], capture_output=True))  # runs before the fixture is removed
        return target

    def run_clean(self, *flags: str) -> dict:
        return self.cli("clean", "--no-report", "--only", "agent-leftovers", *flags)

    def item(self, out: dict, name: str) -> dict:
        return next(i for i in out["items"] if i["path"].endswith(name))

    @unittest.skipUnless(DARWIN, "ACLs are a macOS filesystem feature")
    def test_a_folder_holding_an_acl_that_denies_delete_is_kept_and_named_before_anything_is_tried(self):
        folder = self.leftover()
        self.locked(folder)
        with mock.patch.object(clean, "clean_act", wraps=clean.clean_act) as act:
            out = self.run_clean()
        act.assert_not_called()
        self.assertEqual(out["errors"], [])
        self.assertTrue(folder.exists())
        self.assertTrue(self.item(out, folder.name)["keep"].startswith("filesystem-protected-acl:home/.claude/plugins/by-pid"))
        self.assertEqual(out["summary"]["keptReasons"], {"filesystem-protected-acl": 1})

    @unittest.skipUnless(DARWIN, "ACLs are a macOS filesystem feature")
    def test_the_acl_is_never_cleared_by_the_pass(self):
        folder = self.leftover()
        target = self.locked(folder)
        self.run_clean()
        self.assertIn("deny delete", subprocess.run(["ls", "-lde", str(target)], capture_output=True, text=True).stdout)

    @unittest.skipUnless(DARWIN, "ACLs are a macOS filesystem feature")
    def test_a_removal_that_hits_the_acl_names_it(self):
        folder = self.leftover()
        target = self.locked(folder)
        fence = core.reap_fence(str(folder), str(folder.parent))
        result = {"checks": []}
        core.reap_remove_and_verify(fence, str(folder), [], result, False)
        # the file inside went first: this is the partial removal the pre-check exists to prevent (a stopped sweep needs a person)
        self.assertEqual(result["outcome"], "partial")
        self.assertIn(":acl-deny-delete:home/.claude/plugins/by-pid", result["reason"])
        self.assertTrue(target.exists())

    def test_a_refused_removal_is_kept_with_its_cause_not_reported_as_an_error(self):
        folder = self.leftover()
        with mock.patch.object(core, "reap_remove_tree", side_effect=PermissionError(1, "Operation not permitted")):
            out = self.run_clean()
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["summary"]["failed"], 0)
        self.assertTrue(self.item(out, folder.name)["keep"].startswith("removal-error:PermissionError"))
        self.assertTrue(folder.exists())

    def test_it_is_not_tried_again_on_the_next_pass_until_a_day_has_passed_or_the_folder_changes(self):
        folder = self.leftover()
        attempts = []
        real = clean.clean_act
        def counted(entry, *args):
            attempts.append(entry["path"])
            return real(entry, *args)
        with mock.patch.object(core, "reap_remove_tree", side_effect=PermissionError(1, "Operation not permitted")), \
                mock.patch.object(clean, "clean_act", counted):
            self.run_clean()
            self.assertEqual(len(attempts), 1)
            out = self.run_clean()
            self.assertEqual(len(attempts), 1, "remembered")
            self.assertTrue(self.item(out, folder.name)["keep"].startswith("refused-before:removal-error:PermissionError"))
            self.assertEqual(self.run_clean("--dry-run")["summary"]["candidates"], 0)
            with mock.patch.object(clean, "now", lambda: time.time() + 25 * 3600):
                self.run_clean()
            self.assertEqual(len(attempts), 2, "a day later it is looked at again")
            subprocess.run(["rm", "-rf", str(folder)], check=True)  # a different folder at the same path: not the one that refused
            self.leftover(folder.name)
            self.run_clean()
            self.assertEqual(len(attempts), 3)

    def test_a_transient_refusal_is_not_remembered(self):
        folder = self.leftover()
        table = {"files": [], "commands": [("9", f"node {folder}/server.js")]}
        with mock.patch.object(clean, "reap_process_table", return_value=table):
            out = self.run_clean()
        self.assertTrue(self.item(out, folder.name)["keep"].startswith("in-use"))
        out = self.run_clean("--dry-run")
        self.assertEqual(out["summary"]["candidates"], 1, "the process is gone: the folder is a candidate again")

    def test_remembered_refusals_older_than_a_day_are_dropped(self):
        self.leftover()
        with mock.patch.object(core, "reap_remove_tree", side_effect=PermissionError(1, "Operation not permitted")):
            self.run_clean()
        def remembered():
            with contextlib.closing(self.state()) as conn:
                return conn.execute("SELECT key FROM meta WHERE key LIKE 'clean:refused:%'").fetchall()
        self.assertEqual(len(remembered()), 1)
        clean.clean_forget_refusals()
        self.assertEqual(len(remembered()), 1, "still fresh")
        with mock.patch.object(clean, "now", lambda: time.time() + 26 * 3600):
            clean.clean_forget_refusals()
        self.assertEqual(remembered(), [])


class Cooldown(Home):
    def setUp(self):
        super().setUp()
        self.used = 99
        for patch in (mock.patch.object(clean, "disk_state", lambda: {"path": str(self.tmp), "usedPercent": self.used, "usedBytes": 99_000, "freeBytes": 1_000}),
                      mock.patch.object(clean, "clean_running", return_value=set()),
                      mock.patch.object(clean, "reap_process_table", return_value={"files": [], "commands": []})):
            patch.start()
            self.addCleanup(patch.stop)

    def tick(self, at: float):
        with mock.patch.object(clean, "now", lambda: at):
            return [a for a in self.cli("tick")["actions"] if a.get("did") == "cleaned-at-disk-pressure"]

    def test_a_pass_that_removed_nothing_waits_an_hour_before_the_next(self):
        t = time.time()
        first = self.tick(t)
        self.assertEqual([a["reason"] for a in first], ["crossed"])
        self.assertEqual(first[0]["removedBytes"], 0)
        self.assertFalse(first[0]["goalMet"])
        self.assertEqual(self.tick(t + 700), [], "the ten-minute cooldown used to retry here")
        self.assertEqual(self.tick(t + 3500), [])
        self.assertEqual([a["reason"] for a in self.tick(t + 3700)], ["pressure"])

    def test_new_pressure_ends_the_wait_too(self):
        GB = 1024 ** 3
        def disk(free_gb):
            return {"path": str(self.tmp), "usedBytes": 450 * GB, "freeBytes": free_gb * GB, "usedPercent": 90}
        t = time.time()
        with mock.patch.object(clean, "disk_state", lambda: disk(50)):
            self.assertEqual([a["reason"] for a in self.tick(t)], ["crossed"])
        with mock.patch.object(clean, "disk_state", lambda: disk(49)):
            self.assertEqual(self.tick(t + 700), [], "nothing new: a quiet disk")
        with mock.patch.object(clean, "disk_state", lambda: disk(5)):
            self.assertEqual([a["reason"] for a in self.tick(t + 700)], ["floor"], "below the free-space floor and falling")

    def test_a_finished_job_is_new_information_and_ends_the_wait(self):
        t = time.time()
        self.tick(t)
        self.cli("job", "add", "J-0001", "--status", "running")
        self.cli("job", "status", "J-0001", "done")
        self.assertEqual([a["reason"] for a in self.tick(t + 700)], ["release"])

    def test_a_pass_that_removed_something_keeps_the_ten_minute_cooldown(self):
        repo = self.clone("site", tracked={"package.json": "{}"})
        self.put(repo, "node_modules/pkg/a.js", b"x" * 5000)
        self.age(repo, 3)
        t = time.time()
        first = self.tick(t)
        self.assertGreater(first[0]["removedBytes"], 0)
        self.assertEqual(self.tick(t + 300), [])
        self.assertEqual([a["reason"] for a in self.tick(t + 700)], ["pressure"])

    def test_the_action_reports_removed_and_freed_bytes_and_the_blockers(self):
        young = self.clone("young", tracked={"package.json": "{}"})
        self.put(young, "node_modules/pkg/a.js", b"x" * 5000)
        action = self.tick(time.time())[0]
        self.assertEqual(action["removedBytes"], 0)
        self.assertIn("freedBytes", action)
        self.assertEqual(action["rungs"][0]["name"], "routine")
        self.assertEqual(action["topBlockers"][0]["reason"], "recent")
        self.assertGreaterEqual(action["topBlockers"][0]["bytes"], 5000)
