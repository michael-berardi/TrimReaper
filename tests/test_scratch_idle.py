"""The idle proof of an agent-scratch folder: it covers the whole tree, and the budget it spends is on the record."""
from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from unittest import mock

from trimreaper import core

from helpers import ScratchHome


class ScratchIdle(ScratchHome):
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

    def test_a_deep_metadata_change_keeps_its_real_ctime_in_the_aged_fixture(self):
        task = self.scratch("deep-metadata-task")
        leaf = self.put(task, "a/b/c/inner.txt")
        self.age(task, 20)
        before = leaf.stat().st_mtime_ns
        os.chmod(leaf, 0o600)

        proof = core.reap_scratch_idle_proof(str(task))

        self.assertFalse(proof["idle"], proof)
        self.assertEqual(proof["reason"], "recent-change")
        self.assertEqual(leaf.stat().st_mtime_ns, before)
        self.assertGreaterEqual(proof["newest"], leaf.stat().st_ctime)

    def test_restoring_a_deep_files_mtime_does_not_hide_a_write(self):
        task = self.scratch("restored-mtime-task")
        leaf = self.put(task, "a/b/c/inner.txt")
        self.age(task, 20)
        stamp = leaf.stat().st_mtime_ns
        leaf.write_bytes(b"new data")
        os.utime(leaf, ns=(stamp, stamp))

        proof = core.reap_scratch_idle_proof(str(task))

        self.assertFalse(proof["idle"], proof)
        self.assertEqual(proof["reason"], "recent-change")
        self.assertEqual(leaf.stat().st_mtime_ns, stamp)

    def test_a_root_metadata_change_at_the_end_of_the_walk_is_kept(self):
        task = self.scratch("root-last-look-task")
        self.put(task, "inner.txt")
        self.age(task, 20)
        walk = core.reap_fd_walk

        def changed(*args, **kwargs):
            result = walk(*args, **kwargs)
            os.chmod(task, 0o750)
            return result

        with mock.patch.object(core, "reap_fd_walk", side_effect=changed):
            proof = core.reap_scratch_idle_proof(str(task))

        self.assertFalse(proof["idle"], proof)
        self.assertEqual(proof["reason"], "recent-change")
        self.assertGreaterEqual(proof["newest"], task.stat().st_ctime)

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
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o700)
        self.age(task, 20)

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
        # A listed refusal is not walked again or upgraded: the next pass can list it as disposable.
        task = self.scratch("aging-task")
        self.put(task, "inner.txt")
        self.age(task, 20)
        idle = {"idle": True, "reason": "idle", "entries": 1, "bytes": 0, "newest": None,
                "budget": {"entries": 200000, "bytes": 0, "seconds": 10}, "elapsed": 0.0}
        busy = {**idle, "idle": False, "reason": "recent-change"}
        proofs = [busy, idle]  # the listing sees it busy, the gate sees it idle

        with mock.patch.object(core, "reap_scratch_idle_proof", side_effect=proofs) as checked:
            out = self.cli("reap", "--scratch")

        self.assertEqual(checked.call_count, 1)
        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(task.exists())
        held = next(i for i in out["items"] if i["path"] == str(task))
        self.assertEqual(held["reason"], "idle-recent-change")

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

    def test_a_folder_over_its_budget_at_the_last_look_names_the_budget(self):
        task = self.scratch("budget-task")
        self.put(task, "inner.txt")
        self.age(task, 20)
        idle = {"idle": True, "reason": "idle", "entries": 1, "bytes": 0, "newest": None,
                "budget": {"entries": 200000, "bytes": 0, "seconds": 10}, "elapsed": 0.0}
        slow = {**idle, "idle": False, "reason": "budget-time-exceeded"}
        proofs = [idle, idle, slow]  # the listing, the re-proof and the last look

        with mock.patch.object(core, "reap_scratch_idle_proof", side_effect=proofs):
            out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(task.exists())
        held = next(i for i in out["items"] if i["path"] == str(task))
        self.assertEqual(held["reason"], "idle-budget-time-exceeded")

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

    def test_the_listing_keeps_its_own_budget(self):
        # A listing that may spend its own budget on the whole root: a folder it could not prove is kept, unproven.
        task = self.scratch("late-task")
        self.put(task, "inner.txt")
        self.age(task, 20)

        items = core.reap_agent_scratch_items(deadline=time.monotonic() - 1)

        held = next(i for i in items if i["path"] == str(task))
        self.assertEqual(held["retention"], "unmarked")
        self.assertEqual(held["idleProof"]["reason"], "budget-time-exceeded")
        self.assertEqual(held["idleProof"]["budget"]["seconds"], 0)
        self.assertFalse(held["idleAttempted"])

    def test_the_listing_proves_a_folder_within_its_budget(self):
        task = self.scratch("early-task")
        self.put(task, "inner.txt")
        self.age(task, 20)

        items = core.reap_agent_scratch_items(deadline=time.monotonic() + 30)

        gone = next(i for i in items if i["path"] == str(task))
        self.assertEqual(gone["retention"], core.REAP_DISPOSABLE)
        self.assertTrue(gone["idleProof"]["idle"])
        self.assertTrue(gone["idleAttempted"])

    def test_the_listing_gives_every_folder_only_what_is_left_of_its_budget(self):
        # The whole listing may spend its budget once: the first folder takes what is left, the next one gets nothing.
        first, second = self.scratch("first-task"), self.scratch("second-task")
        for task in (first, second):
            self.put(task, "inner.txt")
            self.age(task, 20)
        spent, clock = [], [10.0]

        def proof(path, policy=None, seconds=None):
            spent.append(seconds)
            clock[0] = 19.0  # the first folder spends nine of the listing's twenty seconds
            return {"idle": False, "reason": "recent-change", "detail": "", "entries": 1, "bytes": 0,
                    "newest": time.time(), "budget": {"entries": 1, "bytes": 0, "seconds": 10}, "elapsed": 0.0}

        with mock.patch.object(core.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(core, "reap_scratch_idle_proof", side_effect=proof):
            items = core.reap_agent_scratch_items(deadline=20.0)

        self.assertEqual(len(spent), 2, spent)
        self.assertGreater(spent[0], 0.0)
        self.assertLessEqual(spent[1], spent[0])
        self.assertTrue(all(i["retention"] == "unmarked" for i in items), items)

    def test_an_unproved_listing_is_not_walked_again_by_the_sweep(self):
        task = self.scratch("unproved-task")
        self.put(task, "inner.txt")
        self.age(task, 20)
        items = core.reap_agent_scratch_items(deadline=time.monotonic() - 1)
        with mock.patch.object(core, "reap_scratch_idle_proof", side_effect=AssertionError("must not rewalk")):
            out = core.reap_agent_scratch(True, items)
        self.assertEqual(out["deleted"], 0)
        self.assertEqual(out["items"][0]["reason"], "idle-budget-time-exceeded")
        self.assertEqual(out["items"][0]["idleProof"]["detail"], "listing-budget-exhausted")
        self.assertTrue(task.exists())

    def test_successive_bounded_passes_give_every_folder_a_turn(self):
        tasks = [self.scratch(name) for name in ("a-task", "b-task", "c-task")]
        for task in tasks:
            self.put(task, "inner.txt")
        clock, checked = [0.0], []

        def slow(path, policy=None, seconds=None):
            checked.append(Path(path).name)
            clock[0] += 2
            return {"idle": False, "reason": "recent-change", "elapsed": 2.0, "budget": {"seconds": seconds}}

        with mock.patch.object(core, "AGENT_SCRATCH_LIST_SECONDS", 1), \
                mock.patch.object(core.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(core, "reap_scratch_idle_proof", side_effect=slow):
            for _ in tasks:
                out = core.reap_agent_scratch(False)
                self.assertEqual(out["deleted"], 0)
        self.assertEqual(checked, [p.name for p in tasks])
        with self.state() as conn:
            cursor = conn.execute("SELECT value FROM meta WHERE key='reaper:agent-scratch-cursor'").fetchone()
        self.assertEqual(cursor[0], "c-task")

    def test_forged_listing_proof_cannot_authorize_removal(self):
        task = self.scratch("forged-task")
        self.put(task, "inner.txt")  # fresh: never idle
        item = core.reap_agent_scratch_items()[0]
        item["retention"] = core.REAP_DISPOSABLE
        item["idleProof"] = {"idle": True, "reason": "idle", "elapsed": 0}
        out = core.reap_item(item, "delete", {"run": "forged-test"})
        self.assertEqual(out["outcome"], "refused")
        self.assertEqual(out["reason"], "idle-recent-change")
        self.assertTrue(task.exists())

    def test_a_slow_folder_does_not_stop_the_folders_behind_it(self):
        # The listing's budget bounds discovery, never the sweep: every folder it listed is visited in this same pass.
        tasks = [self.scratch(name) for name in ("a-task", "b-task", "c-task", "d-task")]
        for task in tasks:
            self.put(task, "inner.txt")
            self.age(task, 20)
        clock = [0.0]

        def slow(path, policy=None, seconds=None):
            proof = {"idle": True, "reason": "idle", "detail": "", "entries": 1, "bytes": 0, "newest": None,
                    "budget": {"entries": 200000, "bytes": 0, "seconds": seconds}, "elapsed": 3.0}
            if seconds is not None and seconds <= 0:
                return {**proof, "idle": False, "reason": "budget-time-exceeded"}  # what a spent budget proves
            clock[0] += 3.0  # a folder whose whole-tree proof spends real time
            return proof

        with mock.patch.object(core.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(core, "reap_scratch_idle_proof", side_effect=slow):
            out = core.reap_agent_scratch(False)

        self.assertEqual(out["deleted"], 4, out)
        self.assertEqual([task.exists() for task in tasks], [False, False, False, False])
        self.assertTrue(all(item["outcome"] == "deleted" for item in out["items"]), out["items"])

    def test_the_last_look_gets_the_whole_per_folder_budget(self):
        # A first proof that spends most of a generous per-folder budget must not shrink the proof before the removal.
        self.write_config({"policy": {"agentScratchIdleMaxSeconds": 300}})
        task = self.scratch("long-budget-task")
        self.put(task, "inner.txt")
        self.age(task, 20)
        spent = []

        def proof(path, policy=None, seconds=None):
            spent.append(seconds)
            return {"idle": True, "reason": "idle", "detail": "", "entries": 1, "bytes": 0, "newest": None,
                    "budget": {"entries": 200000, "bytes": 0, "seconds": seconds}, "elapsed": 200.0}

        with mock.patch.object(core, "reap_scratch_idle_proof", side_effect=proof):
            out = core.reap_agent_scratch(False)

        self.assertEqual(out["deleted"], 1, out)
        self.assertIn(120, spent)  # a gate proof still gets the item's own budget
        self.assertIsNone(spent[-1])  # the last look is not capped by what the first proof left of it

    def test_a_caller_deadline_in_the_past_only_keeps_the_folder(self):
        # The engine sets no deadline of its own; a caller that hands one in can shorten a proof, and a shortened proof
        # only ever keeps a folder.
        task = self.scratch("deadline-task")
        self.put(task, "inner.txt")
        self.age(task, 20)
        item = core.reap_agent_scratch_items()[0]
        item["retention"] = core.REAP_DISPOSABLE
        item["idleDeadline"] = time.monotonic() - 1

        out = core.reap_item(item, "delete", {"run": "deadline-test"})

        self.assertEqual(out["outcome"], "refused")
        self.assertEqual(out["reason"], "idle-budget-time-exceeded")
        self.assertTrue(task.exists())

    def test_a_dry_run_and_an_audit_do_not_advance_the_cursor(self):
        tasks = [self.scratch(name) for name in ("a-task", "b-task")]
        for task in tasks:
            self.put(task, "inner.txt")

        def cursor():
            if not (self.home / ".local" / "state" / "trimreaper" / "state.sqlite3").exists():
                return None  # a read-only pass may not create the store at all
            with self.state() as conn:
                return conn.execute("SELECT value FROM meta WHERE key='reaper:agent-scratch-cursor'").fetchone()

        self.assertIsNone(cursor())
        self.cli("reap", "--scratch", "--dry-run")
        self.assertIsNone(cursor())
        core.reap_agent_scratch_items(time.monotonic() + 10)  # the audit's own listing
        self.assertIsNone(cursor())
        self.cli("reap", "--scratch")  # the deletion pass is the one that records where the listing stopped
        with self.state() as conn:
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='reaper:agent-scratch-cursor'").fetchone()[0], "b-task")

    def test_a_listed_unmarked_item_without_a_proof_is_kept_without_a_walk(self):
        task = self.scratch("no-proof-task")
        self.put(task, "inner.txt")
        item = core.reap_agent_scratch_items()[0]
        item.pop("idleProof")
        with mock.patch.object(core, "reap_scratch_idle_proof", side_effect=AssertionError("must not rewalk")):
            out = core.reap_item(item, "delete", {"run": "missing-test"})
        self.assertEqual(out["reason"], "retention-preserved:unmarked")
        self.assertTrue(task.exists())

    def test_the_proof_names_the_newest_change_it_saw(self):
        task = self.scratch("newest-task")
        self.put(task, "sub/inner.txt")
        self.age(task, 20)
        self.put(task, "sub/new.txt")  # changed now

        proof = core.reap_scratch_idle_proof(str(task))

        self.assertFalse(proof["idle"], proof)
        self.assertEqual(proof["reason"], "recent-change")
        self.assertTrue(proof["newest"] > time.time() - 3600, proof)
