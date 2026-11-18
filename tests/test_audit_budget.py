"""0.20.2: the audit budget is a policy key that escalates after an incomplete audit, agent scratch keeps a reserved share of it (jobs were
audited first and spent all 60 s, so 154 of 157 scratch folders reported 0 bytes and `audit-budget-exhausted`), and a size that timed out
is reported as a lower bound, never as 0 bytes. Synthetic temporary homes only."""
from __future__ import annotations

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

    def test_a_folder_the_budget_never_reached_is_still_measured_for_a_moment(self):
        item = self.item()
        seen = []

        def spy(path, seconds=120, *args, **kwargs):
            seen.append(seconds)
            return {"bytes": 777, "complete": False, "reason": "size-time-limit"}

        with mock.patch.object(core, "reap_metadata_size", spy):
            entry = core.reap_audit_item(item, "job-running", {"run": "audit"}, time.monotonic() - 5)
        self.assertGreaterEqual(seen[0], 1.0)
        self.assertEqual((entry["bytes"], entry["bytesLowerBound"]), (777, True))

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
