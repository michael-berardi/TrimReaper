"""Idle agent scratch (and a finished job's folder) whose only blocker is unsaved git work is archived, then removed.

A folder quiet for `unsavedArchiveDays` (7) that holds dirty files, commits no remote has, a stash, a linked worktree or a bare
repository nested in it used to be kept for good, one more `dirty` / `unpushed` / `linked-worktree` / `unproved-git-dir`
refusal. The routine pass now saves what no remote holds (a verified bundle, a files tar) in `<archive>/scratch-unsaved/`,
stamps the archive as the only copy, removes the folder and makes the main repository forget a linked worktree that lived in it.
Nothing else changes: recent, live, claimed and protected folders stay, and a folder with nothing unsaved still waits for the
longer evidence window.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from unittest import mock

from trimreaper import core

from helpers import GIT_ENV, Home, DAY
import os


class UnsavedFixtures(Home):
    """A scratch root, a bare remote, a persistent main clone in ~/dev and linked worktrees inside scratch folders."""

    config_extra = {"scratchRoot": "~/.cache/agent-scratch"}

    def scratch(self, name: str) -> Path:
        return self.home / ".cache" / "agent-scratch" / name

    def archive_root(self) -> Path:
        return self.home / ".cache" / "trimreaper" / "archive"

    def unsaved_archives(self) -> list[Path]:
        folder = self.archive_root() / core.REAP_ARCHIVE_UNSAVED_OWNER
        return sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".")) if folder.is_dir() else []

    def manifest(self, archive: Path) -> dict:
        return json.loads((archive / "manifest.json").read_text())

    def main_with_worktree(self, task: str, name: str = "app", big: bool = False) -> tuple[Path, Path]:
        """A pushed main clone in ~/dev and a linked worktree of it inside the scratch task, with an unpushed commit, a dirty
        tracked file and an untracked file."""
        main = self.clone(name)
        if big:
            self.put(main, "history/blob.bin", os.urandom(300_000))
            self.git(main, "add", "-A")
            self.git(main, "commit", "-q", "-m", "history")
            self.git(main, "push", "-q", "origin", "main")
        worktree = self.scratch(task) / "wt"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        self.git(main, "worktree", "add", "-q", "-b", f"{task}-work", str(worktree))
        (worktree / "unsaved.txt").write_text("work that no remote has\n")
        self.git(worktree, "add", "unsaved.txt")
        self.git(worktree, "commit", "-q", "-m", "unsaved commit")
        (worktree / "README.md").write_text("dirty edit\n")
        (worktree / "scratch-notes.txt").write_text("untracked\n")
        return main, worktree

    def restored(self, bundle: Path) -> Path:
        """An empty repository that fetches the bundle: what a restore does."""
        target = Path(tempfile.mkdtemp(prefix="restore-", dir=self.tmp))
        subprocess.run(["git", "init", "-q", "--bare", str(target)], check=True, capture_output=True)
        done = subprocess.run(["git", "-C", str(target), "fetch", "-q", str(bundle), "refs/*:refs/ut-archive/*"],
                              capture_output=True, text=True, env={**os.environ, **GIT_ENV})
        self.assertEqual(done.returncode, 0, done.stderr)
        return target


class ScratchUnsaved(UnsavedFixtures):
    def test_idle_scratch_with_unsaved_worktree_is_archived_then_removed(self):
        main, worktree = self.main_with_worktree("task")
        head = self.git(worktree, "rev-parse", "HEAD").strip()
        task = self.scratch("task")
        self.age(task, 9)  # past unsavedArchiveDays (7), inside evidenceExpiryDays (14)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(task.exists())
        (archive,) = self.unsaved_archives()
        manifest = self.manifest(archive)
        self.assertEqual(manifest["class"], "agent-scratch")
        self.assertTrue(manifest["scratchUnsaved"])
        (repo,) = [r for r in manifest["repositories"] if r["mode"] == "git"]
        self.assertTrue(repo["linked"])
        self.assertEqual(repo["head"], head)
        self.assertTrue((archive / "repo-0.bundle").is_file())
        restored = self.restored(archive / "repo-0.bundle")
        done = subprocess.run(["git", "-C", str(restored), "cat-file", "-t", head], capture_output=True, text=True)
        self.assertEqual(done.stdout.strip(), "commit", done)
        with tarfile.open(archive / "files.tar.gz") as tar:  # dirty and untracked files of the worktree
            names = tar.getnames()
            self.assertIn("wt/README.md", names)
            self.assertIn("wt/scratch-notes.txt", names)
            self.assertEqual(tar.extractfile("wt/README.md").read(), b"dirty edit\n")
        self.assertTrue((archive / core.REAP_ARCHIVE_SOLE).is_file())  # stamped as the only copy before the first byte went
        # not the protected-copy archive: it carries no permanent marker, so it follows archiveExpiryDays
        self.assertFalse((archive / core.REAP_ARCHIVE_SCRATCH).exists())
        self.assertFalse(core.reap_archive_scratch(str(archive)))

    def test_the_main_repository_forgets_the_worktree_that_went(self):
        main, worktree = self.main_with_worktree("forgotten")
        self.assertIn(str(worktree), self.git(main, "worktree", "list", "--porcelain"))
        self.age(self.scratch("forgotten"), 9)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 1, out)
        listing = self.git(main, "worktree", "list", "--porcelain")
        self.assertNotIn(str(worktree), listing)
        self.assertEqual(list((main / ".git" / "worktrees").glob("*")) if (main / ".git" / "worktrees").exists() else [], [])
        self.assertIn("forgotten-work", self.git(main, "branch", "--list"))  # the branch stays in the main repository
        self.assertTrue(any(c.startswith("worktree-entry-forgotten") for i in out["items"] for c in i["checks"]), out)

    def test_a_receipt_names_the_archive_before_the_folder_goes(self):
        self.main_with_worktree("receipted")
        self.age(self.scratch("receipted"), 9)

        out = self.cli("reap", "--scratch")

        item = next(i for i in out["items"] if i["path"] == str(self.scratch("receipted")))
        self.assertEqual(item["outcome"], "deleted", item)
        for check in ("archive-settles-refusal", "archive-verified", "archive-sole-copy-recorded", "no-process-or-handle-final"):
            self.assertIn(check, item["checks"])
        self.assertTrue(item["archive"]["path"].startswith(str(self.archive_root())), item["archive"])

    def test_a_recent_folder_is_untouched(self):
        main, worktree = self.main_with_worktree("recent")
        task = self.scratch("recent")
        self.age(task, 3)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(worktree.exists())
        self.assertEqual(self.unsaved_archives(), [])
        held = next(i for i in out["items"] if i["path"] == str(task))
        self.assertEqual(held["reason"], "idle-recent-change")
        self.assertIn(str(worktree), self.git(main, "worktree", "list", "--porcelain"))

    def test_a_folder_a_running_job_declares_is_untouched(self):
        main, worktree = self.main_with_worktree("claimed")
        task = self.scratch("claimed")
        self.add_job("J-0300", task, status="running")
        self.age(task, 9)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(worktree.exists())
        self.assertEqual(self.unsaved_archives(), [])
        held = next(i for i in out["items"] if i["path"] == str(task))
        self.assertTrue(held["reason"].startswith("claimed-by:J-0300"), held)

    def test_a_folder_a_process_holds_is_untouched(self):
        main, worktree = self.main_with_worktree("held")
        task = self.scratch("held")
        self.age(task, 9)
        process = subprocess.Popen(["sleep", "30"], cwd=worktree)
        self.addCleanup(process.kill)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(worktree.exists())
        self.assertEqual(self.unsaved_archives(), [])

    def test_nothing_unsaved_still_waits_for_the_evidence_window(self):
        clean = self.scratch("clean")
        clean.mkdir(parents=True)
        self.clone("app")  # the remote the scratch clone is made from
        subprocess.run(["git", "clone", "-q", str(self.remotes / "app.git"), str(clean / "repo")], check=True, capture_output=True)
        self.age(clean, 9)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 0, out)  # clean and pushed, but quiet only 9 of the 14 days
        self.assertTrue(clean.exists())
        held = next(i for i in out["items"] if i["path"] == str(clean))
        self.assertEqual(held["reason"], "idle-recent-change")
        self.assertEqual(self.unsaved_archives(), [])
        self.age(clean, 20)

        again = self.cli("reap", "--scratch")

        self.assertEqual(again["deleted"], 1, again)  # the ordinary rule, no archive needed
        self.assertEqual(self.unsaved_archives(), [])

    def test_a_plain_folder_between_the_windows_stays(self):
        plain = self.scratch("plain")
        self.put(plain, "notes/one.txt")
        self.age(plain, 9)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(plain.exists())

    def test_unsavedArchiveDays_zero_keeps_the_folder(self):
        self.write_config({"policy": {"unsavedArchiveDays": 0}})
        main, worktree = self.main_with_worktree("off")
        self.age(self.scratch("off"), 30)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(worktree.exists())
        self.assertEqual(self.unsaved_archives(), [])

    def test_cloneArchive_keep_keeps_the_folder(self):
        self.write_config({"policy": {"cloneArchive": "keep"}})
        main, worktree = self.main_with_worktree("keep")
        self.age(self.scratch("keep"), 30)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(worktree.exists())

    def test_a_protected_name_keeps_the_folder_whole(self):
        main, worktree = self.main_with_worktree("secret")
        self.put(self.scratch("secret"), ".env.local", b"TOKEN=1\n")
        self.age(self.scratch("secret"), 9)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue((self.scratch("secret") / ".env.local").exists())
        self.assertEqual(self.unsaved_archives(), [])

    def test_a_nested_bare_repository_is_bundled_whole_and_restorable(self):
        source = self.clone("backup-source")
        task = self.scratch("backup")
        task.mkdir(parents=True)
        subprocess.run(["git", "clone", "-q", "--bare", str(source), str(task / ".git-backup")], check=True, capture_output=True)
        head = self.git(source, "rev-parse", "HEAD").strip()
        self.put(task, "notes.txt")
        self.age(task, 9)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(task.exists())
        (archive,) = self.unsaved_archives()
        manifest = self.manifest(archive)
        (entry,) = [r for r in manifest["repositories"] if r["mode"] == "git-dir"]
        self.assertTrue(entry["bundle"]["whole"], entry)
        restored = self.restored(archive / entry["bundle"]["file"])
        self.assertEqual(subprocess.run(["git", "-C", str(restored), "cat-file", "-t", head], capture_output=True, text=True).stdout.strip(), "commit")
        names = tarfile.open(archive / "files.tar.gz").getnames()
        self.assertIn(".git-backup/HEAD", names)  # the bundle is an extra copy: the raw files stay (older stashes, modules, LFS, config...)
        self.assertIn(".git-backup/packed-refs", names)

    def test_an_unsaved_scratch_archive_expires_with_archiveExpiryDays(self):
        self.main_with_worktree("expiring")
        self.age(self.scratch("expiring"), 9)
        self.cli("reap", "--scratch")
        (archive,) = self.unsaved_archives()
        stamp = json.loads((archive / core.REAP_ARCHIVE_SOLE).read_text())
        stamp["soleCopySince"] = stamp["soleCopySince"] - 31 * DAY
        (archive / core.REAP_ARCHIVE_SOLE).write_text(json.dumps(stamp))

        out = self.cli("reap", "--archives")

        self.assertEqual(out["deleted"], 1, out)
        self.assertEqual(self.unsaved_archives(), [])


class StrayGitDirectory(UnsavedFixtures):
    """B3: the raw files of a stray Git directory always stay in files.tar.gz; its bundle is an extra, verified copy."""

    def stray(self, task: str) -> tuple[Path, Path]:
        """A scratch task holding `.git-backup`: a repository's git directory with a submodule repository under modules/, an LFS
        object under lfs/objects and two stash entries (a bundle `--all` keeps only the newest one)."""
        source = self.tmp / "stray-source"
        sub = self.tmp / "stray-sub"
        for repo in (source, sub):
            repo.mkdir(parents=True)
            self.git(repo, "init", "-q", "-b", "main")
        (sub / "lib.txt").write_text("library\n")
        self.git(sub, "add", "-A")
        self.git(sub, "commit", "-q", "-m", "lib")
        (source / "a.txt").write_text("one\n")
        self.git(source, "add", "-A")
        self.git(source, "commit", "-q", "-m", "base")
        self.git(source, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor/sub")
        self.git(source, "commit", "-q", "-m", "submodule")
        (source / "a.txt").write_text("first stash content\n")
        self.git(source, "stash", "push", "-q", "-m", "first-stash")
        (source / "a.txt").write_text("second stash content\n")
        self.git(source, "stash", "push", "-q", "-m", "second-stash")
        lfs = source / ".git" / "lfs" / "objects" / "ab" / "cd"
        lfs.mkdir(parents=True)
        (lfs / ("abcd" + "0" * 60)).write_bytes(b"the only copy of a large file\n")
        folder = self.scratch(task)
        folder.mkdir(parents=True)
        os.rename(source / ".git", folder / ".git-backup")
        self.put(folder, "notes.txt", b"notes\n")
        return folder, folder / ".git-backup"

    def test_raw_gitdir_files_stay_in_the_archive_and_restore_the_older_stash(self):
        folder, gitdir = self.stray("stray")
        before = self.git(gitdir.parent, "--git-dir", str(gitdir), "stash", "list")
        self.assertEqual(len(before.splitlines()), 2, before)  # sanity: two stash entries
        self.age(folder, 9)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(folder.exists())
        (archive,) = self.unsaved_archives()
        manifest = self.manifest(archive)
        (entry,) = [r for r in manifest["repositories"] if r["mode"] == "git-dir" and r["relative"] == ".git-backup"]
        self.assertTrue(entry["bundle"]["whole"], entry)  # the bundle is still written: an extra, verified copy
        names = tarfile.open(archive / "files.tar.gz").getnames()
        self.assertIn(".git-backup/logs/refs/stash", names)  # the reflog that holds the OLDER stash entry
        self.assertTrue([n for n in names if n.startswith(".git-backup/modules/")], names)  # the submodule repository
        self.assertIn(".git-backup/lfs/objects/ab/cd/abcd" + "0" * 60, names)  # the LFS object
        self.assertIn(".git-backup/config", names)
        self.assertIn(".git-backup/info/exclude", names)
        # restore: extract the tar; the second (older) stash entry is back
        target = self.tmp / "restored-stray"
        target.mkdir()
        with tarfile.open(archive / "files.tar.gz") as tar:
            tar.extractall(target, filter="data")
        listing = self.git(target, "--git-dir", str(target / ".git-backup"), "stash", "list")
        self.assertIn("first-stash", listing)  # stash@{1}: not in `git bundle --all` (it keeps only refs/stash)
        self.assertIn("second-stash", listing)
        restored = self.git(target, "--git-dir", str(target / ".git-backup"), "show", "stash@{1}:a.txt")
        self.assertEqual(restored, "first stash content\n")
        self.assertEqual((target / ".git-backup" / "lfs" / "objects" / "ab" / "cd" / ("abcd" + "0" * 60)).read_bytes(), b"the only copy of a large file\n")

    def test_a_commondir_file_refuses_the_gitdir_bundle(self):
        other = self.clone("other-repo")
        task = self.scratch("common")
        task.mkdir(parents=True)
        gitdir = task / ".git-backup"
        subprocess.run(["git", "init", "-q", "--bare", str(gitdir)], check=True, capture_output=True)
        (gitdir / "commondir").write_text(str(other / ".git") + "\n")  # git would read ANOTHER repository's refs and objects
        with self.assertRaises(core.ReapRefused) as caught:
            core.reap_archive_gitdir_gate(str(gitdir))
        self.assertEqual(str(caught.exception), "git-error:unsafe-metadata:commondir")
        stage = self.tmp / "stage"
        stage.mkdir()
        self.assertIsNone(core.reap_archive_gitdir_bundle({"root": str(gitdir), "kind": "git-dir"}, str(stage), 0))
        self.assertEqual(os.listdir(stage), [])
        self.age(task, 9)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 1, out)
        (archive,) = self.unsaved_archives()
        (entry,) = [r for r in self.manifest(archive)["repositories"] if r["mode"] == "git-dir"]
        self.assertIsNone(entry["bundle"])  # raw-only
        self.assertIn(".git-backup/commondir", tarfile.open(archive / "files.tar.gz").getnames())


class LooseFilesAndBusyMain(UnsavedFixtures):
    """SF1: the unsaved route also archives files outside every repository. NIT: no worktree is forgotten under a busy main."""

    def test_loose_notes_beside_a_dirty_repository_are_archived_and_restore(self):
        main, worktree = self.main_with_worktree("loose")
        task = self.scratch("loose")
        self.put(task, "notes.txt", b"meeting notes, not in any repository\n")
        self.put(task, "copy/docs/plan.md", b"a non-git copy\n")
        self.age(task, 9)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(task.exists())
        (archive,) = self.unsaved_archives()
        manifest = self.manifest(archive)
        self.assertEqual(manifest["looseFiles"], 2)
        target = self.tmp / "restored-loose"
        target.mkdir()
        with tarfile.open(archive / "files.tar.gz") as tar:
            tar.extractall(target, filter="data")
        self.assertEqual((target / "notes.txt").read_bytes(), b"meeting notes, not in any repository\n")
        self.assertEqual((target / "copy" / "docs" / "plan.md").read_bytes(), b"a non-git copy\n")
        self.assertEqual((target / "wt" / "README.md").read_bytes(), b"dirty edit\n")  # the repository's own unsaved file, as before

    def test_regenerable_output_beside_the_repository_is_still_left_out(self):
        self.main_with_worktree("regen")
        task = self.scratch("regen")
        self.put(task, "node_modules/pkg/index.js", b"module.exports = 1\n")
        self.age(task, 9)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 1, out)
        (archive,) = self.unsaved_archives()
        self.assertFalse([n for n in tarfile.open(archive / "files.tar.gz").getnames() if "node_modules" in n])

    def test_oversize_loose_files_keep_the_folder_with_a_named_reason(self):
        self.main_with_worktree("big")
        task = self.scratch("big")
        self.put(task, "dump/blob.bin", os.urandom(200_000))
        self.age(task, 9)

        with mock.patch.object(core, "REAP_ARCHIVE_MAX_BYTES", 100_000):
            out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue((task / "dump" / "blob.bin").exists())
        held = next(i for i in out["items"] if i["path"] == str(task))
        self.assertTrue(held["reason"].startswith("archive-too-large:"), held)
        self.assertIn("files-outside-every-repository", held["reason"])
        self.assertEqual(self.unsaved_archives(), [])

    def test_the_full_window_route_does_not_copy_loose_files_of_a_clean_folder(self):
        # nothing unsaved: the longer evidence window applies and no archive is written (unchanged behaviour)
        task = self.scratch("plain-old")
        self.put(task, "notes.txt", b"quiet notes\n")
        self.age(task, 20)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 1, out)
        self.assertEqual(self.unsaved_archives(), [])

    def test_an_index_lock_in_the_main_repository_keeps_the_folder(self):
        main, worktree = self.main_with_worktree("locked-main")
        task = self.scratch("locked-main")
        self.age(task, 9)
        lock = main / ".git" / "index.lock"
        lock.write_text("")

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(worktree.exists())
        held = next(i for i in out["items"] if i["path"] == str(task))
        self.assertEqual(held["reason"], "worktree-main-busy:index-lock", held)
        lock.unlink()

        again = self.cli("reap", "--scratch")

        self.assertEqual(again["deleted"], 1, again)
        self.assertFalse(task.exists())

    def test_a_main_repository_in_the_middle_of_a_rebase_keeps_the_folder(self):
        main, worktree = self.main_with_worktree("rebasing")
        task = self.scratch("rebasing")
        self.age(task, 9)
        (main / ".git" / "rebase-merge").mkdir()

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(worktree.exists())
        held = next(i for i in out["items"] if i["path"] == str(task))
        self.assertEqual(held["reason"], "worktree-main-busy:mid-operation:rebase-merge", held)

    def test_forgetting_a_worktree_entry_is_skipped_under_a_lock_or_a_merge(self):
        main, worktree = self.main_with_worktree("forget")
        (entry,) = (main / ".git" / "worktrees").iterdir()
        for marker, reason in (("index.lock", "index-lock"), ("MERGE_HEAD", "mid-operation:MERGE_HEAD")):
            (main / ".git" / marker).write_text("")
            result = core.reap_worktree_forget(str(main / ".git"), str(entry), str(worktree))
            (main / ".git" / marker).unlink()
            self.assertEqual(result, f"worktree-entry-kept:main-busy:{reason}")
            self.assertTrue(entry.is_dir())
        shutil.rmtree(worktree)
        self.assertEqual(core.reap_worktree_forget(str(main / ".git"), str(entry), str(worktree)), "worktree-entry-forgotten")
        self.assertFalse(entry.exists())


class JobFolderUnsaved(UnsavedFixtures):
    """A finished job's cache folder holding unsaved git work goes after unsavedArchiveDays, not evidenceExpiryDays."""

    def job_folder(self, job: str) -> Path:
        remote_clone = self.clone("job-app")
        folder = self.home / ".cache" / "trimreaper" / "jobs" / job / "work"
        folder.mkdir(parents=True)
        subprocess.run(["git", "clone", "-q", str(self.remotes / "job-app.git"), str(folder / "repo")], check=True, capture_output=True)
        self.git(folder / "repo", "checkout", "-q", "-b", "wip")
        (folder / "repo" / "wip.txt").write_text("unsaved\n")
        self.git(folder / "repo", "add", "wip.txt")
        self.git(folder / "repo", "commit", "-q", "-m", "unsaved")
        return folder

    def test_a_finished_jobs_unsaved_folder_is_archived_then_removed(self):
        folder = self.job_folder("J-0400")
        self.add_job("J-0400", status="failed", finished_ago=9 * DAY)

        out = self.cli("reap", "--job", "J-0400")

        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(folder.exists())
        archives = [p for p in (self.archive_root() / "J-0400").iterdir() if p.is_dir()]
        self.assertEqual(len(archives), 1)
        bundle = archives[0] / "repo-0.bundle"
        head = json.loads((archives[0] / "manifest.json").read_text())["repositories"][0]["head"]
        restored = self.restored(bundle)
        self.assertEqual(subprocess.run(["git", "-C", str(restored), "cat-file", "-t", head], capture_output=True, text=True).stdout.strip(), "commit")

    def test_a_finished_jobs_clean_folder_waits_for_the_evidence_window(self):
        self.clone("job-app")
        folder = self.home / ".cache" / "trimreaper" / "jobs" / "J-0401" / "work"
        folder.mkdir(parents=True)
        subprocess.run(["git", "clone", "-q", str(self.remotes / "job-app.git"), str(folder / "repo")], check=True, capture_output=True)
        self.add_job("J-0401", status="done", finished_ago=9 * DAY)

        out = self.cli("reap", "--job", "J-0401")

        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(folder.exists())

    def test_a_recently_finished_jobs_unsaved_folder_stays(self):
        folder = self.job_folder("J-0402")
        self.add_job("J-0402", status="done", finished_ago=2 * DAY)

        out = self.cli("reap", "--job", "J-0402")

        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(folder.exists())

    def test_a_running_jobs_unsaved_folder_stays(self):
        folder = self.job_folder("J-0403")
        self.add_job("J-0403", status="running")

        with self.assertRaises(AssertionError):
            self.cli("reap", "--job", "J-0403")  # a running job is not reapable at all
        self.assertTrue(folder.exists())
