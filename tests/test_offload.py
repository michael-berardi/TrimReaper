"""Offload: cold folders move to an overflow volume and leave a symlink; busy, recent or unmounted cases move nothing."""
import os
import time
from pathlib import Path
from unittest import mock

from helpers import Home
from trimreaper import core, host, offload

PRODUCTION_CHANGED = offload.changed_entry  # KeepFolder patches the module name; the real rule is kept here


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
        with mock.patch.object(offload.os, "listdir", side_effect=listing):
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

    def test_cwd_and_open_files_match_whole_path_segments(self):
        root = "/jobs/J-0001"
        files = [("5", "zsh", "cwd", "/jobs/J-00012"), ("6", "vim", "3", "/jobs/J-0001-old/f"), ("7", "vim", "4", "/jobs/J-00010")]
        core.reap_holders(root, root, self.table(files=files))
        with self.assertRaisesRegex(core.ReapRefused, "cwd:5"):
            core.reap_holders(root, root, self.table(files=[("5", "zsh", "cwd", "/jobs/J-0001/wt")]))
