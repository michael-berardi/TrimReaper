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
            if "summary" not in [r[1] for r in conn.execute("PRAGMA table_info(jobs)")]:
                conn.execute("ALTER TABLE jobs ADD COLUMN summary TEXT")  # the UltraTerm host's job store carries the verdict

    def test_only_allowed_sources_conclude_when_the_host_names_them(self):
        self.job("J-0111", terminal="t-11")
        self.source_column()
        policy = {"evidenceExpiryDays": 14, "cloneExpiryDays": 3, "concludedSources": ["verifier:", "verifier-done:"]}
        self.write_config({"policy": policy})
        for source, concluded in (("rejected:J-0111:J-0112", False), ("policy:review-deferred:J-0111", False),
                                  ("parked:J-0111", False), (None, False), ("verifier-done:J-0112", True)):
            with self.subTest(source=source):
                with contextlib.closing(self.state()) as conn, conn:
                    conn.execute("UPDATE cleanup_records SET source=?, job_id='J-0111' WHERE terminal_id='t-11'", (source,))
                self.assertEqual(self.concluded("J-0111"), concluded)

    def test_another_jobs_record_on_a_shared_terminal_does_not_conclude(self):
        self.job("J-0113", terminal="t-13")
        self.source_column()
        self.write_config({"policy": {"evidenceExpiryDays": 14, "cloneExpiryDays": 3, "concludedSources": ["verifier-done:"]}})
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE cleanup_records SET source='verifier-done:J-0200' WHERE terminal_id='t-13'")
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
        self.add_job("J-0121", status="done")
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET summary='verified: ok' WHERE id='J-0121'")
        with mock.patch.object(core, "reap_policy", return_value={"concludedSources": ["verifier:"]}):
            self.assertTrue(self.concluded("J-0120"))

    def test_nested_release_output_keeps_the_clone(self):
        self.job("J-0122")
        clone = self.home / "dev" / "mono-J-0122"
        (clone / "packages" / "app" / "dist").mkdir(parents=True)
        (clone / "packages" / "app" / "dist" / "App-2.0.0.dmg").write_bytes(b"x")
        self.assertFalse(core.reap_clone_expired(["J-0122"], str(clone)))

    def test_a_verifier_source_needs_that_verifiers_accepting_done_report(self):
        self.job("J-0130", terminal="t-30")
        self.source_column()
        self.write_config({"policy": {"evidenceExpiryDays": 14, "cloneExpiryDays": 3, "concludedSources": ["verifier:"]}})
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE cleanup_records SET source='verifier:J-0131 checked it', job_id='J-0130' WHERE terminal_id='t-30'")
        self.assertFalse(self.concluded("J-0130"))  # verifier unknown
        self.add_job("J-0131", status="running")
        self.assertFalse(self.concluded("J-0130"))  # still running: no verdict yet
        for summary, concluded in (("rejected: two findings open", False), ("verified: all checks pass", True),
                                   ("accepted J-0130 after checking its report", True), ("checked it; rejected for now", False)):
            with self.subTest(summary=summary):
                with contextlib.closing(self.state()) as conn, conn:
                    conn.execute("UPDATE jobs SET status='done', summary=?, finished_at=? WHERE id='J-0131'", (summary, time.time() - 60))
                self.assertEqual(self.concluded("J-0130"), concluded)

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


class JobSweepOrder(Home):
    """0.24.4: the clean pass's job sweep looks only at jobs that still hold something, newest first, so the time budget is spent
    where the space is (4,600 finished jobs used to exhaust it before any job holding space was reached)."""

    def test_sweep_skips_empty_jobs_and_starts_with_the_newest(self):
        from trimreaper import clean
        for n in range(1, 40):
            self.add_job(f"J-{n:04d}", finished_ago=(50 - n) * 86400)
        holder = self.home / ".cache/trimreaper/jobs/J-0039"
        (holder / "out").mkdir(parents=True)
        seen = []
        real = core.reap_job_finished_clock
        def spy(conn, job):
            seen.append(job["id"])
            return real(conn, job)
        rung = {"job": {"hours": 1, "filter": "all", "pinned": False, "clones": True, "keepUnsaved": True}}
        with mock.patch.object(clean, "reap_job_finished_clock", side_effect=spy), \
                mock.patch.object(clean, "reap_live_snapshot", return_value=(set(), {})):
            clean.clean_job_sweep(rung, True, "run-test")
        self.assertEqual(seen[:1], ["J-0039"])
        self.assertNotIn("J-0001", seen)


class CacheWorktreeOwner(Home):
    """0.24.4: a worktree at <cache root>/J-NNNN/<name> is judged by that job, not the 1-day window its own name implies."""

    def test_job_folder_names_the_worktrees_job(self):
        from trimreaper import clean
        root = core.reap_cache_root()
        path = str(root / "J-0201" / "wt")
        seen = []
        with mock.patch.object(clean, "clean_jobs_finished", side_effect=lambda ids, p: seen.append(list(ids)) or False), \
                mock.patch.object(clean, "clean_idle_days", return_value=0.0), \
                mock.patch.object(clean, "reap_cache_retention", return_value="unmarked"), \
                mock.patch.object(clean, "reap_vet_root", return_value=None):
            with self.assertRaises(core.ReapRefused):
                clean.clean_worktree_gates({"path": path, "gitdir": None, "git": []}, 1.0, {})
        self.assertEqual(seen, [["J-0201"]])
        seen.clear()
        other = str(self.home / "dev" / "J-0202" / "wt")  # any other place: an ancestor's name never names the job
        with mock.patch.object(clean, "clean_jobs_finished", side_effect=lambda ids, p: seen.append(list(ids)) or False), \
                mock.patch.object(clean, "clean_idle_days", return_value=0.0), \
                mock.patch.object(clean, "reap_cache_retention", return_value="unmarked"), \
                mock.patch.object(clean, "reap_vet_root", return_value=None):
            with self.assertRaises(core.ReapRefused):
                clean.clean_worktree_gates({"path": other, "gitdir": None, "git": []}, 1.0, {})
        self.assertEqual(seen, [])


class SwapInventory(Home):
    """0.24.4: swap is reported with the processes behind it, since no folder rule can free it."""

    def test_large_swap_names_the_holder(self):
        from trimreaper import clean
        outputs = {"sysctl": (0, "total = 66560.00M  used = 65893.94M  free = 666.06M  (encrypted)"),
                   "top": (0, "PID    COMMAND          MEM   CMPRS\n869    fseventsd        78G   74G\n937    WindowServer     1107M 240M\n")}
        with mock.patch.object(clean, "clean_run", side_effect=lambda cmd, t: outputs[cmd[0]]), \
                mock.patch.object(clean.os, "uname", return_value=mock.Mock(sysname="Darwin")):
            entry = clean.clean_swap_inventory()
        self.assertEqual(entry["warning"], "swap-large")
        self.assertEqual(entry["holders"][0]["command"], "fseventsd")
        self.assertIn("fseventsd (pid 869)", entry["note"])
        self.assertGreater(entry["bytes"], 60 * 1024 ** 3)

    def test_small_swap_is_reported_without_a_warning(self):
        from trimreaper import clean
        with mock.patch.object(clean, "clean_run", return_value=(0, "total = 3072.00M  used = 2315.06M  free = 756.94M")), \
                mock.patch.object(clean.os, "uname", return_value=mock.Mock(sysname="Darwin")):
            entry = clean.clean_swap_inventory()
        self.assertNotIn("warning", entry)
