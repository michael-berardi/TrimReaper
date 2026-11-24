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

    def test_non_evidence_job_marker_keeps_fixtures(self):
        root = self.setup_job(marker=False)
        self.put(root, ".ut-retention-customer", b"")
        self.assertEqual(self.fixtures_item(root)["retention"], "customer")

    def _marked(self, marker):
        root = self.setup_job(marker=False)
        self.put(root, marker, b"")
        return self.fixtures_item(root)["retention"]

    def test_release_job_marker_keeps_fixtures(self):
        self.assertNotEqual(self._marked(".ut-retention-release"), "disposable")

    def test_ambiguous_job_marker_keeps_fixtures(self):
        self.assertNotEqual(self._marked(".ut-retention-ambiguous"), "disposable")

    def test_offload_checks_children_for_fixtures(self):
        base = self.home / "jobs"
        (base / "J-0071" / "fixtures").mkdir(parents=True)
        self.assertEqual(offload.fixture_refusal(base), "holds-fixtures-never-moved")

    def test_repository_test_data_named_fixtures_moves(self):
        repo = self.home / "dev" / "app"
        (repo / "tests" / "fixtures").mkdir(parents=True)
        (repo / "tests" / "fixtures" / "case.json").write_text("{}")
        (repo / "fixtures").mkdir()
        (repo / "fixtures" / "seed.sql").write_text("")
        self.assertIsNone(offload.fixture_refusal(repo))
        self.assertIsNone(offload.fixture_refusal(repo / "tests"))
        worktree = self.home / "jobs" / "J-0072" / "wt"
        (worktree / "scripts" / "fixtures").mkdir(parents=True)
        self.assertIsNone(offload.fixture_refusal(worktree))

    def test_job_scaffolding_fixtures_still_never_move(self):
        holder = self.home / "dev" / "other"
        (holder / "fixtures" / "ut-fixture-J-0073-ab").mkdir(parents=True)
        self.assertEqual(offload.fixture_refusal(holder), "holds-fixtures-never-moved")
        self.assertEqual(offload.fixture_refusal(holder / "fixtures"), "fixture-never-moved")
        job = self.home / "jobs" / "J-0074"
        (job / "fixtures").mkdir(parents=True)
        self.assertEqual(offload.fixture_refusal(job / "fixtures"), "fixture-never-moved")
        self.assertEqual(offload.fixture_refusal(job), "holds-fixtures-never-moved")

    def test_offload_refuses_child_named_fixtures_and_oversized_folders(self):
        base = self.home / "many"
        (base / "ut-fixture-x").mkdir(parents=True)
        self.assertEqual(offload.fixture_refusal(base), "holds-fixtures-never-moved")
        other = self.home / "wide"
        other.mkdir()
        for n in range(2001):
            (other / f"d{n}").mkdir()
        self.assertEqual(offload.fixture_refusal(other), "fixtures-unreadable")

    def test_weight_left_out_of_archive_only_inside_fixtures(self):
        for parent, expect_left_out in (("fixtures/ut-fixture-J/model", True), ("repo/models", False), ("fixtures", True)):
            with self.subTest(parent=parent):
                top = self.home / ".cache/trimreaper/jobs/J-0070" / ("a%d" % abs(hash(parent)))
                folder = top / parent
                folder.mkdir(parents=True)
                (folder / "w.safetensors").write_bytes(b"x" * (101 * 2 ** 20))
                item = {"path": str(top), "root": str(top), "class": "job-cache-subdir"}
                try:
                    result = core.reap_archive_collect(item, [], None, loose=True)
                except Exception as error:  # a refusal must name its cause, never hide the weight decision
                    self.fail(f"collect refused: {error}")
                left = [e for e in result["skipped"] if "model-weight" in str(e)]
                self.assertEqual(bool(left), expect_left_out, result)
