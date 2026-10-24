"""Exact Git metadata binding: ownership, gitfiles and relative checkout paths."""
from __future__ import annotations

import contextlib
import os
from unittest import mock

from helpers import Home
from trimreaper import clean, core


class GitMetadataBinding(Home):
    def linked(self):
        repo = self.clone("main")
        worktree = self.dev / "linked"
        self.git(repo, "worktree", "add", "--detach", str(worktree))
        return repo, worktree

    def refuse_foreign_owner(self, repo, targets):
        original = os.lstat
        for target in targets:
            def foreign(path, *args, **kwargs):
                info = original(path, *args, **kwargs)
                if os.path.abspath(path) == str(target):
                    fields = list(info)
                    fields[4] = os.getuid() + 1
                    return os.stat_result(fields)
                return info

            with self.subTest(target=target), mock.patch.object(core.os, "lstat", side_effect=foreign), \
                    mock.patch.object(core, "reap_probe", wraps=core.reap_probe) as probe:
                with self.assertRaisesRegex(core.ReapRefused, "^git-error:unsafe-metadata:foreign-owner$"):
                    core.reap_git_metadata_gate(str(repo))
                probe.assert_not_called()

    def test_foreign_owned_dot_git_directory_is_refused(self):
        repo = self.clone("foreign")
        self.refuse_foreign_owner(repo, [repo / ".git"])

    def test_foreign_owned_gitfile_gitdir_and_commondir_are_refused(self):
        repo, worktree = self.linked()
        gitdir = repo / ".git/worktrees/linked"
        self.refuse_foreign_owner(worktree, [worktree / ".git", gitdir, gitdir / "commondir", repo / ".git"])

    def assert_git_directories(self, repo, expected):
        args = ("rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
        # Discovery is used only as a fixture oracle, never by a reaper probe.
        self.assertEqual(self.git(repo, *args).splitlines(), [str(path) for path in expected])
        for session in (False, True):
            with self.subTest(session=session), core.reap_git_session(str(repo)) if session else contextlib.nullcontext():
                with mock.patch.object(core, "reap_probe", wraps=core.reap_probe) as probe:
                    code, out = core.reap_git(str(repo), *args)
                self.assertEqual(code, 0, out)
                self.assertEqual(out.splitlines(), [str(path) for path in expected])
                bound = [call.args[0][1] for call in probe.call_args_list if call.args[0][1].startswith("--git-dir=")]
                self.assertEqual(bound, ["--git-dir=" + str(expected[0])])
        self.assertIn("git-head", core.reap_git_repo_proof(str(repo), ("clean", "pushed")))

    def test_linked_worktree_gitfile_reports_real_gitdir_and_common_directory(self):
        repo, worktree = self.linked()
        gitdir = repo / ".git/worktrees/linked"
        self.assertTrue((worktree / ".git").is_file())
        self.assert_git_directories(worktree, [gitdir, repo / ".git"])
        self.assertTrue(core.reap_linked_worktree(str(worktree)))
        clean.clean_linked_identity({"path": str(worktree), "repo": str(repo), "gitdir": str(gitdir)})

    def test_submodule_gitfile_reports_real_gitdir_without_weakening_parent_guard(self):
        source = self.clone("source")
        parent = self.clone("parent")
        self.git(parent, "-c", "protocol.file.allow=always", "submodule", "add", str(source), "module")
        self.git(parent, "commit", "-qm", "add local submodule")
        module = parent / "module"
        gitdir = parent / ".git/modules/module"
        self.assertTrue((module / ".git").is_file())
        self.assert_git_directories(module, [gitdir, gitdir])
        self.assertFalse(core.reap_linked_worktree(str(module)))
        with self.assertRaisesRegex(core.ReapRefused, "^git-unsafe-index:submodule$"):
            core.reap_git_clean_closed(str(parent))

    def test_relative_checkout_path_is_not_doubled_after_git_chdir(self):
        repo = self.clone("relative")
        relative = os.path.relpath(repo)
        for session in (False, True):
            with self.subTest(session=session), core.reap_git_session(relative) if session else contextlib.nullcontext():
                code, out = core.reap_git(relative, "rev-parse", "--show-toplevel")
                self.assertEqual(code, 0, out)
                self.assertEqual(out.strip(), str(repo))
                code, out = core.reap_git_input(relative, "node_modules/example\0", "check-ignore", "-z", "--stdin")
                self.assertEqual((code, out), (0, "node_modules/example\0"))
