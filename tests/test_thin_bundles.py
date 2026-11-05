"""`archiveBundleMode: "thin-local"`: archive bundles that hold only what the main repository lacks.

A bundle is a full, self-contained copy of the repository: 1.2 GB each for unsaved commits of a few megabytes. Under
`thin-local` a LINKED worktree's bundle excludes everything reachable from the default remote branch of its source repository
(`refs/remotes/<remote>/HEAD`, else main/master) that the persistent main repository (the worktree's common git directory,
outside the job-cache, scratch and archive roots) also holds. The manifest records the prerequisite commits and the main
repository; the bundle is verified by `git bundle verify` inside that repository plus the coverage check. A missing main
repository, one inside a reaped root, or any failed verification falls back to the self-contained bundle. Restoring a thin
bundle needs the main repository: that is the trade-off, and it is documented.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest import mock

from trimreaper import core

from helpers import GIT_ENV
from test_scratch_unsaved import UnsavedFixtures


class ThinBundles(UnsavedFixtures):
    def archive_for(self, task: str, mode: str | None = None, days: float = 9):
        if mode:
            self.write_config({"policy": {"archiveBundleMode": mode}})
        self.age(self.scratch(task), days)
        out = self.cli("reap", "--scratch")
        self.assertEqual(out["deleted"], 1, out)
        (archive,) = self.unsaved_archives()
        manifest = self.manifest(archive)
        (repo,) = [r for r in manifest["repositories"] if r["mode"] == "git"]
        return archive, manifest, repo

    # ---- the default is unchanged ------------------------------------------------------------------------------------

    def test_the_default_bundle_is_self_contained(self):
        self.main_with_worktree("full", big=True)

        archive, manifest, repo = self.archive_for("full")

        self.assertTrue(repo["bundle"]["selfContained"])
        self.assertNotIn("thin", repo["bundle"])
        self.assertGreater(repo["bundle"]["bytes"], 250_000)  # the pushed history is in it
        self.restored(archive / repo["bundle"]["file"])  # and a restore needs nothing else

    # ---- thin-local ---------------------------------------------------------------------------------------------------

    def test_a_thin_bundle_holds_only_the_commits_the_remote_branch_lacks(self):
        main, worktree = self.main_with_worktree("thin", big=True)
        head = self.git(worktree, "rev-parse", "HEAD").strip()
        base = self.git(main, "rev-parse", "refs/remotes/origin/main").strip()

        archive, manifest, repo = self.archive_for("thin", "thin-local")

        bundle = repo["bundle"]
        self.assertFalse(bundle["selfContained"])
        self.assertLess(bundle["bytes"], 20_000, bundle)  # a few commits, not the 300 KB history
        thin = bundle["thin"]
        self.assertEqual(thin["mainRepository"], str(main / ".git"))
        self.assertEqual(thin["prerequisites"], [base])
        self.assertEqual(thin["baseRef"], "refs/remotes/origin/main")
        self.assertEqual(thin["baseCommit"], base)
        self.assertNotIn("thinFallback", bundle)
        self.assertIn("THIN", manifest["restore"])
        self.assertIn("inside its recorded main repository", manifest["verification"]["bundles"])
        # restorable in the main repository: the verify and the fetch both work there
        target = archive / bundle["file"]
        verify = subprocess.run(["git", "-C", str(main), "bundle", "verify", str(target)], capture_output=True, text=True, env={**os.environ, **GIT_ENV})
        self.assertEqual(verify.returncode, 0, verify.stderr)
        fetch = subprocess.run(["git", "-C", str(main), "fetch", "-q", str(target), "refs/*:refs/ut-archive/*"], capture_output=True, text=True, env={**os.environ, **GIT_ENV})
        self.assertEqual(fetch.returncode, 0, fetch.stderr)
        self.assertEqual(self.git(main, "rev-parse", "refs/ut-archive/heads/ut-archive/0").strip(), head)

    def test_a_thin_bundle_cannot_be_restored_without_its_main_repository(self):
        self.main_with_worktree("needs-main", big=True)

        archive, manifest, repo = self.archive_for("needs-main", "thin-local")

        empty = self.tmp / "empty-restore"
        subprocess.run(["git", "init", "-q", "--bare", str(empty)], check=True, capture_output=True)
        done = subprocess.run(["git", "-C", str(empty), "bundle", "verify", str(archive / repo["bundle"]["file"])], capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)  # the documented trade-off

    def test_the_default_remote_head_names_the_base(self):
        main, worktree = self.main_with_worktree("headref", big=True)
        self.git(main, "update-ref", "refs/remotes/origin/trunk", self.git(main, "rev-parse", "refs/remotes/origin/main").strip())
        self.git(main, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")

        archive, manifest, repo = self.archive_for("headref", "thin-local")

        self.assertEqual(repo["bundle"]["thin"]["baseRef"], "refs/remotes/origin/trunk")

    # ---- the fallbacks -------------------------------------------------------------------------------------------------

    def test_a_main_repository_inside_a_reaped_root_falls_back_to_a_self_contained_bundle(self):
        # the "main" repository lives in the scratch root, which the reaper empties: it cannot be the persistent half
        remote = self.clone("origin-app")
        main = self.scratch("a-main-repo")
        subprocess.run(["git", "clone", "-q", str(self.remotes / "origin-app.git"), str(main)], check=True, capture_output=True)
        worktree = self.scratch("moved") / "wt"
        worktree.parent.mkdir(parents=True)
        self.git(main, "worktree", "add", "-q", "-b", "moved-work", str(worktree))
        (worktree / "unsaved.txt").write_text("unsaved\n")
        self.git(worktree, "add", "unsaved.txt")
        self.git(worktree, "commit", "-q", "-m", "unsaved")
        # only the task folder is old; the "main" repository stays recent and is not reaped
        self.write_config({"policy": {"archiveBundleMode": "thin-local"}})
        self.age(self.scratch("moved"), 9)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 1, out)
        (archive,) = self.unsaved_archives()
        (repo,) = [r for r in self.manifest(archive)["repositories"] if r["mode"] == "git"]
        self.assertTrue(repo["bundle"]["selfContained"], repo["bundle"])
        self.assertEqual(repo["bundle"]["thinFallback"], "thin-main-in-reaped-root")
        self.restored(archive / repo["bundle"]["file"])

    def test_a_missing_main_repository_falls_back_to_a_self_contained_bundle(self):
        self.main_with_worktree("gone", big=True)
        real = core.reap_archive_main_repository

        def missing(common, item_path):
            real(common, item_path)
            raise core.ReapRefused("thin-main-missing")

        with mock.patch.object(core, "reap_archive_main_repository", side_effect=missing):
            archive, manifest, repo = self.archive_for("gone", "thin-local")

        self.assertTrue(repo["bundle"]["selfContained"])
        self.assertEqual(repo["bundle"]["thinFallback"], "thin-main-missing")
        self.assertGreater(repo["bundle"]["bytes"], 250_000)
        self.restored(archive / repo["bundle"]["file"])

    def test_the_main_repository_check_names_a_missing_or_foreign_directory(self):
        with self.assertRaises(core.ReapRefused) as caught:
            core.reap_archive_main_repository(str(self.tmp / "no-such" / ".git"), str(self.scratch("x")))
        self.assertEqual(str(caught.exception), "thin-main-missing")
        plain = self.tmp / "plain" / ".git"
        plain.mkdir(parents=True)
        with self.assertRaises(core.ReapRefused) as caught:
            core.reap_archive_main_repository(str(plain), str(self.scratch("x")))
        self.assertEqual(str(caught.exception), "thin-main-not-a-repository")

    def test_a_verification_failure_in_the_main_repository_falls_back(self):
        main, worktree = self.main_with_worktree("badverify", big=True)
        real = core.reap_archive_git

        def failing(gitdir, *args, **kwargs):
            if gitdir == str(main / ".git") and args[:2] == ("bundle", "verify"):
                return 1, "prerequisite missing"
            return real(gitdir, *args, **kwargs)

        with mock.patch.object(core, "reap_archive_git", side_effect=failing):
            archive, manifest, repo = self.archive_for("badverify", "thin-local")

        self.assertTrue(repo["bundle"]["selfContained"])
        self.assertTrue(repo["bundle"]["thinFallback"].startswith("archive-verify-failed:bundle-verify-main"), repo["bundle"])

    def test_a_coverage_failure_falls_back(self):
        self.main_with_worktree("badcover", big=True)
        real = core.reap_archive_git

        def failing(gitdir, *args, **kwargs):
            if args[:2] == ("rev-list", "--count") and kwargs.get("data") and "^" in kwargs["data"] and "ut-archive" not in kwargs["data"] \
                    and ".bundle-work" in gitdir and not getattr(failing, "done", False):
                failing.done = True
                return 0, "1\n"  # a tip the bundle would not cover
            return real(gitdir, *args, **kwargs)

        with mock.patch.object(core, "reap_archive_git", side_effect=failing):
            archive, manifest, repo = self.archive_for("badcover", "thin-local")

        self.assertTrue(repo["bundle"]["selfContained"])
        self.assertTrue(repo["bundle"]["thinFallback"].startswith("archive-verify-failed:bundle-coverage"), repo["bundle"])

    def test_an_unknown_bundle_mode_is_an_error(self):
        from trimreaper import host
        with self.assertRaises(host.HostError):
            host.check_policy({"archiveBundleMode": "thin"})
        host.check_policy({"archiveBundleMode": "thin-local"})
        self.assertEqual(host.DEFAULT_POLICY["archiveBundleMode"], "self-contained")

    def test_the_default_policy_keys_are_documented_values(self):
        from trimreaper import host
        self.assertEqual((host.DEFAULT_POLICY["unsavedArchiveDays"], host.DEFAULT_POLICY["interruptedExpiryDays"]), (7, 7))
        for key in ("unsavedArchiveDays", "interruptedExpiryDays"):
            with self.assertRaises(host.HostError):
                host.check_policy({key: -1})
            with self.assertRaises(host.HostError):
                host.check_policy({key: "7"})
