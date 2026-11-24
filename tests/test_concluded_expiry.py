"""A job whose work is conclusively over loses its folders at once (0.24.0, `concludedExpiry`).

Finished jobs' worktrees, clones and cache folders used to wait cloneExpiryDays (3) or evidenceExpiryDays (14) even after a verifier had
accepted or rejected the job and its worker was closed: 30 GB of them sat on a 90%-full disk. Concluded = done/failed/cancelled,
settled, its worker terminal's cleanup record `closed`, no live continuation. Anything short of that keeps the old windows.
"""
from __future__ import annotations

import contextlib
import time

from trimreaper import core

from helpers import Home


class ConcludedExpiry(Home):
    def setUp(self):
        super().setUp()
        self.write_config({"policy": {"evidenceExpiryDays": 14, "cloneExpiryDays": 3}})

    def job(self, job_id: str, finished_ago: float = 3600, terminal: str | None = "t-1", record: str | None = "closed"):
        self.add_job(job_id, finished_ago=finished_ago)
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET terminal_id=?, created_at=?, started_at=? WHERE id=?",
                         (terminal, time.time() - finished_ago - 7200, time.time() - finished_ago - 3600, job_id))
            if terminal and record:
                conn.execute("INSERT OR REPLACE INTO cleanup_records(terminal_id,status,recorded_at) VALUES(?,?,?)",
                             (terminal, record, time.time() - 60))

    def concluded(self, job_id: str) -> bool:
        with core.db() as conn:
            return core.reap_job_concluded(conn, core.row(conn, "SELECT * FROM jobs WHERE id=?", (job_id,)))

    def test_verified_and_closed_job_is_concluded_and_expires_at_once(self):
        self.job("J-0101")
        self.assertTrue(self.concluded("J-0101"))
        self.assertTrue(core.reap_clone_expired(["J-0101"]))
        self.assertTrue(core.reap_scratch_expired("J-0101"))
        self.assertTrue(core.reap_jobs_finished_before(["J-0101"], 3 * 86400))

    def test_worker_not_closed_keeps_the_windows(self):
        self.job("J-0102", record="verified")
        self.assertFalse(self.concluded("J-0102"))
        self.assertFalse(core.reap_clone_expired(["J-0102"]))
        self.assertFalse(core.reap_scratch_expired("J-0102"))

    def test_no_terminal_or_no_record_keeps_the_windows(self):
        self.job("J-0103", terminal=None)
        self.job("J-0104", terminal="t-4", record=None)
        self.assertFalse(self.concluded("J-0103"))
        self.assertFalse(self.concluded("J-0104"))

    def test_still_settling_is_not_concluded(self):
        self.job("J-0105", finished_ago=60)
        self.assertFalse(self.concluded("J-0105"))

    def test_running_job_is_never_concluded(self):
        self.add_job("J-0106", status="running")
        self.assertFalse(self.concluded("J-0106"))
        self.assertFalse(core.reap_clone_expired(["J-0106"]))

    def test_policy_switch_turns_it_off(self):
        self.job("J-0107")
        self.write_config({"policy": {"evidenceExpiryDays": 14, "cloneExpiryDays": 3, "concludedExpiry": False}})
        self.assertFalse(self.concluded("J-0107"))
        self.assertFalse(core.reap_clone_expired(["J-0107"]))

    def test_old_windows_still_apply_without_conclusion(self):
        self.job("J-0108", finished_ago=4 * 86400, record=None)
        self.assertTrue(core.reap_clone_expired(["J-0108"]))
        self.assertFalse(core.reap_scratch_expired("J-0108"))
