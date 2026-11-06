"""A Git store nested below a proven scoped top-level folder is cleared with its folder only when a host scope is set.

The fixture root itself, a protected path and another Git store never qualify, and with no scope the Git dir keeps its
old refusal. The scan takes the scope as `git_scope`; removal takes it from `REAP_GIT_SCOPE` (a host sets it only while a
fixture with proven scope is removed).
"""
from __future__ import annotations

from pathlib import Path

from helpers import Home

from trimreaper import core


class ScopedNestedGitStores(Home):
    """Fixture removal with a host-proven scope that names the top-level folders whose nested Git stores may go."""

    def setUp(self):
        super().setUp()
        self.clear_scope()
        self.addCleanup(self.clear_scope)

    def clear_scope(self):
        if hasattr(core.REAP_GIT_SCOPE, "scope"):
            delattr(core.REAP_GIT_SCOPE, "scope")

    def set_scope(self, *tops: str):
        core.REAP_GIT_SCOPE.scope = frozenset(tops)

    def fixture(self, root_git=False, under_git=False, under_protected=False) -> Path:
        """A fixture folder with `top/pkg/.git` as the nested Git store, plus optional extra layouts."""
        root = self.tmp / "fixture"
        root.mkdir()
        if root_git:
            self.git(root, "init", "-q", "-b", "main")
        top = root / "top"
        top.mkdir()
        pkg = top / "pkg"
        pkg.mkdir()
        self.git(pkg, "init", "-q", "-b", "main")
        (pkg / "README.md").write_text("nested\n")
        if under_git:
            repo = top / "repo"
            repo.mkdir()
            self.git(repo, "init", "-q", "-b", "main")
            sub = repo / ".git" / "modules" / "sub"
            sub.mkdir(parents=True)
            (sub / ".git").mkdir()  # a Git store nested inside a Git store
        if under_protected:
            guard = top / "guard" / ".env.local" / "x"
            guard.mkdir(parents=True)
            (guard / ".git").mkdir()  # a Git store under a protected path
        (top / "notes.txt").write_text("removable\n")
        return root

    def remove(self, root: Path):
        fence = core.reap_fence(str(root), str(root.parent))
        result = {"checks": []}
        core.reap_remove_and_verify(fence, str(root), [], result, False)
        return result

    # ---- the predicate ------------------------------------------------------------------------

    def test_reap_git_scoped_only_below_a_proven_top_level_folder(self):
        self.set_scope("top")
        self.assertTrue(core.reap_git_scoped("top/pkg/.git"))
        self.assertTrue(core.reap_git_scoped("top/.git"))  # the scoped folder's own store goes with it
        self.assertFalse(core.reap_git_scoped(".git"))  # the fixture root never qualifies
        self.assertFalse(core.reap_git_scoped("pkg/.git"))  # not below a scoped folder
        self.assertFalse(core.reap_git_scoped("other/pkg/.git"))  # outside the scope
        self.assertFalse(core.reap_git_scoped("top/x/.wrangler/y/.git"))  # under a protected path
        self.assertFalse(core.reap_git_scoped("top/repo/.git/modules/sub/.git"))  # under a Git store

    def test_reap_git_scoped_without_a_scope_refuses(self):
        self.assertFalse(core.reap_git_scoped("top/pkg/.git"))
        self.assertFalse(core.reap_git_scoped(".git"))

    # ---- scan + removal -----------------------------------------------------------------------

    def test_nested_git_store_below_a_scoped_folder_is_removed_with_it(self):
        root = self.fixture()
        self.set_scope("top")
        core.reap_protected_scan(str(root), git_scope=frozenset({"top"}))  # names it, does not refuse it
        result = self.remove(root)
        self.assertEqual(result["outcome"], "deleted", result)
        self.assertFalse(root.exists())

    def test_without_a_scope_the_scan_refuses_the_nested_git_store(self):
        root = self.fixture()
        with self.assertRaises(core.ReapRefused) as raised:
            core.reap_protected_scan(str(root))
        self.assertIn("nested-git:top/pkg", str(raised.exception))
        self.assertTrue((root / "top/pkg/.git").exists())

    def test_without_a_scope_removal_refuses_a_nested_git_store(self):
        root = self.fixture()
        result = self.remove(root)  # the scan is bypassed; the removal gate itself refuses
        self.assertIn(result["outcome"], ("refused", "partial"), result)
        self.assertIn("nested-git-appeared", result["reason"], result)
        self.assertTrue((root / "top/pkg/.git").exists())

    def test_a_git_store_at_the_fixture_root_is_never_cleared(self):
        root = self.fixture(root_git=True)
        self.set_scope("top")
        with self.assertRaises(core.ReapRefused) as raised:
            core.reap_protected_scan(str(root), git_scope=frozenset({"top"}))
        self.assertIn("nested-git:", str(raised.exception))
        self.assertTrue((root / ".git").exists())

    def test_a_git_store_under_a_protected_path_is_never_cleared(self):
        root = self.fixture(under_protected=True)
        self.set_scope("top")
        with self.assertRaises(core.ReapRefused) as raised:
            core.reap_protected_scan(str(root), git_scope=frozenset({"top"}))
        self.assertIn("protected-leaf:", str(raised.exception))
        self.assertIn(".env.local", str(raised.exception))
        self.assertTrue((root / "top/guard/.env.local/x/.git").exists())

    def test_a_git_store_under_another_git_store_is_never_cleared(self):
        root = self.fixture(under_git=True)
        self.set_scope("top")
        core.reap_protected_scan(str(root), git_scope=frozenset({"top"}))  # named, never entered
        result = self.remove(root)
        self.assertIn(result["outcome"], ("refused", "partial"), result)
        self.assertIn("nested-git-appeared", result["reason"], result)
        self.assertTrue((root / "top/repo/.git/modules/sub/.git").exists())
