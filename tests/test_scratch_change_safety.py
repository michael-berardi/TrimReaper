"""Synthetic fixtures for preserved timestamps, metadata changes, mounts and shared idle budgets."""
from __future__ import annotations

import os
import shutil
import time
from types import SimpleNamespace
from unittest import mock

from trimreaper import core

REAL_SCRATCH_CHANGED = core.reap_scratch_changed
from helpers import Home


class ScratchChangeSafety(Home):
    config_extra = {"scratchRoot": "~/.cache/agent-scratch"}

    def scratch(self, name):
        return self.home / ".cache" / "agent-scratch" / name

    def test_a_fresh_copy_with_preserved_mtimes_is_kept_by_the_guarded_cli(self):
        source = self.tmp / "source"
        self.put(source, "a/b/inner.txt")
        self.age(source, 20)
        task = self.scratch("copied-task")
        task.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, task, copy_function=shutil.copy2)
        self.assertEqual(task.stat().st_mtime_ns, source.stat().st_mtime_ns)
        self.assertEqual((task / "a/b/inner.txt").stat().st_mtime_ns, (source / "a/b/inner.txt").stat().st_mtime_ns)
        self.assertGreater(task.stat().st_ctime, time.time() - 60)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue((task / "a/b/inner.txt").exists())
        entry = next(i for i in out["items"] if i["path"] == str(task))
        self.assertEqual(entry["idleProof"]["reason"], "recent-change")
        self.assertGreater(entry["idleProof"]["newest"], time.time() - 60)

    def test_a_recent_root_metadata_change_is_kept_despite_an_old_mtime(self):
        task = self.scratch("metadata-task")
        self.put(task, "inner.txt")
        self.age(task, 20)
        before = task.stat().st_mtime_ns
        os.chmod(task, 0o750)

        proof = core.reap_scratch_idle_proof(str(task))

        self.assertFalse(proof["idle"], proof)
        self.assertEqual(proof["reason"], "recent-change")
        self.assertEqual(task.stat().st_mtime_ns, before)
        self.assertGreaterEqual(proof["newest"], task.stat().st_ctime)

    def test_a_nested_other_device_is_kept_without_opening_it(self):
        task = self.scratch("mount-task")
        self.put(task, "mounted/inner.txt")
        self.age(task, 20)
        original_stat, original_open = core.os.stat, core.os.open
        opened = []

        def mounted(name, *args, **kwargs):
            info = original_stat(name, *args, **kwargs)
            if str(name) == "mounted":
                fields = {key: getattr(info, key) for key in dir(info) if key.startswith("st_")}
                return SimpleNamespace(**{**fields, "st_dev": info.st_dev + 1})
            return info

        def tracked_open(name, *args, **kwargs):
            opened.append(str(name))
            return original_open(name, *args, **kwargs)

        # Advance the proof clock for this synthetic mount fixture: no real ctime can be backdated.
        with mock.patch.object(core, "now", return_value=time.time() + 30 * 86400), \
                mock.patch.object(core.os, "stat", side_effect=mounted), \
                mock.patch.object(core.os, "open", side_effect=tracked_open):
            proof = core.reap_scratch_idle_proof(str(task))

        self.assertFalse(proof["idle"], proof)
        self.assertEqual(proof["reason"], "mountpoint", proof)
        self.assertNotIn("mounted", opened)

    def test_a_caller_supplied_deadline_still_applies_at_the_removal_gate(self):
        task = self.scratch("late-task")
        self.put(task, "inner.txt")
        self.age(task, 20)
        deadline = time.monotonic() + 30
        with mock.patch.object(core, "now", return_value=time.time() + 30 * 86400):
            items = core.reap_agent_scratch_items(deadline=deadline)
        item = next(i for i in items if i["path"] == str(task))
        self.assertEqual(item["retention"], core.REAP_DISPOSABLE)
        # Discovery's deadline is separate from the sweep (0.6.12); an explicit caller deadline still binds re-proofs.
        self.assertNotIn("idleDeadline", item)
        item["idleDeadline"] = time.monotonic() - 1
        with mock.patch.object(core, "now", return_value=time.time() + 30 * 86400):
            result = core.reap_item(item, "dry-run", {"sizeBudget": 120})

        self.assertEqual(result["reason"], "idle-budget-time-exceeded", result)
        self.assertTrue(task.exists())

    def test_a_walk_checks_the_deadline_while_listing_names_not_only_visiting_them(self):
        task = self.scratch("slow-list-task")
        self.put(task, "a.txt")
        self.put(task, "b.txt")
        self.age(task, 20)
        clock, listed = [time.monotonic()], []
        scandir = core.os.scandir

        class SlowListing:
            def __init__(self, path):
                self.entries = scandir(path)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.entries.close()

            def __iter__(self):
                for entry in self.entries:
                    listed.append(entry.name)
                    clock[0] += 2
                    yield entry

        with mock.patch.object(core, "now", return_value=time.time() + 30 * 86400), \
                mock.patch.object(core.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(core.os, "scandir", side_effect=SlowListing):
            proof = core.reap_scratch_idle_proof(str(task), seconds=1)

        self.assertFalse(proof["idle"], proof)
        self.assertEqual(proof["reason"], "budget-time-exceeded", proof)
        self.assertEqual(len(listed), 1)
        self.assertEqual(proof["entries"], 0)

    def test_a_newer_mtime_is_not_lost_when_ctime_is_older(self):
        real = REAL_SCRATCH_CHANGED  # the fixture replaces it with a synthetic-ctime reader; test the engine's own
        self.assertEqual(real(SimpleNamespace(st_mtime=200, st_ctime=100)), 200)
        self.assertEqual(real(SimpleNamespace(st_mtime=100, st_ctime=200)), 200)

    def test_a_root_on_another_device_is_kept_without_opening_it(self):
        task = self.scratch("root-mount-task")
        self.put(task, "inner.txt")
        self.age(task, 20)
        original = core.os.lstat

        def mounted(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if str(path) == str(task):
                fields = {key: getattr(info, key) for key in dir(info) if key.startswith("st_")}
                return SimpleNamespace(**{**fields, "st_dev": info.st_dev + 1})
            return info

        with mock.patch.object(core.os, "lstat", side_effect=mounted), mock.patch.object(core, "reap_fd_walk") as walk:
            proof = core.reap_scratch_idle_proof(str(task))
        self.assertEqual(proof["reason"], "mountpoint", proof)
        self.assertFalse(proof["idle"], proof)
        walk.assert_not_called()

    def test_the_tick_reuses_its_initial_listing_without_restarting_discovery(self):
        deadlines = []
        items = [{"path": "/fixture/task", "retention": core.REAP_DISPOSABLE, "idleAttempted": True}]

        def listed(deadline, after=None):
            deadlines.append(deadline)
            return items

        with mock.patch.object(core, "row", return_value=None), \
                mock.patch.object(core, "reap_release_evidence_items", return_value=[]), \
                mock.patch.object(core, "reap_archive_stamp_legacy"), \
                mock.patch.object(core, "reap_archive_items", return_value=[]), \
                mock.patch.object(core, "reap_agent_scratch_items", side_effect=listed), \
                mock.patch.object(core, "reap_agent_scratch", return_value={"deleted": 0}) as sweep:
            core.reap_agent_scratch_tick(mock.Mock(), [], [])

        self.assertEqual(len(deadlines), 1)
        sweep.assert_called_once_with(False, items)

    def test_an_empty_folder_still_obeys_the_time_budget(self):
        task = self.scratch("empty-task")
        task.mkdir(parents=True)
        self.age(task, 20)
        with mock.patch.object(core, "now", return_value=time.time() + 30 * 86400):
            proof = core.reap_scratch_idle_proof(str(task), seconds=0)
        self.assertFalse(proof["idle"], proof)
        self.assertEqual(proof["reason"], "budget-time-exceeded", proof)
