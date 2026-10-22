"""Synthetic private-home regressions for separately expiring marked job evidence."""
from contextlib import closing
from unittest import mock

from helpers import Home, DAY
from trimreaper import core


class MarkedEvidence(Home):
    config_extra = {"policy": {"evidenceExpiryDays": 1, "markedEvidenceExpiryDays": 5, "cloneArchive": "keep"}}

    def setup_evidence(self, ago=6 * DAY, parent=False, report=True):
        self.add_job("J-0060", finished_ago=ago)
        root = self.home / ".cache/trimreaper/jobs/J-0060"
        child = self.put(root, "screenshots/frame.png").parent
        self.put(root if parent else child, ".ut-retention-evidence", b"")
        if report:
            self.put(root, "report.md", b"retained report\n")
        for name in ("handoff.md", "summary.txt", "outcome.json", "receipts/receipt.jsonl"):
            self.put(root, name, b"durable\n")
        return root, child

    def reap(self, *args):
        return self.cli("reap", "--job", "J-0060", *args)

    def test_parent_marker_expires_children_but_keeps_records_and_marker(self):
        root, child = self.setup_evidence(parent=True)
        self.put(child, "nested/.ut-retention-evidence", b"")
        self.put(child, "nested/data.bin")
        dry = self.reap("--dry-run")
        self.assertTrue(child.exists())
        self.assertTrue(any(i["outcome"] == "would-delete" for i in dry["items"]), dry)
        out = self.reap()
        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(child.exists())
        for name in ("report.md", "handoff.md", "summary.txt", "outcome.json", "receipts/receipt.jsonl", ".ut-retention-evidence"):
            self.assertTrue((root / name).is_file(), name)
        self.assertIn("marked-evidence-producer-locks", next(i for i in out["items"] if i["path"] == str(child))["checks"])

    def test_supported_expire_evidence_route_uses_folder_guard(self):
        _, child = self.setup_evidence()
        out = self.reap("--expire-evidence", "--dry-run")
        self.assertEqual(out["markedEvidence"]["items"][0]["outcome"], "would-delete", out)
        out = self.reap("--expire-evidence")
        self.assertEqual(out["markedEvidence"]["deleted"], 1, out)
        self.assertFalse(child.exists())

    def test_strict_five_day_boundary(self):
        root, child = self.setup_evidence()
        stamp = 2000000000.0
        with closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET finished_at=? WHERE id='J-0060'", (stamp - 5 * DAY,))
        with mock.patch.object(core, "now", return_value=stamp):
            self.assertFalse(core.marked_evidence_job("J-0060"))
        with mock.patch.object(core, "now", return_value=stamp + 0.01):
            self.assertTrue(core.marked_evidence_job("J-0060"))
        self.assertTrue(child.exists())

    def test_missing_or_symlink_report_preserves_marked_data(self):
        root, child = self.setup_evidence(report=False)
        self.assertEqual(self.reap()["deleted"], 0)
        (root / "report.md").symlink_to(root / "handoff.md")
        self.assertEqual(self.reap()["deleted"], 0)
        self.assertTrue(child.exists())

    def test_recent_and_disabled_window_preserve(self):
        _, child = self.setup_evidence(ago=4 * DAY)
        self.assertEqual(self.reap()["deleted"], 0)
        self.write_config({"policy": {"markedEvidenceExpiryDays": 0}})
        self.assertEqual(self.reap()["deleted"], 0)
        self.assertTrue(child.exists())

    def test_keep_records_and_retained_classes_preserve_whole_candidate(self):
        root, child = self.setup_evidence(parent=True)
        for rel in ("mixed/report.md", "cust/.ut-retention-customer", "runtime/.ut-retention-release-runtime-rollback", "protected-archive/data", "nested/protected-archive/data", "release/installer.pkg", "logs/receipts.jsonl"):
            self.put(root, rel)
        self.assertEqual(self.reap()["deleted"], 1)
        self.assertFalse(child.exists())
        for rel in ("mixed/report.md", "cust/.ut-retention-customer", "runtime/.ut-retention-release-runtime-rollback", "protected-archive/data", "nested/protected-archive/data", "release/installer.pkg", "logs/receipts.jsonl"):
            self.assertTrue((root / rel).exists(), rel)

    def test_protected_leaf_preserves_whole_candidate(self):
        _, child = self.setup_evidence()
        self.put(child, ".vercel/state.json")
        out = self.reap()
        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue((child / "frame.png").exists())

    def test_unfinished_claim_preserves_marked_data(self):
        root, child = self.setup_evidence()
        self.add_job("J-0061", cwd=child, status="running")
        out = self.reap()
        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(child.exists())

    def test_parent_claim_also_preserves(self):
        root, child = self.setup_evidence()
        self.add_job("J-0061", cwd=root.parent, status="running")
        self.assertEqual(self.reap()["deleted"], 0)
        self.assertTrue(child.exists())

    def test_nonterminal_owner_never_eligible(self):
        _, child = self.setup_evidence()
        for status in ("running", "parked", "approved", "blocked", "interrupted"):
            with closing(self.state()) as conn, conn:
                conn.execute("UPDATE jobs SET status=? WHERE id='J-0060'", (status,))
            self.assertFalse(core.marked_evidence_job("J-0060"), status)
        self.assertTrue(child.exists())

    def test_top_level_marked_logs_expire_with_manifest_but_records_stay(self):
        root, _ = self.setup_evidence(parent=True)
        log = self.put(root, "large.log")
        self.age(log, 6)
        self.put(root, "report.log", b"keep\n")
        out = self.reap("--expire-evidence")
        self.assertEqual(out["expired"], 1, out)
        self.assertFalse(log.exists())
        self.assertTrue((root / "report.log").exists())
        self.assertIn('"name": "large.log"', (root / "evidence-expired.jsonl").read_text())

    def test_marked_window_still_runs_when_unmarked_expiry_is_off(self):
        _, child = self.setup_evidence()
        root = child.parent
        log = self.put(root, "unmarked.log")
        self.age(log, 6)
        self.write_config({"policy": {"evidenceExpiryDays": 0, "markedEvidenceExpiryDays": 5}})
        out = self.reap("--expire-evidence")
        self.assertTrue(log.exists())
        self.assertEqual(out["markedEvidence"]["deleted"], 1, out)
        self.assertFalse(child.exists())

    def test_tick_expires_marked_evidence_without_manual_invocation(self):
        _, child = self.setup_evidence(parent=True)
        self.cli("tick")
        self.assertFalse(child.exists())

    def test_marked_expiry_precedes_longer_unmarked_window(self):
        root, child = self.setup_evidence(parent=True)
        log = self.put(root, "large.log")
        self.age(log, 6)
        self.write_config({"policy": {"evidenceExpiryDays": 14, "markedEvidenceExpiryDays": 5}})
        out = self.reap("--expire-evidence")
        self.assertEqual(out["markedEvidence"]["deleted"], 1, out)
        self.assertEqual(out["expired"], 0, out)
        self.assertFalse(child.exists())
        self.assertTrue(log.exists())

    def test_unmarked_old_files_wait_for_the_job_window_while_marked_folders_expire(self):
        # Preserve the shipped host rule: file age cannot shorten the window after the job finished.
        self.write_config({"policy": {"evidenceExpiryDays": 14, "markedEvidenceExpiryDays": 5}})
        root, child = self.setup_evidence()
        loose = self.put(root, "old-shot.png", b"x" * 100)
        self.age(loose, 20)
        out = self.reap("--expire-evidence")
        self.assertEqual(out["markedEvidence"]["deleted"], 1, out)
        self.assertEqual(out["expired"], 0, out)
        self.assertFalse(child.exists())
        self.assertTrue(loose.exists(), out)

    def test_job_root_marker_keeps_original_cutoff_for_loose_files(self):
        self.write_config({"policy": {"evidenceExpiryDays": 14, "markedEvidenceExpiryDays": 5}})
        root, child = self.setup_evidence(ago=15 * DAY, parent=True)
        loose = self.put(root, "recent-shot.png")
        self.age(loose, 6)
        old = self.put(root, "old-shot.png")
        self.age(old, 20)
        out = self.reap("--expire-evidence")
        self.assertEqual(out["markedEvidence"]["deleted"], 1, out)
        self.assertEqual(out["expired"], 1, out)
        self.assertFalse(child.exists())
        self.assertTrue(loose.exists(), out)
        self.assertFalse(old.exists(), out)

    def test_late_keep_record_stops_descriptor_removal(self):
        _, child = self.setup_evidence()
        self.put(child, "handoff.md")
        fence = core.reap_fence(str(child), str(child.parent))
        with self.assertRaisesRegex(core.ReapRefused, "kept-record-appeared"):
            core.reap_remove_tree(fence, {"removed": 0}, expiring_evidence=True)
        self.assertTrue((child / "handoff.md").exists())
