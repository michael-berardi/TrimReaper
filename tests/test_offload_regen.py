"""0.22.0: offload deletes regenerable output instead of moving it, ignores reads for job folders, and the overflow has retention."""
import os
import sys
import time
from pathlib import Path
from unittest import mock

from helpers import DAY, Home
from trimreaper import clean, core, host, offload


class Regen(Home):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "overflow"
        self.base = self.home / "jobs"
        self.statuses = {"J-0001": "done", "J-0002": "running"}
        self.script = self.tmp / "jobs.py"
        self.script.write_text("import json;print(json.dumps(%r))" % self.statuses)
        self.write_config({"offload": {"root": str(self.root), "minFreeGB": 0, "quietMinutes": 60,
                                       "jobStatusCommand": [sys.executable, str(self.script)],
                                       "sources": [{"path": "~/jobs", "children": True, "jobs": True}]}})
        for patch in (mock.patch.object(offload, "volume_ready", return_value=None),
                      mock.patch.object(offload.core, "reap_holders", return_value=None),
                      mock.patch.object(clean, "clean_running", return_value=set()),
                      mock.patch.object(offload, "changed_entry", side_effect=lambda info: info.st_mtime),
                      mock.patch.object(core, "REAP_SYSTEM_ROOTS", core.REAP_SYSTEM_ROOTS)):
            patch.start()
            self.addCleanup(patch.stop)
        self.proj = self.base / "J-0001"
        self.put(self.proj, "package.json", b"{}")
        self.put(self.proj, "src/a.txt", b"keep" * 1000)
        self.put(self.proj, "node_modules/x/index.js", b"n" * 5000)
        self.put(self.proj, "web/package.json", b"{}")
        self.put(self.proj, "web/.next/cache/c.bin", b"n" * 5000)
        self.put(self.proj, "pkg/__pycache__/a.pyc", b"p" * 100)
        self.put(self.proj, ".env", b"SECRET=1")
        self.age(self.base, 1)

    def run_pass(self, **kw):
        return offload.offload_pass(everything=True, **kw)

    def test_regenerable_output_is_deleted_not_moved(self):
        out = self.run_pass()
        self.assertEqual(out["errors"], [], out)
        names = sorted(Path(d["path"]).name for d in out["regenerable"]["deleted"])
        self.assertEqual(names, [".next", "__pycache__", "node_modules"])
        self.assertTrue(self.proj.is_symlink())
        mirror = self.root / "home" / "jobs" / "J-0001"
        self.assertTrue((mirror / "src" / "a.txt").exists())
        self.assertFalse((mirror / "node_modules").exists())
        self.assertFalse((mirror / "web" / ".next").exists())
        self.assertTrue((mirror / ".env").exists(), "protected leaf is moved whole, never deleted")

    def test_dry_run_lists_and_removes_nothing(self):
        out = self.run_pass(dry_run=True)
        self.assertEqual(out["regenerable"]["count"], 3, out)
        self.assertTrue((self.proj / "node_modules" / "x" / "index.js").exists())
        self.assertFalse(self.root.exists())

    def test_live_job_regenerable_output_stays(self):
        live = self.base / "J-0002"
        self.put(live, "package.json", b"{}")
        self.put(live, "node_modules/x.js")
        self.age(live, 1)
        out = self.run_pass()
        self.assertTrue((live / "node_modules" / "x.js").exists())
        self.assertFalse(any("J-0002" in d["path"] for d in out["regenerable"]["deleted"]))

    def test_node_modules_without_manifest_or_tracked_stays(self):
        self.put(self.proj, "lib/node_modules/y.js")  # no package.json beside it: a bundled runtime
        self.age(self.base, 1)
        out = self.run_pass()
        self.assertFalse(any("lib/node_modules" in d["path"] for d in out["regenerable"]["deleted"]))
        self.assertIn("no-manifest-beside", [k["why"] for k in out["regenerable"]["kept"]])

    def test_cargo_target_debug_goes_release_bundle_stays(self):
        t = self.proj / "rs" / "target"
        self.put(t, ".rustc_info.json", b"{}")
        self.put(t, "debug/.fingerprint/a", b"x")
        self.put(t, "debug/big.rlib", b"d" * 5000)
        self.put(t, "release/deps/lib.rlib", b"r" * 5000)
        self.put(t, "release/app", b"binary")
        self.age(self.base, 1)
        out = self.run_pass()
        mirror = self.root / "home" / "jobs" / "J-0001" / "rs" / "target"
        self.assertFalse((mirror / "debug").exists(), out)
        self.assertFalse((mirror / "release" / "deps").exists())
        self.assertTrue((mirror / "release" / "app").exists())

    def test_read_by_the_reaper_does_not_hold_a_finished_job(self):
        with mock.patch.object(offload, "reads_recorded", return_value=None):
            os.utime(self.proj / "src" / "a.txt", (time.time() - 5, time.time() - DAY))  # read seconds ago, written a day ago
            out = self.run_pass()
        self.assertTrue(self.proj.is_symlink(), out["kept"])

    def test_read_still_holds_a_non_job_folder(self):
        plain = self.home / "cache" / "old"
        self.put(plain, "a.bin")
        self.age(plain.parent, 1)
        self.write_config({"offload": {"root": str(self.root), "minFreeGB": 0, "quietMinutes": 60,
                                       "sources": [{"path": "~/cache", "children": True}]}})
        with mock.patch.object(offload, "reads_recorded", return_value=None):
            os.utime(plain / "a.bin", (time.time() - 5, time.time() - DAY))
            out = self.run_pass()
        self.assertIn("read-within-60min", [k["why"] for k in out["kept"]])

    def test_finished_job_with_gone_gitdir_moves_and_its_output_is_not_deleted(self):
        wt = self.proj / "wt"
        self.put(wt, "package.json", b"{}")
        self.put(wt, "node_modules/x.js")
        (wt / ".git").write_text(f"gitdir: {self.tmp}/gone/worktrees/x\n")
        self.age(self.base, 1)
        out = self.run_pass()
        self.assertTrue(self.proj.is_symlink(), out["kept"])
        self.assertIn("git-dir-gone:cannot-prove-untracked", [k["why"] for k in out["regenerable"]["kept"]])
        self.assertTrue((self.root / "home" / "jobs" / "J-0001" / "wt" / "node_modules" / "x.js").exists())

    def test_gone_gitdir_is_not_moved_for_an_unfinished_job(self):
        wt = self.proj / "wt"
        self.put(wt, "f.txt")
        (wt / ".git").write_text(f"gitdir: {self.tmp}/gone/worktrees/x\n")
        self.age(self.base, 1)
        self.assertTrue(offload.git_in_use(str(wt)).startswith("git-dirty:unproven:"))

    def test_tracked_output_in_a_repository_above_the_candidate_is_kept(self):
        repo = self.clone("mono", ignored=())
        pkg = repo / "packages" / "web"
        self.put(pkg, "package.json", b"{}")
        self.put(pkg, "node_modules/x.js")
        self.git(repo, "add", "-A"); self.git(repo, "commit", "-q", "-m", "tracked deps"); self.git(repo, "push", "-q")
        self.write_config({"offload": {"root": str(self.root), "minFreeGB": 0, "quietMinutes": 60,
                                       "sources": [{"path": "~/dev/mono/packages", "children": True}]}})
        self.age(repo, 1)
        out = self.run_pass()
        self.assertEqual(out["regenerable"]["deleted"], [], out["regenerable"])
        self.assertTrue(out["regenerable"]["kept"], out["regenerable"])
        moved = self.root / "home" / "dev" / "mono" / "packages" / "web" / "node_modules" / "x.js"
        self.assertTrue(moved.exists() or (pkg / "node_modules" / "x.js").exists())

    def test_kept_folder_loses_no_output(self):
        (self.proj / "node_modules").touch()  # no-op
        live = self.base / "J-0002"
        self.put(live, "package.json", b"{}")
        self.put(live, "node_modules/x.js")
        self.put(self.proj, "src/new.txt")  # a change minutes ago: J-0001 is finished but not settled
        out = self.run_pass()
        self.assertTrue((live / "node_modules" / "x.js").exists())
        self.assertEqual(out["regenerable"]["deleted"], [])
        self.assertTrue((self.proj / "node_modules").exists())

    def test_regen_in_one_kept_folder_child_does_not_block_its_sibling_move(self):
        base = self.home / "kept"
        for inner in ("wt", "notes"):
            self.put(base / "J-0001", f"{inner}/package.json", b"{}")
            self.put(base / "J-0001", f"{inner}/node_modules/x.js")
        self.put(base / "J-0001", "coordination.jsonl", b"log")
        self.age(base, 1)
        self.write_config({"offload": {"root": str(self.root), "minFreeGB": 0, "quietMinutes": 60,
                                       "jobStatusCommand": [sys.executable, str(self.script)],
                                       "sources": [{"path": "~/kept", "children": True, "jobs": True, "keepFolder": True}]}})
        out = self.run_pass()
        self.assertEqual(out["errors"], [], out["errors"])
        self.assertTrue((base / "J-0001" / "wt").is_symlink() and (base / "J-0001" / "notes").is_symlink())

    def test_goal_met_folder_loses_no_output(self):
        with mock.patch.object(offload, "goal_met", return_value=True):
            out = offload.offload_pass()
        self.assertEqual(out["regenerable"]["deleted"], [])
        self.assertTrue((self.proj / "node_modules" / "x" / "index.js").exists())

    def test_release_workspace_loses_no_output(self):
        rel = self.home / "scratch" / "rel-0001"
        self.put(rel, "wt/package.json", b"{}")
        self.put(rel, "wt/.open-next/worker.js")
        self.put(rel, "out/app.dmg")
        self.age(self.home / "scratch", 1)
        self.write_config({"offload": {"root": str(self.root), "minFreeGB": 0, "quietMinutes": 60,
                                       "sources": [{"path": "~/scratch", "children": True}]}})
        out = self.run_pass()
        self.assertEqual(out["regenerable"]["deleted"], [])
        self.assertTrue((rel / "wt" / ".open-next" / "worker.js").exists())
        self.assertTrue(any(k["why"].startswith("release-artifacts") for k in out["kept"]), out["kept"])

    def test_git_error_names_its_cause_through_git_in_use(self):
        wt = self.proj / "wt2"
        self.put(wt, "f.txt")
        (wt / ".git").mkdir()
        with mock.patch.object(core, "reap_git_metadata_gate", side_effect=PermissionError(13, "x")):
            self.assertIn("metadata-unavailable:PermissionError:EACCES", offload.git_in_use(str(wt)))

    def test_metadata_error_names_its_cause(self):
        with mock.patch.object(core, "reap_git_metadata_gate", side_effect=PermissionError(13, "x")):
            with self.assertRaisesRegex(core.ReapRefused, "metadata-unavailable:PermissionError:EACCES"):
                core.reap_git_gate(str(self.tmp))


