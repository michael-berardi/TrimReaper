"""Regression tests for TrimReaper 0.24.5 performance, safety, and observability fixes."""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from trimreaper import clean, core, host, offload


class TestBlockerIndexAndConfigCache(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tr-0245-"))
        self.cfg_path = self.tmp / "config.json"
        self.prev_cfg = os.environ.get("TRIMREAPER_CONFIG")
        os.environ["TRIMREAPER_CONFIG"] = str(self.cfg_path)
        host.clear_config_cache()
        self.cfg_path.write_text(json.dumps({
            "stateDir": str(self.tmp / "state"),
            "cacheRoot": str(self.tmp / "cache"),
            "archiveRoot": str(self.tmp / "archive"),
        }))

    def tearDown(self) -> None:
        import shutil
        if self.prev_cfg is None:
            os.environ.pop("TRIMREAPER_CONFIG", None)
        else:
            os.environ["TRIMREAPER_CONFIG"] = self.prev_cfg
        host.clear_config_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_schema_creates_jobs_terminal_id_index_and_blocker_scales_linearly(self) -> None:
        with host.db() as conn:
            indexes = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertIn("jobs_terminal_id", indexes)
            now = time.time() - 7200
            conn.executemany(
                "INSERT INTO jobs(id, title, status, created_at, finished_at, terminal_id, meta) VALUES (?,?,?,?,?,?,?)",
                [(f"J-{i:04d}", f"Job {i}", "done", now - 60, now, f"term-{i:04d}", "{}") for i in range(2000)],
            )
            conn.execute(
                "INSERT INTO jobs(id, title, status, created_at, meta) VALUES (?,?,?,?,?)",
                ("J-9000", "Waiter", "parked", now, json.dumps({"waitingOn": {"job": "J-0042"}})),
            )
            conn.execute(
                "INSERT INTO jobs(id, title, status, created_at, terminal_id, meta) VALUES (?,?,?,?,?,?)",
                ("J-9001", "Sibling", "running", now, "term-0099", "{}"),
            )
            t0 = time.perf_counter()
            all_done = core.rows(conn, "SELECT * FROM jobs WHERE status='done'")
            with core.reap_blocker_scope(conn):
                blockers = {j["id"]: core.reap_job_blocker(conn, j, set()) for j in all_done}
            elapsed = time.perf_counter() - t0
            self.assertLess(elapsed, 5.0, f"2000-job blocker pass took {elapsed:.2f}s")
            self.assertEqual(blockers["J-0042"], "awaited-by:J-9000(parked)")
            self.assertEqual(blockers["J-0099"], "terminal-shared-with:J-9001(running)")
            self.assertIsNone(blockers["J-0001"])
            # Equivalence check: indexed answer matches non-indexed fallback on representative jobs
            for sample_id in ("J-0042", "J-0099", "J-0001"):
                sample_job = core.row(conn, "SELECT * FROM jobs WHERE id=?", (sample_id,))
                self.assertEqual(blockers[sample_id], core.reap_job_blocker(conn, sample_job, set()))

    def test_config_cache_invalidates_immediately_on_edit_and_rechecks_disk_volume(self) -> None:
        vol = self.tmp / "vol"
        vol.mkdir()
        self.cfg_path.write_text(json.dumps({
            "stateDir": str(self.tmp / "state"),
            "cacheRoot": str(self.tmp / "cache"),
            "archiveRoot": str(self.tmp / "archive"),
            "diskVolume": str(vol),
        }))
        first = host.config()
        self.assertEqual(first["cacheRoot"], str(self.tmp / "cache"))
        # Mutating returned copy must not pollute cache
        first["cacheRoot"] = "/mutated"
        self.assertEqual(host.config()["cacheRoot"], str(self.tmp / "cache"))
        # Unmounting diskVolume is still caught on a cache hit
        vol.rmdir()
        with self.assertRaises(host.HostError):
            host.config()
        vol.mkdir()
        # Writing updated config immediately invalidates stat signature
        self.cfg_path.write_text(json.dumps({
            "stateDir": str(self.tmp / "state"),
            "cacheRoot": str(self.tmp / "cache-v2"),
            "archiveRoot": str(self.tmp / "archive"),
            "diskVolume": str(vol),
        }))
        self.assertEqual(host.config()["cacheRoot"], str(self.tmp / "cache-v2"))

    def test_clean_linked_worktrees_fresh_skips_memo(self) -> None:
        calls = 0

        def fake_git(_repo, *_args):
            nonlocal calls
            calls += 1
            return 0, f"worktree /main\0\0worktree /wt-{calls}\0\0"

        clean.CLEAN_MEMO.data = {}
        try:
            with mock.patch.object(clean, "reap_git", side_effect=fake_git):
                first = clean.clean_linked_worktrees("/main")
                second = clean.clean_linked_worktrees("/main")
                third = clean.clean_linked_worktrees("/main", fresh=True)
        finally:
            clean.CLEAN_MEMO.data = None
        self.assertEqual(first, [{"worktree": "/wt-1"}])
        self.assertEqual(second, [{"worktree": "/wt-1"}])
        self.assertEqual(third, [{"worktree": "/wt-2"}])
        self.assertEqual(calls, 2)

    def test_verifier_accepted_rejects_cross_job_verifier(self) -> None:
        with host.db() as conn:
            conn.execute("ALTER TABLE jobs ADD COLUMN summary TEXT")
            now = time.time() - 120
            conn.execute(
                "INSERT INTO jobs(id, title, status, created_at, finished_at, meta, summary) VALUES (?,?,?,?,?,?,?)",
                ("J-0501", "Verify finished J-0500", "done", now - 30, now, json.dumps({"targetJob": "J-0500"}), "verified: J-0500 ok"),
            )
            self.assertTrue(core.reap_verifier_accepted(conn, "verifier:J-0501", job_id="J-0500"))
            self.assertFalse(core.reap_verifier_accepted(conn, "verifier:J-0501", job_id="J-0599"))
            conn.execute(
                "INSERT INTO jobs(id, title, status, created_at, finished_at, meta, summary) VALUES (?,?,?,?,?,?,?)",
                ("J-0502", "Verify finished J-0500", "done", now - 30, now, "{}", "verified: J-0500 passed all checks"),
            )
            self.assertTrue(core.reap_verifier_accepted(conn, "verifier:J-0502", job_id="J-0500"))
            self.assertFalse(core.reap_verifier_accepted(conn, "verifier:J-0502", job_id="J-0599"))

    def test_move_one_rolls_back_on_symlink_failure_and_aborts_if_job_becomes_live(self) -> None:
        src = self.tmp / "src-dir"
        src.mkdir()
        (src / "a.txt").write_text("hello")
        dest_root = self.tmp / "dest"
        dest_root.mkdir()
        dest = dest_root / "src-dir"
        stats = offload.tree_stats(src)
        item = {
            "path": src,
            "dest": dest,
            "bytes": stats["bytes"],
            "stats": stats,
            "owner": "J-0700",
            "jobs": True,
            "quiet": 60,
            "settled": 60,
        }
        # 1) Live job transition during copy aborts before touching original
        with mock.patch.object(offload, "job_statuses", return_value={"J-0700": "running"}):
            with self.assertRaises(host.HostError) as ctx:
                offload.move_one(item, False, cfg={"stateDir": str(self.tmp / "state")})
            self.assertIn("became running", str(ctx.exception))
        self.assertTrue(src.is_dir() and not src.is_symlink())
        self.assertFalse(dest.exists())

        # 2) Symlink failure rolls back holding rename AND removes orphaned dest copy
        with mock.patch.object(offload.os, "symlink", side_effect=OSError("simulated symlink failure")), \
             mock.patch.object(offload.core, "reap_holders", return_value=None):
            with self.assertRaises(OSError):
                offload.move_one(item, False)
        self.assertTrue(src.is_dir() and not src.is_symlink())
        self.assertEqual((src / "a.txt").read_text(), "hello")
        self.assertFalse((self.tmp / ".offload-src-dir.old").exists())
        self.assertFalse(dest.exists())

    def test_clean_swap_report_surfaces_large_swap(self) -> None:
        errors: list = []
        fake_swap = {
            "kind": "swap",
            "warning": "swap-large",
            "bytes": 20 * 1024 ** 3,
            "holders": [{"pid": 123, "command": "leaker", "memory": "40G", "compressed": "20G"}],
            "note": "swap is large; leaker (pid 123) holds 20G",
        }
        with mock.patch.object(clean, "clean_swap_inventory", return_value=fake_swap):
            clean.clean_swap_report(errors)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["warning"], "swap-large")


if __name__ == "__main__":
    unittest.main()
