"""Offload: cold folders move to an overflow volume and leave a symlink; busy, recent or unmounted cases move nothing."""
import os
import time
from pathlib import Path
from unittest import mock

from helpers import Home
from trimreaper import host, offload


class Base(Home):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "overflow"
        self.src = self.home / "cache"
        (self.src / "old").mkdir(parents=True)
        (self.src / "old" / "a.bin").write_bytes(b"x" * 4096)
        (self.src / "fresh").mkdir()
        (self.src / "fresh" / "b.bin").write_bytes(b"y" * 4096)
        past = time.time() - 10 * 86400
        for path in (self.src / "old" / "a.bin", self.src / "old"):
            os.utime(path, (past, past))
        self.write_config({"offload": {"root": str(self.root), "minFreeGB": 0, "quietMinutes": 3 * 24 * 60,
                                       "sources": [{"path": "~/cache", "children": True}]}})
        ready = mock.patch.object(offload, "volume_ready", return_value=None)
        ready.start()
        self.addCleanup(ready.stop)
        # ctime cannot be set back; treat the tree's newest change as the mtime for the fixture
        stats = mock.patch.object(offload, "tree_stats", side_effect=self.fake_stats)
        stats.start()
        self.addCleanup(stats.stop)

    def fake_stats(self, path):
        newest = max(p.stat().st_mtime for p in [Path(path), *Path(path).rglob("*")])
        return {"entries": 2, "bytes": 8192, "newest": newest, "complete": True}


class Offload(Base):
    def test_old_folder_moves_and_fresh_stays(self):
        out = offload.offload_pass()
        self.assertEqual(out["errors"], [])
        self.assertEqual([m["path"] for m in out["moved"]], [str(self.src / "old")])
        self.assertTrue((self.src / "old").is_symlink())
        self.assertEqual((self.src / "old" / "a.bin").read_bytes(), b"x" * 4096)
        self.assertFalse((self.src / "fresh").is_symlink())
        self.assertIn("changed-within-4320min", [k["why"] for k in out["kept"]])

    def test_dry_run_changes_nothing(self):
        out = offload.offload_pass(dry_run=True)
        self.assertEqual(len(out["moved"]), 1)
        self.assertFalse((self.src / "old").is_symlink())
        self.assertFalse(self.root.exists())

    def test_restore_brings_it_back(self):
        offload.offload_pass()
        offload.restore(str(self.src / "old"))
        self.assertFalse((self.src / "old").is_symlink())
        self.assertEqual((self.src / "old" / "a.bin").read_bytes(), b"x" * 4096)

    def test_unmounted_volume_moves_nothing(self):
        with mock.patch.object(offload, "volume_ready", return_value="the volume is not mounted"):
            out = offload.offload_pass()
        self.assertEqual(out["moved"], [])
        self.assertIn("not mounted", out["skipped"])
        self.assertFalse((self.src / "old").is_symlink())

    def test_open_folder_stays(self):
        from trimreaper import core
        with mock.patch.object(core, "reap_holders", side_effect=core.ReapRefused("in-use:cwd:1(x)")):
            out = offload.offload_pass()
        self.assertEqual(out["moved"], [])
        self.assertFalse((self.src / "old").is_symlink())

    def test_free_space_floor(self):
        self.write_config({"offload": {"root": str(self.root), "minFreeGB": 10 ** 6, "quietMinutes": 4320,
                                       "sources": [{"path": "~/cache", "children": True}]}})
        out = offload.offload_pass()
        self.assertEqual(out["moved"], [])
        self.assertTrue(all("minFreeGB" in k["why"] or k["why"].startswith("changed") for k in out["kept"]))

    def test_bad_config_is_named(self):
        self.write_config({"offload": {"root": str(self.root), "sources": [{"path": "~/cache", "idleDay": 3}]}})
        with self.assertRaisesRegex(host.HostError, "idleDay"):
            host.config()