class OverflowRetention(Home):
    def setUp(self):
        super().setUp()
        self.overflow = self.tmp / "Volumes" / "Drive" / "Overflow"
        (self.overflow / "home").mkdir(parents=True)
        self.write_config({"offload": {"root": str(self.overflow), "sources": []}, "policy": {"archiveExpiryDays": 30}})
        self.statuses = {"J-0101": {"status": "done", "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - 40 * DAY))},
                         "J-0102": {"status": "done", "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - 3 * DAY))}}
        import json
        script = self.tmp / "coord.py"
        script.write_text("import json,sys\njobs=%r\nj=sys.argv[-1]\nprint(json.dumps({'ok':True,'job':{'id':j,**jobs[j]}}))\n" % self.statuses)
        self.write_config({"offload": {"root": str(self.overflow), "sources": []},
                           "policy": {"archiveExpiryDays": 30, "coordinatorRoots": ["~/.cache/ut-coordinator"],
                                      "coordinatorJobCommand": [sys.executable, str(script)]}})
        for patch in (mock.patch.object(core, "reap_overflow_home", lambda: os.path.realpath(self.overflow / "home")),
                      mock.patch.object(clean, "reap_overflow_home", lambda: os.path.realpath(self.overflow / "home")),
                      mock.patch.object(core, "REAP_SYSTEM_ROOTS", core.REAP_SYSTEM_ROOTS + (str(self.tmp / "Volumes"),)),
                      mock.patch.object(clean, "clean_running", return_value=set())):
            patch.start()
            self.addCleanup(patch.stop)
        self.mirror = self.overflow / "home" / ".cache" / "ut-coordinator"
        for job in ("J-0101", "J-0102"):
            self.put(self.mirror / job, "wt/package.json", b"{}")
            self.put(self.mirror / job, "wt/node_modules/x.js", b"n" * 8192)
            self.put(self.mirror / job, "notes/n.md", b"notes")
        self.age(self.mirror, 3)
        self.age(self.mirror / "J-0101", 40)

    def clean(self, dry=False):
        args = ["clean", "--no-report", "--no-escalate", "--only", "overflow-retention"] + (["--dry-run"] if dry else [])
        return self.cli(*args)

    def test_regenerable_output_on_the_overflow_is_swept(self):
        self.clean()
        self.assertFalse((self.mirror / "J-0102" / "wt" / "node_modules").exists())
        self.assertTrue((self.mirror / "J-0102" / "notes" / "n.md").exists())

    def test_job_folder_past_archive_expiry_is_deleted_and_recent_one_kept(self):
        self.clean()
        self.assertFalse((self.mirror / "J-0101").exists())
        self.assertTrue((self.mirror / "J-0102").exists())

    def test_retention_marker_keeps_an_expired_job_folder(self):
        self.put(self.mirror / "J-0101", "notes/.ut-retention-customer", b"")
        self.clean()
        self.assertTrue((self.mirror / "J-0101" / "notes" / "n.md").exists())

    def test_live_job_folder_on_the_overflow_is_not_swept(self):
        self.statuses["J-0102"]["status"] = "running"
        import json
        script = self.tmp / "coord.py"
        script.write_text("import json,sys\njobs=%r\nj=sys.argv[-1]\nprint(json.dumps({'ok':True,'job':{'id':j,**jobs[j]}}))\n" % self.statuses)
        self.clean()
        self.assertTrue((self.mirror / "J-0102" / "wt" / "node_modules" / "x.js").exists())

    def test_a_repository_left_in_home_above_a_moved_folder_keeps_its_output(self):
        repo = self.home / "dev" / "mono"
        (repo / ".git").mkdir(parents=True)
        moved = self.overflow / "home" / "dev" / "mono" / "packages" / "web"
        self.put(moved, "package.json", b"{}")
        self.put(moved, "node_modules/x.js")
        self.age(moved.parent.parent, 3)
        errs = []
        gates = clean.clean_overflow_gates
        with self.assertRaisesRegex(core.ReapRefused, "repository-in-home-above"):
            gates({"path": os.path.realpath(moved / "node_modules"), "regen": "node_modules", "jobs": [], "command": []}, None)

    def test_a_retention_marker_above_the_home_path_keeps_the_moved_copy(self):
        home_dir = self.home / "dev" / "keepme"
        home_dir.mkdir(parents=True)
        (home_dir / ".ut-retention-customer").write_bytes(b"")
        moved = self.overflow / "home" / "dev" / "keepme" / "web"
        self.put(moved, "package.json", b"{}")
        self.put(moved, "node_modules/x.js")
        with self.assertRaisesRegex(core.ReapRefused, "retention:customer"):
            clean.clean_overflow_gates({"path": os.path.realpath(moved / "node_modules"), "regen": "node_modules", "jobs": [], "command": []}, None)

    def test_a_marker_at_a_keepfolder_home_job_root_keeps_the_expired_moved_children(self):
        home_job = self.home / ".cache" / "ut-coordinator" / "J-0101"
        home_job.mkdir(parents=True)
        (home_job / ".ut-retention-customer").write_bytes(b"")
        os.symlink(str(self.mirror / "J-0101" / "wt"), str(home_job / "wt"))
        self.clean()
        self.assertTrue((self.mirror / "J-0101" / "wt" / "package.json").exists())
        self.assertTrue(os.path.islink(home_job / "wt"))

    def test_an_expired_job_folder_holding_a_release_artifact_is_kept(self):
        self.put(self.mirror / "J-0101", "out/App-1.0.dmg", b"d" * 8192)
        self.age(self.mirror / "J-0101", 40)
        self.clean()
        self.assertTrue((self.mirror / "J-0101" / "out" / "App-1.0.dmg").exists())

    def test_keepfolder_links_in_home_go_with_the_expired_job(self):
        home_job = self.home / ".cache" / "ut-coordinator" / "J-0101"
        home_job.mkdir(parents=True)
        os.symlink(str(self.mirror / "J-0101" / "wt"), str(home_job / "wt"))
        self.clean()
        self.assertFalse((self.mirror / "J-0101").exists())
        self.assertFalse(os.path.lexists(home_job / "wt"))

    def test_regenerable_output_inside_a_shipped_bundle_on_the_overflow_is_not_swept(self):
        app = self.mirror / "J-0102" / "wt" / "App.app" / "Contents" / "Resources"
        self.put(app, "node/lib/package.json", b"{}")   # every other gate would pass: only the bundle keeps it
        self.put(app, "node/lib/node_modules/x/index.js", b"n" * 8192)
        self.put(app, "ultrabot/__pycache__/a.pyc", b"p" * 100)
        self.put(self.mirror / "J-0102" / "wt" / "lib" / "App.framework" / "Versions" / "A", "package.json", b"{}")
        self.put(self.mirror / "J-0102" / "wt" / "lib" / "App.framework" / "Versions" / "A", "node_modules/x.js", b"n" * 8192)
        self.age(self.mirror / "J-0102", 3)   # quiet: every other gate would release it
        self.clean()
        self.assertTrue((app / "node" / "lib" / "node_modules" / "x" / "index.js").exists())
        self.assertTrue((app / "ultrabot" / "__pycache__" / "a.pyc").exists())
        self.assertTrue((self.mirror / "J-0102" / "wt" / "lib" / "App.framework" / "Versions" / "A" / "node_modules" / "x.js").exists())
        self.assertFalse((self.mirror / "J-0102" / "wt" / "node_modules").exists())

    def test_the_gates_refuse_a_shipped_bundle_or_a_vendored_tree_by_name(self):
        app = self.mirror / "J-0102" / "wt" / "App.app" / "Contents" / "Resources" / "node_modules"
        self.put(app, "x/index.js")
        bundled = self.mirror / "J-0102" / "wt" / "third_party" / "node_modules"
        self.put(bundled, "x/index.js")
        for path in (app, bundled):
            with self.assertRaisesRegex(core.ReapRefused, "vendored-path"):
                clean.clean_overflow_gates({"path": os.path.realpath(path), "regen": "node_modules", "jobs": [], "command": []}, None)

    def test_regenerable_output_below_a_home_link_is_proven_not_held(self):
        home_job = self.home / ".cache" / "ut-coordinator" / "J-0102"
        home_job.mkdir(parents=True)
        os.symlink(str(self.mirror / "J-0102" / "wt"), str(home_job / "wt"))
        nested = self.mirror / "J-0102" / "wt" / "packages" / "web"
        self.put(nested, "package.json", b"{}")
        self.put(nested, "node_modules/x.js", b"n" * 8192)
        self.age(self.mirror / "J-0102" / "wt", 3)
        self.clean()
        self.assertFalse((nested / "node_modules").exists())
        self.assertTrue((nested / "package.json").exists())

    def test_a_marker_above_a_home_link_still_keeps_regenerable_output_below_it(self):
        home_job = self.home / ".cache" / "ut-coordinator" / "J-0102"
        home_job.mkdir(parents=True)
        (home_job / ".ut-retention-customer").write_bytes(b"")
        os.symlink(str(self.mirror / "J-0102" / "wt"), str(home_job / "wt"))
        nested = self.mirror / "J-0102" / "wt" / "packages" / "web"
        self.put(nested, "package.json", b"{}")
        self.put(nested, "node_modules/x.js", b"n" * 8192)
        self.age(self.mirror / "J-0102" / "wt", 3)
        self.clean()
        self.assertTrue((nested / "node_modules" / "x.js").exists())

    def test_rule_is_part_of_the_steady_pass(self):
        self.assertIn("overflow-retention", clean.CLEAN_STEADY_NAMES)
        self.assertIn("overflow-retention", clean.CLEAN_NAMES)
