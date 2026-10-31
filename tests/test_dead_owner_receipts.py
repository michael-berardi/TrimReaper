"""A dead cleanup owner is auto-acknowledged only when it wrote no receipt (regression: a failed final note left 'started' and
the dead-owner path acknowledged a run that had already written plan receipts)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trimreaper import core


def _dead_pid() -> int:
    # A pid that is not alive: spawn and reap a short child, then reuse its number (no live owner remains).
    import subprocess
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


class DeadOwnerReceipts(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="trimreaper-receipts-"))
        patcher = mock.patch.object(core, "reap_receipt_dir", return_value=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.previous = {"pid": _dead_pid(), "run": "run-dead", "scope": "scope"}

    def _receipt(self, run):
        line = {"v": 2, "phase": "plan", "cleanupOwnerRun": run, "path": "synthetic-placeholder"}
        (self.dir / "receipts-20261031.jsonl").write_text(json.dumps(line) + "\n")

    def test_dead_owner_without_receipts_is_auto_acknowledged(self):
        self.assertIsNotNone(core.reap_dead_owner_note(self.previous, "started"))

    def test_dead_owner_with_a_plan_receipt_is_never_auto_acknowledged(self):
        self._receipt("run-dead")
        self.assertIsNone(core.reap_dead_owner_note(self.previous, "started"))

    def test_a_receipt_for_another_run_does_not_block_the_auto_ack(self):
        self._receipt("run-other")
        self.assertIsNotNone(core.reap_dead_owner_note(self.previous, "started"))

    def test_unreadable_receipts_fail_closed(self):
        (self.dir / "receipts-20261031.jsonl").mkdir()  # a directory where a receipt file should be: open() fails
        self.assertIsNone(core.reap_dead_owner_note(self.previous, "started"))

    def test_a_torn_fragment_does_not_name_a_run(self):
        (self.dir / "receipts-20261031.jsonl").write_text('{"cleanupOwnerRun": "run-dead", "pa\n')
        self.assertIsNotNone(core.reap_dead_owner_note(self.previous, "started"))


if __name__ == "__main__":
    unittest.main()
