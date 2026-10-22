"""The idle proof of an agent-scratch folder: it covers the whole tree, and the budget it spends is on the record."""
from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from unittest import mock

from trimreaper import core

from helpers import Home


class ScratchIdle(Home):
    config_extra = {"scratchRoot": "~/.cache/agent-scratch"}

    def scratch(self, name: str) -> Path:
        return self.home / ".cache" / "agent-scratch" / name

    def many_files(self, folder: Path, count: int) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        for index in range(count):
            (folder / f"f{index:06d}.txt").write_bytes(b"x")

    def receipts(self) -> list[dict]:
        folder = self.home / ".local" / "state" / "trimreaper" / "reaper"
        lines = []
        for path in sorted(folder.glob("receipts-*.jsonl")):
            lines.extend(json.loads(line) for line in path.read_text().splitlines() if line.strip())
        return lines

    # ---- the rule ----------------------------------------------------------------------------------------------------

    def test_a_scratch_folder_with_more_than_twenty_thousand_entries_ages_out(self):
        # The old cap listed 20000 entries and gave up, so a big scratch tree could never age out.
        task = self.scratch("big-task")
        self.many_files(task, 20001)
        self.age(task, 20)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(task.exists())
        self.assertTrue(out["reclaimedBytes"] > 0, out)

    def test_a_change_inside_the_window_keeps_the_folder(self):
        task = self.scratch("busy-task")
        self.put(task, "sub/inner.txt")
        self.age(task, 20)
        self.put(task, "sub/new.txt")  # changed now: the folder is not idle

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(task.exists())
        held = next(i for i in out["items"] if i["path"] == str(task))
        self.assertEqual(held["reason"], "idle-recent-change")
        self.assertEqual(held["idleProof"]["reason"], "recent-change")

    def test_a_change_deep_in_the_tree_keeps_the_folder(self):
        # The walk covers the whole tree: a change five levels down is seen, not only the top of it.
        task = self.scratch("deep-task")
        self.put(task, "a/b/c/d/e/inner.txt")
        self.age(task, 20)
        self.put(task, "a/b/c/d/e/new.txt")

        proof = core.reap_scratch_idle_proof(str(task))

        self.assertFalse(proof["idle"], proof)
        self.assertEqual(proof["reason"], "recent-change")

    def test_a_deep_change_keeps_the_folder_through_the_guarded_path(self):
        task = self.scratch("deep-busy-task")
        self.put(task, "a/b/c/d/e/inner.txt")
        self.age(task, 20)
        self.put(task, "a/b/c/d/e/new.txt")

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(task.exists())
        held = next(i for i in out["items"] if i["path"] == str(task))
        self.assertEqual(held["reason"], "idle-recent-change")

    def test_a_symlinked_entry_is_never_followed(self):
        task = self.scratch("linked-task")
        task.mkdir(parents=True)
        fresh = self.scratch("fresh-elsewhere")
        self.put(fresh, "inner.txt")
        os.symlink(str(fresh), str(task / "link"))
        self.age(task, 20)

        proof = core.reap_scratch_idle_proof(str(task))

        self.assertTrue(proof["idle"], proof)  # the link is an entry of its own; the tree it points at is elsewhere

    # ---- the budget --------------------------------------------------------------------------------------------------

    def test_a_folder_over_its_budget_is_kept_and_the_receipt_names_the_budget(self):
        self.write_config({"policy": {"agentScratchIdleMaxEntries": 10}})
        task = self.scratch("over-budget")
        self.many_files(task, 40)
        self.age(task, 20)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(task.exists())
        held = next(i for i in out["items"] if i["path"] == str(task))
        self.assertEqual(held["reason"], "idle-budget-exceeded")
        self.assertEqual(held["idleProof"]["reason"], "budget-exceeded")
        self.assertEqual(held["idleProof"]["budget"]["entries"], 10)
        result = [r for r in self.receipts() if r.get("phase") == "result" and r.get("path") == str(task)]
        self.assertEqual(len(result), 1, self.receipts())
        self.assertEqual(result[0]["idleProof"]["reason"], "budget-exceeded")
        self.assertEqual(result[0]["idleProof"]["entries"], 11)
        self.assertEqual(result[0]["idleProof"]["budget"]["entries"], 10)

    def test_the_byte_budget_keeps_a_folder_and_is_named(self):
        self.write_config({"policy": {"agentScratchIdleMaxBytes": 4}})
        task = self.scratch("over-bytes")
        self.put(task, "inner.bin", b"x" * 4096)
        self.age(task, 20)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(task.exists())
        held = next(i for i in out["items"] if i["path"] == str(task))
        self.assertEqual(held["reason"], "idle-budget-bytes-exceeded")
        self.assertEqual(held["idleProof"]["budget"]["bytes"], 4)

    def test_the_time_budget_keeps_a_folder_and_is_named(self):
        task = self.scratch("slow-task")
        self.put(task, "inner.txt")
        self.age(task, 20)

        # A host that does not validate the policy: a time budget of zero stops the walk at its first entry.
        proof = core.reap_scratch_idle_proof(str(task), {"evidenceExpiryDays": 1, "agentScratchIdleMaxSeconds": 0})

        self.assertFalse(proof["idle"], proof)
        self.assertEqual(proof["reason"], "budget-time-exceeded", proof)
        self.assertEqual(proof["budget"]["seconds"], 0)

    def test_a_folder_that_fits_its_budget_is_idle_and_goes(self):
        self.write_config({"policy": {"agentScratchIdleMaxEntries": 100}})
        task = self.scratch("inside-budget")
        self.many_files(task, 40)
        self.age(task, 20)

        proof = core.reap_scratch_idle_proof(str(task))

        self.assertTrue(proof["idle"], proof)
        self.assertEqual(proof["reason"], "idle")
        self.assertEqual(proof["entries"], 40)
        self.assertEqual(proof["budget"]["entries"], 100)
        out = self.cli("reap", "--scratch")
        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(task.exists())

    def test_the_budget_is_read_from_the_policy(self):
        self.write_config({"policy": {"agentScratchIdleMaxEntries": 25, "agentScratchIdleMaxBytes": 4096,
                                      "agentScratchIdleMaxSeconds": 3}})
        budget = core.reap_scratch_idle_budget()
        self.assertEqual(budget, {"entries": 25, "bytes": 4096, "seconds": 3})

    def test_the_host_refuses_an_entry_budget_it_cannot_use(self):
        self.write_config({"policy": {"agentScratchIdleMaxEntries": 0}})
        task = self.scratch("zero-budget")
        self.put(task, "inner.txt")
        self.age(task, 20)

        out = self.cli("reap", "--scratch", expect=2)

        self.assertTrue(task.exists())
        self.assertIn("agentScratchIdleMaxEntries", out["error"])

    def test_the_engine_keeps_a_folder_whose_entry_budget_cannot_be_read(self):
        # A host that does not validate the policy: the engine's own fallback is zero, so the walk stops at once.
        task = self.scratch("unreadable-budget")
        self.put(task, "inner.txt")
        self.age(task, 20)

        for value in (0, "10", 1.5, True):
            with self.subTest(value=value):
                proof = core.reap_scratch_idle_proof(str(task), {"evidenceExpiryDays": 1,
                                                                 "agentScratchIdleMaxEntries": value})
                self.assertFalse(proof["idle"], proof)
                self.assertEqual(proof["reason"], "budget-exceeded", proof)

    def test_the_engine_keeps_a_folder_whose_byte_budget_cannot_be_read(self):
        task = self.scratch("unreadable-bytes")
        self.put(task, "inner.txt")
        self.age(task, 20)

        proof = core.reap_scratch_idle_proof(str(task), {"evidenceExpiryDays": 1, "agentScratchIdleMaxBytes": "4096"})

        self.assertFalse(proof["idle"], proof)
        self.assertEqual(proof["reason"], "budget-bytes-exceeded", proof)

    def test_the_policy_refuses_an_unusable_budget(self):
        for key, value in (("agentScratchIdleMaxEntries", 0), ("agentScratchIdleMaxEntries", -1),
                           ("agentScratchIdleMaxEntries", True), ("agentScratchIdleMaxEntries", 1.5),
                           ("agentScratchIdleMaxEntries", "10"), ("agentScratchIdleMaxBytes", -1),
                           ("agentScratchIdleMaxBytes", "4096"), ("agentScratchIdleMaxSeconds", 0),
                           ("agentScratchIdleMaxSeconds", "10")):
            with self.subTest(key=key, value=value):
                self.write_config({"policy": {key: value}})
                error = self.cli("config", expect=2)
                self.assertIn(key, error["error"])

    # ---- the walk's own failures -------------------------------------------------------------------------------------

    def test_a_folder_that_cannot_be_read_keeps_the_folder(self):
        task = self.scratch("unreadable-task")
        locked = task / "locked"
        locked.mkdir(parents=True)
        self.put(locked, "inner.txt")
        self.age(task, 20)
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o700)

        proof = core.reap_scratch_idle_proof(str(task))

        self.assertFalse(proof["idle"], proof)
        self.assertEqual(proof["reason"], "unreadable")
        self.assertTrue(proof["detail"].startswith("oserror:"), proof)
        self.assertTrue(task.exists())

    def test_a_protected_name_keeps_the_folder_and_is_never_stat_ed_or_opened(self):
        task = self.scratch("protected-task")
        self.put(task, ".wrangler/state/db")
        self.put(task, "ordinary.txt")
        self.age(task, 20)
        original_stat, original_lstat = core.os.stat, core.os.lstat
        original_open, original_scandir = core.os.open, core.os.scandir
        seen, opened = [], []

        def checked_stat(name, *args, **kwargs):
            seen.append(os.path.basename(str(name)))
            return original_stat(name, *args, **kwargs)

        def checked_lstat(name, *args, **kwargs):
            seen.append(os.path.basename(str(name)))
            return original_lstat(name, *args, **kwargs)

        def checked_open(name, *args, **kwargs):
            opened.append(os.path.basename(str(name)))
            return original_open(name, *args, **kwargs)

        def checked_scandir(name=".", *args, **kwargs):
            opened.append(os.path.basename(str(name)))
            return original_scandir(name, *args, **kwargs)

        with mock.patch.object(core.os, "stat", side_effect=checked_stat), \
                mock.patch.object(core.os, "lstat", side_effect=checked_lstat), \
                mock.patch.object(core.os, "open", side_effect=checked_open), \
                mock.patch.object(core.os, "scandir", side_effect=checked_scandir):
            proof = core.reap_scratch_idle_proof(str(task))

        self.assertFalse(proof["idle"], proof)
        self.assertEqual(proof["reason"], "protected-leaf")
        self.assertEqual([name for name in seen if name.startswith(".wrangler")], [])
        self.assertEqual([name for name in opened if name.startswith(".wrangler")], [])
        self.assertIn("ordinary.txt", seen)  # the control: the walk does read the names around the protected one

    def test_a_root_that_is_not_a_folder_is_not_idle(self):
        other = self.scratch("not-a-folder")
        self.put(other, "inner.txt")

        proof = core.reap_scratch_idle_proof(str(other / "inner.txt"))

        self.assertFalse(proof["idle"], proof)
        self.assertEqual(proof["reason"], "unreadable")

    # ---- the receipt and the audit -----------------------------------------------------------------------------------

    def test_the_receipt_of_a_removed_folder_carries_the_proof(self):
        task = self.scratch("idle-task")
        self.put(task, "inner.txt")
        self.age(task, 20)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 1, out)
        result = [r for r in self.receipts() if r.get("phase") == "result" and r.get("path") == str(task)]
        self.assertEqual(len(result), 1, self.receipts())
        self.assertEqual(result[0]["outcome"], "deleted")
        self.assertEqual(result[0]["retention"], "disposable")
        self.assertTrue(result[0]["idleProof"]["idle"], result[0]["idleProof"])
        self.assertEqual(result[0]["idleProof"]["reason"], "idle")
        plan = [r for r in self.receipts() if r.get("phase") == "plan" and r.get("path") == str(task)]
        self.assertEqual(len(plan), 1, self.receipts())
        self.assertIn("idleProof", plan[0])

    def test_the_receipt_of_a_marked_folder_carries_no_proof(self):
        task = self.scratch("marked-task")
        self.put(task, "inner.txt")
        self.cli("mark", str(task), "disposable")

        out = self.cli("reap", "--scratch")

        result = [r for r in self.receipts() if r.get("phase") == "result" and r.get("path") == str(task)]
        self.assertEqual(len(result), 1, self.receipts())
        self.assertNotIn("idleProof", result[0])
        item = next(i for i in out["items"] if i["path"] == str(task))
        self.assertNotIn("idleProof", item)  # a marked folder has no proof: the key is not there at all

    def test_the_dry_run_shows_the_proof_of_a_kept_folder(self):
        task = self.scratch("busy-task")
        self.put(task, "inner.txt")

        out = self.cli("reap", "--scratch", "--dry-run")

        entry = next(i for i in out["items"] if i["path"] == str(task))
        self.assertEqual(entry["reason"], "idle-recent-change")
        self.assertEqual(entry["idleProof"]["reason"], "recent-change")
        self.assertIn("budget", entry["idleProof"])

    def test_the_audit_carries_the_proof_of_a_kept_folder(self):
        task = self.scratch("audited-task")
        self.put(task, "inner.txt")
        item = next(i for i in core.reap_agent_scratch_items() if i["path"] == str(task))
        ctx = {"run": "test", "sizeBudget": 60}

        entry = core.reap_audit_item(item, None, ctx, time.monotonic() + 60)

        self.assertEqual(entry["blocking"], "idle-recent-change")
        self.assertEqual(entry["idleProof"]["reason"], "recent-change")
        self.assertIn("budget", entry["idleProof"])

    def test_a_folder_that_changes_after_the_listing_is_kept(self):
        task = self.scratch("growing-task")
        self.put(task, "inner.txt")
        self.age(task, 20)
        idle = {"idle": True, "reason": "idle", "entries": 1, "bytes": 0, "newest": None,
                "budget": {"entries": 200000, "bytes": 0, "seconds": 10}, "elapsed": 0.0}
        busy = {**idle, "idle": False, "reason": "recent-change"}
        proofs = [idle, busy]  # the listing sees it idle, the re-proof does not

        with mock.patch.object(core, "reap_scratch_idle_proof", side_effect=proofs):
            out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(task.exists())
        held = next(i for i in out["items"] if i["path"] == str(task))
        self.assertEqual(held["reason"], "idle-recent-change")

    def test_a_folder_that_ages_out_between_the_listing_and_the_gate_is_kept(self):
        # The gate never upgrades a declared "unmarked" folder: the next pass lists it as disposable and removes it.
        task = self.scratch("aging-task")
        self.put(task, "inner.txt")
        self.age(task, 20)
        idle = {"idle": True, "reason": "idle", "entries": 1, "bytes": 0, "newest": None,
                "budget": {"entries": 200000, "bytes": 0, "seconds": 10}, "elapsed": 0.0}
        busy = {**idle, "idle": False, "reason": "recent-change"}
        proofs = [busy, idle]  # the listing sees it busy, the gate sees it idle

        with mock.patch.object(core, "reap_scratch_idle_proof", side_effect=proofs):
            out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(task.exists())
        held = next(i for i in out["items"] if i["path"] == str(task))
        self.assertEqual(held["reason"], "retention-changed")

    def test_a_folder_that_changes_before_the_removal_is_kept(self):
        task = self.scratch("last-look-task")
        self.put(task, "inner.txt")
        self.age(task, 20)
        idle = {"idle": True, "reason": "idle", "entries": 1, "bytes": 0, "newest": None,
                "budget": {"entries": 200000, "bytes": 0, "seconds": 10}, "elapsed": 0.0}
        busy = {**idle, "idle": False, "reason": "recent-change"}
        proofs = [idle, idle, busy]  # the listing, the re-proof and the last look

        with mock.patch.object(core, "reap_scratch_idle_proof", side_effect=proofs):
            out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(task.exists())
        held = next(i for i in out["items"] if i["path"] == str(task))
        self.assertEqual(held["reason"], "retention-changed")

    def test_a_disposable_marker_decides_over_the_idle_proof(self):
        # A marker decides as it does everywhere else: a folder marked disposable goes whatever the proof says.
        task = self.scratch("marked-task")
        self.put(task, "inner.txt")
        self.cli("mark", str(task), "disposable")

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(task.exists())

    def test_expiry_turned_off_keeps_every_unmarked_folder(self):
        self.write_config({"policy": {"evidenceExpiryDays": 0}})
        task = self.scratch("no-expiry-task")
        self.put(task, "inner.txt")
        self.age(task, 20)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(task.exists())
        held = next(i for i in out["items"] if i["path"] == str(task))
        self.assertEqual(held["reason"], "idle-expiry-off")

    def test_the_proof_names_the_newest_mtime_it_saw(self):
        task = self.scratch("newest-task")
        self.put(task, "sub/inner.txt")
        self.age(task, 20)
        self.put(task, "sub/new.txt")  # changed now

        proof = core.reap_scratch_idle_proof(str(task))

        self.assertFalse(proof["idle"], proof)
        self.assertEqual(proof["reason"], "recent-change")
        self.assertTrue(proof["newest"] > time.time() - 3600, proof)
