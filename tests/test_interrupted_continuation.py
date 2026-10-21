"""An interrupted job whose work a durable continuation took over is reclaimed by the ordinary windows.

A job whose worker terminal vanished without a report stays `interrupted`: it never reported, so the terminal rules do not
release its folders and it kept them for good. A host that continues such a job records the continuation it queued. With that
record, a verified interruption clock and a continuation chain that has finished, the evidence window releases the job's
scratch and the clone window releases its checkout. Without a provable interruption, without a continuation record, with a
record naming a continuation that does not exist, or with a continuation that is still live, the job keeps everything.

The job records here are consistent: a job is created before it starts, it starts before it is interrupted, and a
continuation is created after the job it continues had started.
"""
from __future__ import annotations

import contextlib
import json
import time

from helpers import Home, DAY


class InterruptedContinuation(Home):
    def setUp(self):
        super().setUp()
        self.write_config({"policy": {"evidenceExpiryDays": 1, "cloneExpiryDays": 3}})

    # ---- the job store ------------------------------------------------------------------------------

    def interrupt(self, job: str, finished_ago: float = 2 * DAY):
        """A host that lost the worker terminal: the job is interrupted, with its own interruption clock."""
        stamp = time.time() - finished_ago
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET status='interrupted', created_at=?, started_at=?, finished_at=?, meta=? WHERE id=?",
                         (stamp - 3600, stamp - 60, stamp, json.dumps({"interrupted": {"at": stamp}}), job))

    def interrupt_with_event(self, job: str, finished_ago: float = 2 * DAY):
        """An older host: the interruption is only in the event log, and the clock must match the job's own finish time."""
        stamp = time.time() - finished_ago
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET status='interrupted', created_at=?, started_at=?, finished_at=?, meta=NULL WHERE id=?",
                         (stamp - 3600, stamp - 60, stamp, job))
            conn.execute("INSERT INTO events(ts,kind,subject,actor,data) VALUES(?,?,?,?,?)",
                         (stamp, "job.interrupted", job, "host", "{}"))

    def finish(self, job: str, finished_ago: float):
        """A consistent finished job: it was created before it started, and it started before it finished."""
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET created_at=?, started_at=?, finished_at=? WHERE id=?",
                         (time.time() - finished_ago - 60, time.time() - finished_ago - 30, time.time() - finished_ago, job))

    def continue_with(self, source: str, heir: str):
        """The host's durable continuation record: the continuation it queued for the interrupted job."""
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", ("interrupt-heir:" + source, heir))

    def scope_record(self, key: str, source: str, successor: str):
        """The host's scope handoff record: keyed by the source's terminal id, naming the job it took over from."""
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                         (key, json.dumps({"job": source, "successor": successor})))

    def cache_subdir(self, job: str, name: str = "scratch"):
        """An unmarked scratch folder of the job, the kind the evidence window releases when the job is finished."""
        path = self.home / ".cache" / "trimreaper" / "jobs" / job / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "notes.txt").write_text("scratch\n")
        return path

    # ---- the rule ------------------------------------------------------------------------------------

    def test_continued_interrupted_job_releases_its_scratch(self):
        repo = self.clone("site-j9001")
        self.add_job("J-9001", repo)
        self.add_job("J-9002")
        self.finish("J-9002", 2 * DAY)
        self.interrupt("J-9001", finished_ago=4 * DAY)
        scratch = self.cache_subdir("J-9001")
        self.continue_with("J-9001", "J-9002")
        out = self.cli("reap", "--job", "J-9001")
        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(scratch.exists())

    def test_interrupted_job_without_a_continuation_keeps_its_scratch(self):
        repo = self.clone("site-j9003")
        self.add_job("J-9003", repo)
        self.interrupt("J-9003", finished_ago=4 * DAY)
        scratch = self.cache_subdir("J-9003")
        out = self.cli("reap", "--job", "J-9003", expect=2)
        self.assertIn("job-interrupted", json.dumps(out))
        self.assertTrue(scratch.exists())

    def test_continuation_record_naming_no_job_keeps_the_scratch(self):
        repo = self.clone("site-j9004")
        self.add_job("J-9004", repo)
        self.interrupt("J-9004", finished_ago=4 * DAY)
        scratch = self.cache_subdir("J-9004")
        self.continue_with("J-9004", "J-9999")
        out = self.cli("reap", "--job", "J-9004", expect=2)
        self.assertIn("job-interrupted", json.dumps(out))
        self.assertTrue(scratch.exists())

    def test_interrupted_job_without_a_provable_interruption_keeps_its_scratch(self):
        """The status alone proves nothing: without the job's own interruption record the clock is unknown."""
        repo = self.clone("site-j9005")
        self.add_job("J-9005", repo)
        self.add_job("J-9006")
        self.finish("J-9006", 2 * DAY)
        stamp = time.time() - 4 * DAY
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET status='interrupted', created_at=?, started_at=?, finished_at=?, meta=NULL WHERE id=?",
                         (stamp - 3600, stamp - 60, stamp, "J-9005"))
        scratch = self.cache_subdir("J-9005")
        self.continue_with("J-9005", "J-9006")
        out = self.cli("reap", "--job", "J-9005", expect=2)
        self.assertIn("job-interrupted", json.dumps(out))
        self.assertTrue(scratch.exists())

    def test_interruption_event_is_a_provable_clock(self):
        """An older host records the interruption in the event log; the clock must match the job's own finish time."""
        repo = self.clone("site-j9007")
        self.add_job("J-9007", repo)
        self.add_job("J-9008")
        self.finish("J-9008", 2 * DAY)
        self.interrupt_with_event("J-9007", finished_ago=4 * DAY)
        scratch = self.cache_subdir("J-9007")
        self.continue_with("J-9007", "J-9008")
        out = self.cli("reap", "--job", "J-9007")
        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(scratch.exists())

    def test_continued_interrupted_job_releases_its_clone_after_the_clone_window(self):
        repo = self.clone("site-j9009")
        self.add_job("J-9009", repo)
        self.add_job("J-9010")
        self.finish("J-9010", 4 * DAY)
        self.interrupt("J-9009", finished_ago=6 * DAY)
        self.continue_with("J-9009", "J-9010")
        out = self.cli("reap", "--job", "J-9009")
        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(repo.exists())

    def test_a_clone_named_for_a_live_job_never_expires(self):
        """The clone is named for the continued interrupted job and for a live one: the live job keeps it."""
        repo = self.clone("site-j9011-j9012")
        self.add_job("J-9011", repo)
        self.add_job("J-9012", repo, status="running")
        self.interrupt("J-9011", finished_ago=6 * DAY)
        self.add_job("J-9013")
        self.finish("J-9013", 4 * DAY)
        self.continue_with("J-9011", "J-9013")
        out = self.cli("reap", "--job", "J-9011")
        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(repo.exists())

    def test_a_live_continuation_keeps_its_own_paths(self):
        """The continuation is a live job that works in the source's folder: that folder is still its claim."""
        repo = self.clone("site-j9014")
        self.add_job("J-9014", repo)
        self.interrupt("J-9014", finished_ago=6 * DAY)
        self.add_job("J-9015", repo, status="running")
        self.continue_with("J-9014", "J-9015")
        out = self.cli("reap", "--job", "J-9014", expect=2)
        self.assertIn("job-interrupted", json.dumps(out))
        self.assertTrue(repo.exists())

    def test_a_queued_continuation_keeps_the_source_artifacts(self):
        """A continuation that waits for approval has not worked yet: the source's folders are still its starting point."""
        repo = self.clone("site-j9016")
        self.add_job("J-9016", repo)
        self.interrupt("J-9016", finished_ago=4 * DAY)
        scratch = self.cache_subdir("J-9016")
        self.add_job("J-9017", status="queued")
        self.continue_with("J-9016", "J-9017")
        out = self.cli("reap", "--job", "J-9016", expect=2)
        self.assertIn("job-interrupted", json.dumps(out))
        self.assertTrue(scratch.exists())

    def test_a_chained_continuation_releases_the_source_after_the_whole_chain_finished(self):
        """Source -> an interrupted continuation -> a finished one: the source ages from the chain's last finish."""
        repo = self.clone("site-j9018")
        self.add_job("J-9018", repo)
        self.add_job("J-9019")
        self.add_job("J-9020")
        self.interrupt("J-9018", finished_ago=10 * DAY)
        self.interrupt("J-9019", finished_ago=5 * DAY)
        scratch = self.cache_subdir("J-9018")
        self.continue_with("J-9018", "J-9019")
        self.continue_with("J-9019", "J-9020")
        self.finish("J-9020", 2 * DAY)
        out = self.cli("reap", "--job", "J-9018")
        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(scratch.exists())

    def test_a_chain_that_finished_momently_ago_keeps_the_source(self):
        """The window runs from the chain's last finish: work that stopped an hour ago is not old enough to go."""
        repo = self.clone("site-j9021")
        self.add_job("J-9021", repo)
        self.add_job("J-9022")
        self.interrupt("J-9021", finished_ago=9 * DAY)
        scratch = self.cache_subdir("J-9021")
        self.continue_with("J-9021", "J-9022")
        self.finish("J-9022", 3600)
        out = self.cli("reap", "--job", "J-9021")
        self.assertEqual(out["deleted"], 0, out)
        self.assertIn("retention-preserved", json.dumps(out))
        self.assertTrue(scratch.exists())

    def test_a_continued_interrupted_job_that_worked_in_a_clone_extends_its_expiry(self):
        """The job that worked in the clone counts as a recent user of it, so the clone does not expire under it."""
        repo = self.clone("site-j9023")
        self.add_job("J-9023", repo, finished_ago=4 * DAY)
        self.add_job("J-9024", repo)
        self.add_job("J-9025")
        self.interrupt("J-9024", finished_ago=2 * DAY)
        self.continue_with("J-9024", "J-9025")
        self.finish("J-9025", 1 * DAY)
        out = self.cli("reap", "--job", "J-9023")
        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(repo.exists())

    # ---- the whole chain must have finished, whatever each job's status is ----------------------------------------

    def test_a_live_second_continuation_keeps_the_source(self):
        """Source -> an interrupted continuation -> a live one: the work is not finished."""
        repo = self.clone("site-j9026")
        self.add_job("J-9026", repo)
        self.add_job("J-9027")
        self.interrupt("J-9026", finished_ago=5 * DAY)
        self.interrupt("J-9027", finished_ago=3 * DAY)
        scratch = self.cache_subdir("J-9026")
        self.continue_with("J-9026", "J-9027")
        self.add_job("J-9028", status="running")
        self.continue_with("J-9027", "J-9028")
        out = self.cli("reap", "--job", "J-9026", expect=2)
        self.assertIn("job-interrupted", json.dumps(out))
        self.assertTrue(scratch.exists())

    def test_a_finished_second_continuation_releases_the_source(self):
        """The same chain, with the last link finished: the source is released."""
        repo = self.clone("site-j9026b")
        self.add_job("J-9026", repo)
        self.add_job("J-9027")
        self.interrupt("J-9026", finished_ago=5 * DAY)
        self.interrupt("J-9027", finished_ago=3 * DAY)
        scratch = self.cache_subdir("J-9026")
        self.continue_with("J-9026", "J-9027")
        self.add_job("J-9028")
        self.continue_with("J-9027", "J-9028")
        self.finish("J-9028", 2 * DAY)
        out = self.cli("reap", "--job", "J-9026")
        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(scratch.exists())

    def test_a_failed_job_redriven_into_a_live_child_keeps_the_source(self):
        """A failed job can be redriven: its child is queued, so the work it holds is not finished."""
        repo = self.clone("site-j9029")
        self.add_job("J-9026", repo)
        self.add_job("J-9030")
        self.interrupt("J-9026", finished_ago=4 * DAY)
        scratch = self.cache_subdir("J-9026")
        self.continue_with("J-9026", "J-9030")
        self.finish("J-9030", 2 * DAY)
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET status='failed' WHERE id=?", ("J-9030",))
        self.add_job("J-9031", status="queued")
        self.continue_with("J-9030", "J-9031")
        out = self.cli("reap", "--job", "J-9026", expect=2)
        self.assertIn("job-interrupted", json.dumps(out))
        self.assertTrue(scratch.exists())

    def test_a_done_job_with_a_live_handoff_successor_keeps_the_source(self):
        """A finished job can hand its scope to a successor: while that successor is live the source keeps everything."""
        repo = self.clone("site-j9032")
        self.add_job("J-9032", repo)
        self.add_job("J-9033")
        self.interrupt("J-9032", finished_ago=4 * DAY)
        scratch = self.cache_subdir("J-9032")
        self.continue_with("J-9032", "J-9033")
        self.finish("J-9033", 2 * DAY)
        self.scope_record("cleanup:successor:2b0f5f2e-0000-4000-8000-000000000000", "J-9033", "J-9034")
        self.add_job("J-9034", status="running")
        out = self.cli("reap", "--job", "J-9032", expect=2)
        self.assertIn("job-interrupted", json.dumps(out))
        self.assertTrue(scratch.exists())

    def test_a_chain_that_cycles_keeps_the_source(self):
        """A record that points back at the source proves nothing."""
        repo = self.clone("site-j9035")
        self.add_job("J-9035", repo)
        self.add_job("J-9036")
        self.interrupt("J-9035", finished_ago=4 * DAY)
        scratch = self.cache_subdir("J-9035")
        self.continue_with("J-9035", "J-9036")
        self.continue_with("J-9036", "J-9035")
        out = self.cli("reap", "--job", "J-9035", expect=2)
        self.assertIn("job-interrupted", json.dumps(out))
        self.assertTrue(scratch.exists())

    def test_a_continuation_without_a_finish_time_keeps_the_source(self):
        """A finished job with no finish time proves nothing."""
        repo = self.clone("site-j9037")
        self.add_job("J-9037", repo)
        self.add_job("J-9038")
        self.interrupt("J-9037", finished_ago=4 * DAY)
        scratch = self.cache_subdir("J-9037")
        self.continue_with("J-9037", "J-9038")
        self.finish("J-9038", 2 * DAY)
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET finished_at=NULL WHERE id=?", ("J-9038",))
        out = self.cli("reap", "--job", "J-9037", expect=2)
        self.assertIn("job-interrupted", json.dumps(out))
        self.assertTrue(scratch.exists())

    def test_a_zoned_iso_interruption_clock_is_proved(self):
        """The host's own interruption record may be a zoned ISO timestamp."""
        repo = self.clone("site-j9039")
        self.add_job("J-9039", repo)
        self.add_job("J-9040")
        self.finish("J-9040", 2 * DAY)
        self.interrupt("J-9039", finished_ago=4 * DAY)
        scratch = self.cache_subdir("J-9039")
        with contextlib.closing(self.state()) as conn, conn:
            stamp = time.time() - 2 * DAY
            conn.execute("UPDATE jobs SET meta=? WHERE id=?",
                         (json.dumps({"interrupted": {"at": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(stamp))}}), "J-9039"))
        self.continue_with("J-9039", "J-9040")
        out = self.cli("reap", "--job", "J-9039")
        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(scratch.exists())

    def test_an_unrelated_live_job_claiming_the_clone_keeps_it(self):
        """Another live job that declared the clone keeps it, whatever the source's own chain says."""
        repo = self.clone("site-j9041")
        self.add_job("J-9041", repo, finished_ago=4 * DAY)
        self.add_job("J-9042", repo, status="running")
        out = self.cli("reap", "--job", "J-9041")
        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(repo.exists())

    # ---- the records the host writes, in the shape it writes them ---------------------------------------------------

    def test_a_successor_record_keyed_by_the_terminal_id_keeps_the_source(self):
        """The host keys the scope record by the source's terminal id, not by the job id: it is found by the job it names."""
        repo = self.clone("site-j9043")
        self.add_job("J-9043", repo)
        self.add_job("J-9044")
        self.interrupt("J-9043", finished_ago=4 * DAY)
        scratch = self.cache_subdir("J-9043")
        self.continue_with("J-9043", "J-9044")
        self.scope_record("cleanup:successor:2b0f5f2e-0000-4000-8000-000000000000", "J-9043", "J-9044")
        out = self.cli("reap", "--job", "J-9043", expect=2)
        self.assertIn("job-interrupted", json.dumps(out))
        self.assertTrue(scratch.exists())

    def test_a_released_scope_with_a_live_successor_keeps_the_source(self):
        """The heir that released the scope may still be working: the chain follows it anyway.

        The live successor is reachable only through the scope record of the finished middle link, so this test fails if a
        released scope ends the chain."""
        repo = self.clone("site-j9045")
        self.add_job("J-9045", repo)
        self.add_job("J-9046")
        self.interrupt("J-9045", finished_ago=4 * DAY)
        scratch = self.cache_subdir("J-9045")
        self.continue_with("J-9045", "J-9046")
        self.finish("J-9046", 2 * DAY)
        self.scope_record("cleanup:successor:2b0f5f2e-0000-4000-8000-000000000001", "J-9046", "J-9047")
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                         ("cleanup:released:J-9046", json.dumps({"by": "J-9047"})))
        self.add_job("J-9047", status="running")
        out = self.cli("reap", "--job", "J-9045", expect=2)
        self.assertIn("job-interrupted", json.dumps(out))
        self.assertTrue(scratch.exists())

    def test_a_record_that_points_at_itself_keeps_the_source(self):
        repo = self.clone("site-j9047")
        self.add_job("J-9047", repo)
        self.interrupt("J-9047", finished_ago=4 * DAY)
        # a record that points at the job itself: the chain cannot run, whatever the order check says
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET created_at=started_at WHERE id=?", ("J-9047",))
        scratch = self.cache_subdir("J-9047")
        self.continue_with("J-9047", "J-9047")
        out = self.cli("reap", "--job", "J-9047", expect=2)
        self.assertIn("job-interrupted", json.dumps(out))
        self.assertTrue(scratch.exists())

    def test_a_chain_deeper_than_the_cap_keeps_the_source(self):
        repo = self.clone("site-j9048")
        self.add_job("J-9048", repo)
        self.interrupt("J-9048", finished_ago=30 * DAY)
        scratch = self.cache_subdir("J-9048")
        previous = "J-9048"
        last = None
        for index in range(12):
            job = f"J-90{50 + index}"
            self.add_job(job)
            # each link is created after the job it continues started, so the chain runs to its own depth cap
            stamp = time.time() - (20 - index) * DAY
            with contextlib.closing(self.state()) as conn, conn:
                conn.execute("UPDATE jobs SET status='interrupted', created_at=?, started_at=?, finished_at=?, meta=? WHERE id=?",
                             (stamp - 3600, stamp - 60, stamp, json.dumps({"interrupted": {"at": stamp}}), job))
            self.continue_with(previous, job)
            previous, last = job, job
        # the last link has finished: only the depth cap can stop this chain
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET status='done', created_at=?, started_at=?, finished_at=? WHERE id=?",
                         (time.time() - 3 * DAY - 60, time.time() - 3 * DAY - 30, time.time() - 3 * DAY, last))
        out = self.cli("reap", "--job", "J-9048", expect=2)
        self.assertIn("job-interrupted", json.dumps(out))
        self.assertTrue(scratch.exists())

    def test_a_chain_within_the_cap_is_released(self):
        """The same chain, one link shorter: the depth cap is not what stopped it."""
        repo = self.clone("site-j9049")
        self.add_job("J-9049", repo)
        self.interrupt("J-9049", finished_ago=20 * DAY)
        scratch = self.cache_subdir("J-9049")
        previous = "J-9049"
        for index in range(6):
            job = f"J-90{70 + index}"
            self.add_job(job)
            stamp = time.time() - (15 - index) * DAY
            with contextlib.closing(self.state()) as conn, conn:
                conn.execute("UPDATE jobs SET status='interrupted', created_at=?, started_at=?, finished_at=?, meta=? WHERE id=?",
                             (stamp - 3600, stamp - 60, stamp, json.dumps({"interrupted": {"at": stamp}}), job))
            self.continue_with(previous, job)
            previous = job
        last = f"J-90{70 + 5}"
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET status='done', created_at=?, started_at=?, finished_at=? WHERE id=?",
                         (time.time() - 3 * DAY - 60, time.time() - 3 * DAY - 30, time.time() - 3 * DAY, last))
        out = self.cli("reap", "--job", "J-9049")
        self.assertGreaterEqual(out["deleted"], 1, out)
        self.assertFalse(scratch.exists())

    def test_a_finish_time_that_is_not_a_number_keeps_the_source(self):
        repo = self.clone("site-j9062")
        self.add_job("J-9062", repo)
        self.add_job("J-9063")
        self.interrupt("J-9062", finished_ago=4 * DAY)
        scratch = self.cache_subdir("J-9062")
        self.continue_with("J-9062", "J-9063")
        self.finish("J-9063", 2 * DAY)
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET finished_at='yesterday' WHERE id=?", ("J-9063",))
        out = self.cli("reap", "--job", "J-9062", expect=2)
        self.assertIn("job-interrupted", json.dumps(out))
        self.assertTrue(scratch.exists())

    # ---- the scope records themselves ------------------------------------------------------------------------------

    def test_an_unrelated_successor_record_does_not_stop_the_release(self):
        """A well-formed record about another job proves nothing about this one."""
        repo = self.clone("site-j9064")
        self.add_job("J-9064", repo)
        self.add_job("J-9065")
        self.finish("J-9065", 2 * DAY)
        self.interrupt("J-9064", finished_ago=4 * DAY)
        scratch = self.cache_subdir("J-9064")
        self.continue_with("J-9064", "J-9065")
        self.scope_record("cleanup:successor:2b0f5f2e-0000-4000-8000-000000000002", "J-9066", "J-9067")
        out = self.cli("reap", "--job", "J-9064")
        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(scratch.exists())

    def test_a_finished_successor_in_a_scope_record_releases_the_source(self):
        """The scope record names a successor that has finished: the chain ends there and the source goes."""
        repo = self.clone("site-j9068")
        self.add_job("J-9068", repo)
        self.add_job("J-9069")
        self.add_job("J-9070")
        self.interrupt("J-9068", finished_ago=4 * DAY)
        scratch = self.cache_subdir("J-9068")
        self.continue_with("J-9068", "J-9069")
        self.finish("J-9069", 3 * DAY)
        self.finish("J-9070", 2 * DAY)
        self.scope_record("cleanup:successor:2b0f5f2e-0000-4000-8000-000000000003", "J-9069", "J-9070")
        out = self.cli("reap", "--job", "J-9068")
        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(scratch.exists())

    def test_an_unreadable_successor_record_keeps_the_source(self):
        """A record that cannot be read proves nothing: the source keeps everything."""
        repo = self.clone("site-j9071")
        self.add_job("J-9071", repo)
        self.add_job("J-9072")
        self.finish("J-9072", 2 * DAY)
        self.interrupt("J-9071", finished_ago=4 * DAY)
        scratch = self.cache_subdir("J-9071")
        self.continue_with("J-9071", "J-9072")
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                         ("cleanup:successor:2b0f5f2e-0000-4000-8000-000000000004", "{not json"))
        out = self.cli("reap", "--job", "J-9071", expect=2)
        self.assertIn("job-interrupted", json.dumps(out))
        self.assertTrue(scratch.exists())
