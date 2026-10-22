"""Scratch-only protected copies: throwaway homes, verified archives, no live artifacts."""
import json
import os
import stat
import tarfile
import time
from pathlib import Path
from unittest import mock

from trimreaper import core
from helpers import Home


class ScratchProtected(Home):
    config_extra = {"scratchRoot": "~/.cache/agent-scratch"}

    def task(self, archive=True):
        self.write_config({"policy": {"agentScratchProtectedCopies": "archive" if archive else "keep"}})
        root = self.home / ".cache" / "agent-scratch"
        root.mkdir(parents=True, mode=0o700)
        task = root / "old-task"
        for name in (".env", ".dev.vars", ".wrangler/a/b/deep", "node_modules/pkg/.env.example"):
            self.put(task, name, ("saved:" + name).encode())
        self.age(task, 20)
        return task

    def reap(self, *args, expect=0):
        with mock.patch.object(core, "reap_item_holders"), mock.patch.object(core, "reap_free_bytes", return_value=10**12):
            return self.cli("reap", "--scratch", *args, expect=expect)

    def test_archive_success_preserves_every_protected_byte(self):
        task = self.task()
        out = self.reap()
        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(task.exists())
        self.assertGreater(out["reclaimedBytes"], 0, out)
        item = next(i for i in out["items"] if i["path"] == str(task))
        with tarfile.open(os.path.join(item["archive"]["path"], "protected.tar.gz")) as tar:
            for name in (".env", ".dev.vars", ".wrangler/a/b/deep", "node_modules/pkg/.env.example"):
                self.assertEqual(tar.extractfile(name).read(), ("saved:" + name).encode())
        saved = item["archive"]["path"]
        for current, dirs, files in os.walk(saved):
            self.assertEqual(stat.S_IMODE(os.stat(current).st_mode), 0o700)
            for name in files:
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(current, name)).st_mode), 0o600)
        manifest = json.loads(Path(saved, "manifest.json").read_text())
        self.assertEqual(manifest["class"], "agent-scratch")
        self.assertIsNone(manifest["files"])
        self.assertIsNotNone(core.reap_archive_sole_since(saved))

    def test_default_keep_never_stats_or_opens_protected_entries(self):
        task = self.task(False)
        self.write_config({})  # omitted policy is keep, not the scratch host's approved archive default
        original_stat, original_open = core.os.stat, core.os.open
        def checked_stat(name, *args, **kwargs):
            self.assertFalse(core.reap_protected(os.path.basename(str(name))), name)
            return original_stat(name, *args, **kwargs)
        def checked_open(name, *args, **kwargs):
            self.assertFalse(core.reap_protected(os.path.basename(str(name))), name)
            return original_open(name, *args, **kwargs)
        with mock.patch.object(core.os, "stat", side_effect=checked_stat), mock.patch.object(core.os, "open", side_effect=checked_open):
            proof = core.reap_scratch_idle_proof(str(task))
        self.assertEqual(proof["reason"], "protected-leaf", proof)
        self.assertEqual(self.reap()["deleted"], 0)

    def test_unprotected_default_scratch_still_expires_with_nonprivate_root(self):
        self.write_config({})
        task = self.home / ".cache/agent-scratch/plain"
        self.put(task, "plain.txt")
        os.chmod(task.parent, 0o755)
        self.age(task, 20)
        self.assertEqual(self.reap()["deleted"], 1)

    def test_protected_deep_recent_change_keeps(self):
        task = self.task()
        self.put(task, ".wrangler/a/b/deep", b"new")
        out = self.reap()
        self.assertEqual(out["deleted"], 0, out)
        self.assertIn("idle-recent-change", out["reasons"])
        self.assertFalse(core.reap_archive_root().exists())

    def test_protected_walk_uses_the_same_entry_and_byte_budgets(self):
        task = self.task()
        for policy, reason in (({"agentScratchIdleMaxEntries": 2}, "budget-exceeded"),
                               ({"agentScratchIdleMaxBytes": 1}, "budget-bytes-exceeded")):
            self.write_config({"policy": {"agentScratchProtectedCopies": "archive", **policy}})
            proof = core.reap_scratch_idle_proof(str(task))
            self.assertEqual(proof["reason"], reason, proof)
            self.assertEqual(self.reap()["deleted"], 0)

    def test_preserving_marker_under_protected_directory_keeps(self):
        task = self.task()
        self.put(task, ".wrangler/a/b/.ut-retention-evidence", b"")
        self.age(task, 20)
        out = self.reap()
        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(task.exists())
        self.assertIn("nested-marker", out["items"][0]["reason"], out)

    def test_immutable_protected_descendant_keeps(self):
        task = self.task()
        leaf = task / ".wrangler/a/b/deep"
        if not hasattr(os, "chflags") or not hasattr(stat, "UF_IMMUTABLE"):
            self.skipTest("filesystem flags unavailable")
        try:
            os.chflags(leaf, stat.UF_IMMUTABLE)
        except OSError:
            self.skipTest("filesystem does not support immutable flags")
        self.addCleanup(os.chflags, leaf, 0)
        out = self.reap()
        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(any("filesystem-protected-flags" in r for r in out["reasons"]), out)
        self.assertTrue(os.stat(leaf).st_flags & stat.UF_IMMUTABLE)

    def test_special_protected_material_keeps(self):
        task = self.task()
        os.mkfifo(task / ".wrangler/pipe")
        self.age(task, 20)
        out = self.reap()
        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(any("protected-special-file" in r for r in out["reasons"]), out)

    def test_symlink_is_archived_as_link_never_followed(self):
        task = self.task()
        outside = self.put(self.home, "outside.txt", b"do not read")
        os.symlink(outside, task / ".env.link")
        os.symlink(outside.parent, task / ".wrangler/a/link")
        self.age(task, 20)
        out = self.reap()
        self.assertEqual(out["deleted"], 1, out)
        saved = out["items"][0]["archive"]["path"]
        with tarfile.open(os.path.join(saved, "protected.tar.gz")) as tar:
            self.assertTrue(tar.getmember(".env.link").issym())
            self.assertTrue(tar.getmember(".wrangler/a/link").issym())
            self.assertNotIn(".wrangler/a/link/outside.txt", tar.getnames())
        self.assertEqual(outside.read_bytes(), b"do not read")

    def test_dryrun_writes_nothing(self):
        task = self.task()
        out = self.reap("--dry-run")
        self.assertEqual(out["deleted"], 0, out)
        self.assertEqual(out["items"][0]["outcome"], "would-delete", out)
        self.assertIn("archive-planned", out["items"][0]["checks"])
        # CLI initialization may create the job database; archive/receipt/lock writes are forbidden.
        self.assertFalse(core.reap_archive_root().exists())
        self.assertTrue(task.exists())
        self.assertFalse((self.home / ".local/state/trimreaper/reaper").exists())

    def test_archive_failure_keeps(self):
        task = self.task()
        with mock.patch.object(core, "reap_archive_tar", side_effect=core.ReapRefused("archive-verify-failed:injected")):
            out = self.reap()
        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue((task / ".env").exists())
        self.assertEqual(out["items"][0]["reason"], "archive-verify-failed:injected")

    def test_archive_size_and_space_failures_keep(self):
        task = self.task()
        with mock.patch.object(core, "REAP_ARCHIVE_MAX_BYTES", 1):
            out = self.reap()
        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(task.exists())
        # Space failure tested directly to avoid the deliberate refusal backoff on the same path.
        with mock.patch.object(core, "reap_archive_refused_for", return_value=None), mock.patch.object(core, "reap_free_bytes", return_value=0):
            item = core.reap_agent_scratch_items()[0]
            with self.assertRaisesRegex(core.ReapRefused, "archive-no-space"):
                core.reap_archive_create(item, {"reason": "test"}, [], [], None)
        self.assertTrue(task.exists())

    def test_mutation_after_archive_refuses_even_with_old_mtime(self):
        task = self.task()
        original = core.reap_archive_create
        def mutate(*args, **kwargs):
            made = original(*args, **kwargs)
            leaf = task / ".wrangler/a/b/deep"
            before = leaf.stat()
            leaf.write_bytes(b"z" * before.st_size)
            os.utime(leaf, ns=(before.st_atime_ns, before.st_mtime_ns))
            return made
        with mock.patch.object(core, "reap_archive_create", side_effect=mutate):
            out = self.reap()
        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(task.exists())
        self.assertTrue(any("archive-stale" in r for r in out["reasons"]), out)

    def test_scratch_archives_never_expire_even_with_disposable_marker(self):
        task = self.task()
        out = self.reap()
        saved = out["items"][0]["archive"]["path"]
        self.age(Path(saved), 100)
        Path(saved, core.REAP_ARCHIVE_SOLE).write_text(json.dumps({"soleCopySince": time.time() - 100*86400}))
        self.assertFalse(core.reap_archive_expired(saved))
        self.put(Path(saved), ".ut-retention-disposable", b"")
        item = {"path": saved, "root": os.path.dirname(saved), "owner": "agent-scratch", "jobs": [],
                "class": "job-archive", "kind": "archive", "ownership": "reaper-archive", "retention": "disposable"}
        result = core.reap_item(item)
        self.assertEqual(result["outcome"], "refused", result)
        self.assertEqual(result["reason"], "scratch-protected-archive-permanent")
        self.assertTrue(Path(saved, "protected.tar.gz").exists())

    def test_supersede_does_not_prune_scratch_archives(self):
        task = self.task()
        out = self.reap()
        saved = Path(out["items"][0]["archive"]["path"])
        newer = saved.parent / "newer"
        newer.mkdir()
        (newer / "manifest.json").write_bytes((saved / "manifest.json").read_bytes())
        self.age(saved, 20)
        self.assertEqual(core.reap_archive_supersede(str(saved.parent), str(newer), str(task), "agent-scratch"), 0)
        self.assertTrue((saved / "protected.tar.gz").exists())

    def test_late_protected_file_is_never_removed_without_copy(self):
        task = self.task()
        original = core.reap_remove_and_verify
        def appeared(*args, **kwargs):
            self.put(task, ".wrangler/a/new-secret", b"sole copy")
            return original(*args, **kwargs)
        with mock.patch.object(core, "reap_remove_and_verify", side_effect=appeared):
            out = self.reap(expect=3)
        self.assertEqual(out["deleted"], 0, out)
        self.assertEqual((task / ".wrangler/a/new-secret").read_bytes(), b"sole copy")
        self.assertIn("scratch-protected-copy-changed", out["items"][0]["reason"], out)

    def test_same_length_restore_mtime_mutation_with_disposable_marker_keeps(self):
        task = self.task()
        self.put(task, ".ut-retention-disposable", b"")
        self.age(task, 20)
        original = core.reap_archive_create
        def mutate(*args, **kwargs):
            made = original(*args, **kwargs)
            leaf = task / ".env"
            previous = leaf.stat()
            leaf.write_bytes(b"q" * previous.st_size)
            os.utime(leaf, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            return made
        with mock.patch.object(core, "reap_archive_create", side_effect=mutate):
            out = self.reap()
        self.assertEqual(out["deleted"], 0, out)
        self.assertIn("archive-stale", out["items"][0]["reason"], out)
        self.assertTrue(task.exists())

    def test_clean_pushed_scratch_checkout_archives_protected_copies(self):
        task = self.task()
        repo = self.clone("checkout", ignored=(".env*", ".wrangler/", ".dev.vars"))
        for name in (".git", ".gitignore", "README.md"):
            (repo / name).rename(task / name)
        self.age(task, 20)
        out = self.reap()
        self.assertEqual(out["deleted"], 1, out)
        self.assertIn("nested-git-all-branches-and-reflogs-pushed", out["items"][0]["checks"])
        with tarfile.open(os.path.join(out["items"][0]["archive"]["path"], "protected.tar.gz")) as tar:
            self.assertEqual(tar.extractfile(".env").read(), b"saved:.env")

    def test_foreign_owned_protected_metadata_keeps_without_reading_bytes(self):
        task = self.task()
        original_stat = core.os.stat
        def foreign(name, *args, **kwargs):
            info = original_stat(name, *args, **kwargs)
            if str(name) == ".env":
                # A stat-shaped object retains nanosecond and flags fields while changing only ownership.
                return mock.Mock(wraps=info, st_uid=os.getuid() + 1, st_mode=info.st_mode,
                                 st_mtime=info.st_mtime, st_dev=info.st_dev, st_ino=info.st_ino, st_blocks=info.st_blocks)
            return info
        with mock.patch.object(core.os, "stat", side_effect=foreign), mock.patch.object(core, "reap_archive_tar") as tar:
            out = self.reap()
        self.assertEqual(out["deleted"], 0, out)
        self.assertIn("foreign-owner", out["items"][0]["reason"], out)
        tar.assert_not_called()
        self.assertTrue(task.exists())

    def test_nested_dirty_git_is_not_settled_by_scratch_archive(self):
        task = self.task()
        repo = self.clone("nested-source")
        repo.rename(task / "repo")
        self.put(task / "repo", "README.md", b"unsaved source")
        self.age(task, 20)
        out = self.reap()
        self.assertEqual(out["deleted"], 0, out)
        self.assertIsNone(out["items"][0]["archive"], out)
        self.assertTrue((task / "repo/README.md").exists())

    def test_job_cache_cannot_claim_protected_ok(self):
        self.task()
        self.add_job("J-1000", finished_ago=20*86400)
        root = core.reap_cache_root() / "J-1000"
        leaf = root / "output"
        self.put(leaf, ".env", b"sole copy")
        self.put(leaf, ".ut-retention-disposable", b"")
        self.age(leaf, 20)
        item = {"path": str(leaf), "root": str(root), "owner": "J-1000", "jobs": ["J-1000"],
                "class": "job-cache-subdir", "kind": "cache-subdir", "ownership": "job-cache-dir",
                "retention": "disposable", "protected_ok": True}
        result = core.reap_item(item)
        self.assertEqual(result["outcome"], "refused", result)
        self.assertIn("protected-leaf", result["reason"], result)
        self.assertTrue((leaf / ".env").exists())

    def test_tar_source_never_follows_swapped_ancestor(self):
        task = self.task()
        item = core.reap_agent_scratch_items()[0]
        entries = core.reap_scratch_archive_collect(item)["protected"]
        outside = self.home / "outside"
        self.put(outside, "a/b/deep", b"must not read")
        (task / ".wrangler").rename(task / ".wrangler-original")
        os.symlink(outside, task / ".wrangler")
        target = self.tmp / "test.tar.gz"
        with self.assertRaises(OSError):
            core.reap_archive_tar(str(task), entries, str(target))
        self.assertEqual((outside / "a/b/deep").read_bytes(), b"must not read")

    def test_archive_permanent_record_survives_unreadable_manifest(self):
        self.task()
        out = self.reap()
        saved = Path(out["items"][0]["archive"]["path"])
        (saved / "manifest.json").write_bytes(b"invalid")
        (saved / core.REAP_ARCHIVE_SOLE).write_text(json.dumps({"soleCopySince": time.time() - 100*86400}))
        self.assertFalse(core.reap_archive_expired(str(saved)))

    def test_archive_time_failure_keeps(self):
        task = self.task()
        with mock.patch.object(core, "reap_archive_remaining", side_effect=core.ReapRefused("archive-failed:time-limit")):
            out = self.reap()
        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue((task / ".env").exists())

    def test_nested_unpushed_git_is_not_settled_by_scratch_archive(self):
        task = self.task()
        repo = self.clone("unpushed-source")
        repo.rename(task / "repo")
        self.put(task / "repo", "README.md", b"committed local source")
        self.git(task / "repo", "add", "README.md")
        self.git(task / "repo", "commit", "-q", "-m", "local")
        self.age(task, 20)
        out = self.reap()
        self.assertEqual(out["deleted"], 0, out)
        self.assertIn("unpushed", out["items"][0]["reason"], out)
        self.assertIsNone(out["items"][0]["archive"], out)

    def test_archive_receipt_failure_keeps(self):
        task = self.task()
        original = core.reap_receipt
        def receipt(record):
            if record.get("phase") == "archive":
                raise core.ReapRefused("receipt-unavailable:injected")
            return original(record)
        with mock.patch.object(core, "reap_receipt", side_effect=receipt):
            out = self.reap()
        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue((task / ".env").exists())

    def test_nonprivate_or_preserving_root_keeps(self):
        task = self.task()
        os.chmod(task.parent, 0o755)
        self.assertEqual(self.reap()["deleted"], 0)
        os.chmod(task.parent, 0o700)
        self.put(task.parent, ".ut-retention-evidence", b"")
        self.assertEqual(self.reap()["deleted"], 0)
        self.assertTrue(task.exists())

    def test_cache_root_overlap_and_forged_class_keep(self):
        task = self.task()
        for overlap in ({"cacheRoot": str(task.parent)}, {"cacheRoot": str(task.parent / "job-cache")},
                        {"archiveRoot": str(task.parent)}, {"cacheRoot": str(task.parent.parent)}):
            self.write_config({**overlap, "policy": {"agentScratchProtectedCopies": "archive"}})
            out = self.reap()
            self.assertEqual(out["deleted"], 0, out)
            forged = {"path": str(task), "root": str(task.parent), "owner": "agent-scratch", "jobs": [],
                      "class": "agent-scratch", "kind": "agent-scratch", "retention": "disposable", "protected_ok": True}
            with mock.patch.object(core, "reap_cache_root", return_value=task.parent):
                result = core.reap_item(forged)
            self.assertEqual(result["outcome"], "refused", result)
            self.assertEqual(result["reason"], "scratch-root-overlap", result)
        self.assertTrue(task.exists())
