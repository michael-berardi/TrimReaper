"""A job whose work is conclusively over loses its folders at once (0.24.0, `concludedExpiry`).

Finished jobs' worktrees, clones and cache folders used to wait cloneExpiryDays (3) or evidenceExpiryDays (14) even after a verifier had
accepted or rejected the job and its worker was closed: 30 GB of them sat on a 90%-full disk. Concluded = done/failed/cancelled,
settled, its worker terminal's cleanup record `closed`, no live continuation. Anything short of that keeps the old windows.
"""
from __future__ import annotations

import contextlib
import time

from unittest import mock

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
                             (terminal, record, time.time() - 1800))

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

    def test_live_repair_continuation_or_dependant_keeps_the_windows(self):
        import json
        for key, value in (("repairFor", "J-0109"), ("continuationOf", "J-0109"), ("dependsOn", ["J-0109"])):
            with self.subTest(key=key):
                self.job("J-0109")
                self.assertTrue(self.concluded("J-0109"))
                self.add_job("J-0110", status="running")
                with contextlib.closing(self.state()) as conn, conn:
                    conn.execute("UPDATE jobs SET meta=? WHERE id='J-0110'", (json.dumps({key: value}),))
                self.assertFalse(self.concluded("J-0109"))
                self.assertFalse(core.reap_clone_expired(["J-0109"]))
                with contextlib.closing(self.state()) as conn, conn:
                    conn.execute("UPDATE jobs SET status='done', finished_at=? WHERE id='J-0110'", (time.time() - 60,))
                self.assertTrue(self.concluded("J-0109"))
                with contextlib.closing(self.state()) as conn, conn:
                    conn.execute("DELETE FROM jobs WHERE id IN ('J-0109','J-0110')")
                    conn.execute("DELETE FROM cleanup_records WHERE terminal_id='t-1'")

    def source_column(self):
        with contextlib.closing(self.state()) as conn, conn:
            columns = [r[1] for r in conn.execute("PRAGMA table_info(cleanup_records)")]
            for name in ("source", "job_id"):
                if name not in columns:
                    conn.execute(f"ALTER TABLE cleanup_records ADD COLUMN {name} TEXT")

    def test_only_allowed_sources_conclude_when_the_host_names_them(self):
        self.job("J-0111", terminal="t-11")
        self.source_column()
        policy = {"evidenceExpiryDays": 14, "cloneExpiryDays": 3, "concludedSources": ["verifier:", "verifier-done:"]}
        self.write_config({"policy": policy})
        for source, concluded in (("rejected:J-0111:J-0112", False), ("policy:review-deferred:J-0111", False),
                                  ("parked:J-0111", False), (None, False), ("verifier-done:J-0112", True), ("verifier:J-0112", True)):
            with self.subTest(source=source):
                with contextlib.closing(self.state()) as conn, conn:
                    conn.execute("UPDATE cleanup_records SET source=?, job_id='J-0111' WHERE terminal_id='t-11'", (source,))
                self.assertEqual(self.concluded("J-0111"), concluded)

    def test_another_jobs_record_on_a_shared_terminal_does_not_conclude(self):
        self.job("J-0113", terminal="t-13")
        self.source_column()
        self.write_config({"policy": {"evidenceExpiryDays": 14, "cloneExpiryDays": 3, "concludedSources": ["verifier:"]}})
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE cleanup_records SET source='verifier:J-0200' WHERE terminal_id='t-13'")
        self.assertFalse(self.concluded("J-0113"))  # a record naming no job says nothing about this one
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE cleanup_records SET job_id='J-0999' WHERE terminal_id='t-13'")
        self.assertFalse(self.concluded("J-0113"))
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE cleanup_records SET job_id='J-0113' WHERE terminal_id='t-13'")
        self.assertTrue(self.concluded("J-0113"))

    def test_a_store_with_sources_and_no_allowlist_concludes_nothing(self):
        self.job("J-0120", terminal="t-20")
        self.source_column()
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE cleanup_records SET source='verifier:J-0121', job_id='J-0120' WHERE terminal_id='t-20'")
        self.assertFalse(self.concluded("J-0120"))
        for bad in ("verifier:", [], [""], [1]):
            with self.subTest(bad=bad):
                with mock.patch.object(core, "reap_policy", return_value={"concludedSources": bad}):
                    self.assertFalse(self.concluded("J-0120"))
        with mock.patch.object(core, "reap_policy", return_value={"concludedSources": ["verifier:"]}):
            self.assertTrue(self.concluded("J-0120"))

    def test_nested_release_output_keeps_the_clone(self):
        self.job("J-0122")
        clone = self.home / "dev" / "mono-J-0122"
        (clone / "packages" / "app" / "dist").mkdir(parents=True)
        (clone / "packages" / "app" / "dist" / "App-2.0.0.dmg").write_bytes(b"x")
        self.assertFalse(core.reap_clone_expired(["J-0122"], str(clone)))

    def test_a_fresh_close_has_to_settle_too(self):
        self.job("J-0114", terminal="t-14")
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE cleanup_records SET recorded_at=? WHERE terminal_id='t-14'", (time.time() - 30,))
        self.assertFalse(self.concluded("J-0114"))

    def test_dependson_as_a_string_counts(self):
        import json
        self.job("J-0115")
        self.add_job("J-0116", status="running")
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET meta=? WHERE id='J-0116'", (json.dumps({"dependsOn": "J-0115"}),))
        self.assertFalse(self.concluded("J-0115"))

    def test_a_clone_with_unshipped_release_output_waits_for_the_window(self):
        self.job("J-0117")
        clone = self.home / "dev" / "app-J-0117"
        (clone / "dist").mkdir(parents=True)
        (clone / "dist" / "App-1.2.0.dmg").write_bytes(b"x")
        self.assertTrue(core.reap_clone_expired(["J-0117"]))
        self.assertFalse(core.reap_clone_expired(["J-0117"], str(clone)))
        (clone / "dist" / "App-1.2.0.dmg").unlink()
        (clone / "package.json").write_text('{"scripts": {"build": "vite build"}}')
        (clone / "dist" / "index.html").write_text("<p>")
        self.assertTrue(core.reap_clone_expired(["J-0117"], str(clone)))

    def test_dist_rule_keeps_the_real_window(self):
        self.job("J-0118")
        self.assertFalse(core.reap_clone_expired(["J-0118"], concluded=False))
        self.assertTrue(core.reap_clone_expired(["J-0118"]))

    def test_archive_evidence_cutoff_ignores_conclusion(self):
        self.job("J-0119")
        self.assertTrue(core.reap_jobs_finished_before(["J-0119"], 14 * 86400))
        self.assertFalse(core.reap_jobs_finished_before(["J-0119"], 14 * 86400, concluded=False))

    def test_policy_switch_turns_it_off(self):
        self.job("J-0107")
        self.write_config({"policy": {"evidenceExpiryDays": 14, "cloneExpiryDays": 3, "concludedExpiry": False}})
        self.assertFalse(self.concluded("J-0107"))
        self.assertFalse(core.reap_clone_expired(["J-0107"]))

    def test_old_windows_still_apply_without_conclusion(self):
        self.job("J-0108", finished_ago=4 * 86400, record=None)
        self.assertTrue(core.reap_clone_expired(["J-0108"]))
        self.assertFalse(core.reap_scratch_expired("J-0108"))
