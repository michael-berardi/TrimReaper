"""A cached audit is a hint, never an authority.

When the audit's sizing budget runs out, the jobs after the cursor show the compact audit of an earlier pass
(meta `reap:audit:<job>`) for up to a day. Every path in that view is proved again, metadata only, before it can
count as reapable: a path that is gone counts for nothing, and a path that cannot be read, that was replaced or that
is a link now is kept but never reapable. The revalidation reads no content, enters no folder and spends no sizing
budget. A stored retention hold is re-derived from that identity under a small names-only budget, and the stored hold
stands when the budget is gone.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from contextlib import closing
from unittest import mock

from trimreaper import core
from helpers import DAY, Home


class CachedAudit(Home):
    def setUp(self):
        super().setUp()
        self.repo = self.clone("app-j0101")
        self.put(self.repo, "node_modules/pkg/index.js", b"x" * 4096)
        self.put(self.repo, "target/build.bin", b"y" * 8192)
        self.add_job("J-0100", None, finished_ago=3600)      # audited first, so the job below is served from the cache
        self.add_job("J-0101", self.repo, finished_ago=3600)
        self.first = core.reap_audit(None, 40, True, 60, persist=True)   # the pass that writes the cache

    def audit(self, budget: int = 0) -> dict:
        """A pass whose budget is spent before the second job: that job is shown from the cache."""
        return core.reap_audit(None, 40, True, budget)

    def entry(self, out: dict, path) -> dict | None:
        for job in out["jobs"]:
            for item in job["detail"] or []:
                if item["path"] == str(path):
                    return item
        return None

    def test_the_cached_view_is_used_and_the_fresh_audit_still_sees_everything(self):
        self.assertEqual(self.first["summary"]["audit"]["cachedJobs"], 0)
        self.assertEqual(self.entry(self.first, self.repo / "target")["state"], "reapable")
        after = self.audit()
        self.assertEqual(after["summary"]["audit"]["cachedJobs"], 1)
        self.assertEqual([job["job"] for job in after["jobs"]], ["J-0101"])
        self.assertTrue(after["jobs"][0]["cached"])

    def test_a_cached_path_that_is_gone_counts_for_nothing(self):
        target = self.repo / "target"
        before = self.entry(self.first, target)
        self.assertEqual(before["state"], "reapable")
        shutil.rmtree(target)

        after = self.audit()

        self.assertIsNone(self.entry(after, target), "a path that is gone must not be listed at all")
        self.assertEqual(after["summary"]["reapableBytes"], self.first["summary"]["reapableBytes"] - before["bytes"])
        self.assertTrue(any("cached audit" in note for note in after["incomplete"]), after["incomplete"])

    def test_a_partly_missing_cached_job_keeps_the_paths_that_are_still_there(self):
        target, modules = self.repo / "target", self.repo / "node_modules"
        kept = self.entry(self.first, modules)
        shutil.rmtree(target)

        after = self.audit()

        self.assertIsNone(self.entry(after, target))
        still = self.entry(after, modules)
        self.assertEqual(still["state"], "reapable")
        self.assertEqual(still["bytes"], kept["bytes"])
        self.assertEqual(after["summary"]["reapableBytes"], kept["bytes"])

    def test_an_unchanged_cached_path_still_counts_with_its_own_bytes(self):
        after = self.audit()

        for path in (self.repo / "target", self.repo / "node_modules"):
            before, now = self.entry(self.first, path), self.entry(after, path)
            self.assertEqual(now["state"], "reapable")
            self.assertEqual(now["bytes"], before["bytes"])
        self.assertEqual(after["summary"]["reapableBytes"], self.first["summary"]["reapableBytes"])

    def denied(self, path, error: OSError):
        real = os.lstat

        def lstat(candidate, *args, **kwargs):
            if str(candidate) == str(path):
                raise error
            return real(candidate, *args, **kwargs)

        return lstat

    def test_an_unreadable_cached_path_is_never_reapable(self):
        target, modules = self.repo / "target", self.repo / "node_modules"
        kept = self.entry(self.first, modules)

        with mock.patch.object(core.os, "lstat", self.denied(target, PermissionError(13, "Permission denied"))):
            after = self.audit()

        item = self.entry(after, target)
        self.assertIsNotNone(item)
        self.assertEqual(item["state"], "blocked")
        self.assertEqual(item["blocking"], "audit-path-unreadable")
        self.assertEqual(item["bytes"], 0)
        self.assertTrue(item["sizeIncomplete"])
        self.assertEqual(after["summary"]["reapableBytes"], kept["bytes"], "the unreadable path is not counted at all")

    def test_an_unknown_lstat_is_never_reapable(self):
        target, modules = self.repo / "target", self.repo / "node_modules"
        kept = self.entry(self.first, modules)

        with mock.patch.object(core.os, "lstat", self.denied(target, OSError(5, "Input/output error"))):
            after = self.audit()

        item = self.entry(after, target)
        self.assertIsNotNone(item)
        self.assertEqual(item["state"], "blocked")
        self.assertEqual(item["blocking"], "audit-path-unknown")
        self.assertEqual(item["bytes"], 0)
        self.assertEqual(after["summary"]["reapableBytes"], kept["bytes"])

    def test_a_cached_path_that_became_a_link_is_never_reapable(self):
        target, modules = self.repo / "target", self.repo / "node_modules"
        kept = self.entry(self.first, modules)
        shutil.rmtree(target)
        other = self.repo / "other"
        other.mkdir()
        os.symlink(other, target)

        after = self.audit()

        item = self.entry(after, target)
        self.assertIsNotNone(item)
        self.assertEqual(item["state"], "blocked")
        self.assertEqual(item["blocking"], "audit-path-symlink")
        self.assertEqual(item["bytes"], 0)
        self.assertEqual(after["summary"]["reapableBytes"], kept["bytes"])

    def test_a_cached_path_that_was_replaced_is_never_reapable(self):
        target, modules = self.repo / "target", self.repo / "node_modules"
        kept = self.entry(self.first, modules)
        shutil.rmtree(target)
        target.mkdir()

        after = self.audit()

        item = self.entry(after, target)
        self.assertIsNotNone(item)
        self.assertEqual(item["state"], "blocked")
        self.assertEqual(item["blocking"], "audit-path-changed")
        self.assertEqual(item["bytes"], 0)
        self.assertEqual(after["summary"]["reapableBytes"], kept["bytes"])

    def test_a_legacy_cache_without_identity_is_never_reapable(self):
        with closing(self.state()) as conn, conn:
            stored = conn.execute("SELECT value FROM meta WHERE key='reap:audit:J-0101'").fetchone()
            cached = json.loads(stored[0])
            for entry in cached["entries"]:
                entry.pop("dev")
                entry.pop("ino")
            conn.execute("UPDATE meta SET value=? WHERE key='reap:audit:J-0101'", (json.dumps(cached),))

        after = self.audit()

        self.assertEqual(after["summary"]["reapableBytes"], 0)
        for path in (self.repo / "target", self.repo / "node_modules"):
            item = self.entry(after, path)
            self.assertEqual(item["state"], "blocked")
            self.assertEqual(item["blocking"], "audit-path-unknown")
            self.assertEqual(item["bytes"], 0)
        fresh = core.reap_audit(None, 40, True, 60, persist=True)
        self.assertEqual(fresh["summary"]["reapableBytes"], self.first["summary"]["reapableBytes"])
        self.assertEqual(self.audit()["summary"]["reapableBytes"], self.first["summary"]["reapableBytes"])

    def test_identity_is_checked_even_when_the_mtime_matches(self):
        target = self.repo / "target"
        before = self.entry(self.first, target)
        target.rename(self.repo / "old-target")  # retain the old inode so it cannot be reused
        target.mkdir()
        old = os.lstat(self.repo / "old-target")
        os.utime(target, ns=(old.st_atime_ns, old.st_mtime_ns))

        item = self.entry(self.audit(), target)

        self.assertEqual(item["mtime"], before["mtime"])
        self.assertEqual(item["state"], "blocked")
        self.assertEqual(item["blocking"], "audit-path-changed")
        self.assertEqual(item["bytes"], 0)

    def test_missing_metadata_cannot_prove_a_cached_object(self):
        before = self.entry(self.first, self.repo / "target")
        for key in ("dev", "ino", "mtime"):
            with self.subTest(key=key):
                entry = {k: value for k, value in before.items() if k != key}
                result = core.reap_audit_revalidate(entry)
                self.assertEqual(result["blocking"], "audit-path-unknown")
                self.assertEqual(result["bytes"], 0)

    def test_the_cached_audit_spends_no_sizing_budget(self):
        sized: list[str] = []
        real = core.reap_metadata_size

        def record(path, *args, **kwargs):
            sized.append(str(path))
            return real(path, *args, **kwargs)

        with mock.patch.object(core, "reap_metadata_size", record):
            after = self.audit()

        self.assertNotIn(str(self.repo / "target"), sized)
        self.assertNotIn(str(self.repo / "node_modules"), sized)
        self.assertEqual(after["summary"]["audit"]["cachedJobs"], 1)

    def test_the_cache_keeps_the_identity_of_the_path_it_audited(self):
        with closing(self.state()) as conn:
            stored = conn.execute("SELECT value FROM meta WHERE key='reap:audit:J-0101'").fetchone()
        entries = json.loads(stored[0])["entries"]
        for entry in entries:
            self.assertIn("dev", entry)
            self.assertIn("ino", entry)
            self.assertIn("mtime", entry)
        self.assertEqual({entry["path"] for entry in entries},
                         {str(self.repo / "target"), str(self.repo / "node_modules")})


class CachedRetentionIdentity(Home):
    """A cached entry keeps the identity its retention is derived from, so the cached view states the retention a fresh pass
    states now (J-4588: `<cache>/J-3814/evidence` was shown as `retention-preserved:unmarked` from a cached record for a day
    while a fresh pass derived `disposable` and the guarded reap deleted it). The record that a pass writes carries the
    audited item's owner, root and derived retention; a record of the old half-item shape is never read, so its stale verdict
    cannot surface as a hold; and a stored hold whose retention no longer holds is renamed, never promoted to reapable."""

    def setUp(self):
        super().setUp()
        self.cache = core.reap_cache_root() / "J-0200"
        self.evidence = self.cache / "evidence"
        self.file = self.put(self.cache, "evidence/review-diff.txt", b"x" * 1024)
        self.add_job("J-0001", None, finished_ago=3 * DAY)  # audited first, so the job below is served from the cache
        self.add_job("J-0200", None, finished_ago=30 * DAY)  # past evidenceExpiryDays (14 by default): the folder derives disposable
        self.first = core.reap_audit(None, 40, True, 60, persist=True)  # the tick's pass writes the compact record

    def record(self) -> dict:
        with closing(self.state()) as conn:
            stored = conn.execute("SELECT value FROM meta WHERE key='reap:audit:J-0200'").fetchone()
        return json.loads(stored[0])

    def store(self, record: dict) -> None:
        with closing(self.state()) as conn, conn:
            conn.execute("UPDATE meta SET value=? WHERE key='reap:audit:J-0200'", (json.dumps(record),))

    def job(self, job: str = "J-0200") -> dict:
        with core.db() as conn:
            return core.row(conn, "SELECT * FROM jobs WHERE id=?", (job,))

    def fresh_item(self, path, job: str = "J-0200") -> dict:
        items, _ = core.reap_job_items(self.job(job), [])
        return {i["path"]: i for i in items}[str(path)]

    def entry(self, out: dict, path) -> dict | None:
        for job in out["jobs"]:
            for item in job["detail"] or []:
                if item["path"] == str(path):
                    return item
        return None

    def cached(self, out: dict, path) -> dict | None:
        """One job's cached entry for `path`: a zero budget spends the pass before it audits the job."""
        return self.entry(out, path)

    def test_a_cached_entry_keeps_the_owner_and_root_and_derives_the_retention_a_fresh_item_derives(self):
        record = self.record()
        self.assertEqual(record["version"], core.REAP_AUDIT_RECORD_VERSION)
        cached = {e["path"]: e for e in record["entries"]}[str(self.evidence)]

        self.assertEqual(cached["owner"], "J-0200")
        self.assertEqual(cached["root"], str(core.reap_canon(self.cache)))
        self.assertEqual(cached["retention"], core.REAP_DISPOSABLE)
        fresh = self.fresh_item(self.evidence)
        self.assertEqual(core.reap_item_retention(dict(fresh)), core.reap_item_retention(dict(cached)))
        self.assertEqual(core.reap_item_retention(dict(cached)), core.REAP_DISPOSABLE)

    def test_the_cached_listing_states_the_same_retention_without_becoming_authority(self):
        after = core.reap_audit(None, 40, True, 0)

        self.assertGreaterEqual(after["summary"]["audit"]["cachedJobs"], 1)
        self.assertTrue(after["jobs"][0]["cached"])
        item = self.cached(after, self.evidence)
        self.assertTrue(item["cached"])
        self.assertEqual(item["retention"], core.REAP_DISPOSABLE)
        self.assertEqual(item["owner"], "J-0200")
        self.assertEqual(item["root"], str(core.reap_canon(self.cache)))
        self.assertEqual(after["deleted"], 0, "a cached view never deletes")
        self.assertTrue(self.file.exists())

    def test_the_old_cached_shape_never_surfaces_as_an_unmarked_hold(self):
        record = self.record()
        record.pop("version")
        for entry in record["entries"]:
            for key in ("owner", "root", "jobs", "ownership", "retention"):
                entry.pop(key, None)
            entry.update({"state": "blocked", "blocking": "retention-preserved:unmarked"})
        self.store(record)

        after = core.reap_audit(None, 40, True, 0)

        self.assertEqual(after["jobs"], [], "a half-item record is not a listing; the job is pending, never a hold")
        self.assertEqual(after["summary"]["audit"]["pendingJobs"], 1)
        self.assertNotIn("retention-preserved:unmarked", json.dumps(after))
        rewritten = core.reap_audit(None, 40, True, 60, persist=True)  # the next pass audits it afresh and rewrites the record
        self.assertEqual(rewritten["summary"]["audit"]["cachedJobs"], 0)
        self.assertNotIn("retention-preserved:unmarked", json.dumps(rewritten))
        self.assertEqual(self.record()["version"], core.REAP_AUDIT_RECORD_VERSION)

    def test_a_stale_cached_hold_is_renamed_and_never_promoted_to_reapable(self):
        record = self.record()
        for entry in record["entries"]:
            entry.update({"state": "blocked", "blocking": "retention-preserved:unmarked", "retention": "unmarked"})
        self.store(record)

        after = core.reap_audit(None, 40, True, 0)

        item = self.cached(after, self.evidence)
        self.assertIsNotNone(item)
        self.assertEqual(item["state"], "blocked", "a cached entry never authorizes a deletion")
        self.assertEqual(item["blocking"], "audit-retention-changed")
        self.assertEqual(item["retention"], core.REAP_DISPOSABLE)
        self.assertEqual(after["summary"]["reapableBytes"], 0)
        self.assertNotIn("retention-preserved:unmarked", json.dumps(after))
        self.assertTrue(self.file.exists())


