"""J-4353: fixtures of finished jobs are scaffolding, never evidence; offload never moves them; the cleanup lock is waited for."""
import os
import threading
import time
from pathlib import Path
from unittest import mock

from helpers import Home, DAY
from trimreaper import core, offload


class FixtureExpiry(Home):
    config_extra = {"policy": {"evidenceExpiryDays": 1, "markedEvidenceExpiryDays": 5, "cloneArchive": "keep"}}

    def setup_job(self, ago=2 * DAY, marker=True):
        self.add_job("J-0070", finished_ago=ago)
        root = self.home / ".cache/trimreaper/jobs/J-0070"
        fixtures = root / "fixtures" / "ut-fixture-J-0070-" / "model"
        fixtures.mkdir(parents=True)
        (fixtures / "w.safetensors").write_bytes(b"x" * 4096)
        (fixtures / "tokenizer").write_text("t")
        if marker:
            self.put(root, ".ut-retention-evidence", b"")
        return root

    def fixtures_item(self, root):
        job = {"id": "J-0070", "status": "done"}
        with core.db() as conn:
            job = core.row(conn, "SELECT * FROM jobs WHERE id='J-0070'")
        items, _ = core.reap_job_items(job, [])
        return next(i for i in items if os.path.basename(i["path"]) == "fixtures")

    def test_cleanup_wait_is_bounded_but_long(self):
        self.assertGreaterEqual(core.FIXTURE_CLEANUP_WAIT_SECONDS, 120)

    def test_lock_wait_backs_off_and_succeeds_when_released(self):
        release = threading.Event()
        started = threading.Event()

        def holder():
            with core.reap_cleanup_owner("holder"):
                started.set()
                release.wait(10)
        thread = threading.Thread(target=holder)
        thread.start()
        started.wait(5)
        sleeps = []
        real = time.sleep
        threading.Timer(0.6, release.set).start()
        with mock.patch.object(core.time, "sleep", side_effect=lambda s: (sleeps.append(s), real(s))):
            with core.reap_cleanup_owner("fixture", wait=10):
                pass
        thread.join()
        self.assertGreater(max(sleeps), 0.05, sleeps)

    def test_finished_jobs_fixtures_are_disposable_despite_job_evidence_marker(self):
        root = self.setup_job()
        self.assertEqual(self.fixtures_item(root)["retention"], "disposable")

    def test_recent_job_fixtures_stay(self):
        root = self.setup_job(ago=3600)
        self.assertNotEqual(self.fixtures_item(root)["retention"], "disposable")

    def test_fixture_own_marker_still_keeps(self):
        root = self.setup_job()
        self.put(root / "fixtures", ".ut-retention-evidence", b"")
        self.assertEqual(self.fixtures_item(root)["retention"], "evidence")

    def test_model_weights_are_not_evidence(self):
        self.assertTrue(core.reap_model_weight("a.safetensors", 200 * 2 ** 20))
        self.assertFalse(core.reap_model_weight("a.safetensors", 10))
        self.assertFalse(core.reap_model_weight("a.png", 200 * 2 ** 20))

    def test_offload_never_moves_fixtures(self):
        root = self.setup_job(marker=False)
        self.assertEqual(offload.fixture_refusal(root / "fixtures"), "fixture-never-moved")
        self.assertEqual(offload.fixture_refusal(root), "holds-fixtures-never-moved")
        self.assertIsNone(offload.fixture_refusal(self.home / "nothing"))
