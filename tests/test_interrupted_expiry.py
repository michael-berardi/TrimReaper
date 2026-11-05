"""An interrupted job nothing continues ages like a failed one after `interruptedExpiryDays` (7).

A job whose worker terminal vanished without a report stays `interrupted`, and with no continuation, successor or resumed job it
kept its folders for good (2026-11: one interrupted 15 days earlier still held 2 GB). Past `interruptedExpiryDays` after its
verified interruption it is treated like a failed job: its scratch goes by the evidence window, its clone by the clone window
(archived first when it holds unsaved work). A live continuation, an unfinished job waiting on it, a live worker terminal, a missing
interruption record or `interruptedExpiryDays: 0` keep everything, exactly as before.
"""
from __future__ import annotations

import contextlib
import json
import subprocess
import time

from trimreaper import core

from helpers import Home, DAY


class InterruptedExpiry(Home):
    def setUp(self):
        super().setUp()
        self.write_config({"policy": {"evidenceExpiryDays": 1, "cloneExpiryDays": 3}})

    def interrupt(self, job: str, finished_ago: float):
        stamp = time.time() - finished_ago
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET status='interrupted', created_at=?, started_at=?, finished_at=?, meta=? WHERE id=?",
                         (stamp - 3600, stamp - 60, stamp, json.dumps({"interrupted": {"at": stamp}}), job))

    def finish(self, job: str, finished_ago: float):
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET created_at=?, started_at=?, finished_at=? WHERE id=?",
                         (time.time() - finished_ago - 60, time.time() - finished_ago - 30, time.time() - finished_ago, job))

    def continue_with(self, source: str, heir: str):
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", ("interrupt-heir:" + source, heir))

    def cache_subdir(self, job: str, name: str = "scratch"):
        path = self.home / ".cache" / "trimreaper" / "jobs" / job / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "notes.txt").write_text("scratch\n")
        return path

    def reap(self, job: str) -> dict:
        return self.cli("reap", "--job", job)

    def interrupted_job(self, job: str, days: float, cwd=None):
        self.add_job(job, cwd)
        self.interrupt(job, days * DAY)

    # ---- the rule ------------------------------------------------------------------------------------------------

    def test_an_interrupted_job_with_no_continuation_releases_its_scratch_after_the_window(self):
        self.interrupted_job("J-9101", 15)
        scratch = self.cache_subdir("J-9101")

        out = self.reap("J-9101")

        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(scratch.exists())

    def test_inside_the_window_it_keeps_everything(self):
        self.interrupted_job("J-9102", 4)
        scratch = self.cache_subdir("J-9102")

        out = self.cli("reap", "--job", "J-9102", expect=2)  # not reapable at all: still an interrupted job

        self.assertTrue(scratch.exists())

    def test_the_window_is_the_policy_key(self):
        self.write_config({"policy": {"evidenceExpiryDays": 1, "cloneExpiryDays": 3, "interruptedExpiryDays": 2}})
        self.interrupted_job("J-9103", 4)
        scratch = self.cache_subdir("J-9103")

        out = self.reap("J-9103")

        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(scratch.exists())

    def test_zero_keeps_an_interrupted_job_for_good(self):
        self.write_config({"policy": {"evidenceExpiryDays": 1, "cloneExpiryDays": 3, "interruptedExpiryDays": 0}})
        self.interrupted_job("J-9104", 90)
        scratch = self.cache_subdir("J-9104")

        self.cli("reap", "--job", "J-9104", expect=2)

        self.assertTrue(scratch.exists())

    def test_a_live_continuation_keeps_it(self):
        self.interrupted_job("J-9105", 15)
        self.add_job("J-9106", status="running")  # the continuation is working
        self.continue_with("J-9105", "J-9106")
        scratch = self.cache_subdir("J-9105")

        self.cli("reap", "--job", "J-9105", expect=2)

        self.assertTrue(scratch.exists())

    def test_a_continuation_record_naming_no_job_keeps_it(self):
        self.interrupted_job("J-9107", 15)
        self.continue_with("J-9107", "J-9999")  # no such job: proves nothing
        scratch = self.cache_subdir("J-9107")

        self.cli("reap", "--job", "J-9107", expect=2)

        self.assertTrue(scratch.exists())

    def test_a_finished_continuation_still_releases_it_from_the_chain_clock(self):
        self.interrupted_job("J-9108", 15)
        self.add_job("J-9109", status="done", finished_ago=12 * DAY)
        self.finish("J-9109", 12 * DAY)
        self.continue_with("J-9108", "J-9109")
        scratch = self.cache_subdir("J-9108")

        out = self.reap("J-9108")

        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(scratch.exists())

    def test_an_unfinished_job_waiting_on_it_still_pins_it(self):
        self.interrupted_job("J-9110", 15)
        self.cli("job", "add", "J-9111", "--waits-on", "J-9110", "--status", "running")
        scratch = self.cache_subdir("J-9110")

        out = self.cli("reap", "--job", "J-9110", expect=2)

        self.assertTrue(scratch.exists())

    def test_a_missing_interruption_record_proves_nothing(self):
        self.add_job("J-9112")
        stamp = time.time() - 15 * DAY
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET status='interrupted', created_at=?, started_at=?, finished_at=?, meta=NULL WHERE id=?",
                         (stamp - 3600, stamp - 60, stamp, "J-9112"))
        scratch = self.cache_subdir("J-9112")

        self.cli("reap", "--job", "J-9112", expect=2)

        self.assertTrue(scratch.exists())

    def test_the_chain_clock_is_the_interruption_once_the_window_passed(self):
        self.interrupted_job("J-9113", 10)
        with core.db() as conn:
            job = dict(conn.execute("SELECT * FROM jobs WHERE id='J-9113'").fetchone())
            clock = core.reap_chain_clock(conn, job)
        self.assertIsNotNone(clock)
        self.assertAlmostEqual(clock, time.time() - 10 * DAY, delta=60)
        self.write_config({"policy": {"interruptedExpiryDays": 30}})
        with core.db() as conn:
            self.assertIsNone(core.reap_chain_clock(conn, dict(conn.execute("SELECT * FROM jobs WHERE id='J-9113'").fetchone())))

    # ---- the clone: removed by the clone window, archived first when it holds unsaved work -------------------------

    def test_its_clean_clone_goes_with_the_clone_window(self):
        repo = self.clone("site-j9120")
        self.interrupted_job("J-9120", 15, repo)

        out = self.reap("J-9120")

        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(repo.exists())

    def test_its_clone_with_unsaved_work_is_archived_then_removed(self):
        repo = self.clone("site-j9121")
        (repo / "wip.txt").write_text("unsaved\n")
        self.interrupted_job("J-9121", 15, repo)

        out = self.reap("J-9121")

        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(repo.exists())
        self.assertEqual(out["archived"], 1, out)
        archive = out["items"][0]["archive"]["path"]
        self.assertTrue(archive and subprocess.run(["tar", "-tzf", archive + "/files.tar.gz"], capture_output=True, text=True).stdout.count("wip.txt"))

    def test_its_clone_inside_the_window_is_kept(self):
        repo = self.clone("site-j9122")
        self.interrupted_job("J-9122", 4, repo)

        self.cli("reap", "--job", "J-9122", expect=2)

        self.assertTrue(repo.exists())
