"""2026-11-26: idle regenerable output in agent scratch and finished job folders waited for the whole folder's evidence window, and
any Cargo build anywhere kept every Rust target. 25 GB of debug builds and node_modules stayed on a 95% full disk."""
from __future__ import annotations

import os
import time
from unittest import mock

from helpers import ScratchHome
from trimreaper import clean, core

EMPTY_TABLE = {"files": [], "commands": []}


class RegenInScratch(ScratchHome):
    config_extra = {"scratchRoot": "~/.cache/agent-scratch"}

    def setUp(self):
        super().setUp()
        for patch in (mock.patch.object(clean, "reap_process_table", lambda: dict(EMPTY_TABLE)),
                      mock.patch.object(core, "reap_process_table", lambda: dict(EMPTY_TABLE)),
                      mock.patch.object(clean, "reap_holders", lambda *a, **kw: None)):
            patch.start()
            self.addCleanup(patch.stop)

    def scratch_repo(self, task: str):
        repo = self.clone(f"src-{task}", tracked={"README.md": "hello\n", "package.json": "{}"})
        dest = self.home / ".cache" / "agent-scratch" / task / "wt"
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.git(repo, "worktree", "add", "-q", "-b", f"b-{task}", str(dest))
        return dest

    def test_idle_node_modules_in_a_recent_scratch_folder_is_reclaimed(self):
        wt = self.scratch_repo("busy-task")
        self.put(wt, "node_modules/pkg/index.js")
        self.age(wt / "node_modules", 1)
        self.put(wt.parent, "notes.md", b"written just now\n")   # the folder itself is recent
        with mock.patch.object(clean, "clean_running", lambda: set()), \
                mock.patch.object(clean, "clean_repo_idle_hours", lambda repo, fresh: 100.0):
            out = self.cli("clean", "--only", "project-dependencies")
        self.assertFalse((wt / "node_modules").exists(), out)
        self.assertTrue((wt.parent / "notes.md").exists())

    def test_output_changed_within_the_quiet_window_is_kept(self):
        wt = self.scratch_repo("fresh-task")
        self.put(wt, "node_modules/pkg/index.js")   # just installed
        with mock.patch.object(clean, "clean_running", lambda: set()), \
                mock.patch.object(clean, "clean_repo_idle_hours", lambda repo, fresh: 100.0):
            self.cli("clean", "--only", "project-dependencies")
        self.assertTrue((wt / "node_modules").exists())

    def test_a_live_job_claim_keeps_output(self):
        wt = self.scratch_repo("claimed-task")
        self.put(wt, "node_modules/pkg/index.js")
        self.age(wt / "node_modules", 1)
        entry = {"name": "project-dependencies", "path": str(wt / "node_modules"), "repo": str(wt), "regenQuietHours": 2}
        with mock.patch.object(clean, "clean_tagged_live_job", side_effect=core.ReapRefused("in-use-by-running-job:J-0001")):
            with self.assertRaisesRegex(core.ReapRefused, "in-use-by-running-job"):
                clean.clean_scratch_debris_gates(entry, None)

    def test_evidence_in_the_folder_is_never_a_candidate(self):
        task = self.home / ".cache" / "agent-scratch" / "evidence-task"
        note = self.put(task, "report.md")
        self.age(task, 1)
        self.cli("clean", "--only", "project-dependencies,rust-build-output")
        self.assertTrue(note.exists())


class PerTargetLocks(ScratchHome):
    def test_routine_pass_still_keeps_targets_while_any_cargo_runs(self):
        with mock.patch.object(clean, "clean_running", lambda: {"cargo"}):
            with self.assertRaisesRegex(core.ReapRefused, "in-use:cargo"):
                clean.clean_rust_busy_gate({})

    def test_from_the_first_rung_another_build_does_not_keep_this_target(self):
        with mock.patch.object(clean, "clean_running", lambda: {"cargo"}):
            clean.clean_rust_busy_gate({"perTargetLocks": True})

    def test_rungs_set_per_target_locks_and_routine_does_not(self):
        seen = {}
        for rung_id in (0, 1):
            rung = clean.clean_profile({}, rung_id)
            with mock.patch.object(clean, "clean_rust_targets", lambda errors: [(str(self.tmp / "t"), str(self.tmp))]), \
                    mock.patch.object(clean, "clean_detached_rust_targets", lambda errors: []):
                (self.tmp / "t" / "debug").mkdir(parents=True, exist_ok=True)
                entries = clean.clean_extra_entries({"rust-build-output"}, [], 48, rung)
            seen[rung_id] = [e.get("perTargetLocks") for e in entries]
        self.assertEqual(seen[0], [None])
        self.assertEqual(seen[1], [True])

    def test_emergency_shortens_the_quiet_window(self):
        rung = dict(clean.clean_profile({}, 2), emergency=True)
        with mock.patch.object(clean, "clean_rust_targets", lambda errors: [(str(self.tmp / "t"), str(self.tmp))]), \
                mock.patch.object(clean, "clean_detached_rust_targets", lambda errors: []):
            (self.tmp / "t" / "debug").mkdir(parents=True, exist_ok=True)
            entries = clean.clean_extra_entries({"rust-build-output"}, [], 48, rung)
        self.assertEqual(entries[0]["regenQuietHours"], clean.CLEAN_REGEN_EMERGENCY_HOURS)