class OffloadJobs(Base):
    def setUp(self):
        super().setUp()
        self.quiet_since = time.time() - 600
        self.script = self.tmp / "jobs.py"
        self.statuses = {"J-0001": "done", "J-0002": "running", "J-0003": "parked"}
        self.script.write_text("import json;print(json.dumps(%r))" % self.statuses)
        self.base = self.home / "jobs"
        for name in ("app-j0001", "app-j0002", "app-j0003", "app-j0009"):
            (self.base / name).mkdir(parents=True)
            (self.base / name / "f").write_bytes(b"z" * 4096)
        self.write_config({"offload": {"root": str(self.root), "minFreeGB": 0, "quietMinutes": 10 ** 6,
                                       "jobStatusCommand": ["python3", str(self.script)],
                                       "sources": [{"path": "~/jobs", "children": True, "jobs": True}]}})

    def fake_stats(self, path):
        return {"entries": 2, "bytes": 8192, "newest": self.quiet_since, "complete": True}

    def test_finished_job_moves_at_once_live_and_unknown_stay(self):
        out = offload.offload_pass(everything=True)
        moved = {Path(m["path"]).name for m in out["moved"]}
        self.assertEqual(moved, {"app-j0001"})
        why = {Path(k["path"]).name: k["why"] for k in out["kept"]}
        self.assertTrue(why["app-j0002"].startswith("job-live"))
        self.assertTrue(why["app-j0003"].startswith("changed-within"))
        self.assertTrue(why["app-j0009"].startswith("job-unknown"))

    def test_moved_folder_returns_when_its_job_runs_again(self):
        offload.offload_pass(everything=True)
        self.assertTrue((self.base / "app-j0001").is_symlink())
        self.statuses["J-0001"] = "running"
        self.script.write_text("import json;print(json.dumps(%r))" % self.statuses)
        out = offload.offload_pass(everything=True)
        self.assertEqual([r["job"] for r in out["returned"]], ["J-0001"])
        self.assertFalse((self.base / "app-j0001").is_symlink())

    def test_status_command_failure_moves_nothing(self):
        self.script.write_text("import sys;sys.exit(3)")
        with self.assertRaisesRegex(host.HostError, "exited 3"):
            offload.offload_pass(everything=True)
        self.assertFalse((self.base / "app-j0001").is_symlink())


class KeepFolder(Base):
    def setUp(self):
        super().setUp()
        self.quiet_since = time.time() - 600
        self.script = self.tmp / "jobs.py"
        self.statuses = {"J-0001": "done", "J-0002": "running"}
        self.script.write_text("import json;print(json.dumps(%r))" % self.statuses)
        self.base = self.home / "jobs"
        for name in ("J-0001", "J-0002"):
            for inner in ("wt", "notes"):
                (self.base / name / inner).mkdir(parents=True)
                (self.base / name / inner / "f").write_bytes(b"z" * 4096)
            (self.base / name / "coordination.jsonl").write_text("log")
        self.write_config({"offload": {"root": str(self.root), "minFreeGB": 0, "quietMinutes": 10 ** 6,
                                       "jobStatusCommand": ["python3", str(self.script)],
                                       "sources": [{"path": "~/jobs", "children": True, "jobs": True, "keepFolder": True}]}})
        self.age_everything()
        # ctime cannot be set back; the change test reads the mtime in the fixture
        patch = mock.patch.object(offload, "changed_entry", side_effect=lambda info: info.st_mtime)
        patch.start()
        self.addCleanup(patch.stop)

    def fake_stats(self, path):
        return {"entries": 2, "bytes": 8192, "newest": self.quiet_since, "complete": True}

    def age_everything(self):
        past = time.time() - 3600
        for path in [self.base, *self.base.rglob("*")]:
            os.utime(path, (past, past), follow_symlinks=False)

    def test_job_folder_stays_a_real_directory(self):
        out = offload.offload_pass(everything=True)
        self.assertEqual(out["errors"], [])
        self.assertFalse((self.base / "J-0001").is_symlink())
        self.assertTrue((self.base / "J-0001" / "wt").is_symlink())
        self.assertEqual((self.base / "J-0001" / "wt" / "f").read_bytes(), b"z" * 4096)
        self.assertFalse((self.base / "J-0002" / "wt").is_symlink())
        self.assertFalse((self.base / "J-0001" / "coordination.jsonl").is_symlink())

    def test_file_links_from_an_earlier_version_become_files_again(self):
        offload.offload_pass(everything=True)
        dest = self.root / "home" / "jobs" / "J-0001" / "coordination.jsonl"
        dest.parent.mkdir(parents=True, exist_ok=True)
        (self.base / "J-0001" / "coordination.jsonl").rename(dest)
        os.symlink(dest, self.base / "J-0001" / "coordination.jsonl")
        offload.offload_pass(everything=True)
        self.assertFalse((self.base / "J-0001" / "coordination.jsonl").is_symlink())
        self.assertEqual((self.base / "J-0001" / "coordination.jsonl").read_text(), "log")
        self.assertFalse(dest.exists())

    def test_earlier_whole_folder_link_is_split_without_copying(self):
        dest = self.root / "home" / "jobs" / "J-0001"
        dest.parent.mkdir(parents=True)
        os.rename(self.base / "J-0001", dest)
        os.symlink(dest, self.base / "J-0001")
        offload.offload_pass(everything=True)
        self.assertFalse((self.base / "J-0001").is_symlink())
        self.assertTrue((self.base / "J-0001" / "notes").is_symlink())
        self.assertEqual((self.base / "J-0001" / "notes" / "f").read_bytes(), b"z" * 4096)

    def test_entries_return_when_job_runs_again(self):
        offload.offload_pass(everything=True)
        self.statuses["J-0001"] = "running"
        self.script.write_text("import json;print(json.dumps(%r))" % self.statuses)
        out = offload.offload_pass(everything=True)
        self.assertEqual(len(out["returned"]), 2)
        self.assertFalse((self.base / "J-0001" / "wt").is_symlink())
        self.assertEqual(offload.status()["offloaded"], 0)


