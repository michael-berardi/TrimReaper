"""Synthetic: the clone-dedupe target keeps every path's bytes, mode and times, and never touches code, links or live jobs."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from trimreaper import clean

OLD = time.time() - 2 * 86400
DATA = os.urandom(300 * 1024)


def write(path: Path, data: bytes = DATA, mode: int = 0o644, age: float = OLD) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, mode)
    os.utime(path, (age, age))
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="trimreaper-dedupe-")))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # ctime cannot be backdated: judge age against a clock six hours and a minute ahead.
        self.now = time.time() + clean.CLEAN_DEDUPE_MIN_AGE_SECONDS + 60


class Rules(Base):
    def test_only_old_plain_data_files_qualify(self):
        plain = write(self.tmp / "plain.bin")
        self.assertIsNotNone(clean.clean_dedupe_candidate(str(plain), self.now))
        self.assertIsNone(clean.clean_dedupe_candidate(str(write(self.tmp / "tool", mode=0o755)), self.now))
        self.assertIsNone(clean.clean_dedupe_candidate(str(write(self.tmp / "lib.dat", b"\xcf\xfa\xed\xfe" + DATA)), self.now))
        self.assertIsNone(clean.clean_dedupe_candidate(str(write(self.tmp / "small.bin", b"x" * 1024)), self.now))
        self.assertIsNone(clean.clean_dedupe_candidate(str(plain), time.time()))  # changed within six hours
        os.link(plain, self.tmp / "hard.bin")
        self.assertIsNone(clean.clean_dedupe_candidate(str(plain), self.now))  # hard-linked
        os.symlink(self.tmp / "hard.bin", self.tmp / "link.bin")
        self.assertIsNone(clean.clean_dedupe_candidate(str(self.tmp / "link.bin"), self.now))

    def test_walk_skips_git_and_symlinked_folders(self):
        write(self.tmp / "a" / ".git" / "objects" / "big")
        target = write(self.tmp / "elsewhere" / "big")
        os.symlink(target.parent, self.tmp / "a" / "linked")
        found = [p for p, _ in clean.clean_dedupe_walk([str(self.tmp / "a")], self.now)]
        self.assertEqual(found, [])

    def test_roots_skip_live_and_unknown_job_folders(self):
        cache = self.tmp / "cache"
        for name in ("J-1", "J-2", "J-3", "notes"):
            (cache / name).mkdir(parents=True)
        roots = clean.clean_dedupe_roots(str(cache), None, {"J-1": "running", "J-2": "parked", "J-3": "cancelled"})
        self.assertEqual([Path(r).name for r in roots], ["J-2", "J-3"])


@unittest.skipUnless(os.uname().sysname == "Darwin", "APFS clones need macOS")
class Clone(Base):
    def test_duplicates_become_clones_with_identical_bytes_mode_and_times(self):
        a = write(self.tmp / "J-1" / "corpus.txt")
        b = write(self.tmp / "J-2" / "corpus.txt", mode=0o600, age=OLD - 3600)
        before = os.lstat(b)
        plan = clean.clean_dedupe_run([str(self.tmp)], dry_run=True, at=self.now)
        self.assertEqual((plan["files"], plan["bytes"]), (1, len(DATA)))
        self.assertEqual(os.lstat(b).st_ino, before.st_ino)  # a dry run changes nothing
        done = clean.clean_dedupe_run([str(self.tmp)], dry_run=False, at=self.now)
        self.assertEqual((done["files"], done["errors"]), (1, []))
        after = os.lstat(b)
        self.assertNotEqual(after.st_ino, before.st_ino)
        self.assertEqual((b.read_bytes(), a.read_bytes()), (DATA, DATA))
        self.assertEqual((after.st_mode, after.st_mtime_ns), (before.st_mode, before.st_mtime_ns))
        b.write_bytes(b"changed")  # still independently writable
        self.assertEqual(a.read_bytes(), DATA)
        self.assertEqual(list(self.tmp.rglob("*" + clean.CLEAN_DEDUPE_TEMP)), [])

    def test_files_that_already_share_their_blocks_are_neither_redone_nor_counted(self):
        # 2026-10-30: every pass re-cloned the same pairs and reported 30 GB saved while the disk gained 3.8 GB.
        write(self.tmp / "J-1" / "corpus.txt")
        b = write(self.tmp / "J-2" / "corpus.txt")
        first = clean.clean_dedupe_run([str(self.tmp)], dry_run=False, at=self.now)
        self.assertEqual(first["files"], 1)
        inode = os.lstat(b).st_ino
        again = clean.clean_dedupe_run([str(self.tmp)], dry_run=False, at=self.now)
        self.assertEqual((again["files"], again["bytes"], again.get("alreadyShared")), (0, 0, 1))
        self.assertEqual(os.lstat(b).st_ino, inode)  # not replaced a second time
        self.assertEqual(clean.clean_dedupe_private_bytes(str(b)), 0)

    def test_a_file_changed_after_the_scan_is_left_alone(self):
        write(self.tmp / "J-1" / "x.bin")
        b = write(self.tmp / "J-2" / "x.bin")
        def racing_clone(src, dst):
            write(b, os.urandom(len(DATA)))  # a writer changes the target between scan and swap
            clean.clean_dedupe_clone(src, dst)

        before = b.read_bytes()
        done = clean.clean_dedupe_run([str(self.tmp)], dry_run=False, at=self.now, clone=racing_clone)
        self.assertEqual(done["files"], 0)
        self.assertNotEqual(b.read_bytes(), DATA)
        self.assertEqual(len(b.read_bytes()), len(before))
        self.assertEqual(list(self.tmp.rglob("*" + clean.CLEAN_DEDUPE_TEMP)), [])


class Elsewhere(Base):
    def test_off_macos_the_target_says_why_and_changes_nothing(self):
        b = write(self.tmp / "J-2" / "x.bin")
        write(self.tmp / "J-1" / "x.bin")
        inode = os.lstat(b).st_ino
        fake = os.uname_result(("Linux", "h", "6", "v", "x86_64"))
        with unittest.mock.patch.object(clean.os, "uname", return_value=fake):
            out = clean.clean_dedupe_run([str(self.tmp)], dry_run=False, at=self.now)
        self.assertIn("macOS", out["skipped"])
        self.assertEqual((out["files"], os.lstat(b).st_ino), (0, inode))

    def test_the_target_is_a_named_clean_and_steady_target(self):
        self.assertIn("dedupe-clones", clean.CLEAN_NAMES)
        self.assertIn("dedupe-clones", clean.CLEAN_STEADY_NAMES)


if __name__ == "__main__":
    unittest.main()
