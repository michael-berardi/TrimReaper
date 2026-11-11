"""2026-11-10 disk audit (Data volume at 90%, overflow drive at 97%): what kept the reaper and the overflow from reaching the goal.

1. A process working in one agent-scratch folder pinned every sibling: the holder scope was the scratch root.
2. Expired archives moved to the overflow volume were refused as `allowed-root-inside-system-root` (/Volumes) forever.
3. Offloaded job-cache folders (a link in home, the data on the overflow volume) were never listed, so they never expired.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest import mock

from trimreaper import core

from helpers import ScratchHome


class ScratchHolderScope(ScratchHome):
    config_extra = {"scratchRoot": "~/.cache/agent-scratch"}

    def scratch(self, name: str) -> Path:
        return self.home / ".cache" / "agent-scratch" / name

    def test_a_process_in_one_scratch_folder_does_not_pin_an_idle_sibling(self):
        idle = self.scratch("finished-task")
        self.put(idle, "notes.txt")
        self.age(idle, 20)
        busy = self.scratch("live-task")
        self.put(busy, "server/index.js")
        sleeper = subprocess.Popen(["sleep", "60"], cwd=busy / "server")
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)

        out = self.cli("reap", "--scratch")

        self.assertFalse(idle.exists(), out)
        self.assertTrue(busy.exists())
        removed = next(i for i in out["items"] if i["path"] == str(idle))
        self.assertEqual(removed["outcome"], "deleted", removed)

    def test_a_process_inside_the_scratch_folder_still_keeps_it(self):
        task = self.scratch("held-task")
        self.put(task, "server/index.js")
        self.age(task, 20)
        sleeper = subprocess.Popen(["sleep", "60"], cwd=task / "server")
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)

        out = self.cli("reap", "--scratch")

        self.assertTrue(task.exists())
        held = next(i for i in out["items"] if i["path"] == str(task))
        self.assertTrue(str(held.get("reason", "")).startswith("in-use:"), held)


class OverflowExpiry(ScratchHome):
    """A job-cache folder moved to the overflow volume ages out under the rules it had at home, and its link goes with it."""

    config_extra = {"policy": {"evidenceExpiryDays": 1, "cloneArchive": "keep"}}

    def setUp(self):
        super().setUp()
        self.volume = self.tmp / "Volumes" / "Drive"
        self.overflow = self.volume / "Overflow"
        (self.overflow / "home").mkdir(parents=True)
        self.write_config({"offload": {"root": str(self.overflow), "sources": []}})
        # The test volume is not below /Volumes: stand in for the mount check, and treat the fake volume as a system root
        # so the overflow exception is what lets it through.
        for patch in (mock.patch.object(core, "reap_overflow_home", lambda: os.path.realpath(self.overflow / "home")),
                      mock.patch.object(core, "REAP_SYSTEM_ROOTS", core.REAP_SYSTEM_ROOTS + (str(self.tmp / "Volumes"),))):
            patch.start()
            self.addCleanup(patch.stop)

    def offload(self, job: str, name: str, files: dict[str, bytes]) -> tuple[Path, Path]:
        """Move <cache>/<job>/<name> to the overflow mirror and leave a link, as `trimreaper offload` does."""
        cache = self.home / ".cache" / "trimreaper" / "jobs" / job
        moved = self.overflow / "home" / ".cache" / "trimreaper" / "jobs" / job / name
        for rel, data in files.items():
            self.put(moved, rel, data)
        cache.mkdir(parents=True, exist_ok=True)
        self.put(cache, "report.md", b"report\n")
        os.symlink(str(moved), str(cache / name))
        return cache / name, moved

    def test_an_expired_offloaded_folder_is_removed_with_its_link(self):
        self.add_job("J-0101", finished_ago=3 * 86400)
        link, moved = self.offload("J-0101", "screens", {"a.png": b"x" * 8192})
        self.age(moved, 3)

        out = self.cli("reap", "--job", "J-0101")

        self.assertFalse(moved.exists(), out)
        self.assertFalse(os.path.lexists(link), out)
        item = next(i for i in out["items"] if i["path"] == os.path.realpath(moved))
        self.assertEqual(item["outcome"], "deleted", item)
        self.assertEqual(item.get("linkRemoved"), str(link), item)
        self.assertTrue((link.parent / "report.md").is_file(), "the job folder itself and its records stay")

    def test_an_offloaded_folder_inside_its_window_is_kept(self):
        self.add_job("J-0102", finished_ago=3 * 3600)  # settled, still inside the one-day evidence window
        link, moved = self.offload("J-0102", "screens", {"a.png": b"x" * 8192})

        out = self.cli("reap", "--job", "J-0102")

        self.assertTrue(moved.exists(), out)
        self.assertTrue(os.path.islink(link))

    def test_a_job_folder_evidence_marker_keeps_the_offloaded_child(self):
        self.add_job("J-0103", finished_ago=3 * 86400)
        link, moved = self.offload("J-0103", "screens", {"a.png": b"x" * 8192})
        self.put(link.parent, ".ut-retention-evidence", b"")
        self.age(moved, 3)

        out = self.cli("reap", "--job", "J-0103")

        self.assertTrue(moved.exists(), out)
        self.assertTrue(os.path.islink(link))

    def test_a_link_to_anything_but_its_exact_mirror_is_not_an_item(self):
        self.add_job("J-0104", finished_ago=3 * 86400)
        cache = self.home / ".cache" / "trimreaper" / "jobs" / "J-0104"
        elsewhere = self.overflow / "home" / "Documents" / "keep"
        self.put(elsewhere, "letter.txt")
        cache.mkdir(parents=True)
        os.symlink(str(elsewhere), str(cache / "screens"))
        self.age(elsewhere, 3)

        out = self.cli("reap", "--job", "J-0104")

        self.assertTrue((elsewhere / "letter.txt").exists(), out)
        self.assertTrue(os.path.islink(cache / "screens"))
        self.assertIsNone(core.reap_offloaded_mirror(str(cache / "screens")))

    def test_the_overflow_exception_never_widens_to_other_volume_folders(self):
        inside = os.path.realpath(self.overflow / "home" / ".cache" / "x")
        os.makedirs(inside)
        core.reap_vet_root(inside)  # allowed: below the overflow home
        other = os.path.realpath(self.volume / "Other")
        os.makedirs(other)
        with self.assertRaises(core.ReapRefused) as caught:
            core.reap_vet_root(other)
        self.assertEqual(str(caught.exception), "allowed-root-inside-system-root")
        with self.assertRaises(core.ReapRefused):
            core.reap_vet_root(os.path.realpath(self.overflow / "home"))  # the overflow home itself is never a root


class OverflowHomeCheck(ScratchHome):
    def test_an_overflow_root_outside_volumes_is_not_trusted(self):
        target = self.tmp / "Overflow"
        (target / "home").mkdir(parents=True)
        self.write_config({"offload": {"root": str(target), "sources": []}})
        self.assertIsNone(core.reap_overflow_home())

    def test_no_offload_config_means_no_overflow(self):
        self.assertIsNone(core.reap_overflow_home())


from test_offload import Base as OffloadBase  # noqa: E402
from trimreaper import offload  # noqa: E402


class OrphanPrune(OffloadBase):
    """A moved folder whose home owner the reaper expired is pruned from the overflow volume; anything else stays."""

    def moved_then_owner_expired(self) -> Path:
        out = offload.offload_pass()
        self.assertEqual(out["errors"], [])
        link = self.src / "old"
        mirror = Path(os.readlink(link))
        link.unlink()  # the reaper removed the expired folder that held the link
        return mirror

    def test_an_orphaned_mirror_is_pruned(self):
        mirror = self.moved_then_owner_expired()

        out = offload.offload_pass()

        self.assertFalse(mirror.exists(), out)
        self.assertEqual([p["path"] for p in out["orphans"]["pruned"]], [str(mirror)])
        self.assertEqual(out["orphans"]["prunedBytes"], 8192)

    def test_dry_run_prunes_nothing(self):
        mirror = self.moved_then_owner_expired()

        out = offload.offload_pass(dry_run=True)

        self.assertTrue(mirror.exists())
        self.assertEqual(out["orphans"]["pruned"][0]["pruned"], False)

    def test_a_preserving_marker_keeps_the_orphan(self):
        mirror = self.moved_then_owner_expired()
        (mirror / "deep").mkdir()
        (mirror / "deep" / ".ut-retention-evidence").write_bytes(b"")

        out = offload.offload_pass()

        self.assertTrue(mirror.exists())
        self.assertIn("retention:.ut-retention-evidence", [k["why"] for k in out["orphans"]["kept"]])

    def test_a_linked_or_restored_folder_is_not_an_orphan(self):
        offload.offload_pass()
        mirror = Path(os.readlink(self.src / "old"))

        out = offload.offload_pass()

        self.assertTrue(mirror.exists())
        self.assertEqual(out["orphans"]["pruned"], [])

    def test_a_held_orphan_stays(self):
        from trimreaper import core
        mirror = self.moved_then_owner_expired()
        with mock.patch.object(core, "reap_holders", side_effect=core.ReapRefused("in-use:cwd:1(x)")):
            out = offload.offload_pass()
        self.assertTrue(mirror.exists())
        self.assertEqual(out["orphans"]["pruned"], [])


class OrphanPruneKeepFolder(OffloadBase):
    def setUp(self):
        super().setUp()
        self.write_config({"offload": {"root": str(self.root), "minFreeGB": 0, "quietMinutes": 3 * 24 * 60,
                                       "sources": [{"path": "~/cache", "children": True, "keepFolder": True}]}})

    def test_owner_present_but_link_missing_is_kept_and_reported(self):
        mirror = self.root / "home" / "cache" / "task" / "sub"
        mirror.mkdir(parents=True)
        (mirror / "a.bin").write_bytes(b"z")
        (self.src / "task").mkdir()  # the owner folder is still at home; only the link is gone

        out = offload.offload_pass()

        self.assertTrue(mirror.exists())
        self.assertIn("owner-present-link-missing", [k["why"] for k in out["orphans"]["kept"]])

    def test_owner_gone_prunes_the_inner_mirror(self):
        mirror = self.root / "home" / "cache" / "gone-task" / "sub"
        mirror.mkdir(parents=True)
        (mirror / "a.bin").write_bytes(b"z")

        out = offload.offload_pass()

        self.assertFalse(mirror.exists(), out)
