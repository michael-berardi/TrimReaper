"""Offload: cold folders move to an overflow volume and leave a symlink; busy, recent or unmounted cases move nothing."""
import os
import time
from pathlib import Path
from unittest import mock

from helpers import Home
from trimreaper import core, host, offload

PRODUCTION_CHANGED = offload.changed_entry  # KeepFolder patches the module name; the real rule is kept here
REAL_TREE_STATS = offload.tree_stats  # Base patches the module name; InUse walks for real
REAL_READS = getattr(offload, "reads_recorded", None)  # InUse patches the module name


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


class MoveOne(Base):
    """The last checks before the swap protect both copies, not just the folder being moved."""

    def setUp(self):
        super().setUp()
        self.path = self.src / "old"
        self.dest = self.root / "home" / "jobs" / "old"
        self.item = {"path": self.path, "dest": self.dest, "bytes": 8192,
                     "stats": self.fake_stats(self.path), "quiet": 1, "reads": None, "siblings": {"peers": {}}}
        holders = mock.patch.object(core, "reap_holders", return_value=None)
        holders.start()
        self.addCleanup(holders.stop)

    def test_existing_real_destination_is_set_aside_never_deleted(self):
        self.dest.parent.mkdir(parents=True)
        self.dest.mkdir()
        sentinel = self.dest / "only-copy.txt"
        sentinel.write_text("keep me")
        result = offload.move_one(self.item, False)
        self.assertTrue(result["moved"])
        aside = list(self.dest.parent.glob(".offload-old.superseded-*"))
        self.assertEqual(len(aside), 1)
        self.assertEqual((aside[0] / "only-copy.txt").read_text(), "keep me")
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(os.readlink(self.path), str(self.dest))
        self.assertEqual(list(self.dest.parent.glob(".offload-*.partial")), [])

    def test_appledouble_names_survive_the_copy(self):
        (self.path / "__MACOSX").mkdir()
        (self.path / "__MACOSX" / "._App.app").write_bytes(b"d" * 220)
        (self.path / "App.app").mkdir()
        (self.path / "._loose").write_bytes(b"e")
        self.item["stats"] = self.fake_stats(self.path)
        result = offload.move_one(self.item, False)
        self.assertTrue(result["moved"])
        self.assertTrue((self.dest / "__MACOSX" / "._App.app").is_file())
        self.assertTrue((self.dest / "._loose").is_file())

    def test_holders_are_rechecked_when_regen_did_not_touch_the_folder(self):
        self.assertNotIn("regenDone", self.item)
        calls = []

        def holders(path, root, table=None):
            calls.append((path, root))
            if root == str(self.path):
                raise core.ReapRefused("in-use:cwd:99(zsh)")

        with mock.patch.object(core, "reap_holders", side_effect=holders):
            with self.assertRaisesRegex(core.ReapRefused, "in-use:cwd:99"):
                offload.move_one(self.item, False)
        self.assertIn((str(self.path), str(self.path)), calls)
        self.assertTrue(self.path.is_dir())
        self.assertFalse(self.path.is_symlink())
        self.assertFalse(self.dest.exists())
        self.assertEqual(list(self.dest.parent.glob(".offload-*.partial")), [])

    def test_partial_named_destination_is_kept_too(self):
        # the interrupted-copy leftover is .offload-<name>.partial beside the destination, never the destination itself
        self.dest = self.root / "home" / "old.partial"
        self.item["dest"] = self.dest
        self.dest.parent.mkdir(parents=True)
        self.dest.mkdir()
        (self.dest / "keep.txt").write_text("only copy")
        offload.move_one(self.item, False)
        aside = list(self.dest.parent.glob(".offload-old.partial.superseded-*"))
        self.assertEqual(len(aside), 1)
        self.assertEqual((aside[0] / "keep.txt").read_text(), "only copy")


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
        items = offload.candidates(offload.settings())
        self.assertEqual(sorted(i["path"].name for i in items), ["J-0001", "J-0002"])
        self.assertTrue(all("parent" not in item for item in items))
        self.assertTrue(all(offload.parent_active(item, 60, {}, time.time()) is None for item in items))

    def table(self, cwd=None, command=None):
        files = [("42", "zsh", "cwd", core.reap_canon(cwd))] if cwd else []
        return {"files": files, "commands": [("43", command)] if command else []}

    def run_pass(self, table):
        with mock.patch.object(offload.core, "reap_process_table", return_value=table):
            out = offload.offload_pass(everything=True)
        return out, {k["path"]: k["why"] for k in out["kept"]}

    def test_active_release_folder_keeps_every_child_when_one_is_a_process_cwd(self):
        # a running process works in one child of a kept folder; the cold sibling must not move to the external disk
        wt, notes = self.base / "J-0001" / "wt", self.base / "J-0001" / "notes"
        out, kept = self.run_pass(self.table(cwd=notes))
        self.assertEqual(out["errors"], [])
        self.assertFalse(wt.is_symlink())
        self.assertFalse(notes.is_symlink())
        self.assertIn("in-use:cwd:42(zsh)", kept[str(notes)])
        self.assertIn("parent-active: in-use:cwd:42(zsh)", kept[str(wt)])

    def test_process_inside_an_already_offloaded_sibling_keeps_the_kept_folder(self):
        notes, wt = self.base / "J-0001" / "notes", self.base / "J-0001" / "wt"
        dest = self.root / "home" / "jobs" / "J-0001" / "notes"
        dest.parent.mkdir(parents=True)
        os.rename(notes, dest)
        os.symlink(dest, notes)
        out, kept = self.run_pass(self.table(cwd=dest))
        self.assertEqual(out["errors"], [])
        self.assertFalse(wt.is_symlink())
        self.assertIn("parent-active: in-use:cwd:42(zsh)", kept[str(wt)])

    def test_open_file_inside_an_offloaded_sibling_keeps_the_kept_folder(self):
        notes, wt = self.base / "J-0001" / "notes", self.base / "J-0001" / "wt"
        dest = self.root / "home" / "jobs" / "J-0001" / "notes"
        dest.parent.mkdir(parents=True)
        os.rename(notes, dest)
        os.symlink(dest, notes)
        table = {"files": [("42", "node", "7", str(dest / "f"))], "commands": []}
        _, kept = self.run_pass(table)
        self.assertIn("parent-active: in-use:open-handle:42(node)", kept[str(wt)])
        self.assertFalse(wt.is_symlink())

    def test_sibling_link_that_cannot_be_resolved_keeps(self):
        notes, wt = self.base / "J-0001" / "notes", self.base / "J-0001" / "wt"
        notes.rename(self.tmp / "gone")
        os.symlink(self.root / "home" / "missing", notes)
        _, kept = self.run_pass(self.table())
        self.assertRegex(kept[str(wt)], r"^parent-active: notes link cannot be resolved")
        self.assertFalse(wt.is_symlink())

    def test_link_pointing_outside_the_overflow_root_is_not_scanned(self):
        notes = self.base / "J-0001" / "notes"
        elsewhere = self.tmp / "elsewhere"
        notes.rename(elsewhere)
        os.symlink(elsewhere, notes)
        out, _ = self.run_pass(self.table(cwd=elsewhere))
        self.assertTrue((self.base / "J-0001" / "wt").is_symlink())

    def test_production_rule_sees_a_change_of_ctime_only(self):
        quiet = self.base / "J-0001" / "notes" / "f"
        os.chmod(quiet, 0o600)  # ctime moves, mtime stays an hour old
        folder = str(self.base / "J-0001" / "notes")
        self.assertEqual(offload.recent_change(folder, time.time() - 600), (None, True))  # fixture rule: mtime only
        with mock.patch.object(offload, "changed_entry", PRODUCTION_CHANGED):
            newest, complete = offload.recent_change(folder, time.time() - 600)
        self.assertTrue(complete)
        self.assertGreater(newest, time.time() - 60)

    def test_own_pass_does_not_touch_the_change_time_of_kept_siblings(self):
        loose = self.base / "J-0001" / "coordination.jsonl"
        before = loose.lstat().st_ctime_ns
        with self.no_holders:
            offload.offload_pass(everything=True)
        self.assertTrue((self.base / "J-0001" / "wt").is_symlink())
        self.assertEqual(loose.lstat().st_ctime_ns, before)

    def test_scan_hitting_its_bound_keeps(self):
        with mock.patch.object(offload, "WALK_ENTRIES", 0):
            out, kept = self.moved_and_kept()
        self.assertRegex(kept[str(self.base / "J-0001" / "wt")], r"^parent-active: \S+ scan incomplete$")
        self.assertFalse((self.base / "J-0001" / "wt").is_symlink())

    def test_one_bound_covers_every_sibling_of_a_kept_folder(self):
        scan = {}
        with mock.patch.object(offload, "WALK_ENTRIES", 3):
            first = offload.recent_change(str(self.base / "J-0001" / "wt"), time.time() - 600, scan)
            second = offload.recent_change(str(self.base / "J-0001" / "notes"), time.time() - 600, scan)
        self.assertEqual(first, (None, True))
        self.assertEqual(second, (None, False))

    def test_unreadable_kept_folder_keeps(self):
        real = os.listdir
        def listing(path):
            if str(path) == str(self.base / "J-0001"):
                raise PermissionError("denied")
            return real(path)
        # Python 3.12's Path.iterdir calls os.listdir, while older versions use os.scandir. Keep candidate
        # discovery version-independent so the mock reaches only parent_active's listing of the unreadable parent.
        def iterdir_via_scandir(path):
            with os.scandir(path) as it:
                return [Path(entry.path) for entry in it]
        with mock.patch.object(Path, "iterdir", iterdir_via_scandir), \
             mock.patch.object(offload.os, "listdir", side_effect=listing):
            _, kept = self.moved_and_kept()
        self.assertEqual(kept[str(self.base / "J-0001" / "wt")], "parent-active: unreadable PermissionError")

    def test_sibling_changed_during_the_copy_stops_the_swap(self):
        loose = self.base / "J-0001" / "coordination.jsonl"
        real_copy = offload.copy_tree
        def slow_copy(source, partial):
            real_copy(source, partial)
            if source.name == "wt":
                loose.write_text("new work")  # a sibling changes while the copy ran
        with mock.patch.object(offload, "copy_tree", side_effect=slow_copy), self.no_holders:
            out = offload.offload_pass(everything=True)
        self.assertFalse((self.base / "J-0001" / "wt").is_symlink())
        self.assertTrue(any("kept folder became active during the copy" in e and "coordination.jsonl changed" in e for e in out["errors"]), out["errors"])
        self.assertEqual(list((self.base / "J-0001").glob(".offload-*")), [])


