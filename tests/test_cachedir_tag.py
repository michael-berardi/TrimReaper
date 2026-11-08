"""The `tagged-caches` clean target: any folder holding a valid CACHEDIR.TAG goes once it is idle, unheld and safe.

Throwaway homes only; the process table and the running-program list are fixture snapshots unless a test says otherwise."""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from unittest import mock

from helpers import DAY, Home

from trimreaper import clean, core

TAG = b"Signature: 8a477f597d28d172789f06886806bc55\n# This file is a cache directory tag.\n"
HOUR = DAY / 24
EMPTY = {"files": [], "commands": []}


class Tagged(Home):
    def setUp(self):
        super().setUp()
        self.sharedtmp = self.tmp / "tmp-shared"
        self.usertmp = self.tmp / "tmp-user"
        self.sharedtmp.mkdir()
        self.usertmp.mkdir()
        self.running = set()
        self.table = dict(EMPTY)
        for patch in (mock.patch.object(clean, "clean_running", lambda: set(self.running)),
                      mock.patch.object(clean, "reap_process_table", lambda: self.table),
                      mock.patch.object(clean, "clean_temp_roots", lambda: [(os.path.realpath("/tmp"), 24), (str(self.usertmp), 72)])):
            patch.start()
            self.addCleanup(patch.stop)
        self.jobs = self.home / ".cache" / "trimreaper" / "jobs"

    def cache(self, folder: Path, hours: float = 10, tag: bytes = TAG, files=("debug/app", "debug/deps/lib.rlib", "out/app")) -> Path:
        """A tagged folder with some content, every entry `hours` old."""
        for rel in files:
            self.put(folder, rel)
        (folder / "CACHEDIR.TAG").write_bytes(tag)
        self.age(folder, hours / 24)
        return folder

    def target(self, name="target", job="J-0042", hours=10) -> Path:
        return self.cache(self.jobs / job / "repo" / "src-tauri" / name, hours)

    def run_clean(self, dry=False, policy=None):
        if policy:
            self.write_config({"policy": policy})
        return self.cli("clean", *(["--dry-run"] if dry else []), "--only", "tagged-caches")

    def item(self, out, folder: Path) -> dict:
        found = [i for i in out["items"] if i["path"].endswith(str(folder).split(str(self.home))[1])] if out["items"] else []
        self.assertEqual(len(found), 1, out)
        return found[0]

    def kept(self, folder: Path, reason: str, **kwargs):
        out = self.run_clean(**kwargs)
        self.assertTrue(folder.exists(), out)
        self.assertEqual(out["summary"]["done"], 0, out)
        item = self.item(out, folder)
        self.assertTrue(item["keep"].startswith(reason), item)
        return out

    # ---- discovery ------------------------------------------------------------------------------------------------

    def test_idle_tagged_folders_are_removed_wherever_they_sit(self):
        """Build output nested in job folders, a custom-named target, and tagged folders in every searched root."""
        self.add_job("J-0042", status="blocked")
        nested = [self.target("target"), self.cache(self.jobs / "J-0042" / "integration" / "src-tauri" / "target"),
                  self.cache(self.jobs / "J-0042" / "wt-main" / "src-tauri" / "target"), self.cache(self.jobs / "J-0042" / "rust-test-target")]
        elsewhere = [self.cache(self.dev / "app" / "build-cache"), self.cache(self.usertmp / "scratch" / "out")]
        planned = self.run_clean(dry=True)
        self.assertEqual(planned["summary"]["candidates"], 6, planned)
        self.assertTrue(all(p.exists() for p in nested + elsewhere))
        out = self.run_clean()
        self.assertEqual(out["summary"]["done"], 6, out)
        for folder in nested + elsewhere:
            self.assertFalse(folder.exists(), folder)
        self.assertTrue((self.jobs / "J-0042").is_dir())
        self.assertTrue(self.cli("clean", "--dry-run", "--only", "tagged-caches")["summary"]["candidates"] == 0)

    def test_agent_scratch_and_worktree_homes_are_searched(self):
        scratch, trees = self.tmp / "scratch", self.tmp / "trees"
        self.write_config({"scratchRoot": str(scratch), "worktreeHomes": [str(trees)]})
        found = [self.cache(scratch / "task" / "out"), self.cache(trees / "w1" / "x" / "target")]
        out = self.run_clean()
        self.assertEqual(out["summary"]["done"], 2, out)
        self.assertFalse(any(p.exists() for p in found))

    def test_discovery_stops_at_a_tagged_folder_and_never_enters_git_or_dependency_stores(self):
        outer = self.cache(self.dev / "app" / "outer")
        self.cache(outer / "inner")
        self.cache(self.dev / "app" / "node_modules" / "pkg" / "cache")
        self.cache(self.dev / "app" / ".git" / "objects" / "cache")
        found = clean.clean_tagged_find([])
        self.assertEqual([path for path, _ in found], [str(outer)])

    def test_a_tag_that_is_not_valid_is_not_a_cache(self):
        wrong = self.cache(self.dev / "app" / "wrong", tag=b"Signature: 00000000000000000000000000000000\n")
        short = self.cache(self.dev / "app" / "short", tag=b"Signature: 8a477f59")
        linked = self.cache(self.dev / "app" / "linked", tag=b"x")
        (linked / "CACHEDIR.TAG").unlink()
        real = self.tmp / "real-tag"
        real.write_bytes(TAG)
        (linked / "CACHEDIR.TAG").symlink_to(real)
        multi = self.cache(self.dev / "app" / "multi")
        os.link(multi / "CACHEDIR.TAG", self.tmp / "second-link")
        self.assertEqual(clean.clean_tagged_find([]), [])
        out = self.run_clean()
        self.assertEqual(out["summary"]["done"], 0, out)
        for folder in (wrong, short, linked, multi):
            self.assertTrue(folder.exists())

    def test_a_symlinked_folder_is_neither_followed_nor_removed(self):
        real = self.cache(self.tmp / "elsewhere" / "cache")
        (self.dev / "app").mkdir()
        (self.dev / "app" / "link").symlink_to(real)
        self.assertEqual(clean.clean_tagged_find([]), [])
        self.run_clean()
        self.assertTrue(real.exists())

    def test_protected_folders_are_never_entered_during_discovery(self):
        self.cache(self.dev / "app" / ".wrangler" / "state" / "cache")
        self.cache(self.dev / "app" / ".vercel" / "cache")
        self.cache(self.dev / "app" / ".env.d" / "cache")
        self.assertEqual(clean.clean_tagged_find([]), [])

    def test_the_homes_dot_cache_is_not_searched_except_the_job_and_scratch_roots(self):
        tool = self.cache(self.home / ".cache" / "sometool" / "store")
        self.assertEqual(clean.clean_tagged_find([]), [])
        job = self.cache(self.jobs / "J-0007" / "x")
        self.assertEqual([p for p, _ in clean.clean_tagged_find([])], [str(job)])
        self.assertTrue(tool.exists())

    def test_tool_cache_folders_directly_in_a_code_home_are_left_to_their_cleaners(self):
        self.cache(self.dev / "uv")
        mine = self.cache(self.dev / "proj" / "uv")
        self.assertEqual([p for p, _ in clean.clean_tagged_find([])], [str(mine)])

    def test_the_shared_temp_folder_is_not_searched(self):
        shared = clean.os.path.realpath("/tmp")
        self.assertNotIn(shared, clean.clean_tagged_roots([]))
        self.assertIn(str(self.usertmp), clean.clean_tagged_roots([]))

    # ---- the gates ------------------------------------------------------------------------------------------------

    def test_recent_change_keeps_it(self):
        folder = self.target(hours=5)
        self.kept(folder, "recent:less-than-6h")
        folder = self.target("rust-test-target", hours=8)
        out = self.run_clean()
        self.assertFalse(folder.exists(), out)

    def test_a_change_deep_inside_counts_even_with_a_preserved_modification_time(self):
        folder = self.target()
        fresh = self.put(folder, "debug/deps/new.rlib")
        old = time.time() - 20 * HOUR
        os.utime(fresh, (old, old))   # mtime is old; ctime is now
        os.utime(fresh.parent, (old, old))
        self.kept(folder, "recent:less-than-6h")

    def test_the_window_is_the_policy_and_zero_turns_the_target_off(self):
        folder = self.target(hours=3)
        self.kept(folder, "recent", policy={"taggedCacheIdleHours": 6}) if False else None
        policy = dict(clean.CLEAN_POLICY)
        self.assertEqual(policy["taggedCacheIdleHours"], 6)
        plan = clean.clean_plan({"tagged-caches"}, False, [], [], 7, 48, clean.clean_profile({**policy, "taggedCacheIdleHours": 2}, 0))
        self.assertNotIn("keep", plan[0])
        plan = clean.clean_plan({"tagged-caches"}, False, [], [], 7, 48, clean.clean_profile(policy, 0))
        self.assertTrue(plan[0]["keep"].startswith("recent"))
        off = clean.clean_plan({"tagged-caches"}, False, [], [], 7, 48, clean.clean_profile({**policy, "taggedCacheIdleHours": 0}, 0))
        self.assertEqual(off, [])
        self.assertTrue(folder.exists())

    def test_ladder_rungs_accept_two_quiet_hours_and_never_more_than_the_policy(self):
        policy = dict(clean.CLEAN_POLICY)
        self.assertEqual(clean.clean_profile(policy, 0)["taggedHours"], 6)
        self.assertEqual(clean.clean_profile(policy, "steady")["taggedHours"], 6)
        for rung in range(1, 6):
            self.assertEqual(clean.clean_profile(policy, rung)["taggedHours"], 2)
            self.assertEqual(clean.clean_profile({**policy, "taggedCacheIdleHours": 1}, rung)["taggedHours"], 1)
            self.assertEqual(clean.clean_profile({**policy, "taggedCacheIdleHours": 0}, rung)["taggedHours"], 0)
        folder = self.target(hours=3)
        plan = clean.clean_plan({"tagged-caches"}, False, [], [], 7, 48, clean.clean_profile(policy, 1))
        self.assertNotIn("keep", plan[0], plan)
        self.assertTrue(folder.exists())

    def test_a_bad_window_is_named(self):
        for bad in (-1, "6", True, "x"):
            with self.subTest(bad=bad), self.assertRaisesRegex(clean.HostError, "taggedCacheIdleHours"):
                clean.clean_profile({**clean.CLEAN_POLICY, "taggedCacheIdleHours": bad}, 0)

    def test_it_is_part_of_the_steady_pass(self):
        self.assertIn("tagged-caches", clean.CLEAN_NAMES)
        self.assertIn("tagged-caches", clean.CLEAN_EXTRA_NAMES)
        self.assertIn("tagged-caches", clean.CLEAN_STEADY_NAMES)
        folder = self.target()
        out = clean.clean_pass(False, only=["tagged-caches"], steady=True, report=False)
        self.assertEqual(out["summary"]["done"], 1, out)
        self.assertFalse(folder.exists())

    def test_an_open_handle_or_working_directory_keeps_it(self):
        folder = self.target()
        self.table = {"files": [("4242", "tool", "7", str(folder / "debug" / "deps" / "lib.rlib"))], "commands": []}
        self.kept(folder, "in-use:open-handle:4242")
        self.table = {"files": [("4243", "shell", "cwd", str(folder / "debug"))], "commands": []}
        self.kept(folder, "in-use:cwd:4243")
        self.table = {"files": [], "commands": [("4244", f"tool --out {folder}/debug")]}
        self.kept(folder, "in-use:command-line:4244")
        self.table = dict(EMPTY)
        self.assertEqual(self.run_clean()["summary"]["done"], 1)

    def test_a_process_with_its_cwd_inside_keeps_it_under_the_real_probe(self):
        folder = self.target()
        process = subprocess.Popen(["sleep", "60"], cwd=folder / "debug")
        self.addCleanup(process.kill)
        self.addCleanup(process.wait)
        with mock.patch.object(clean, "reap_process_table", core.reap_process_table):
            out = self.run_clean()
        self.assertTrue(folder.exists(), out)
        self.assertEqual(out["summary"]["done"], 0, out)
        self.assertTrue(self.item(out, folder)["keep"].startswith("in-use:cwd:"), out)

    def test_the_holder_check_runs_again_right_before_the_removal(self):
        folder = self.target()
        seen = []
        original = clean.reap_holders
        def late(path, root, table=None):
            seen.append(table is None)
            if table is None and path == str(folder):
                raise clean.ReapRefused("in-use:cwd:1(late)")
            return original(path, root, table)
        with mock.patch.object(clean, "reap_holders", late):
            out = self.run_clean()
        self.assertIn(True, seen)
        self.assertTrue(folder.exists(), out)
        self.assertEqual(out["summary"]["done"], 0, out)

    def test_any_rust_producer_keeps_it(self):
        folder = self.target()
        for name in clean.CLEAN_RUST_BUSY:
            self.running = {name}
            self.kept(folder, "in-use:" + name)
        self.running = set()
        self.assertEqual(self.run_clean()["summary"]["done"], 1)

    def test_a_release_artifact_keeps_it(self):
        for rel in ("release/bundle/dmg/App.dmg", "release/bundle/macos/App.app/Contents/binary", "release/App.pkg",
                    "aarch64-apple-darwin/release/bundle/macos/App.app/Contents/MacOS/app"):
            with self.subTest(rel=rel):
                folder = self.cache(self.jobs / "J-0050" / "repo" / "target", files=(rel,))
                out = self.kept(folder, "release-artifact:")
                shutil.rmtree(folder)

    def test_a_tree_the_release_scan_cannot_finish_is_kept(self):
        folder = self.target()
        with mock.patch.object(clean, "REAP_WALK_ENTRIES", 5), mock.patch.object(clean, "clean_tree_walk", lambda *a, **k: ("idle", 0.0, None)):
            out = self.run_clean()
        self.assertTrue(folder.exists(), out)
        self.assertTrue(self.item(out, folder)["keep"].startswith("release-artifact:scan-incomplete"), out)

    def test_a_protected_leaf_inside_keeps_it_whole(self):
        for rel in (".env", ".env.local", ".dev.vars", ".wrangler/state/x", ".vercel/project.json", "debug/.env.production"):
            with self.subTest(rel=rel):
                folder = self.cache(self.jobs / "J-0051" / "repo" / "target", files=("debug/app", rel))
                self.kept(folder, "protected-leaf")
                self.assertTrue((folder / rel).exists())
                shutil.rmtree(folder)

    def test_a_protected_name_beside_it_is_never_entered_or_removed(self):
        folder = self.target()
        env = self.put(self.jobs / "J-0042" / "repo" / "src-tauri", ".env", b"SECRET=1\n")
        self.age(env.parent, 1)
        out = self.run_clean()
        self.assertFalse(folder.exists(), out)
        self.assertEqual(env.read_bytes(), b"SECRET=1\n")

    def test_a_protected_ancestor_keeps_it(self):
        folder = self.cache(self.dev / "app" / ".wrangler" / "cache")
        self.assertEqual(clean.clean_tagged_find([]), [])
        with self.assertRaisesRegex(clean.ReapRefused, "protected-ancestor|outside-root"):
            clean.clean_tagged_gates({"path": str(folder), "root": str(self.dev), "idleHours": 6}, None)
        self.assertTrue(folder.exists())

    def test_a_nested_repository_keeps_it(self):
        folder = self.target()
        self.put(folder, "checkout/.git/HEAD", b"ref: refs/heads/main\n")
        self.age(folder, 1)
        self.kept(folder, "nested-git")

    def test_a_retention_marker_other_than_disposable_keeps_it(self):
        folder = self.target()
        self.put(self.jobs / "J-0042", ".ut-retention-evidence", b"")
        self.age(self.jobs / "J-0042", 1)
        self.kept(folder, "retention:evidence")

    def test_the_release_root_is_never_touched(self):
        self.write_config({"releaseRoot": str(self.dev / "releases")})
        folder = self.cache(self.dev / "releases" / "1.2.0" / "target")
        self.kept(folder, "release-root")

    def test_a_repository_that_tracks_files_in_it_keeps_it(self):
        repo = self.clone("tracked-cache", ignored=())
        folder = self.cache(repo / "kept-cache", files=("data/x",))
        self.git(repo, "add", "-f", "kept-cache")
        self.git(repo, "commit", "-qm", "tracked cache")
        self.git(repo, "push", "-q")
        self.age(folder, 1)
        self.kept(folder, "tracked-files")
        untracked = self.cache(repo / "other-cache")
        out = self.run_clean()
        self.assertFalse(untracked.exists(), out)
        self.assertTrue(folder.exists())

    def test_a_busy_cargo_lock_keeps_it(self):
        folder = self.cache(self.jobs / "J-0042" / "repo" / "target", files=("debug/app", "debug/.cargo-lock"))
        with clean.reap_locks([folder / "debug" / ".cargo-lock"], create=False):
            out = self.run_clean()
        self.assertTrue(folder.exists(), out)
        self.assertTrue(self.item(out, folder)["keep"].startswith("lock-busy:"), out)

    def test_locks_are_held_while_it_is_removed(self):
        folder = self.cache(self.jobs / "J-0042" / "repo" / "target", files=("debug/app", "debug/.cargo-lock"))
        lock, checked, remove = folder / "debug" / ".cargo-lock", [], clean.reap_remove_and_verify
        def verify(*args, **kwargs):
            with self.assertRaises(clean.ReapRefused):
                with clean.reap_locks([lock], create=False):
                    pass
            checked.append(True)
            return remove(*args, **kwargs)
        with mock.patch.object(clean, "reap_remove_and_verify", verify):
            out = self.run_clean()
        self.assertEqual(checked, [True], out)
        self.assertFalse(folder.exists())

    def test_removal_is_receipted_before_it_happens(self):
        folder = self.target()
        out = self.run_clean()
        self.assertEqual(out["summary"]["done"], 1, out)
        lines = [l for f in (self.home / ".local/state/trimreaper/reaper").glob("*.jsonl") for l in f.read_text().splitlines() if str(folder) in l]
        phases = [json.loads(l)["phase"] for l in lines]
        self.assertEqual(phases[:1], ["plan"], phases)
        self.assertIn("result", phases)

    # ---- jobs -----------------------------------------------------------------------------------------------------

    def test_a_running_or_starting_job_keeps_what_is_inside_its_folder(self):
        folder = self.target()
        self.add_job("J-0042", status="running")
        self.kept(folder, "in-use-by-running-job:J-0042")
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET status='starting' WHERE id='J-0042'")
        self.kept(folder, "in-use-by-running-job:J-0042")
        self.cli("job", "status", "J-0042", "parked")
        self.assertEqual(self.run_clean()["summary"]["done"], 1)

    def test_a_job_that_declares_the_folder_keeps_it_while_it_runs(self):
        folder = self.cache(self.dev / "app" / "build-cache")
        self.add_job("J-0044", cwd=self.dev / "app", status="running")
        self.kept(folder, "in-use-by-running-job:J-0044")

    def test_a_collection_root_cwd_names_no_project(self):
        folder = self.cache(self.dev / "app" / "build-cache")
        self.add_job("J-0045", cwd=self.dev, status="running")
        self.assertEqual(self.run_clean()["summary"]["done"], 1)
        self.assertFalse(folder.exists())

    def test_blocked_parked_interrupted_queued_and_finished_jobs_do_not_keep_idle_caches(self):
        for number, status in enumerate(("blocked", "parked", "interrupted", "done", "failed", "cancelled", "queued"), 60):
            with self.subTest(status=status):
                job = f"J-{number:04d}"
                self.add_job(job, status=status)
                folder = self.cache(self.jobs / job / "repo" / "src-tauri" / "target")
                out = self.run_clean()
                self.assertFalse(folder.exists(), out)

    def test_unreadable_job_state_keeps_it(self):
        folder = self.target()
        with mock.patch.object(clean, "db", side_effect=clean.HostError("no store")):
            out = self.run_clean()
        self.assertTrue(folder.exists(), out)
        self.assertEqual(self.item(out, folder)["keep"], "job-state-unreadable")

    # ---- bounds ---------------------------------------------------------------------------------------------------

    def test_the_discovery_entry_cap_is_honoured_and_reported(self):
        for n in range(30):
            self.put(self.dev / "app" / f"d{n}", "f")
        self.cache(self.dev / "zzz" / "cache")
        errors = []
        with mock.patch.object(clean, "CLEAN_RUST_SCAN_ENTRIES", 20):
            clean.clean_tagged_find(errors)
        self.assertTrue(any("discovery-incomplete" in e["error"] and e["clean"] == "tagged-caches" for e in errors), errors)

    def test_an_incomplete_discovery_is_in_the_pass_result(self):
        for n in range(30):
            self.put(self.dev / "app" / f"d{n}", "f")
        with mock.patch.object(clean, "CLEAN_RUST_SCAN_ENTRIES", 20):
            out = self.cli("clean", "--dry-run", "--only", "tagged-caches", expect=3)
        self.assertTrue(any("discovery-incomplete" in str(e) for e in out["errors"]), out["errors"])

    def test_the_depth_cap_is_honoured(self):
        deep = self.dev
        for n in range(clean.CLEAN_TAGGED_DEPTH + 2):
            deep = deep / f"l{n}"
        self.cache(deep / "cache")
        self.assertEqual(clean.clean_tagged_find([]), [])

    def test_a_walk_that_runs_out_of_entries_keeps_it_and_says_why(self):
        folder = self.target()
        with mock.patch.object(clean, "REAP_WALK_ENTRIES", 3):
            with self.assertRaisesRegex(clean.ReapRefused, "idle-scan-incomplete:entries"):
                clean.clean_stale_tree(str(folder), 6, fresh=True, strict=True)
        self.assertTrue(folder.exists())

    def test_a_walk_that_runs_out_of_time_keeps_it_and_says_why(self):
        folder = self.cache(self.jobs / "J-0042" / "t", files=tuple(f"d/f{n}" for n in range(600)))
        with mock.patch.object(clean, "CLEAN_TAGGED_WALK_SECONDS", -1):
            with self.assertRaisesRegex(clean.ReapRefused, "idle-scan-incomplete:time"):
                clean.clean_stale_tree(str(folder), 6, fresh=True, strict=True)
        self.assertTrue(folder.exists())

    def test_the_idle_check_is_fresh_at_removal(self):
        folder = self.target()
        original = clean.clean_remove_folder
        def late(entry, run_id, root, allow_git, last_look):
            self.put(folder, "debug/just-now")
            return original(entry, run_id, root, allow_git, last_look)
        with mock.patch.object(clean, "clean_remove_folder", late):
            out = self.run_clean()
        self.assertTrue(folder.exists(), out)
        self.assertTrue((folder / "debug/just-now").exists())

    def test_nothing_outside_the_scan_roots_is_found(self):
        outside = self.cache(self.tmp / "unrelated" / "cache")
        self.assertEqual(clean.clean_tagged_find([]), [])
        self.run_clean()
        self.assertTrue(outside.exists())

    def test_removal_never_leaves_its_scan_root(self):
        folder = self.target()
        with self.assertRaisesRegex(clean.ReapRefused, "outside-root"):
            clean.clean_tagged_gates({"path": str(folder), "root": str(self.dev), "idleHours": 6}, None)

    # ---- Cargo targets, job evidence and unarchived work (Opus review of 0.15.0) -----------------------------------

    CARGO_TAG = b"Signature: 8a477f597d28d172789f06886806bc55\n# This file is a cache directory tag created by cargo.\n"

    def test_a_cargo_target_with_an_extensionless_release_binary_is_kept(self):
        folder = self.cache(self.jobs / "J-0042" / "repo" / "target", files=("debug/app", "release/myapp", "release/.fingerprint/x/lib"),
                            tag=self.CARGO_TAG)
        self.kept(folder, "cargo-target")
        self.assertTrue((folder / "release" / "myapp").exists())
        bare = self.cache(self.dev / "app" / "tool-out", files=("debug/app", "release/myapp"))   # no Cargo marker at all
        self.kept(bare, "release-artifact:release-bearing")
        self.assertTrue((bare / "release" / "myapp").exists())

    def test_a_release_bearing_folder_that_is_not_superseded_is_refused_by_the_engine_proof(self):
        folder = self.cache(self.dev / "app" / "tool-out", files=("debug/app", "out/data"))
        with mock.patch.object(clean, "reap_target_release_bearing", lambda path: True), \
                mock.patch.object(clean, "reap_target_release_superseded", lambda path: False):
            self.kept(folder, "release-artifact:release-bearing")
        with mock.patch.object(clean, "reap_target_release_bearing", lambda path: True), \
                mock.patch.object(clean, "reap_target_release_superseded", lambda path: True):
            self.assertEqual(self.run_clean()["summary"]["done"], 1)

    def test_a_cargo_target_is_never_taken_by_tagged_caches(self):
        layouts = {"rustc-info": ((".rustc_info.json", "debug/app"), TAG),
                   "cargo-tag": (("debug/app",), self.CARGO_TAG),
                   "debug-fingerprint": (("debug/.fingerprint/a/b", "debug/app"), TAG),
                   "release-fingerprint": (("release/.fingerprint/a/b",), TAG),
                   "triple": (("aarch64-apple-darwin/debug/.fingerprint/a/b",), TAG),
                   "custom-profile": (("dist-lto/.fingerprint/a/b", "dist-lto/app"), TAG)}
        for name, (files, tag) in layouts.items():
            with self.subTest(layout=name):
                folder = self.cache(self.jobs / "J-0042" / "repo" / "target", files=files, tag=tag)
                self.kept(folder, "cargo-target")
                shutil.rmtree(folder)

    def test_rust_build_output_still_removes_only_debug_and_incremental(self):
        repo = self.clone("rustproj")
        for rel in ("target/debug/app", "target/debug/.fingerprint/a/b", "target/release/app", "target/release/.fingerprint/a/b",
                    "target/release/incremental/state"):
            self.put(repo, rel)
        (repo / "target" / "CACHEDIR.TAG").write_bytes(self.CARGO_TAG)
        (repo / "target" / ".rustc_info.json").write_text("{}")
        self.age(repo / "target", 1)
        tagged = self.cli("clean", "--only", "tagged-caches")
        self.assertEqual(tagged["summary"]["done"], 0, tagged)
        self.assertTrue((repo / "target" / "release" / "app").exists())
        out = self.cli("clean", "--only", "rust-build-output")
        self.assertGreaterEqual(out["summary"]["done"], 1, out)
        self.assertFalse((repo / "target" / "debug").exists())
        self.assertFalse((repo / "target" / "release" / "incremental").exists())
        self.assertTrue((repo / "target" / "release" / "app").exists())

    def test_a_tagged_job_folder_is_kept(self):
        folder = self.cache(self.jobs / "J-0042")
        out = self.kept(folder, "job-folder")
        self.assertTrue((folder / "debug" / "app").exists(), out)

    def test_a_tagged_top_level_scratch_task_and_the_roots_are_kept(self):
        scratch = self.tmp / "scratch"
        self.write_config({"scratchRoot": str(scratch)})
        task = self.cache(scratch / "task")
        out = self.run_clean()
        self.assertTrue(task.exists(), out)
        self.assertEqual(out["summary"]["done"], 0, out)
        self.assertEqual([i["keep"] for i in out["items"] if i["path"].endswith("scratch/task")], ["scratch-task-folder"], out)
        for path in (scratch, self.jobs):
            with self.assertRaisesRegex(clean.ReapRefused, "collection-root|outside-root"):
                clean.clean_tagged_gates({"path": str(path), "root": str(path), "idleHours": 6}, None)
        with self.assertRaisesRegex(clean.ReapRefused, "collection-root"):
            clean.clean_tagged_identity(str(scratch), str(self.home))

    def test_a_tagged_folder_holding_job_evidence_or_key_material_is_kept(self):
        for name, rel in (("report", "notes/report.md"), ("pem", "keys/signing.pem"), ("archive", "protected-archive/x"),
                          ("receipt", "out/review-receipt.md"), ("handoff", "handoffs/note.txt")):
            with self.subTest(name=name):
                folder = self.cache(self.dev / "app" / f"cache-{name}", files=("debug/app", rel))
                self.kept(folder, "retained-names")
                self.assertTrue((folder / rel).exists())
                shutil.rmtree(folder)

    def test_a_tagged_folder_inside_an_evidence_folder_is_kept(self):
        folder = self.cache(self.dev / "app" / "protected-archive" / "cache")
        with self.assertRaisesRegex(clean.ReapRefused, "evidence-folder"):
            clean.clean_tagged_gates({"path": str(folder), "root": str(self.dev), "idleHours": 6}, None)
        self.assertTrue(folder.exists())

    def test_a_release_artifact_deeper_than_the_depth_cap_keeps_the_folder(self):
        deep = "/".join(f"l{n}" for n in range(clean.CLEAN_TAGGED_RELEASE_LEVELS + 3))
        folder = self.cache(self.dev / "app" / "deep-cache", files=("debug/app", f"{deep}/App.dmg"))
        self.kept(folder, "release-scan-incomplete")
        self.assertTrue((folder / deep / "App.dmg").exists())
        self.assertIsNone(clean.clean_release_like(str(folder), 2))   # the shared default still fails open for dist/build callers
        self.assertEqual(clean.clean_release_like(str(folder), 2, strict=True), "release-scan-incomplete")

    def test_a_release_scan_that_runs_out_of_time_keeps_the_folder(self):
        folder = self.target()
        with mock.patch.object(clean, "CLEAN_RELEASE_SCAN_SECONDS", -1):
            out = self.kept(folder, "release-scan-incomplete")
        self.assertEqual(self.run_clean()["summary"]["done"], 1, out)

    def test_the_last_look_refuses_when_its_release_scan_is_incomplete(self):
        folder = self.target()
        original, calls = clean.clean_release_like, []
        def second_time_incomplete(*args, **kwargs):
            calls.append(args[0])
            return original(*args, **kwargs) if len(calls) == 1 else "release-scan-incomplete"
        with mock.patch.object(clean, "clean_release_like", second_time_incomplete):
            out = self.run_clean()
        self.assertGreaterEqual(len(calls), 2, calls)
        self.assertTrue(folder.exists(), out)
        self.assertEqual(out["summary"]["done"], 0, out)

    def test_system_named_entries_of_the_user_temp_folder_are_skipped(self):
        mine = self.cache(self.usertmp / "my-build")
        system = [self.cache(self.usertmp / name) for name in ("com.apple.cache", ".hidden-cache", "tmux-501", "launchd-1.x")]
        self.assertEqual([p for p, _ in clean.clean_tagged_find([])], [str(mine)])
        for folder in system:
            with self.assertRaisesRegex(clean.ReapRefused, "system-temp-entry"):
                clean.clean_tagged_gates({"path": str(folder), "root": str(self.usertmp), "idleHours": 6}, None)
        self.assertEqual(self.run_clean()["summary"]["done"], 1)
        self.assertTrue(all(p.exists() for p in system))

    def test_code_homes_are_not_swept_in_the_steady_pass(self):
        code = self.cache(self.dev / "app" / "build-cache")
        job = self.cache(self.jobs / "J-0042" / "repo" / "out")
        temp = self.cache(self.usertmp / "scratch" / "out")
        roots = clean.clean_tagged_roots([], steady=True)
        self.assertNotIn(str(self.dev), roots)
        self.assertIn(str(self.usertmp), roots)
        self.assertEqual(sorted(p for p, _ in clean.clean_tagged_find([], steady=True)), sorted([str(job), str(temp)]))
        out = clean.clean_pass(False, only=["tagged-caches"], steady=True, report=False)
        self.assertEqual(out["summary"]["done"], 2, out)
        self.assertTrue(code.exists())
        self.assertFalse(job.exists() or temp.exists())
        self.assertEqual(self.run_clean()["summary"]["done"], 1)   # the ordinary pass still takes it
        self.assertFalse(code.exists())

    def test_names_that_hold_no_rust_target_are_skipped_in_code_homes_only(self):
        for name in ("dist", "build", "coverage"):
            self.cache(self.dev / "app" / name / "cache")
        elsewhere = self.cache(self.jobs / "J-0042" / "dist" / "cache")
        self.assertEqual([p for p, _ in clean.clean_tagged_find([])], [str(elsewhere)])

    def test_each_root_has_the_whole_discovery_budget(self):
        """0.17.2: the budget was divided by the number of roots (every linked worktree is one), starving ~/dev at 47k of 3M entries."""
        for n in range(300):
            self.put(self.dev / "huge", f"d{n}/f")
        mine = self.cache(self.usertmp / "my-build")
        errors = []
        share = 40
        with mock.patch.object(clean, "CLEAN_RUST_SCAN_ENTRIES", share):
            found = clean.clean_tagged_find(errors)
        self.assertEqual([p for p, _ in found], [str(mine)], found)
        incomplete = [e for e in errors if "discovery-incomplete" in e["error"]]
        self.assertEqual(len(incomplete), 1, errors)
        self.assertIn("~/dev", incomplete[0]["error"])
        self.assertEqual(incomplete[0]["path"], "~/dev")


    def test_a_folder_removed_while_discovery_walks_is_not_an_error(self):
        """0.15.1: the shared temp folder churns (other processes' own temp folders come and go), and a folder listed by its parent but
        gone before it was checked or scanned made `tick` exit 3 with `missing:FileNotFoundError:ENOENT`. Gone is not a refusal."""
        gone = self.usertmp / "uo-gone"
        gone.mkdir()
        real_scandir = os.scandir

        def scandir(path):
            if str(path) != str(self.usertmp):
                return real_scandir(path)
            entries = list(real_scandir(path))
            if gone.exists():
                gone.rmdir()  # removed by its owner after the listing
            return contextlib.nullcontext(entries)

        with mock.patch.object(clean.os, "scandir", scandir):
            errors: list = []
            clean.clean_tagged_find(errors)
        self.assertEqual([e for e in errors if "ENOENT" in str(e.get("error"))], [])
