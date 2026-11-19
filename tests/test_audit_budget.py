"""0.20.2: the audit budget is a policy key that escalates after an incomplete audit, agent scratch keeps a reserved share of it (jobs were
audited first and spent all 60 s, so 154 of 157 scratch folders reported 0 bytes and `audit-budget-exhausted`), and a size that timed out
is reported as a lower bound, never as 0 bytes. Synthetic temporary homes only."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest import mock

from trimreaper import core

from helpers import Home


class BudgetPolicy(Home):
    core_disk_percent = 50

    def test_the_budget_is_a_policy_key_with_a_default_of_sixty(self):
        self.assertEqual(core.reap_audit_budget(), 60)
        self.write_config({"policy": {"reapAuditSeconds": 90}})
        self.assertEqual(core.reap_audit_budget(), 90)
        self.write_config({"policy": {"reapAuditSeconds": 0}})
        self.cli("status", expect=2)
        self.write_config({"policy": {"reapAuditSeconds": "60"}})
        self.cli("status", expect=2)

    def test_a_named_budget_wins_and_an_explicit_status_never_escalates(self):
        self.write_config({"policy": {"reapAuditSeconds": 30}})
        self.cli("job", "add", "J-0701", "--status", "done")
        with mock.patch.object(core, "reap_audit", wraps=core.reap_audit) as audit:
            self.cli("status", "--budget", "7")
            self.cli("status")
        self.assertEqual([c.args[3] for c in audit.call_args_list], [7, 30])

    def test_an_incomplete_tick_audit_doubles_the_next_budget_up_to_eight_times_and_a_complete_one_resets_it(self):
        self.write_config({"policy": {"reapAuditSeconds": 10}})
        for number in range(3):
            job = f"J-070{number + 2}"
            self.cli("job", "add", job, "--status", "running")
            (self.home / ".cache/trimreaper/jobs" / job / "scratch").mkdir(parents=True)
        real = core.reap_audit_item

        def slow(*args, **kwargs):
            time.sleep(0.4)
            return real(*args, **kwargs)

        factors = []
        for _ in range(5):
            with mock.patch.object(core, "reap_audit_item", slow):
                core.reap_audit(None, 10, False, 1, persist=True)  # a budget of one second ends before every job is reached
            with core.db() as conn:
                factors.append(core.reap_audit_cursor(conn)["budgetFactor"])
            core.REAP_AUDIT_PERSIST.on = True
            try:
                self.assertEqual(core.reap_audit_budget(True), 10 * factors[-1])
            finally:
                core.REAP_AUDIT_PERSIST.on = False
        self.assertEqual(factors, [2, 4, 8, 8, 8])
        core.reap_audit(None, 10, False, 60, persist=True)  # complete
        with core.db() as conn:
            self.assertEqual(core.reap_audit_cursor(conn)["budgetFactor"], 1)
        self.assertEqual(core.reap_audit_budget(False), 10)  # the explicit status keeps the plain value


class ScratchShare(Home):
    config_extra = {"scratchRoot": "~/.cache/agent-scratch"}
    core_disk_percent = 50

    def folders(self, count: int) -> list[Path]:
        root = self.home / ".cache" / "agent-scratch"
        found = []
        for number in range(count):
            self.put(root / f"task-{number:03d}", "notes.txt", b"y" * 5000)
            found.append(root / f"task-{number:03d}")
        return found

    def test_jobs_cannot_spend_the_whole_budget_before_scratch(self):
        """Scratch is audited first, inside its share of the budget; the worktrees that follow cannot starve it."""
        self.folders(6)
        for number in range(3):
            job = f"J-071{number}"
            self.cli("job", "add", job, "--status", "running")
            (self.home / ".cache/trimreaper/jobs" / job / "wt").mkdir(parents=True)
        real = core.reap_audit_item

        def heavy(item, blocker, ctx, deadline, *rest):
            if item["class"] != "agent-scratch":  # a worktree that sizes for as long as it is allowed (up to 10 s)
                time.sleep(max(0.0, min(10.0, deadline - time.monotonic())))
            return real(item, blocker, ctx, deadline, *rest)

        with mock.patch.object(core, "reap_audit_item", heavy):
            out = core.reap_audit(None, 10, True, 5)  # the jobs below outlast the whole budget; scratch was audited before them
        reasons = [e["blocking"] for e in out["agentScratch"]]
        self.assertEqual(len(reasons), 6)
        self.assertNotIn("audit-budget-exhausted", reasons, reasons)
        self.assertTrue(all(e["bytes"] for e in out["agentScratch"]), out["agentScratch"])

    def test_each_remaining_folder_gets_a_share_of_what_is_left_not_all_of_it(self):
        self.folders(4)
        seen = []
        real = core.reap_metadata_size

        def spy(path, seconds=120, *args, **kwargs):
            seen.append(seconds)
            return real(path, seconds, *args, **kwargs)

        with mock.patch.object(core, "reap_metadata_size", spy):
            core.reap_audit(None, 10, True, 40)
        # scratch has 40% of the 40 s budget: the first of four folders gets a quarter of that, never all of it (a quick folder hands its
        # time on to the rest)
        self.assertTrue(len(seen) == 4 and seen[0] <= 4.5 and max(seen) <= 16.0, seen)


class LowerBound(Home):
    core_disk_percent = 50
    config_extra = {"scratchRoot": "~/.cache/agent-scratch"}

    def item(self) -> dict:
        folder = self.home / ".cache" / "agent-scratch" / "big"
        self.put(folder, "a.bin", b"z" * 9000)
        return {"path": str(folder), "root": str(folder.parent), "owner": "agent-scratch", "jobs": [], "class": "agent-scratch",
                "kind": "agent-scratch", "ownership": "agent-scratch-dir", "retention": "unmarked"}

    def test_a_size_that_timed_out_is_reported_as_a_lower_bound_never_as_zero(self):
        item = self.item()
        partial = {"bytes": 123456, "complete": False, "reason": "size-time-limit"}
        with mock.patch.object(core, "reap_metadata_size", return_value=partial):
            entry = core.reap_audit_item(item, "job-parked", {"run": "audit"}, time.monotonic() + 30)
        self.assertEqual(entry["bytes"], 123456)
        self.assertTrue(entry["sizeIncomplete"] and entry["bytesLowerBound"], entry)
        self.assertEqual(entry["sizeReason"], "size-time-limit")
        categories, reasons = {}, {}
        core.reap_bucket(categories, reasons, entry)
        self.assertEqual(categories["agent-scratch"]["bytes"], 123456)
        self.assertEqual(categories["agent-scratch"]["lowerBoundItems"], 1)

    def test_a_folder_past_the_deadline_gets_no_guaranteed_second_and_keeps_a_lower_bound(self):
        item = self.item()
        seen = []

        def spy(path, seconds=120, *args, **kwargs):
            seen.append(seconds)
            return {"bytes": 777, "complete": False, "reason": "size-time-limit"}

        with mock.patch.object(core, "reap_metadata_size", spy):
            entry = core.reap_audit_item(item, "job-running", {"run": "audit"}, time.monotonic() - 5)
            core.reap_audit_item(item, "job-running", {"run": "audit", "hardDeadline": time.monotonic() - 1}, time.monotonic() + 30)
        self.assertLessEqual(seen[0], core.REAP_AUDIT_FLOOR_SECONDS)   # a token amount, not the second that was promised before
        self.assertEqual(seen[1], 0.0)                                  # nothing at all past the hard ceiling
        self.assertEqual((entry["bytes"], entry["bytesLowerBound"]), (777, True))

    def test_a_share_is_never_longer_than_what_the_hard_ceiling_leaves(self):
        ctx = {"hardDeadline": time.monotonic() + 2.0}
        self.assertLessEqual(core.reap_audit_share(ctx, time.monotonic() + 60, 1), 2.0)
        self.assertEqual(core.reap_audit_share({}, time.monotonic() - 3, 4), core.REAP_AUDIT_FLOOR_SECONDS)
        self.assertEqual(core.reap_audit_share({"hardDeadline": time.monotonic() - 1}, time.monotonic() + 30, 4), 0.0)

    def test_a_job_cache_folder_is_never_floored_to_zero_when_its_listed_items_hold_more_than_it(self):
        folder = "/x/J-1/wt"
        entries = [{"class": "job-cache-subdir", "path": folder, "bytes": 8192},
                   {"class": "job-clone-regenerable", "path": folder + "/node_modules", "bytes": 20000}]   # a stale, larger inner size
        core.reap_audit_net(entries)
        self.assertEqual(entries[0]["bytes"], 1)
        self.assertTrue(entries[0]["bytesLowerBound"])
        sound = [{"class": "job-cache-subdir", "path": folder, "bytes": 30000},
                 {"class": "job-clone-regenerable", "path": folder + "/node_modules", "bytes": 20000}]
        core.reap_audit_net(sound)
        self.assertEqual(sound[0]["bytes"], 10000)
        self.assertNotIn("bytesLowerBound", sound[0])

    def test_a_complete_size_is_not_marked(self):
        item = self.item()
        entry = core.reap_audit_item(item, "job-running", {"run": "audit"}, time.monotonic() + 30)
        self.assertGreater(entry["bytes"], 0)
        self.assertNotIn("bytesLowerBound", entry)

    def test_the_summary_says_when_the_unreaped_total_is_a_lower_bound(self):
        self.item()
        self.cli("job", "add", "J-0720", "--status", "running")
        out = self.cli("status")
        self.assertFalse(out["summary"]["unreapedBytesIsLowerBound"])
        partial = {"bytes": 5, "complete": False, "reason": "size-time-limit"}
        with mock.patch.object(core, "reap_metadata_size", return_value=partial):
            self.assertTrue(self.cli("status")["summary"]["unreapedBytesIsLowerBound"])


class TickBounds(Home):
    """Through the entry point the tick's sizing pass uses (tool_reap_status with the persist flag the host sets), not the helpers."""
    config_extra = {"scratchRoot": "~/.cache/agent-scratch"}
    core_disk_percent = 50

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, core.REAP_AUDIT_PERSIST, "on", False)

    def build(self, scratch: int, jobs: int):
        root = self.home / ".cache" / "agent-scratch"
        for number in range(scratch):
            self.put(root / f"task-{number:03d}", "notes.txt", b"y" * 5000)
        for number in range(jobs):
            job = f"J-09{number:02d}"
            self.cli("job", "add", job, "--status", "running")
            for part in range(3):
                self.put(self.home / ".cache/trimreaper/jobs" / job / f"part{part}", "a.bin", b"z" * 5000)

    def tick_status(self) -> dict:
        core.REAP_AUDIT_PERSIST.on = True
        try:
            return core.tool_reap_status({"items": True})
        finally:
            core.REAP_AUDIT_PERSIST.on = False

    def test_the_whole_audit_stays_inside_its_hard_ceiling_even_with_every_size_slow(self):
        self.write_config({"policy": {"reapAuditSeconds": 5}, "scratchRoot": "~/.cache/agent-scratch"})
        self.build(scratch=30, jobs=6)

        def slow(path, seconds=120, *args, **kwargs):
            time.sleep(min(seconds, 0.4))   # every folder would like 0.4 s: 30 + 18 of them, far more than the budget
            return {"bytes": 4096, "complete": False, "reason": "size-time-limit"}

        began = time.monotonic()
        with mock.patch.object(core, "reap_metadata_size", slow):
            out = self.tick_status()
        elapsed = time.monotonic() - began
        self.assertLess(elapsed, 5 * core.REAP_AUDIT_OVERRUN + 1.0, elapsed)   # the 1.0 s a folder was promised after the deadline would be 48 s
        self.assertTrue(out["summary"]["unreapedBytesIsLowerBound"])

    def test_scratch_and_job_audits_each_keep_a_share_so_neither_starves_the_other(self):
        self.write_config({"policy": {"reapAuditSeconds": 10}, "scratchRoot": "~/.cache/agent-scratch"})
        self.build(scratch=12, jobs=3)
        real = core.reap_audit_item

        def greedy(item, blocker, ctx, deadline, *rest):
            time.sleep(core.reap_audit_share(ctx, deadline, rest[0] if rest else 1))   # every item spends all it is offered
            return real(item, blocker, ctx, deadline, *rest)

        began = time.monotonic()
        with mock.patch.object(core, "reap_audit_item", greedy):
            out = self.tick_status()
        self.assertLess(time.monotonic() - began, 10 * core.REAP_AUDIT_OVERRUN + 1.5)
        self.assertTrue(all(e["bytes"] for e in out["agentScratch"]) and len(out["agentScratch"]) == 12,
                        [(e["state"], e["blocking"], e["bytes"], e.get("sizeReason")) for e in out["agentScratch"]])
        fresh = [j for j in out["jobs"] if not j.get("cached")]
        self.assertGreaterEqual(len(fresh), 1, out["jobs"])                     # scratch ran first and did not starve the job audits
        self.assertTrue(all(j["bytes"] for j in fresh))

    def test_the_tick_budget_is_capped_at_the_absolute_maximum_whatever_the_policy_says(self):
        self.write_config({"policy": {"reapAuditSeconds": 3600}, "scratchRoot": "~/.cache/agent-scratch"})
        with core.db() as conn:
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (core.REAP_AUDIT_CURSOR, json.dumps({"budgetFactor": 8, "complete": False})))
        with mock.patch.object(core, "reap_audit", wraps=core.reap_audit) as audit:
            self.tick_status()
        self.assertEqual(audit.call_args.args[3], core.REAP_AUDIT_MAX_SECONDS)
        self.assertEqual(core.REAP_AUDIT_MAX_SECONDS, 480)
        self.write_config({"policy": {"reapAuditSeconds": 100}, "scratchRoot": "~/.cache/agent-scratch"})
        with core.db() as conn:   # the first audit completed and reset the factor: raise it again
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (core.REAP_AUDIT_CURSOR, json.dumps({"budgetFactor": 8, "complete": False})))
        with mock.patch.object(core, "reap_audit", wraps=core.reap_audit) as audit:
            self.tick_status()
        self.assertEqual(audit.call_args.args[3], 480)                          # 100 x 8 = 800, capped
        core.REAP_AUDIT_PERSIST.on = False
        with mock.patch.object(core, "reap_audit", wraps=core.reap_audit) as audit:
            core.tool_reap_status({})
        self.assertEqual(audit.call_args.args[3], 100)                          # control: an explicit status never escalates