class CachedProofIsBounded(Home):
    """The cached listing stays cheap: re-deriving one stored hold is names-only work under a small budget, and when that budget
    is gone the stored hold stands. A refused or unfinished scan must never become a disposable answer (J-4588 review)."""

    def setUp(self):
        super().setUp()
        self.cache = core.reap_cache_root() / "J-0202"
        self.evidence = self.cache / "evidence"
        self.file = self.put(self.cache, "evidence/review-diff.txt", b"z" * 512)
        self.add_job("J-0001", None, finished_ago=3 * DAY)  # audited first, so the job below is served from the cache
        self.add_job("J-0202", None, finished_ago=30 * DAY)  # past evidenceExpiryDays: it derives disposable now
        core.reap_audit(None, 40, True, 60, persist=True)

    def record(self) -> dict:
        with closing(self.state()) as conn:
            return json.loads(conn.execute("SELECT value FROM meta WHERE key='reap:audit:J-0202'").fetchone()[0])

    def store(self, record: dict) -> None:
        with closing(self.state()) as conn, conn:
            conn.execute("UPDATE meta SET value=? WHERE key='reap:audit:J-0202'", (json.dumps(record),))

    def stale(self) -> None:
        record = self.record()
        for entry in record["entries"]:
            entry.update({"state": "blocked", "blocking": "retention-preserved:unmarked", "retention": "unmarked"})
        self.store(record)

    def test_a_cached_hold_is_never_re_derived_without_time_left_to_prove_it(self):
        self.stale()

        def no_walk(*args, **kwargs):
            raise AssertionError("the cached listing walked a folder")

        with mock.patch.object(core, "REAP_AUDIT_CACHED_PROOF", 0.0), \
                mock.patch.object(core, "reap_fd_walk", side_effect=no_walk), \
                mock.patch.object(core, "reap_item_retention", side_effect=no_walk):
            after = core.reap_audit(None, 40, True, 0)

        item = after["jobs"][0]["detail"][0]
        self.assertEqual(item["blocking"], "retention-preserved:unmarked", "out of time: the stored hold stands")
        self.assertEqual(item["state"], "blocked")

    def test_the_cached_re_derivation_spends_a_bounded_scan_and_no_more(self):
        self.stale()

        def names(path, deadline=None):
            self.assertIsNotNone(deadline, "the cached re-derivation bounds its name scan")
            self.assertLessEqual(deadline, time.monotonic() + core.REAP_AUDIT_CACHED_PROOF)
            return True

        with mock.patch.object(core, "reap_no_retained_names", side_effect=names), \
                mock.patch.object(core, "reap_no_secret_material", return_value=True):
            after = core.reap_audit(None, 40, True, 0)

        self.assertEqual(after["jobs"][0]["detail"][0]["blocking"], "audit-retention-changed")

    def test_a_listing_whose_proof_budget_is_spent_keeps_every_hold_it_did_not_prove(self):
        self.stale()

        with mock.patch.object(core, "REAP_AUDIT_CACHED_PROOF_PASS", 0.0), \
                mock.patch.object(core, "reap_item_retention", side_effect=AssertionError("no proof budget left")):
            after = core.reap_audit(None, 40, True, 0)

        self.assertEqual(after["jobs"][0]["detail"][0]["blocking"], "retention-preserved:unmarked")

    def test_a_stored_label_without_its_retention_is_never_rewritten(self):
        record = self.record()
        for entry in record["entries"]:
            entry.update({"state": "blocked", "blocking": "retention-preserved:unmarked"})
            entry.pop("retention", None)
        self.store(record)

        after = core.reap_audit(None, 40, True, 0)

        self.assertEqual(after["jobs"][0]["detail"][0]["blocking"], "retention-preserved:unmarked")


