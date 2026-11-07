"""Offload: cold folders move to an overflow volume and leave a symlink; busy, recent or unmounted cases move nothing."""
import os
import time
from pathlib import Path
from unittest import mock

from helpers import Home
from trimreaper import host, offload


class Offload(Home):
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
        self.write_config({"offload": {"root": str(self.root), "minFreeGB": 0,
                                       "sources": [{"path": "~/cache", "children": True, "idleDays": 3}]}})
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

    def test_old_folder_moves_and_fresh_stays(self):
        out = offload.offload_pass()
        self.assertEqual(out["errors"], [])
        self.assertEqual([m["path"] for m in out["moved"]], [str(self.src / "old")])
        self.assertTrue((self.src / "old").is_symlink())
        self.assertEqual((self.src / "old" / "a.bin").read_bytes(), b"x" * 4096)
        self.assertFalse((self.src / "fresh").is_symlink())
        self.assertIn("changed-within-3d", [k["why"] for k in out["kept"]])

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
        self.write_config({"offload": {"root": str(self.root), "minFreeGB": 10 ** 6,
                                       "sources": [{"path": "~/cache", "children": True, "idleDays": 3}]}})
        out = offload.offload_pass()
        self.assertEqual(out["moved"], [])
        self.assertTrue(all("minFreeGB" in k["why"] or k["why"].startswith("changed") for k in out["kept"]))

    def test_bad_config_is_named(self):
        self.write_config({"offload": {"root": str(self.root), "sources": [{"path": "~/cache", "idleDay": 3}]}})
        with self.assertRaisesRegex(host.HostError, "idleDay"):
            host.config()
