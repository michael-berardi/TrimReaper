"""A kept folder is named by the engine's exact fence, never only by its bare class (J-4706).

A finished job's `dist` that is not recognisable web output is kept for a structural reason the engine derives itself
(`dist-not-web-output`). The audit's per-item line used to report only `retention-preserved:unmarked`, so the operator
could not see why; the same text was lost again when the job was shown from the cached audit. Every refusal here is
still a refusal: the fence is a label, and a cached entry still never authorizes a removal.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest import mock

from trimreaper import core
from helpers import Home

FENCE = "dist-not-web-output"


class QuietReason(Home):
    def setUp(self):
        super().setUp()
        self.repo = self.clone("app-j0100", tracked={"package.json": json.dumps({"name": "fixture", "scripts": {"build": "vite build"}})})
        self.put(self.repo, "dist/app.js", b"x" * 1500)
        self.put(self.repo, "dist/client/model.glb", b"g" * 2048)  # not a web output type: the allowlist keeps the dist
        self.add_job("J-0100", self.repo, finished_ago=3600)

    @staticmethod
    def entry(out: dict, path: Path) -> dict | None:
        for job in out["jobs"]:
            for item in job["detail"] or []:
                if item["path"] == str(path):
                    return item
        return None

    def test_the_audit_line_states_the_exact_fence_and_the_guarded_reap_still_refuses(self):
        dist = self.repo / "dist"
        out = core.reap_audit(None, 40, True, 60)
        item = self.entry(out, dist)
        self.assertIsNotNone(item, out["jobs"])
        self.assertEqual((item["state"], item["retention"]), ("blocked", "unmarked"))
        self.assertEqual(item["quietReason"], FENCE)                                  # the engine's own derivation, on the entry
        self.assertEqual(item["blocking"], "retention-preserved:" + FENCE)            # and the per-item line names it
        result = core.reap_item(dict(item), "dry-run")
        self.assertEqual(result["outcome"], "refused")
        self.assertEqual(result["reason"], "retention-preserved:" + FENCE)
        self.assertEqual(core.reap_item(dict(item))["reason"], "retention-preserved:" + FENCE)
        self.assertTrue((dist / "client" / "model.glb").exists())

    def test_a_cached_hold_gives_its_dist_walk_only_the_listings_own_slice(self):
        # The cached listing gives each stored hold a few seconds to prove itself; the `dist` walk it starts must spend that
        # slice and no more, never the whole audit's ceiling.
        stored = {"path": str(self.repo / "dist"), "class": "job-clone-regenerable", "kind": "dist", "owner": "J-0100",
                  "jobs": ["J-0100"], "root": str(self.repo), "ownership": "name-pattern", "retention": "unmarked",
                  "state": "blocked", "blocking": "retention-preserved:unmarked", "bytes": 1}
        seen = {}
        original = core.reap_dist_keep_reason
        def recording(path, deadline=None):
            seen.setdefault("deadline", deadline)
            seen.setdefault("now", time.monotonic())
            return original(path, deadline)
        with mock.patch.object(core, "reap_dist_keep_reason", recording):
            kept = core.reap_audit_cached_retention(dict(stored))
        self.assertIn("deadline", seen, "the dist walk ran without asking this listing what it can afford")
        self.assertIsNotNone(seen["deadline"], "the dist walk got no deadline at all")
        self.assertLessEqual(seen["deadline"] - seen["now"], core.REAP_AUDIT_CACHED_PROOF + 0.05)
        self.assertEqual((kept["state"], kept["blocking"]), ("blocked", "retention-preserved:" + FENCE))
        # and a listing whose slice is already spent starts no walk at all: the stored hold stands, named or not, never promoted
        called = []
        def refuse_to_scan(path, deadline=None):
            called.append(path)
            raise AssertionError("a listing with no budget left walked a folder")
        previous = getattr(core.REAP_PROBE, "cachedProof", None)
        try:
            core.REAP_PROBE.cachedProof = time.monotonic() - 1
            with mock.patch.object(core, "reap_dist_keep_reason", refuse_to_scan):
                spent = core.reap_audit_cached_retention(dict(stored))
        finally:
            core.REAP_PROBE.cachedProof = previous
        self.assertEqual(called, [])
        self.assertEqual((spent["state"], spent["retention"]), ("blocked", "unmarked"))

    def test_a_stored_reason_never_survives_a_derivation_that_proves_none(self):
        # quietReason is part of the stored identity, so a reason an earlier pass wrote must not be echoed as current when the
        # fresh derivation proves nothing, and must be replaced when it proves a different one.
        stored = {"path": str(self.repo / "dist"), "class": "job-clone-regenerable", "kind": "dist", "owner": "J-0100",
                  "jobs": ["J-0100"], "root": str(self.repo), "ownership": "name-pattern", "retention": "unmarked",
                  "state": "blocked", "blocking": "retention-preserved:unmarked", "quietReason": "dist-release-bearing"}
        with mock.patch.object(core, "reap_item_retention", lambda item: "unmarked"):
            kept = core.reap_audit_cached_retention(dict(stored))
        self.assertEqual((kept["state"], kept["blocking"]), ("blocked", "retention-preserved:unmarked"))
        self.assertNotIn("quietReason", kept)
        def proves_fence(item):
            item["quietReason"] = FENCE
            return "unmarked"
        with mock.patch.object(core, "reap_item_retention", proves_fence):
            named = core.reap_audit_cached_retention(dict(stored))
        self.assertEqual((named["quietReason"], named["blocking"]), (FENCE, "retention-preserved:" + FENCE))
        self.assertEqual(named["state"], "blocked")

    def test_a_scoped_stored_hold_also_walks_inside_the_listings_slice(self):
        # Every branch that re-derives a stored hold spends the listing's slice, not only the name-fence branch: a scratch
        # dist goes through reap_scoped_fence directly.
        scratch = self.home / "scratch" / "J-0100"
        (scratch / "dist" / "client").mkdir(parents=True)
        (scratch / "dist" / "client" / "model.glb").write_bytes(b"g" * 64)  # not web output: the walk has to happen
        (scratch / "package.json").write_text(json.dumps({"name": "fixture", "scripts": {"build": "vite build"}}))
        stored = {"path": str(scratch / "dist"), "class": "scratch-regenerable", "kind": "dist", "owner": "J-0100",
                  "jobs": ["J-0100"], "root": str(scratch), "ownership": "scratch-checkout", "retention": "unmarked",
                  "state": "blocked", "blocking": "retention-preserved:unmarked", "bytes": 1}
        seen = {}
        real_walk = core.reap_dist_keep_reason
        def walk(path, deadline=None):
            seen.setdefault("deadline", deadline)
            seen.setdefault("now", time.monotonic())
            return real_walk(path, deadline)
        with mock.patch.object(core, "reap_dist_keep_reason", walk):
            kept = core.reap_audit_cached_retention(dict(stored))
        self.assertIn("deadline", seen, "the scoped stored hold never walked its dist, so this test proved nothing")
        self.assertIsNotNone(seen["deadline"], "a scoped stored hold walked its dist without this listing's budget")
        self.assertLessEqual(seen["deadline"] - seen["now"], core.REAP_AUDIT_CACHED_PROOF + 0.05)
        self.assertEqual((kept["state"], kept["retention"]), ("blocked", "unmarked"))

    def test_a_cached_hold_states_the_fresh_fence_and_is_never_promoted(self):
        dist = self.repo / "dist"
        first = core.reap_audit(None, 40, True, 60)
        stored = {"path": str(dist), "class": "job-clone-regenerable", "kind": "dist", "owner": "J-0100", "jobs": ["J-0100"],
                  "root": str(self.repo), "ownership": "name-pattern", "retention": "unmarked", "state": "blocked",
                  "blocking": "retention-preserved:unmarked", "bytes": 1}  # as an earlier pass stored the class alone
        kept = core.reap_audit_cached_retention(dict(stored))
        self.assertEqual(kept["state"], "blocked")
        self.assertEqual(kept["blocking"], "retention-preserved:" + FENCE)
        self.assertEqual(kept["quietReason"], FENCE)
        # a stored hold the engine now derives as disposable is named as changed and stays blocked: never promoted
        with mock.patch.object(core, "reap_clone_expired", lambda *args, **kwargs: True):
            stale = core.reap_audit_cached_retention(dict(stored))
        self.assertEqual((stale["state"], stale["blocking"]), ("blocked", "audit-retention-changed"))
        self.assertIsNotNone(self.entry(first, dist))
        self.assertTrue((dist / "app.js").exists())