class InUse(Base):
    """A folder in use for work stays even after an hour of quiet: work in progress in git, a recent read, release artifacts.
    A clean, pushed, idle worktree still moves."""
    def setUp(self):
        super().setUp()
        self.app = self.clone("app")
        self.base = self.home / "scratch"
        self.rel = self.base / "rel-0001"
        self.rel.mkdir(parents=True)
        self.wt = self.rel / "wt"
        self.git(self.app, "worktree", "add", "-q", "-b", "rel-1", str(self.wt))
        self.git(self.wt, "push", "-q", "-u", "origin", "rel-1")
        self.put(self.wt, "target/big.bin")  # ignored build output: not work in progress
        self.config({})
        # ctime cannot be set back; the walk reads the mtime as the change time in the fixture
        patch = mock.patch.object(offload, "changed_entry", side_effect=lambda info: info.st_mtime)
        patch.start()
        self.addCleanup(patch.stop)
        reads = mock.patch.object(offload, "reads_recorded", return_value=None, create=True)
        reads.start()
        self.addCleanup(reads.stop)

    def config(self, extra):
        self.write_config({"offload": {"root": str(self.root), "minFreeGB": 0, "quietMinutes": 60, **extra,
                                       "sources": [{"path": "~/scratch", "children": True, "keepFolder": True}]}})

    def fake_stats(self, path):
        stats = REAL_TREE_STATS(path)
        stats["newest"] = max(p.lstat().st_mtime for p in [Path(path), *Path(path).rglob("*")])
        return stats

    def run_pass(self):
        self.age(self.base, 1)
        with mock.patch.object(offload.core, "reap_holders", return_value=None):
            out = offload.offload_pass(everything=True)
        return out, {Path(k["path"]).relative_to(self.base).as_posix(): k["why"] for k in out["kept"]}

    def test_clean_pushed_idle_worktree_still_moves(self):
        out, kept = self.run_pass()
        self.assertEqual(out["errors"], [])
        self.assertEqual(kept, {})
        self.assertTrue(self.wt.is_symlink())
        self.assertEqual((self.wt / "README.md").read_text(), "hello\n")

    def test_uncommitted_change_keeps_the_worktree(self):
        (self.wt / "README.md").write_text("half done\n")
        out, kept = self.run_pass()
        self.assertFalse(self.wt.is_symlink())
        self.assertEqual(kept["rel-0001/wt"], "git-dirty:M")

    def test_untracked_file_keeps_the_worktree(self):
        self.put(self.wt, "notes/new.txt")
        _, kept = self.run_pass()
        self.assertFalse(self.wt.is_symlink())
        self.assertTrue(kept["rel-0001/wt"].startswith("git-dirty:"), kept)

    def test_unpushed_commit_keeps_the_worktree(self):
        (self.wt / "VERSION").write_text("1.2.3\n")
        self.git(self.wt, "add", "VERSION")
        self.git(self.wt, "commit", "-q", "-m", "bump")
        _, kept = self.run_pass()
        self.assertFalse(self.wt.is_symlink())
        self.assertEqual(kept["rel-0001/wt"], "git-unpushed:1:rel-1")

    def test_branch_without_upstream_keeps_the_worktree(self):
        self.git(self.wt, "checkout", "-q", "-b", "local-only")
        _, kept = self.run_pass()
        self.assertFalse(self.wt.is_symlink())
        self.assertEqual(kept["rel-0001/wt"], "git-unpushed:no-upstream:local-only")

    def test_branch_whose_upstream_was_deleted_keeps_the_worktree(self):
        self.git(self.wt, "push", "-q", "origin", "--delete", "rel-1")
        self.git(self.wt, "fetch", "-q", "--prune", "origin")
        _, kept = self.run_pass()
        self.assertFalse(self.wt.is_symlink())
        self.assertEqual(kept["rel-0001/wt"], "git-unpushed:no-upstream:rel-1")

    def test_repository_nested_in_a_moving_folder_is_checked(self):
        self.write_config({"offload": {"root": str(self.root), "minFreeGB": 0, "quietMinutes": 60,
                                       "sources": [{"path": "~/scratch", "children": True}]}})
        (self.wt / "README.md").write_text("half done\n")
        _, kept = self.run_pass()
        self.assertFalse(self.rel.is_symlink())
        self.assertEqual(kept["rel-0001"], "git-dirty:M (wt)")

    def pass_after_read(self, path):
        self.age(self.base, 1)
        os.utime(path, (time.time() - 120, time.time() - 86400))  # read two minutes ago, last written a day ago
        with mock.patch.object(offload.core, "reap_holders", return_value=None):
            out = offload.offload_pass(everything=True)
        return out, {Path(k["path"]).relative_to(self.base).as_posix(): k["why"] for k in out["kept"]}

    def test_file_read_within_the_quiet_time_keeps_the_folder(self):
        _, kept = self.pass_after_read(self.wt / "README.md")
        self.assertFalse(self.wt.is_symlink())
        self.assertEqual(kept["rel-0001/wt"], "read-within-60min")

    def test_volume_without_access_times_falls_back_and_says_so(self):
        with mock.patch.object(offload, "reads_recorded", return_value="the volume is mounted noatime", create=True):
            out, _ = self.pass_after_read(self.wt / "README.md")
        self.assertTrue(self.wt.is_symlink())
        self.assertEqual(out["moved"][0].get("reads"), "not-recorded: the volume is mounted noatime; only changes count as use")

    def test_sibling_read_within_the_quiet_time_keeps_the_kept_folder(self):
        notes = self.put(self.rel, "notes/log.txt")
        _, kept = self.pass_after_read(notes)
        self.assertFalse(self.wt.is_symlink())
        self.assertRegex(kept["rel-0001/wt"], r"^parent-active: notes read [12] min ago$")
        self.assertEqual(kept["rel-0001/notes"], "read-within-60min")

    def test_release_artifacts_keep_the_release_folder(self):
        self.put(self.rel, "out/app-1.2.3.dmg")
        self.put(self.rel, "unz/App/bin")
        self.put(self.rel, "crate/lib.rs")
        out, kept = self.run_pass()
        self.assertEqual(out["moved"], [])
        self.assertEqual(kept, {name: "release-artifacts:out,unz" for name in ("rel-0001/wt", "rel-0001/out", "rel-0001/unz", "rel-0001/crate")})

    def test_artifacts_beside_a_worktree_keep_a_folder_of_any_name(self):
        other = self.base / "build-7"
        self.put(other, "wt/a")
        self.put(other, "dist/pkg.tar")
        _, kept = self.run_pass()
        self.assertEqual(kept["build-7/dist"], "release-artifacts:dist")
        self.assertFalse((other / "dist").is_symlink())

    def test_artifact_name_alone_does_not_hold_an_unrelated_folder(self):
        self.put(self.base / "misc", "out/x")
        self.put(self.rel, "out")  # an empty folder holds nothing: a file named like an artifact is not one
        out, kept = self.run_pass()
        self.assertEqual(kept, {})
        self.assertTrue((self.base / "misc" / "out").is_symlink())

    def test_release_artifact_rule_is_configured(self):
        self.put(self.rel, "out/app.dmg")
        self.put(self.rel, "pkg/app.pkg")
        self.config({"releaseArtifacts": ["pkg"]})
        _, kept = self.run_pass()
        self.assertEqual(kept["rel-0001/wt"], "release-artifacts:pkg")
        self.config({"releaseArtifacts": []})
        out, kept = self.run_pass()
        self.assertEqual(kept, {})
        self.assertTrue(self.wt.is_symlink())

    def test_bad_release_settings_are_named(self):
        self.config({"releaseNamePattern": "("})
        with self.assertRaisesRegex(host.HostError, "releaseNamePattern"):
            host.config()
        self.config({"releaseArtifacts": "out"})
        with self.assertRaisesRegex(host.HostError, "releaseArtifacts"):
            host.config()

    def test_mount_options_decide_whether_reads_are_recorded(self):
        self.assertIsNotNone(REAL_READS, "offload has no access-time check")
        for options, verdict in (({"apfs", "local"}, None), ({"apfs", "noatime"}, "the volume is mounted noatime"), (None, "unknown (no mount entry for the volume)")):
            offload._MOUNTS.clear()
            table = [("/nonexistent-volume", {"noatime"})] + ([(str(self.tmp), options)] if options is not None else [])
            with mock.patch.object(offload, "mount_table", return_value=table):
                self.assertEqual(REAL_READS(self.tmp / "home"), verdict)
        offload._MOUNTS.clear()