class CachedHoldStillRefused(Home):
    """The guarded deletion path still refuses when retention is not disposable, whatever a cached record says (the repair
    must never turn a cached hint into an authority)."""

    def setUp(self):
        super().setUp()
        self.evidence = core.reap_cache_root() / "J-0201" / "evidence"
        self.file = self.put(self.evidence.parent, "evidence/review-diff.txt", b"y" * 512)
        self.add_job("J-0001", None, finished_ago=3600)  # audited first, so the job below is served from the cache
        self.add_job("J-0201", None, finished_ago=3600)  # inside evidenceExpiryDays: no disposable marker, no derived proof

    def test_the_guarded_deletion_of_an_unmarked_folder_is_still_refused(self):
        item = {i["path"]: i for i in core.reap_job_items(self.job(), [])[0]}[str(self.evidence)]

        self.assertEqual(item["retention"], "unmarked")
        result = core.reap_item(item)

        self.assertEqual(result["outcome"], "refused")
        self.assertEqual(result["reason"], "retention-preserved:unmarked")
        self.assertTrue(self.file.exists())
        # refused at the declared-retention gate, before any boundary or git proof ran
        self.assertEqual(result["checks"], ["single-cleanup-worker"])

    def test_the_cached_identity_still_refuses_and_the_cached_view_says_blocked(self):
        first = core.reap_audit(None, 40, True, 60, persist=True)
        self.assertEqual({e["path"]: e for e in first["jobs"][0]["detail"]}[str(self.evidence)]["blocking"], "retention-preserved:unmarked")
        after = core.reap_audit(None, 40, True, 0)
        cached = [e for e in after["jobs"][0]["detail"] if e["path"] == str(self.evidence)][0]

        self.assertEqual(cached["state"], "blocked")
        self.assertEqual(cached["blocking"], "retention-preserved:unmarked")
        result = core.reap_item(dict(cached))  # the cached identity alone, no fresh listing

        self.assertEqual(result["outcome"], "refused")
        self.assertEqual(result["reason"], "retention-preserved:unmarked")
        self.assertTrue(self.file.exists())

    def job(self) -> dict:
        with core.db() as conn:
            return core.row(conn, "SELECT * FROM jobs WHERE id=?", ("J-0201",))
