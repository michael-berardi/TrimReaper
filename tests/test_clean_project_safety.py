"""Generic build cleanup proofs, using only throwaway homes and local Git remotes."""
from __future__ import annotations

import contextlib
from pathlib import Path
from unittest import mock

from helpers import Home
from trimreaper import clean


class ProjectSafety(Home):
    def setUp(self):
        super().setUp()
        for patch in (mock.patch.object(clean, "clean_running", return_value=set()),
                      mock.patch.object(clean, "reap_process_table", return_value={"files": [], "commands": []})):
            patch.start()
            self.addCleanup(patch.stop)
        self.repo = self.clone("site", ignored=(".next/", "target/", ".env*"),
                               tracked={"package.json": "{}\n", "src/main.rs": "fn main() {}\n"})
        self.put(self.repo, ".next/cache/data")
        self.put(self.repo, "target/debug/app")
        self.put(self.repo, "target/debug/.cargo-lock", b"")
        self.put(self.repo, "target/release/app")
        self.put(self.repo, "target/release/bundle/App.app/Contents/binary")
        self.put(self.repo, "target/release/incremental/state")
        self.age(self.repo, 5)

    def entry(self, name):
        path = self.repo / ("target/debug" if name == "rust-build-output" else ".next")
        entry = {"name": name, "kind": "debris", "path": str(path), "paths": [str(path)],
                 "repo": str(self.repo), "root": str(path.parent), "idleHours": 48}
        if name == "rust-build-output":
            entry["target"] = str(self.repo / "target")
        return entry

    def entries(self):
        return [self.entry(name) for name in ("rust-build-output", "project-dependencies")]

    def refused(self, reason):
        for entry in self.entries():
            with self.subTest(category=entry["name"]), self.assertRaisesRegex(clean.ReapRefused, reason):
                clean.clean_extra_gates(entry)
            self.assertTrue(Path(entry["path"]).exists())

    def test_dirty_tracked_source_refused(self):
        self.put(self.repo, "src/main.rs", b"unfinished source\n")
        self.age(self.repo, 5)
        self.refused("dirty")

    def test_untracked_source_refused(self):
        self.put(self.repo, "src/new.rs", b"unfinished source\n")
        self.age(self.repo, 5)
        self.refused("dirty")

    def test_unpushed_head_refused(self):
        self.put(self.repo, "src/main.rs", b"fn main() { println!(\"local\"); }\n")
        self.git(self.repo, "commit", "-qam", "local only")
        self.age(self.repo, 5)
        self.refused("unpushed")

    def test_stash_refused(self):
        self.put(self.repo, "src/main.rs", b"stashed work\n")
        self.git(self.repo, "stash", "push", "-qm", "unfinished")
        self.age(self.repo, 5)
        self.refused("stash-present")

    def test_missing_head_refused(self):
        (self.repo / ".git/refs/heads/main").unlink()
        self.refused("git-error:head-unresolved")

    def test_invalid_git_refused(self):
        (self.repo / ".git/HEAD").write_text("invalid\n")
        self.refused("git-error")

    def test_hidden_index_edits_refused(self):
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag=flag):
                self.git(self.repo, "update-index", flag, "src/main.rs")
                self.put(self.repo, "src/main.rs", b"hidden edit\n")
                self.age(self.repo, 5)
                self.refused("git-unsafe-index:index-flag")
                self.git(self.repo, "update-index", "--no-assume-unchanged", "--no-skip-worktree", "src/main.rs")

    def test_tracked_candidate_refused_even_when_ignore_matches(self):
        self.put(self.repo, ".next/tracked", b"keep\n")
        self.put(self.repo, "target/debug/tracked", b"keep\n")
        self.git(self.repo, "add", "-f", ".next/tracked", "target/debug/tracked")
        self.git(self.repo, "commit", "-qm", "tracked output")
        self.git(self.repo, "push", "-q")
        self.age(self.repo, 5)
        self.refused("tracked-files|not-git-ignored")

    def test_not_ignored_candidate_refused(self):
        (self.repo / ".gitignore").write_text(".env*\n")
        self.git(self.repo, "commit", "-qam", "stop ignoring output")
        self.git(self.repo, "push", "-q")
        self.age(self.repo, 5)
        self.refused("dirty|not-git-ignored")

    def test_ancestor_evidence_refused_through_home(self):
        for ancestor in (self.repo, self.dev, self.home):
            with self.subTest(ancestor=ancestor):
                marker = self.put(ancestor, ".ut-retention-evidence", b"")
                self.refused("retention:evidence")
                marker.unlink()

    def test_job_root_evidence_above_repository_refused(self):
        root = self.dev / "J-4242"
        root.mkdir()
        self.repo.rename(root / self.repo.name)
        self.repo = root / self.repo.name
        self.put(root, ".ut-retention-evidence", b"")
        self.refused("retention:evidence")

    def test_protected_and_symlink_ancestors_not_traversed(self):
        original = self.repo
        for name, reason in ((".env", "protected-ancestor"), ("alias", "non-directory-ancestor")):
            with self.subTest(name=name):
                if name == "alias":
                    (self.dev / name).symlink_to(original, target_is_directory=True)
                    self.repo = self.dev / name
                else:
                    parent = self.dev / name
                    parent.mkdir()
                    original.rename(parent / "site")
                    self.repo = parent / "site"
                with mock.patch.object(clean, "reap_cache_retention", side_effect=AssertionError("must not scan")):
                    self.refused(reason)
                if name != "alias":
                    self.repo.rename(original)
                self.repo = original

    def test_running_and_starting_concrete_declared_jobs_refused(self):
        for status in ("running", "starting"):
            with self.subTest(status=status):
                job = "J-4242" if status == "running" else "J-4243"
                self.add_job(job, cwd=self.repo, status="running")
                if status == "starting":
                    with contextlib.closing(self.state()) as conn, conn:
                        conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job))
                self.refused("in-use-by-running-job:" + job)
                self.cli("job", "status", job, "done")

    def test_named_live_job_in_ancestor_refused(self):
        root = self.dev / "J-4242" / "nested"
        root.mkdir(parents=True)
        self.repo.rename(root / "site")
        self.repo = root / "site"
        self.add_job("J-4242", cwd=self.home, status="running")
        self.refused("in-use-by-running-job:J-4242")
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET status='starting' WHERE id='J-4242'")
        self.refused("in-use-by-running-job:J-4242")

    def test_finished_distant_ancestor_does_not_shorten_dependency_idle_window(self):
        parent = self.dev / "group-j4242" / "nested"
        parent.mkdir(parents=True)
        self.repo.rename(parent / "site")
        self.repo = parent / "site"
        self.add_job("J-4242", cwd=self.home, status="done")
        self.age(self.repo, 0.5)
        with self.assertRaisesRegex(clean.ReapRefused, "recent:12h-of-48h"):
            clean.clean_extra_gates(self.entry("project-dependencies"))
        clean.clean_extra_gates(self.entry("rust-build-output"))

    def test_dependency_missing_idle_policy_refused(self):
        entry = self.entry("project-dependencies")
        entry.pop("idleHours")
        with self.assertRaisesRegex(clean.ReapRefused, "project-idle-policy-unproved"):
            clean.clean_extra_gates(entry)

    def test_broad_home_and_dev_cwd_do_not_claim_idle_repository(self):
        self.add_job("J-4242", cwd=self.home, status="running")
        self.add_job("J-4243", cwd=self.dev, status="running")
        for entry in self.entries():
            clean.clean_extra_gates(entry)

    def test_explicit_worktree_claim_not_skipped_with_broad_cwd(self):
        self.cli("job", "add", "J-4242", "--status", "running", "--cwd", str(self.home),
                 "--worktree", str(self.repo))
        self.refused("in-use-by-running-job:J-4242")

    def test_explicit_collection_worktree_claim_not_skipped(self):
        self.cli("job", "add", "J-4242", "--status", "running", "--cwd", str(self.home),
                 "--worktree", str(self.dev))
        self.refused("in-use-by-running-job:J-4242")

    def test_parked_named_and_declared_regenerable_output_remains_eligible(self):
        moved = self.dev / "site-j4242"
        self.repo.rename(moved)
        self.repo = moved
        self.add_job("J-4242", cwd=self.repo, status="parked")
        self.add_job("J-4243", cwd=self.repo, status="parked")
        for entry in self.entries():
            clean.clean_extra_gates(entry)

    def test_protected_output_leaves_preserved(self):
        for entry in self.entries():
            protected = self.put(Path(entry["path"]), ".env.local", b"secret")
            with self.assertRaisesRegex(clean.ReapRefused, "protected-leaf"):
                clean.clean_act(entry, "fixture", 7)
            self.assertEqual(protected.read_bytes(), b"secret")

    def test_actual_queue_and_cargo_locks_held_through_removal_and_receipt(self):
        queue = clean.reap_queue_lock(str(self.repo))
        self.assertFalse(queue.exists())
        original_remove = clean.reap_remove_and_verify
        original_receipt = clean.clean_receipt
        checked = []

        def assert_locked():
            self.assertTrue(queue.exists())
            with self.assertRaisesRegex(clean.ReapRefused, "lock-busy"):
                with clean.reap_locks([queue], create=False):
                    pass

        def remove(*args, **kwargs):
            assert_locked()
            cargo = self.repo / "target/debug/.cargo-lock"
            if entry["name"] == "rust-build-output" and cargo.exists():
                with self.assertRaisesRegex(clean.ReapRefused, "lock-busy"):
                    with clean.reap_locks([cargo], create=False):
                        pass
            checked.append("remove")
            return original_remove(*args, **kwargs)

        def receipt(event, *args, **kwargs):
            assert_locked()
            checked.append(event)
            return original_receipt(event, *args, **kwargs)

        with mock.patch.object(clean, "reap_remove_and_verify", side_effect=remove), \
                mock.patch.object(clean, "clean_receipt", side_effect=receipt):
            # Dependency removal must not touch Cargo; then Rust holds its own locks as well.
            for name in ("project-dependencies", "rust-build-output"):
                entry = self.entry(name)
                clean.clean_act(entry, "fixture", 7)
                self.assertEqual(entry["outcome"], "removed")
        self.assertEqual(checked, ["plan", "remove", "result"] * 2)
        self.assertTrue((self.repo / "src/main.rs").exists())
        self.assertTrue((self.repo / "target/release/app").exists())
        self.assertTrue((self.repo / "target/release/bundle/App.app/Contents/binary").exists())
        self.assertTrue((self.repo / "target/release/incremental/state").exists())

    def test_busy_queue_lock_prevents_actual_removal(self):
        queue = clean.reap_queue_lock(str(self.repo))
        with clean.reap_locks([queue], create=True):
            for entry in self.entries():
                with self.assertRaisesRegex(clean.ReapRefused, "lock-busy"):
                    clean.clean_act(entry, "fixture", 7)
                self.assertTrue(Path(entry["path"]).exists())

    def test_unavailable_queue_lock_prevents_actual_removal(self):
        queue = clean.reap_queue_lock(str(self.repo))
        queue.parent.mkdir(parents=True)
        queue.symlink_to(self.tmp / "must-not-create")
        for entry in self.entries():
            with self.assertRaisesRegex(clean.ReapRefused, "lock-unavailable"):
                clean.clean_act(entry, "fixture", 7)
            self.assertTrue(Path(entry["path"]).exists())
        self.assertFalse((self.tmp / "must-not-create").exists())

    def test_zero_queue_lock_count_refused(self):
        from contextlib import contextmanager

        @contextmanager
        def missing(*args, **kwargs):
            yield 0

        with mock.patch.object(clean, "reap_locks", missing):
            for entry in self.entries():
                with self.assertRaisesRegex(clean.ReapRefused, "lock-unavailable:project-queue"):
                    clean.clean_act(entry, "fixture", 7)

    def test_revalidation_under_queue_lock_catches_new_dirty_state(self):
        for entry in self.entries():
            clean.clean_extra_gates(entry)
        original = clean.clean_receipt

        def receipt(event, *args, **kwargs):
            result = original(event, *args, **kwargs)
            if event == "plan":
                self.put(self.repo, "src/main.rs", b"new edit\n")
            return result

        with mock.patch.object(clean, "clean_receipt", side_effect=receipt):
            for entry in self.entries():
                with self.assertRaisesRegex(clean.ReapRefused, "dirty"):
                    clean.clean_act(entry, "fixture", 7)
                self.assertTrue(Path(entry["path"]).exists())

    def test_revalidation_refuses_new_live_job_or_evidence(self):
        for entry in self.entries():
            clean.clean_extra_gates(entry)
        self.add_job("J-4242", cwd=self.repo, status="running")
        for entry in self.entries():
            with self.assertRaisesRegex(clean.ReapRefused, "in-use-by-running-job"):
                clean.clean_act(entry, "fixture", 7)
            self.assertTrue(Path(entry["path"]).exists())
        self.cli("job", "status", "J-4242", "done")
        self.put(self.dev, ".ut-retention-evidence", b"")
        for entry in self.entries():
            with self.assertRaisesRegex(clean.ReapRefused, "retention:evidence"):
                clean.clean_act(entry, "fixture", 7)
            self.assertTrue(Path(entry["path"]).exists())

    def test_dry_run_does_not_create_queue_or_cargo_locks(self):
        queue = clean.reap_queue_lock(str(self.repo))
        before = sorted(str(p.relative_to(self.repo)) for p in self.repo.rglob("*.cargo*"))
        for name in ("rust-build-output", "project-dependencies"):
            entries = clean.clean_extra_entries({name}, [])
            self.assertTrue(entries)
            self.assertFalse(any("keep" in entry for entry in entries), entries)
        self.assertFalse(queue.exists())
        self.assertEqual(before, sorted(str(p.relative_to(self.repo)) for p in self.repo.rglob("*.cargo*")))