class HolderBoundaries(Base):
    def table(self, **kw):
        return {"files": kw.get("files", []), "commands": kw.get("commands", [])}

    def test_command_line_naming_a_longer_folder_name_is_not_a_hit(self):
        root = "/jobs/J-0001"
        core.reap_holders(root, root, self.table(commands=[("5", "/bin/tool /jobs/J-00012/x"), ("6", "tool /jobs/J-0001x")]))

    def test_command_line_naming_the_folder_or_below_it_is_a_hit(self):
        root = "/jobs/J-0001"
        for command in ("tool /jobs/J-0001", "tool /jobs/J-0001/x", "tool '/jobs/J-0001'", 'tool "/jobs/J-0001/x"', "tool /jobs/J-0001 --flag", "tool /jobs/j-0001/X"):
            with self.assertRaisesRegex(core.ReapRefused, "command-line:5"):
                core.reap_holders(root, root, self.table(commands=[("5", command)]))

    def test_command_line_naming_a_temp_folder_by_its_short_spelling_is_a_hit(self):
        """A process names its temp folder as it was given it (/var/..., /tmp/...); the scan resolves to /private/...: both count."""
        root = "/private/var/folders/ab/T/tool-AbC123"
        with self.assertRaisesRegex(core.ReapRefused, "command-line:7"):
            core.reap_holders(root, root, self.table(commands=[("7", "node tool --patch /var/folders/ab/T/tool-AbC123/overlay.yml")]))
        with self.assertRaisesRegex(core.ReapRefused, "command-line:8"):
            core.reap_holders("/tmp/work-9", "/tmp/work-9", self.table(commands=[("8", "node tool --dir=/private/tmp/work-9")]))
        with self.assertRaisesRegex(core.ReapRefused, "command-line:9"):
            core.reap_holders(root, root, self.table(commands=[("9", "node tool --patch=/private/var/folders/ab/T/tool-AbC123")]))

    def test_command_line_match_starts_at_a_path_boundary(self):
        root = "/private/var/folders/ab/T/tool-AbC123"
        core.reap_holders(root, root, self.table(commands=[("5", "node tool /var/folders/ab/T/tool-AbC1234/overlay.yml"),
                                                           ("6", "node tool /data/var/folders/ab/T/tool-AbC123/f")]))

    def test_cwd_and_open_files_match_whole_path_segments(self):
        root = "/jobs/J-0001"
        files = [("5", "zsh", "cwd", "/jobs/J-00012"), ("6", "vim", "3", "/jobs/J-0001-old/f"), ("7", "vim", "4", "/jobs/J-00010")]
        core.reap_holders(root, root, self.table(files=files))
        with self.assertRaisesRegex(core.ReapRefused, "cwd:5"):
            core.reap_holders(root, root, self.table(files=[("5", "zsh", "cwd", "/jobs/J-0001/wt")]))
