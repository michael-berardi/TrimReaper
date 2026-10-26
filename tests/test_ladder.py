"""The escalation ladder, the steady pass and the honest report: what each rung may take, and what no rung may.

Occupancy here follows the real bytes under the fixture home, so a rung only counts when it really freed something."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tarfile
import time
from pathlib import Path
from unittest import mock

from helpers import DAY
from test_clean import Home

from trimreaper import clean, core

BIG = b"x" * 700_000
HOUR = 3600 / DAY


class Ladder(Home):
    TOTAL = 2_000_000

    def setUp(self):
        super().setUp()
        self.base_used = 0
        for patch in (mock.patch.object(clean, "disk_state", self.fake_disk),
                      mock.patch.object(clean, "clean_running", return_value=set()),
                      mock.patch.object(clean, "reap_process_table", return_value={"files": [], "commands": []})):
            patch.start()
            self.addCleanup(patch.stop)

    # ---- a disk whose fullness is the fixture's own bytes -------------------------------------------------------------------------

    def payload(self) -> int:
        total = 0
        for current, _, files in os.walk(self.home):
            for name in files:
                try:
                    total += os.lstat(os.path.join(current, name)).st_size
                except OSError:
                    pass
        return total

    def fake_disk(self) -> dict:
        used = self.base_used + self.payload()
        return {"path": str(self.tmp), "usedBytes": used, "freeBytes": self.TOTAL - used, "usedPercent": int(100 * used / self.TOTAL)}

    def fill_to(self, percent: float):
        """Everything on the fixture home counts; the rest of the disk is whatever makes it `percent` full."""
        self.base_used = int(self.TOTAL * percent / 100) - self.payload()
        self.assertGreater(self.base_used, 0)

    def clean_all(self, *flags: str, expect: int = 0) -> dict:
        return self.cli("clean", "--no-report", *flags, expect=expect)

    @staticmethod
    def rungs(out: dict) -> list:
        return [r["name"] for r in out["rungs"]]

    # ---- fixtures ---------------------------------------------------------------------------------------------------------------

    def project(self, name: str = "site", size: bytes = BIG, ignored=("node_modules/", "dist/", "build/")) -> Path:
        repo = self.clone(name, ignored=ignored, tracked={"package.json": "{}", "README.md": "hello\n"})
        self.put(repo, "node_modules/pkg/index.js", size)
        return repo

    def worktree(self, repo: Path, name: str, days: float = 2) -> Path:
        path = repo.parent / name
        self.git(repo, "worktree", "add", "-b", name, str(path))
        self.age(path, days)
        gitdir = Path((path / ".git").read_text().split("gitdir:", 1)[1].strip())
        self.age(gitdir, days)
        return path

    def items(self, out: dict) -> dict:
        return {Path(item["path"]).name: item for item in out["items"] if item.get("path")}


class IdleRepositories(Ladder):
    def test_routine_keeps_what_the_first_rung_takes(self):
        repo = self.project()
        self.age(repo, HOUR * 3)
        self.fill_to(90)
        out = self.clean_all("--no-escalate")
        self.assertTrue((repo / "node_modules").exists())
        self.assertEqual(self.rungs(out), ["routine"])
        self.assertIs(out["goalMet"], False)

    def test_idle_repository_output_goes_after_two_hours_even_when_dirty_and_stashed(self):
        repo = self.project()
        (repo / "README.md").write_text("work in progress\n")
        self.git(repo, "stash")
        (repo / "README.md").write_text("more work\n")
        self.age(repo, HOUR * 3)
        young = self.project("young")
        self.age(young, HOUR * 1)
        self.fill_to(90)
        out = self.clean_all()
        self.assertFalse((repo / "node_modules").exists())
        self.assertEqual((repo / "README.md").read_text(), "more work\n")  # the source and the unsaved edit are exactly as they were
        self.assertTrue((young / "node_modules").exists())
        self.assertEqual(self.rungs(out)[:2], ["routine", "idle-repositories"])
        self.assertGreaterEqual(out["rungs"][1]["removedBytes"], 700_000)

    def test_the_ladder_stops_at_the_first_rung_that_reaches_the_goal(self):
        repo = self.project()
        self.age(repo, HOUR * 3)
        wt = self.worktree(self.clone("app"), "app-dirty", days=2)
        self.put(wt, "notes.txt", BIG)  # unsaved work: only the last rung may even look at it
        self.fill_to(80)  # 80% -> 45% once the 700 kB of node_modules is gone
        out = self.clean_all()
        self.assertIs(out["goalMet"], True)
        self.assertEqual(self.rungs(out), ["routine", "idle-repositories"])
        self.assertTrue((wt / "notes.txt").exists())

    def test_nothing_is_escalated_when_the_disk_cannot_be_measured(self):
        repo = self.project()
        self.age(repo, HOUR * 3)
        with mock.patch.object(clean, "disk_state", lambda: {"path": str(self.tmp), "usedPercent": 99, "freeBytes": 1}):
            out = self.clean_all()
        self.assertTrue((repo / "node_modules").exists())
        self.assertEqual(self.rungs(out), ["routine"])
        self.assertIsNone(out["goalMet"])

    def test_the_ladder_can_be_turned_off(self):
        self.write_config({"policy": {"cleanEscalate": False}})
        repo = self.project()
        self.age(repo, HOUR * 3)
        self.fill_to(90)
        out = self.clean_all()
        self.assertTrue((repo / "node_modules").exists())
        self.assertEqual(self.rungs(out), ["routine"])

    def test_a_repository_a_running_job_works_in_keeps_its_output_on_every_rung(self):
        repo = self.project("busy-j4242")
        self.age(repo, HOUR * 3)
        self.cli("job", "add", "J-4242", "--cwd", str(repo), "--status", "running")
        self.fill_to(95)
        out = self.clean_all()
        self.assertTrue((repo / "node_modules").exists())
        self.assertTrue(self.items(out)["node_modules"]["keep"].startswith("in-use-by-running-job:J-4242"))

    def test_a_process_using_the_repository_keeps_its_output(self):
        repo = self.project()
        self.age(repo, HOUR * 3)
        self.fill_to(95)
        table = {"files": [], "commands": [("77", f"node {repo}/server.js")]}
        with mock.patch.object(clean, "reap_process_table", return_value=table), \
                mock.patch("trimreaper.core.reap_process_table", return_value=table):
            out = self.clean_all()
        self.assertTrue((repo / "node_modules").exists())
        self.assertTrue(self.items(out)["node_modules"]["keep"].startswith("in-use"))

    def test_output_git_tracks_or_does_not_ignore_stays_on_every_rung(self):
        repo = self.clone("tracked", ignored=("dist/",), tracked={"package.json": "{}"})
        self.put(repo, "node_modules/pkg/index.js", BIG)  # not ignored by this repo's .gitignore
        self.put(repo, "dist/main.js", BIG)
        self.age(repo, HOUR * 3)
        self.fill_to(95)
        self.clean_all()
        self.assertTrue((repo / "node_modules").exists())  # git does not ignore it: it may be unsaved work
        self.assertFalse((repo / "dist").exists())  # ignored, untracked, a manifest beside it, no release inside

    def test_dist_and_build_never_go_by_the_routine_rules_and_never_with_a_release_inside(self):
        repo = self.project()
        self.put(repo, "dist/main.js", b"y" * 1000)
        self.put(repo, "build/App.dmg", b"z" * 1000)
        self.age(repo, HOUR * 3)
        self.fill_to(95)
        self.clean_all("--no-escalate")
        self.assertTrue((repo / "dist").exists())
        out = self.clean_all()
        self.assertFalse((repo / "dist").exists())
        self.assertTrue((repo / "build").exists())
        self.assertTrue(self.items(out)["build"]["keep"].startswith("release-artifact:App.dmg"))

    def test_a_dry_run_reports_each_rung_and_removes_nothing(self):
        repo = self.project()
        self.age(repo, HOUR * 3)
        self.fill_to(90)
        out = self.clean_all("--dry-run")
        self.assertTrue((repo / "node_modules").exists())
        self.assertEqual(self.rungs(out)[:2], ["routine", "idle-repositories"])
        self.assertEqual(out["rungs"][0]["candidates"], 0)
        self.assertEqual(out["rungs"][1]["candidates"], 1)
        self.assertGreaterEqual(out["rungs"][1]["candidateBytes"], 700_000)
        self.assertEqual(len(out["rungs"]), 6)

    def test_the_report_names_the_removed_bytes_and_the_top_blockers_by_bytes(self):
        repo = self.project()
        self.age(repo, HOUR * 3)
        small = self.project("small", b"s" * 1000)
        self.age(small, HOUR * 1)
        big = self.project("big", b"b" * 5000)
        self.age(big, HOUR * 1)
        self.fill_to(97)
        out = self.clean_all()
        self.assertGreaterEqual(out["summary"]["removedBytes"], 700_000)
        self.assertIsNotNone(out["summary"]["freedBytes"])
        blockers = out["topBlockers"]
        self.assertEqual(blockers, sorted(blockers, key=lambda b: (-b["bytes"], -b["items"])))
        self.assertEqual(blockers[0]["reason"], "recent")
        self.assertGreaterEqual(blockers[0]["bytes"], 6000)


class Worktrees(Ladder):
    def test_a_clean_pushed_worktree_idle_a_day_goes_on_the_worktree_rung_not_before(self):
        repo = self.clone("app")
        wt = self.worktree(repo, "app-feature", days=2)
        self.put(wt, "node_modules/x", BIG)  # ignored output only: the tree is clean
        self.fill_to(90)
        out = self.clean_all("--no-escalate")
        self.assertTrue(wt.exists(), "seven days is the routine window")
        out = self.clean_all()
        self.assertFalse(wt.exists())
        self.assertEqual(self.git(repo, "branch", "--list", "app-feature").strip(), "app-feature", "the branch stays")
        self.assertIn("worktrees-and-finished-jobs", self.rungs(out))

    def test_a_worktree_idle_less_than_a_day_stays_on_every_rung_but_the_routine_keeps_its_name(self):
        repo = self.clone("app")
        wt = self.worktree(repo, "app-fresh", days=HOUR * 5)
        self.put(wt, "node_modules/x", BIG)
        self.fill_to(95)
        out = self.clean_all()
        self.assertTrue(wt.exists())
        self.assertTrue(self.items(out)["app-fresh"]["keep"].startswith("recent:"))


class ArchiveThenRemove(Ladder):
    def dirty_worktree(self, name="app-dirty", days=2):
        repo = self.clone("app")
        wt = self.worktree(repo, name, days=days)
        (wt / "README.md").write_text("edited and not committed\n")
        (wt / "scratch.txt").write_text("untracked work\n" * 50)
        self.put(wt, "big.bin", BIG)
        (wt / "committed.txt").write_text("a commit nobody pushed\n")
        self.git(wt, "add", "committed.txt")
        self.git(wt, "commit", "-q", "-m", "only here")
        self.age(wt, days)
        gitdir = Path((wt / ".git").read_text().split("gitdir:", 1)[1].strip())
        self.age(gitdir, days)
        return repo, wt

    def archives(self):
        return sorted((self.home / ".cache" / "trimreaper" / "archive" / "clean-worktrees").glob("*"))

    def test_unsaved_work_is_archived_verified_and_only_then_the_worktree_goes(self):
        repo, wt = self.dirty_worktree()
        self.fill_to(90)
        out = self.clean_all()
        self.assertFalse(wt.exists())
        self.assertEqual(self.rungs(out)[-1], "archive-then-remove")
        saved = self.archives()
        self.assertEqual(len(saved), 1, out)
        manifest = json.loads((saved[0] / "manifest.json").read_text())
        self.assertEqual(manifest["repositories"][0]["bundle"] is not None, True)
        bundle = next(saved[0].glob("repo-0.bundle"))
        empty = self.tmp / "empty-repo"
        subprocess.run(["git", "init", "-q", str(empty)], check=True)
        verify = subprocess.run(["git", "bundle", "verify", str(bundle)], capture_output=True, text=True, cwd=str(empty))
        self.assertEqual(verify.returncode, 0, verify.stderr)
        with tarfile.open(saved[0] / "files.tar.gz") as tar:
            names = tar.getnames()
        self.assertTrue(any(n.endswith("scratch.txt") for n in names), names)
        self.assertTrue(any(n.endswith("README.md") for n in names), names)
        self.assertFalse(any("node_modules" in n for n in names))
        self.assertEqual(self.git(repo, "branch", "--list", "app-dirty").strip(), "app-dirty", "the branch stays")
        self.assertTrue((saved[0] / "sole-copy-since.json").exists())
        self.assertGreaterEqual(out["summary"]["removedBytes"], 700_000)

    def test_without_the_last_rung_unsaved_work_is_never_touched(self):
        repo, wt = self.dirty_worktree()
        self.fill_to(90)
        out = self.clean_all("--no-escalate")
        self.assertTrue((wt / "scratch.txt").exists())
        self.assertEqual(self.archives(), [])
        self.assertEqual(self.items(out)["app-dirty"]["keep"], "recent:2.0d-of-7d")

    def test_the_rungs_before_the_last_keep_dirty_worktrees(self):
        repo, wt = self.dirty_worktree()
        self.fill_to(90)
        with mock.patch.object(clean, "CLEAN_RUNGS", clean.CLEAN_RUNGS[:5]), \
                mock.patch.object(clean, "clean_ladder", lambda policy, escalate: [0, 1, 2, 3, 4]):
            out = self.clean_all()
        self.assertTrue((wt / "scratch.txt").exists())
        self.assertEqual(self.items(out)["app-dirty"]["keep"], "dirty")

    def test_a_git_directory_without_a_dot_git_entry_is_saved_whole_on_the_last_rung_only(self):
        repo = self.clone("app")
        wt = self.worktree(repo, "app-backup", days=2)
        subprocess.run(["git", "init", "-q", "--bare", str(wt / ".git-backup")], check=True)
        self.put(wt, "big.bin", BIG)
        self.age(wt, 2)
        gitdir = Path((wt / ".git").read_text().split("gitdir:", 1)[1].strip())
        self.age(gitdir, 2)
        self.fill_to(90)
        with mock.patch.object(clean, "CLEAN_RUNGS", clean.CLEAN_RUNGS[:5]), \
                mock.patch.object(clean, "clean_ladder", lambda policy, escalate: [0, 1, 2, 3, 4]):
            out = self.clean_all()
        self.assertTrue(wt.exists())
        self.assertTrue(self.items(out)["app-backup"]["keep"].startswith("unproved-git-dir:.git-backup"))
        self.clean_all()
        self.assertFalse(wt.exists())
        with tarfile.open(self.archives()[0] / "files.tar.gz") as tar:
            self.assertTrue(any(".git-backup/HEAD" in n for n in tar.getnames()), tar.getnames())

    def test_a_failed_archive_keeps_the_worktree_whole(self):
        repo, wt = self.dirty_worktree()
        self.fill_to(90)
        with mock.patch.object(clean, "reap_archive_create", side_effect=clean.ReapRefused("archive-failed:synthetic")):
            out = self.clean_all()
        self.assertTrue((wt / "scratch.txt").exists())
        self.assertEqual((wt / "README.md").read_text(), "edited and not committed\n")
        self.assertEqual(self.items(out)["app-dirty"]["keep"], "archive-failed:synthetic")
        self.assertEqual(self.archives(), [])

    def test_a_change_after_the_archive_keeps_the_worktree_and_the_next_archive_covers_it(self):
        repo, wt = self.dirty_worktree()
        self.fill_to(90)
        real = clean.reap_archive_create
        def archive_then_edit(*args, **kwargs):
            made = real(*args, **kwargs)
            (wt / "scratch.txt").write_text("changed after the archive was made\n")
            return made
        with mock.patch.object(clean, "reap_archive_create", archive_then_edit):
            out = self.clean_all()
        self.assertTrue((wt / "scratch.txt").exists())
        # the folder's own change restarts its idle clock, which the last look reads before it compares the archive with the tree
        self.assertTrue(self.items(out)["app-dirty"]["keep"].startswith(("archive-stale", "recent:")), self.items(out)["app-dirty"])
        self.assertFalse((self.archives()[0] / "sole-copy-since.json").exists(), "nothing was removed: the worktree is still the primary copy")

    def test_a_secret_in_the_worktree_keeps_it_on_every_rung(self):
        repo, wt = self.dirty_worktree()
        (wt / ".env").write_text("TOKEN=not-for-an-archive\n")
        self.fill_to(95)
        out = self.clean_all()
        self.assertEqual((wt / ".env").read_text(), "TOKEN=not-for-an-archive\n")
        self.assertTrue(self.items(out)["app-dirty"]["keep"].startswith("protected-leaf"))
        self.assertEqual(self.archives(), [])

    def test_a_job_that_declares_the_worktree_keeps_it(self):
        repo, wt = self.dirty_worktree()
        self.cli("job", "add", "J-0900", "--cwd", str(wt), "--status", "running")
        self.fill_to(95)
        out = self.clean_all()
        self.assertTrue((wt / "scratch.txt").exists())
        self.assertEqual(self.items(out)["app-dirty"]["keep"], "declared-by-unfinished-job:J-0900")

    def test_a_clean_worktree_is_not_archived_at_all(self):
        repo = self.clone("app")
        wt = self.worktree(repo, "app-clean", days=2)
        self.put(wt, "node_modules/x", BIG)
        self.fill_to(90)
        with mock.patch.object(clean, "reap_archive_create") as archive:
            self.clean_all()
        archive.assert_not_called()
        self.assertFalse(wt.exists())


def interrupt(test, job: str, hours_ago: float, waits_on: str | None = None):
    """A job the host lost the worker of: interrupted, with its own verified interruption clock."""
    stamp = time.time() - hours_ago * 3600
    meta = {"interrupted": {"at": stamp}, **({"waitingOn": {"job": waits_on}} if waits_on else {})}
    with test.state() as conn, conn:
        conn.execute("UPDATE jobs SET status='interrupted', created_at=?, started_at=?, finished_at=?, meta=? WHERE id=?",
                     (stamp - 7200, stamp - 3600, stamp, json.dumps(meta), job))


class FinishedJobs(Ladder):
    def cache(self, job: str, name: str, data: bytes = BIG) -> Path:
        return self.put(self.home / ".cache" / "trimreaper" / "jobs" / job, f"{name}/payload.bin", data).parent

    def test_fixtures_of_a_finished_job_go_before_its_other_folders(self):
        self.add_job("J-4242", status="done", finished_ago=2 * DAY * 1)
        fixtures, notes = self.cache("J-4242", "fixtures"), self.cache("J-4242", "notes")
        self.fill_to(93)
        out = self.clean_all("--no-escalate")
        self.assertTrue(fixtures.exists() and notes.exists(), "the routine rules wait out evidenceExpiryDays (14)")
        out = self.clean_all()
        self.assertFalse(fixtures.exists())
        self.assertTrue(notes.exists(), "a folder with no regenerable name waits for the unmarked-retention rung")
        self.assertEqual(self.rungs(out)[-1], "worktrees-and-finished-jobs")
        self.assertGreaterEqual(out["summary"]["removedBytes"], 700_000)

    def test_unmarked_retention_of_a_job_finished_a_day_ago_goes_on_its_rung(self):
        self.add_job("J-4242", status="done", finished_ago=2 * DAY)
        fixtures, notes = self.cache("J-4242", "fixtures"), self.cache("J-4242", "notes")
        self.fill_to(99)
        out = self.clean_all()
        self.assertFalse(fixtures.exists() or notes.exists())
        self.assertEqual(self.rungs(out)[:5], ["routine", "idle-repositories", "worktrees-and-finished-jobs", "pinned-folders", "unmarked-retention"])

    def test_a_retention_marker_and_a_secret_keep_a_job_folder_on_every_rung(self):
        self.add_job("J-4242", status="done", finished_ago=5 * DAY)
        marked, secret = self.cache("J-4242", "fixtures"), self.cache("J-4242", "deploy")
        (marked / ".ut-retention-customer").write_text("")
        self.put(secret, "creds/.env", b"TOKEN=1\n")
        self.fill_to(99)
        self.clean_all()
        self.assertTrue((marked / "payload.bin").exists())
        self.assertTrue((secret / "creds" / ".env").exists())

    def test_a_job_finished_hours_ago_keeps_its_folders(self):
        self.add_job("J-4242", status="done", finished_ago=3 * 3600)
        fixtures = self.cache("J-4242", "fixtures")
        self.fill_to(99)
        self.clean_all()
        self.assertTrue(fixtures.exists())

    def test_running_and_parked_jobs_keep_everything_and_the_report_says_so(self):
        self.add_job("J-4301", status="running")
        self.add_job("J-4302", status="parked")
        running, parked = self.cache("J-4301", "fixtures"), self.cache("J-4302", "fixtures")
        with self.state() as conn, conn:
            conn.execute("UPDATE jobs SET finished_at=? WHERE id IN ('J-4301','J-4302')", (time.time() - 9 * DAY,))
        self.fill_to(99)
        self.clean_all()
        self.assertTrue(running.exists() and parked.exists())

    def test_a_finished_waiter_never_pins_and_a_stale_interrupted_one_pins_until_the_pinned_rung(self):
        self.add_job("J-4400", status="done", finished_ago=4 * DAY)
        fixtures = self.cache("J-4400", "fixtures")
        self.add_job("J-4401", status="running")
        interrupt(self, "J-4401", hours_ago=30, waits_on="J-4400")
        self.fill_to(93)
        out = self.clean_all("--dry-run")
        self.assertTrue(fixtures.exists())
        rung = {r["name"]: r for r in out["rungs"]}
        self.assertEqual(rung["worktrees-and-finished-jobs"]["candidateBytes"], 0, "still pinned by the interrupted job")
        self.assertGreaterEqual(rung["pinned-folders"]["candidateBytes"], 700_000, "released: it was interrupted more than a day ago")
        out = self.clean_all()
        self.assertFalse(fixtures.exists())
        self.assertEqual(self.rungs(out)[-1], "pinned-folders")

    def test_a_recently_interrupted_or_parked_waiter_keeps_its_pin_on_every_rung(self):
        self.add_job("J-4500", status="done", finished_ago=4 * DAY)
        self.add_job("J-4502", status="done", finished_ago=4 * DAY)
        recent, parked = self.cache("J-4500", "fixtures"), self.cache("J-4502", "fixtures")
        self.add_job("J-4501", status="running")
        interrupt(self, "J-4501", hours_ago=3, waits_on="J-4500")
        self.add_job("J-4503", status="parked")
        with self.state() as conn, conn:
            conn.execute("UPDATE jobs SET meta=? WHERE id='J-4503'", (json.dumps({"waitingOn": {"job": "J-4502"}}),))
        self.fill_to(99)
        self.clean_all()
        self.assertTrue(recent.exists() and parked.exists())

    def test_a_failed_waiter_does_not_pin(self):
        self.add_job("J-4600", status="done", finished_ago=4 * DAY)
        fixtures = self.cache("J-4600", "fixtures")
        self.cli("job", "add", "J-4601", "--waits-on", "J-4600", "--status", "failed")
        self.fill_to(93)
        self.clean_all()
        self.assertFalse(fixtures.exists())

    def test_the_policy_windows_only_shorten_and_zero_stays_off(self):
        self.write_config({"policy": {"evidenceExpiryDays": 0, "cloneExpiryDays": 0}})
        self.add_job("J-4700", status="done", finished_ago=9 * DAY)
        fixtures = self.cache("J-4700", "fixtures")
        self.fill_to(99)
        self.clean_all()
        self.assertTrue(fixtures.exists(), "an operator who turned expiry off is not overruled")

    def test_a_clean_pushed_job_clone_goes_with_its_job_after_a_day_without_an_archive(self):
        clone = self.clone("app-j4810", tracked={"README.md": "hello\n", "big.txt": "x" * 700_000})  # committed and pushed: nothing a remote lacks
        self.add_job("J-4810", cwd=clone, status="done", finished_ago=2 * DAY)
        self.age(clone, 2)
        self.fill_to(93)
        self.clean_all("--no-escalate")
        self.assertTrue(clone.exists(), "cloneExpiryDays (3) has not passed")
        with mock.patch.object(core, "reap_archive_create") as archive:
            out = self.clean_all()
        archive.assert_not_called()
        self.assertFalse(clone.exists())
        self.assertEqual(self.rungs(out)[-1], "worktrees-and-finished-jobs")

    def test_a_dirty_job_clone_is_kept_by_every_rung_but_the_last(self):
        clone = self.clone("app-j4811")
        self.put(clone, "notes.txt", BIG)
        self.add_job("J-4811", cwd=clone, status="done", finished_ago=2 * DAY)
        self.age(clone, 2)
        self.fill_to(95)
        with mock.patch.object(clean, "CLEAN_RUNGS", clean.CLEAN_RUNGS[:5]), \
                mock.patch.object(clean, "clean_ladder", lambda policy, escalate: [0, 1, 2, 3, 4]):
            self.clean_all()
        self.assertTrue((clone / "notes.txt").exists())
        self.assertFalse((self.home / ".cache" / "trimreaper" / "archive" / "J-4811").exists())

    def test_a_dirty_job_clone_is_archived_then_removed_only_on_the_last_rung(self):
        clone = self.clone("app-j4800")
        self.put(clone, "notes.txt", BIG)  # unsaved work, and the weight that makes the clone worth removing
        self.put(clone, "node_modules/x", b"n" * 100)
        self.add_job("J-4800", cwd=clone, status="done", finished_ago=2 * DAY)
        self.age(clone, 2)
        self.fill_to(93)
        self.clean_all("--dry-run")
        self.assertTrue((clone / "notes.txt").exists())
        out = self.clean_all("--no-escalate")
        self.assertTrue((clone / "notes.txt").exists())
        out = self.clean_all()
        self.assertEqual(self.rungs(out)[-1], "archive-then-remove")
        self.assertFalse(clone.exists())
        saved = sorted((self.home / ".cache" / "trimreaper" / "archive" / "J-4800").glob("app-j4800-*"))
        self.assertEqual(len(saved), 1)
        with tarfile.open(saved[0] / "files.tar.gz") as tar:
            self.assertTrue(any(n.endswith("notes.txt") for n in tar.getnames()))


class SteadyState(Ladder):
    TOTAL = 20_000_000

    def test_the_steady_pass_takes_only_long_idle_output_and_old_job_artifacts(self):
        old, recent = self.project("old"), self.project("recent")
        self.age(old, 4)
        self.age(recent, 2)  # past the routine 48 hours, short of the steady three days
        wt = self.worktree(self.clone("app"), "app-two-days", days=2)
        self.put(wt, "node_modules/x", BIG)
        self.add_job("J-4900", status="done", finished_ago=3 * DAY)
        self.add_job("J-4901", status="done", finished_ago=1 * DAY)
        old_job = self.put(self.home / ".cache/trimreaper/jobs/J-4900", "fixtures/x", BIG).parent
        new_job = self.put(self.home / ".cache/trimreaper/jobs/J-4901", "fixtures/x", BIG).parent
        self.fill_to(40)  # far below the high watermark: the goal plays no part
        out = self.clean_all("--steady")
        self.assertEqual(out["mode"], "steady")
        self.assertEqual(self.rungs(out), ["steady"])
        self.assertFalse((old / "node_modules").exists())
        self.assertTrue((recent / "node_modules").exists())
        self.assertTrue(wt.exists(), "the steady pass takes no worktree")
        self.assertFalse(old_job.exists())
        self.assertTrue(new_job.exists())

    def test_the_steady_pass_never_runs_a_tool_cleaner(self):
        profile = clean.clean_profile({}, clean.CLEAN_STEADY)
        self.assertFalse(profile["tools"])
        self.assertEqual(profile["projectHours"], 72)
        self.assertEqual(profile["job"]["hours"], 48)

    def test_the_tick_runs_the_steady_pass_below_the_watermark_every_six_hours(self):
        old = self.project("old")
        self.age(old, 4)
        self.fill_to(40)
        t = time.time()
        def tick(at):
            with mock.patch.object(clean, "now", lambda: at):
                return [a["did"] for a in self.cli("tick")["actions"]]
        self.assertIn("cleaned-steady", tick(t))
        self.assertFalse((old / "node_modules").exists())
        again = self.project("again")
        self.age(again, 4)
        self.assertNotIn("cleaned-steady", tick(t + 3600))
        self.assertTrue((again / "node_modules").exists())
        self.assertIn("cleaned-steady", tick(t + 6 * 3600 + 5))
        self.assertFalse((again / "node_modules").exists())

    def test_the_steady_pass_can_be_turned_off(self):
        self.write_config({"policy": {"cleanSteadyHours": 0}})
        old = self.project("old")
        self.age(old, 4)
        self.fill_to(40)
        self.assertNotIn("cleaned-steady", [a["did"] for a in self.cli("tick")["actions"]])
        self.assertTrue((old / "node_modules").exists())
        self.write_config({"policy": {"cleanAtPercent": 0}})
        self.assertNotIn("cleaned-steady", [a["did"] for a in self.cli("tick")["actions"]])


class Plumbing(Ladder):
    def test_a_planning_pass_walks_each_leftover_tree_once_however_many_rungs_it_climbs(self):
        folder = self.buildtmp / "old-session"
        self.put(folder, "a/b/c.txt")
        self.age(folder, 5)
        self.fill_to(95)
        walks = []
        real = clean.clean_tree_walk
        def counted(path, floor):
            walks.append(path)
            return real(path, floor)
        with mock.patch.object(clean, "clean_tree_walk", counted):
            out = self.clean_all("--dry-run", "--only", "agent-leftovers")
        self.assertEqual(len(out["rungs"]), 6)
        self.assertLessEqual(len(walks), 2, "once at the routine window, at most once more at the shorter one")

    def test_the_removals_own_last_look_never_reads_the_memo(self):
        folder = self.buildtmp / "old-session"
        self.put(folder, "a.txt")
        self.age(folder, 5)
        clean.CLEAN_MEMO.data = {}
        self.addCleanup(setattr, clean.CLEAN_MEMO, "data", None)
        clean.clean_stale_tree(str(folder), 72, fresh=False)
        (folder / "late.txt").write_text("written after planning")
        with self.assertRaises(clean.ReapRefused) as refused:
            clean.clean_stale_tree(str(folder), 72, fresh=True)
        self.assertTrue(str(refused.exception).startswith("recent:"))
        clean.clean_stale_tree(str(folder), 72, fresh=False)  # the plan's own answer, as it was

    def test_repository_discovery_runs_once_per_pass_and_drops_what_is_gone(self):
        repo = self.project()
        self.age(repo, HOUR * 3)
        self.fill_to(90)
        scans = []
        real = clean.clean_repo_walk_scan
        def counted(errors, wanted, label):
            scans.append(label)
            return real(errors, wanted, label)
        with mock.patch.object(clean, "clean_repo_walk_scan", counted):
            self.clean_all("--no-report")
        # two kinds of walk per window set: rust targets and project outputs; the second and later rungs reuse the first rung's wide walk
        self.assertLessEqual(len(scans), 4, scans)

    def test_a_steady_pass_is_not_a_pressure_pass_for_a_host_that_watches_those(self):
        old = self.project("old")
        self.age(old, 4)
        self.fill_to(40)
        watcher = mock.Mock(side_effect=clean.CLEAN_PASS)
        with mock.patch.object(clean, "clean_pass", watcher):
            self.assertIn("cleaned-steady", [a["did"] for a in self.cli("tick")["actions"]])
        watcher.assert_not_called()
        self.assertFalse((old / "node_modules").exists())

    def test_the_steady_pass_is_due_only_after_its_hours(self):
        import contextlib
        with contextlib.closing(self.state_connection()) as conn:
            policy = {"cleanSteadyHours": 6, "cleanAtPercent": 75}
            self.assertTrue(clean.clean_steady_due(conn, policy))
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (clean.CLEAN_STEADY_KEY, str(time.time() - 3600)))
            self.assertFalse(clean.clean_steady_due(conn, policy))
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (clean.CLEAN_STEADY_KEY, str(time.time() - 7 * 3600)))
            self.assertTrue(clean.clean_steady_due(conn, policy))
            self.assertFalse(clean.clean_steady_due(conn, {**policy, "cleanSteadyHours": 0}))
            self.assertFalse(clean.clean_steady_due(conn, {**policy, "cleanAtPercent": 0}))
            self.assertFalse(clean.clean_steady_due(conn, {"cleanAtPercent": 75}), "a partial policy has not opted in")

    def state_connection(self):
        self.cli("job", "list")  # creates the store
        conn = self.state()
        conn.row_factory = sqlite3.Row
        return conn

    def test_a_pass_still_running_blocks_the_next_one_for_half_an_hour(self):
        import contextlib
        with contextlib.closing(self.state_connection()) as conn, conn:
            disk = {"usedBytes": 99_000, "freeBytes": 1_000, "usedPercent": 99}
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (clean.CLEAN_AUTO_KEY, json.dumps(
                {"at": time.time() - 1200, "freeBytes": 1_000, "goalMet": False, "event": None, "removedBytes": None, "running": True})))
            self.assertIsNone(clean.clean_auto_observe(conn, {"cleanAtPercent": 75, "cleanToPercent": 60}, disk))
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (clean.CLEAN_AUTO_KEY, json.dumps(
                {"at": time.time() - 1900, "freeBytes": 1_000, "goalMet": False, "event": None, "removedBytes": None, "running": True})))
            self.assertEqual(clean.clean_auto_observe(conn, {"cleanAtPercent": 75, "cleanToPercent": 60}, disk), "pressure", "a crashed pass is not waited on forever")

    def test_only_finished_jobs_runs_only_the_job_sweeps(self):
        repo = self.project()
        self.age(repo, HOUR * 3)
        self.add_job("J-4242", status="done", finished_ago=2 * DAY)
        fixtures = self.put(self.home / ".cache/trimreaper/jobs/J-4242", "fixtures/x", BIG).parent
        self.fill_to(99)
        self.clean_all("--only", "finished-jobs")
        self.assertFalse(fixtures.exists())
        self.assertTrue((repo / "node_modules").exists())

    def test_removed_bytes_and_freed_bytes_disagree_honestly(self):
        repo = self.project()
        self.age(repo, HOUR * 3)
        busy = {"path": str(self.tmp), "usedBytes": 1_800_000, "freeBytes": 200_000, "usedPercent": 90}  # something else writes as fast as we remove
        with mock.patch.object(clean, "disk_state", lambda: busy):
            out = self.clean_all()
        self.assertGreaterEqual(out["summary"]["removedBytes"], 700_000)
        self.assertEqual(out["summary"]["freedBytes"], 0)
        self.assertIn("other writers", out["summary"]["freedNote"])
        self.assertIs(out["goalMet"], False)
