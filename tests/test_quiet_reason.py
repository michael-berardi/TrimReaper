"""A kept folder is named by the engine's exact fence, never only by its bare class (J-4706).

A finished job's `dist` that is not recognisable web output is kept for a structural reason the engine derives itself
(`dist-not-web-output`). The audit's per-item line used to report only `retention-preserved:unmarked`, so the operator
could not see why; the same text was lost again when the job was shown from the cached audit. Every refusal here is
still a refusal: the fence is a label, and a cached entry still never authorizes a removal.
"""
from __future__ import annotations

import json
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
