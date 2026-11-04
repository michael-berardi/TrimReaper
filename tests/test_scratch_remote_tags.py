"""Remote tag proofs for whole disposable repositories."""
import os
from unittest import mock

from helpers import Home
from trimreaper import core


class ScratchRemoteTags(Home):
    def setUp(self):
        super().setUp()
        self.write_config({"scratchRoot": "~/.cache/scratch"})
        source = self.clone("tagged")
        self.repo = self.home / ".cache" / "scratch" / "task" / "repo"
        self.repo.parent.mkdir(parents=True)
        os.rename(source, self.repo)
        self.git(self.repo, "tag", "-a", "v1", "-m", "release")

    def proof(self):
        return core.reap_git_repo_proof(str(self.repo), ("all-refs",))

    def test_remote_must_have_exact_annotated_tag_object(self):
        with self.assertRaisesRegex(core.ReapRefused, "unproved-tags"):
            self.proof()
        self.git(self.repo, "push", "-q", "origin", "refs/tags/v1")
        self.proof()
        self.git(self.repo, "tag", "-f", "-a", "v1", "-m", "changed")
        with self.assertRaisesRegex(core.ReapRefused, "unproved-tags"):
            self.proof()

    def test_unavailable_remote_and_timeout_keep_repository(self):
        self.git(self.repo, "push", "-q", "origin", "refs/tags/v1")
        self.git(self.repo, "remote", "set-url", "origin", str(self.remotes / "missing.git"))
        with self.assertRaisesRegex(core.ReapRefused, "unproved-tags"):
            self.proof()
        original = core.reap_probe
        def probe(argv, timeout=60, env=None, data=None):
            if "ls-remote" in argv:
                self.assertEqual(timeout, 30)
                self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
                raise core.ReapRefused("probe-timeout:git")
            return original(argv, timeout, env, data)
        with mock.patch.object(core, "reap_probe", side_effect=probe):
            with self.assertRaisesRegex(core.ReapRefused, "probe-timeout"):
                self.proof()

    def test_tagged_clone_outside_disposable_roots_is_still_kept(self):
        self.git(self.repo, "push", "-q", "origin", "refs/tags/v1")
        dest = self.dev / "outside"
        os.rename(self.repo, dest)
        with self.assertRaisesRegex(core.ReapRefused, "unproved-tags"):
            core.reap_git_repo_proof(str(dest), ("all-refs",))