class ParentActive(KeepFolder):
    def setUp(self):
        super().setUp()
        self.no_holders = mock.patch.object(offload.core, "reap_holders", return_value=None)

    def fake_stats(self, path):
        newest = max(p.lstat().st_mtime for p in [Path(path), *Path(path).rglob("*")])
        return {"entries": 2, "bytes": 8192, "newest": newest, "complete": True}

    def moved_and_kept(self):
        with self.no_holders:
            out = offload.offload_pass(everything=True)
        return out, {k["path"]: k["why"] for k in out["kept"]}

    def test_cold_subfolder_beside_recent_sibling_is_kept_with_cause(self):
        recent = time.time() - 120
        os.utime(self.base / "J-0001" / "notes" / "f", (recent, recent))
        out, kept = self.moved_and_kept()
        self.assertEqual(out["errors"], [])
        self.assertFalse((self.base / "J-0001" / "wt").is_symlink())
        self.assertRegex(kept[str(self.base / "J-0001" / "wt")], r"^parent-active: notes changed [12] min ago$")
        self.assertFalse((self.base / "J-0001" / "notes").is_symlink())

    def test_recent_loose_file_sibling_keeps_it(self):
        (self.base / "J-0001" / "coordination.jsonl").write_text("new")
        os.utime(self.base / "J-0001" / "coordination.jsonl", None)
        _, kept = self.moved_and_kept()
        self.assertIn("parent-active: coordination.jsonl changed 0 min ago", kept[str(self.base / "J-0001" / "wt")])

    def test_fully_quiet_parent_still_moves(self):
        out, _ = self.moved_and_kept()
        self.assertEqual(out["errors"], [])
        self.assertTrue((self.base / "J-0001" / "wt").is_symlink())
        self.assertTrue((self.base / "J-0001" / "notes").is_symlink())

    def test_process_cwd_under_the_parent_keeps_it(self):
        from trimreaper import core
        parent = str(self.base / "J-0001")
        def holders(path, root, table=None):
            if root == parent:
                raise core.ReapRefused("in-use:cwd:42(zsh)")
        with mock.patch.object(offload.core, "reap_holders", side_effect=holders):
            out = offload.offload_pass(everything=True)
        kept = {k["path"]: k["why"] for k in out["kept"]}
        self.assertEqual(kept[str(self.base / "J-0001" / "wt")], "parent-active: in-use:cwd:42(zsh)")
        self.assertFalse((self.base / "J-0001" / "wt").is_symlink())

    def test_plain_children_source_has_no_parent_check(self):
        self.write_config({"offload": {"root": str(self.root), "minFreeGB": 0, "quietMinutes": 1,
                                       "sources": [{"path": "~/jobs", "children": True}]}})
        self.assertTrue(all("parent" not in item for item in offload.candidates(offload.settings())))
