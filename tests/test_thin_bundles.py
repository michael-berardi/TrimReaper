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


class ThinPins(UnsavedFixtures):
    """SF2: a thin archive pins its prerequisite commits in the main repository (refs/trimreaper/thin/<archive-id>), so an upstream
    force-push followed by a prune and gc there cannot make the bundle unrestorable. The pin ends with the archive."""

    def archive_for(self, task: str, config: dict | None = None, days: float = 9):
        self.write_config({"policy": {"archiveBundleMode": "thin-local", **(config or {})}})
        self.age(self.scratch(task), days)
        out = self.cli("reap", "--scratch")
        self.assertEqual(out["deleted"], 1, out)
        (archive,) = self.unsaved_archives()
        manifest = self.manifest(archive)
        (repo,) = [r for r in manifest["repositories"] if r["mode"] == "git"]
        return archive, manifest, repo, out

    def pins(self, main: Path) -> list[str]:
        return self.git(main, "for-each-ref", "--format=%(refname)", "refs/trimreaper/").split()

    def test_a_thin_archive_pins_its_prerequisites_in_the_main_repository(self):
        main, worktree = self.main_with_worktree("pinned", big=True)
        base = self.git(main, "rev-parse", "refs/remotes/origin/main").strip()

        archive, manifest, repo, out = self.archive_for("pinned")

        thin = repo["bundle"]["thin"]
        self.assertTrue(thin["pinRef"].startswith("refs/trimreaper/thin/"), thin)
        self.assertEqual(self.pins(main), [thin["pinRef"]])
        self.assertEqual(self.git(main, "rev-parse", thin["pinRef"]).strip(), thin["pinCommit"])
        self.assertEqual(self.git(main, "rev-list", "--parents", "-n1", thin["pinRef"]).split()[1:], [base])  # its parents ARE the prerequisites
        self.assertEqual(thin["prerequisites"], [base])

    def test_the_pin_keeps_the_archive_restorable_after_a_force_push_and_gc(self):
        main, worktree = self.main_with_worktree("forced", big=True)
        head = self.git(worktree, "rev-parse", "HEAD").strip()
        base = self.git(main, "rev-parse", "refs/remotes/origin/main").strip()
        archive, manifest, repo, out = self.archive_for("forced")
        # upstream force-pushes a different history; the main repository fetches it, drops the worktree's branch, forgets its
        # reflogs and prunes: the old base is reachable from nothing but the pin
        remote = self.remotes / "app.git"
        other = self.tmp / "rewritten"
        subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True, capture_output=True, env={**os.environ, **GIT_ENV})
        self.git(other, "checkout", "-q", "--orphan", "rewritten")
        (other / "fresh.txt").write_text("a new root\n")
        self.git(other, "add", "fresh.txt")
        self.git(other, "commit", "-q", "-m", "rewritten history")
        self.git(other, "push", "-q", "--force", "origin", "rewritten:main")
        self.git(main, "fetch", "-q", "origin")
        self.assertNotEqual(self.git(main, "rev-parse", "refs/remotes/origin/main").strip(), base)
        self.git(main, "reset", "-q", "--hard", "refs/remotes/origin/main")  # the local main branch followed the rewrite too
        self.git(main, "branch", "-q", "-D", "forced-work")
        self.git(main, "reflog", "expire", "--expire=now", "--all")
        self.git(main, "gc", "-q", "--prune=now")

        self.git(main, "cat-file", "-e", base + "^{commit}")  # still there: only the pin holds it
        target = archive / repo["bundle"]["file"]
        verify = subprocess.run(["git", "-C", str(main), "bundle", "verify", str(target)], capture_output=True, text=True, env={**os.environ, **GIT_ENV})
        self.assertEqual(verify.returncode, 0, verify.stderr)
        fetch = subprocess.run(["git", "-C", str(main), "fetch", "-q", str(target), "refs/*:refs/ut-archive/*"], capture_output=True, text=True, env={**os.environ, **GIT_ENV})
        self.assertEqual(fetch.returncode, 0, fetch.stderr)
        self.assertEqual(self.git(main, "rev-parse", "refs/ut-archive/heads/ut-archive/0").strip(), head)

    def test_the_pin_is_removed_when_the_archive_expires(self):
        main, worktree = self.main_with_worktree("expires", big=True)
        archive, manifest, repo, out = self.archive_for("expires")
        pin = repo["bundle"]["thin"]["pinRef"]
        self.assertEqual(self.pins(main), [pin])
        young = self.cli("reap", "--archives")
        self.assertEqual(young["deleted"], 0, young)
        self.assertEqual(self.pins(main), [pin])  # not expired yet: the pin stands
        stamp = json.loads((archive / core.REAP_ARCHIVE_SOLE).read_text())
        stamp["soleCopySince"] -= 31 * 86400
        (archive / core.REAP_ARCHIVE_SOLE).write_text(json.dumps(stamp))

        expired = self.cli("reap", "--archives")

        self.assertEqual(expired["deleted"], 1, expired)
        self.assertFalse(archive.exists())
        self.assertEqual(self.pins(main), [])
        self.assertIn("thin-pin-released", [c for i in expired["items"] for c in i["checks"]])

    def test_a_dry_run_expiry_keeps_the_pin(self):
        main, worktree = self.main_with_worktree("dry", big=True)
        archive, manifest, repo, out = self.archive_for("dry")
        stamp = json.loads((archive / core.REAP_ARCHIVE_SOLE).read_text())
        stamp["soleCopySince"] -= 31 * 86400
        (archive / core.REAP_ARCHIVE_SOLE).write_text(json.dumps(stamp))

        planned = self.cli("reap", "--archives", "--dry-run")

        self.assertEqual(planned["deleted"], 0, planned)
        self.assertTrue(archive.exists())
        self.assertEqual(len(self.pins(main)), 1)

    def test_a_superseded_archive_releases_its_pin(self):
        main = self.clone("sup-main", ignored=())
        base = self.git(main, "rev-parse", "HEAD").strip()
        job_dir = self.tmp / "archives" / "J-0001"
        job_dir.mkdir(parents=True)
        pins = [core.reap_archive_pin_write(str(main / ".git"), f"sup-{n}", [base]) for n in (1, 2)]
        for number, (name, pin) in enumerate(zip(("old", "new"), pins)):
            folder = job_dir / name
            folder.mkdir()
            (folder / "manifest.json").write_text(json.dumps({"kind": core.REAP_ARCHIVE_KIND, "path": "/clone", "job": "J-0001", "class": "job-clone-root",
                                                              "repositories": [{"bundle": {"thin": {"mainRepository": str(main / ".git"), **pin}}}]}))
            os.utime(folder, (1_000_000 + number * 100, 1_000_000 + number * 100))
        self.assertEqual(len(self.pins(main)), 2)

        removed = core.reap_archive_supersede(str(job_dir), str(job_dir / "new"), "/clone", "J-0001")

        self.assertEqual(removed, 1)
        self.assertFalse((job_dir / "old").exists())
        self.assertEqual(self.pins(main), [pins[1]["pinRef"]])

    def test_a_pin_that_cannot_be_written_falls_back_to_a_self_contained_bundle(self):
        main, worktree = self.main_with_worktree("nopin", big=True)

        with mock.patch.object(core, "reap_archive_pin_write", side_effect=core.ReapRefused("thin-pin-write:ref:128")):
            archive, manifest, repo, out = self.archive_for("nopin")

        self.assertTrue(repo["bundle"]["selfContained"])
        self.assertEqual(repo["bundle"]["thinFallback"], "thin-pin-write:ref:128")
        self.assertEqual(self.pins(main), [])
        self.restored(archive / repo["bundle"]["file"])  # restorable from nothing

    def test_a_pin_that_does_not_verify_is_removed_and_falls_back(self):
        main, worktree = self.main_with_worktree("badpin", big=True)
        real = core.reap_probe

        def missing(argv, *args, **kwargs):
            if "rev-parse" in argv and any(str(a).startswith("refs/trimreaper/thin/") and str(a).endswith("^{commit}") for a in argv):
                return 1, ""  # the ref cannot be read back
            return real(argv, *args, **kwargs)

        with mock.patch.object(core, "reap_probe", side_effect=missing):
            archive, manifest, repo, out = self.archive_for("badpin")

        self.assertTrue(repo["bundle"]["selfContained"])
        self.assertEqual(repo["bundle"]["thinFallback"], "thin-pin-verify:ref-missing")
        self.assertEqual(self.pins(main), [])  # no leftover pin of a bundle that was not accepted

    def test_a_failed_archive_leaves_no_pin_behind(self):
        main, worktree = self.main_with_worktree("failed", big=True)
        self.write_config({"policy": {"archiveBundleMode": "thin-local"}})
        self.age(self.scratch("failed"), 9)

        written = []
        real = core.reap_archive_pin_write

        def recording(*args, **kwargs):
            written.append(real(*args, **kwargs))
            return written[-1]

        with mock.patch.object(core, "reap_archive_pin_write", side_effect=recording), \
                mock.patch.object(core, "reap_archive_staged_patch", side_effect=core.ReapRefused("archive-failed:staged-patch:1")):
            out = self.cli("reap", "--scratch")

        self.assertEqual(len(written), 1)  # the pin WAS written, then the archive failed after it
        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(worktree.exists())
        self.assertEqual(self.pins(main), [])

    def test_thin_mode_is_forbidden_for_an_archive_that_never_expires(self):
        main, worktree = self.main_with_worktree("forever", big=True)

        archive, manifest, repo, out = self.archive_for("forever", {"archiveExpiryDays": 0})

        self.assertTrue(repo["bundle"]["selfContained"], repo["bundle"])
        self.assertEqual(repo["bundle"]["thinFallback"], "thin-forbidden:never-expires:archiveExpiryDays")
        self.assertEqual(self.pins(main), [])
        self.restored(archive / repo["bundle"]["file"])

    def test_the_never_expires_rule_per_archive_kind(self):
        scratch_item = {"class": "agent-scratch"}
        self.assertEqual(core.reap_archive_never_expires(scratch_item, "J-0001"), "thin-forbidden:never-expires:protected-copies")
        self.assertIsNone(core.reap_archive_never_expires({"class": "agent-scratch", "archiveUnsaved": True}, core.REAP_ARCHIVE_UNSAVED_OWNER))
        job = {"class": "job-clone-root"}
        self.assertIsNone(core.reap_archive_never_expires(job, "J-0001"))  # archiveExpiryDays defaults to 30
        self.assertEqual(core.reap_archive_never_expires(job, core.REAP_ARCHIVE_CLEAN_OWNER), "thin-forbidden:never-expires:cleanWorktreeArchiveExpiryDays")
        self.write_config({"policy": {"cleanWorktreeArchiveExpiryDays": 5}})
        self.assertIsNone(core.reap_archive_never_expires(job, core.REAP_ARCHIVE_CLEAN_OWNER))
        self.write_config({"policy": {"archiveExpiryDays": 0}})
        self.assertEqual(core.reap_archive_never_expires(job, "J-0001"), "thin-forbidden:never-expires:archiveExpiryDays")

    def test_a_reused_thin_archive_needs_its_pin(self):
        main, worktree = self.main_with_worktree("reuse", big=True)
        archive, manifest, repo, out = self.archive_for("reuse")
        folder = archive.parent
        self.assertIsNotNone(core.reap_archive_reuse(str(folder), manifest["path"], manifest["fingerprint"]))
        self.git(main, "update-ref", "-d", repo["bundle"]["thin"]["pinRef"])

        self.assertIsNone(core.reap_archive_reuse(str(folder), manifest["path"], manifest["fingerprint"]))

