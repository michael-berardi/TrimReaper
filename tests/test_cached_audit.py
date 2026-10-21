"""A cached audit is a hint, never an authority.

When the audit's sizing budget runs out, the jobs after the cursor show the compact audit of an earlier pass
(meta `reap:audit:<job>`) for up to a day. Every path in that view is proved again, metadata only, before it can
count as reapable: a path that is gone counts for nothing, and a path that cannot be read, that was replaced or that
is a link now is kept but never reapable. The revalidation reads no content, enters no folder and spends no sizing
budget.
"""
from __future__ import annotations

import json
import os
import shutil
from contextlib import closing
from unittest import mock

from trimreaper import core
from helpers import Home


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
