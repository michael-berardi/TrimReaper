"""The agent-scratch listing is resumable: a cursor and remembered per-folder idle proofs.

The listing spends at most `AGENT_SCRATCH_LIST_SECONDS` proving folders. On a big scratch root the same first folders used up the
budget every pass and the rest were reported `audit-budget-exhausted` / `idle-budget-time-exceeded` without ever being proved.
The listing now starts at a persisted cursor and wraps, and remembers each proof keyed by the folder's (device, inode, ctime):
a folder proved lately costs no budget, so every folder is proved within a few passes. A remembered proof is only a hint:
a change of the folder's identity or ctime invalidates it, and the removal walks the folder afresh.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest import mock

from trimreaper import core

from helpers import ScratchHome


class ScratchCursor(ScratchHome):
    config_extra = {"scratchRoot": "~/.cache/agent-scratch"}
    NAMES = ("a-task", "b-task", "c-task", "d-task", "e-task", "f-task")

    def scratch(self, name: str) -> Path:
        return self.home / ".cache" / "agent-scratch" / name

    def make(self, names=NAMES):
        for name in names:
            self.put(self.scratch(name), "inner.txt")  # changed now: every proof is `recent-change`

    def meta(self, key: str):
        with self.state() as conn:
            found = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return found[0] if found else None

    def slow_proofs(self, cost: float = 30.0):
        """Every real proof costs `cost` seconds of the (fake) clock, so a 20 s listing budget proves exactly one folder per pass."""
        clock, walked, real = [0.0], [], core.reap_scratch_idle_proof

        def proof(path, policy=None, seconds=None):
            walked.append(Path(path).name)
            clock[0] += cost
            return real(path, policy, seconds)

        patches = (mock.patch.object(core.time, "monotonic", side_effect=lambda: clock[0]),
                   mock.patch.object(core, "reap_scratch_idle_proof", side_effect=proof))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return walked

    def listing(self) -> list:
        with core.db() as conn:
            return core.reap_agent_scratch_listing(conn)

    # ---- the cursor ------------------------------------------------------------------------------------------------

    def test_every_folder_is_proved_within_as_many_passes_as_the_budget_needs(self):
        self.make()
        walked = self.slow_proofs()

        reports = [self.listing().report for _ in self.NAMES]

        self.assertEqual(walked, list(self.NAMES))  # each folder walked exactly once: the cursor moved on every pass
        self.assertEqual([r["proved"] for r in reports], [1] * 6)
        self.assertEqual([r["unproved"] for r in reports], [5, 4, 3, 2, 1, 0])
        # the last pass starts behind e-task, walks f-task and finds every other folder's proof current: it ends where it began
        self.assertEqual([r["cursor"] for r in reports], ["a-task", "b-task", "c-task", "d-task", "e-task", "e-task"])
        self.assertEqual(self.meta("reaper:agent-scratch-cursor"), "e-task")

    def test_once_everything_is_proved_a_pass_walks_nothing_and_reports_no_unproved(self):
        self.make()
        walked = self.slow_proofs()
        for _ in self.NAMES:
            self.listing()
        walked.clear()

        report = self.listing().report

        self.assertEqual(walked, [])
        self.assertEqual((report["cached"], report["proved"], report["unproved"], report["folders"]), (6, 0, 0, 6))

    def test_the_remembered_proofs_are_keyed_by_identity_and_ctime(self):
        self.make(("a-task",))
        self.listing()

        proofs = json.loads(self.meta("reaper:agent-scratch-proofs"))

        info = os.lstat(self.scratch("a-task"))
        entry = proofs["a-task"]
        self.assertEqual((entry["dev"], entry["ino"], entry["ctimeNs"]), (info.st_dev, info.st_ino, info.st_ctime_ns))
        self.assertEqual(entry["proof"]["reason"], "recent-change")

    def test_a_change_to_a_folder_invalidates_only_its_proof(self):
        self.make()
        walked = self.slow_proofs()
        for _ in self.NAMES:
            self.listing()
        walked.clear()
        self.put(self.scratch("c-task"), "another.txt")  # the folder's own ctime moves

        report = self.listing().report

        self.assertEqual(walked, ["c-task"])
        self.assertEqual(report["cached"], 5)

    def test_a_replaced_folder_is_not_the_proved_one(self):
        self.make(("a-task",))
        self.listing()
        folder = self.scratch("a-task")
        os.rename(folder, self.scratch("moved"))
        folder.mkdir()  # same name, another inode
        (folder / "inner.txt").write_text("x")

        items = self.listing()

        self.assertEqual(items.report["cached"], 0)

    def test_a_proof_older_than_its_lifetime_is_taken_again(self):
        self.make(("a-task",))
        self.listing()
        with self.state() as conn:
            proofs = json.loads(self.meta("reaper:agent-scratch-proofs"))
            proofs["a-task"]["at"] -= core.AGENT_SCRATCH_PROOF_SECONDS + 10
            conn.execute("UPDATE meta SET value=? WHERE key='reaper:agent-scratch-proofs'", (json.dumps(proofs),))

        self.assertEqual(self.listing().report["cached"], 0)

    def test_a_proof_taken_under_other_windows_is_taken_again(self):
        self.make(("a-task",))
        self.listing()
        self.write_config({"policy": {"evidenceExpiryDays": 30}})

        self.assertEqual(self.listing().report["cached"], 0)

    def test_a_recent_change_is_proved_again_once_it_is_older_than_the_window(self):
        self.make(("a-task",))
        self.listing()
        with mock.patch.object(core, "now", side_effect=lambda: time.time() + 30 * 86400):
            self.assertEqual(self.listing().report["cached"], 0)

    def test_a_dry_run_and_the_audit_without_persist_write_nothing(self):
        self.make(("a-task", "b-task"))
        with core.db():
            pass  # the store exists, empty
        before = (self.meta("reaper:agent-scratch-cursor"), self.meta("reaper:agent-scratch-proofs"))
        self.cli("reap", "--scratch", "--dry-run")
        core.reap_audit(None, 10, False, 5)
        self.assertEqual((self.meta("reaper:agent-scratch-cursor"), self.meta("reaper:agent-scratch-proofs")), before)

    # ---- the cache is a hint, never an authority --------------------------------------------------------------------

    def test_a_remembered_idle_proof_cannot_remove_a_folder_that_changed_deeper_down(self):
        task = self.scratch("x-task")
        self.put(task, "sub/inner.txt")
        self.age(task, 20)
        items = self.listing()  # remembered as idle: the pass that listed it has not swept yet
        self.assertEqual(items[0]["retention"], core.REAP_DISPOSABLE)
        (task / "sub" / "inner.txt").write_text("changed deep down")  # no change to the folder's own ctime

        again = self.listing()
        self.assertEqual(again.report["cached"], 1)  # the listing still believes the remembered proof ...
        self.assertEqual(again[0]["retention"], core.REAP_DISPOSABLE)
        out = core.reap_item(again[0], "delete", {"run": "cache-hint"})  # ... the removal does not

        self.assertEqual(out["outcome"], "refused", out)
        self.assertEqual(out["reason"], "idle-recent-change")
        self.assertTrue(task.exists())

    def test_a_remembered_proof_is_used_once(self):
        task = self.scratch("y-task")
        self.put(task, "inner.txt")
        self.age(task, 20)
        self.listing()
        item = self.listing()[0]
        self.assertTrue(item["idleProof"].get("cached"))
        self.assertNotIn("idleProofHint", item)  # consumed by the listing: every later look walks afresh
        calls = []
        real = core.reap_scratch_idle_proof
        with mock.patch.object(core, "reap_scratch_idle_proof", side_effect=lambda *a, **k: (calls.append(1), real(*a, **k))[1]):
            core.reap_retention(item)
        self.assertEqual(len(calls), 1)

    def test_a_listing_that_deletes_still_sweeps_every_idle_folder_it_listed(self):
        for name in ("p-task", "q-task"):
            self.put(self.scratch(name), "inner.txt")
            self.age(self.scratch(name), 20)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 2, out)
        self.assertEqual(out["listing"]["unproved"], 0)

    # ---- reporting -------------------------------------------------------------------------------------------------

    def test_the_sweep_reports_the_cursor_and_the_unproved_count(self):
        self.make()
        self.slow_proofs()

        out = core.reap_agent_scratch(False)

        self.assertEqual(out["listing"]["cursor"], "a-task")
        self.assertEqual(out["listing"]["unproved"], 5)
        self.assertEqual(out["listing"]["folders"], 6)

    def test_the_audit_reads_the_cursor_and_names_proved_folders_by_their_reason(self):
        self.make()
        for _ in range(1):
            self.listing()  # the unbounded real listing proves all six and remembers them
        self.put(self.scratch("g-task"), "inner.txt")  # a seventh, never proved
        clock = [0.0]
        real = core.reap_release_candidates

        def late():
            clock[0] = 100.0  # the audit's budget is spent by the time it reaches agent scratch
            return real()

        with mock.patch.object(core.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(core, "reap_release_candidates", side_effect=late):
            out = core.reap_audit(None, 10, True, 1)

        rows = {os.path.basename(r["path"]): r for r in out["agentScratch"]}
        self.assertEqual(rows["a-task"]["blocking"], "idle-recent-change")
        self.assertEqual(rows["g-task"]["blocking"], "audit-budget-exhausted")
        report = out["agentScratchListing"]
        self.assertEqual((report["cached"], report["unproved"]), (6, 1))

    def test_the_audit_with_persist_advances_the_cursor(self):
        self.make(("a-task", "b-task"))

        core.reap_audit(None, 10, False, 5, persist=True)

        self.assertEqual(self.meta("reaper:agent-scratch-cursor"), "b-task")
        self.assertIn("a-task", json.loads(self.meta("reaper:agent-scratch-proofs")))
