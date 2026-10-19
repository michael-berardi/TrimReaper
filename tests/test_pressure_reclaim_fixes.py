"""Synthetic regressions for pressure retry safety, Astro output and protected filesystem flags."""
import os
import stat
from types import SimpleNamespace
from unittest import mock

from helpers import Home
from trimreaper import core


class AstroOutput(Home):
    def setup_astro(self):
        repo = self.clone('site-j0080')
        self.add_job('J-0080', repo)
        self.put(repo, '.astro/types.d.ts', b'generated\n')
        return repo

    def test_unignored_untracked_astro_is_regenerable(self):
        repo = self.setup_astro()
        out = self.cli('reap', '--job', 'J-0080')
        self.assertEqual(out['deleted'], 1, out)
        self.assertFalse((repo / '.astro').exists())
        self.assertTrue((repo / 'README.md').exists())

    def test_tracked_astro_is_never_removed(self):
        repo = self.setup_astro()
        self.git(repo, 'add', '.astro/types.d.ts')
        self.git(repo, 'commit', '-qm', 'tracked source')
        self.git(repo, 'push', '-q')
        out = self.cli('reap', '--job', 'J-0080')
        self.assertEqual(out['deleted'], 0, out)
        self.assertTrue((repo / '.astro/types.d.ts').exists())

    def test_dirty_source_preserves_itself_and_still_gives_up_generated_output(self):
        """A finished job's generated output rebuilds from the source that stays: uncommitted tracked work is not its business
        (0.6.2). The clone keeps the edit, the generated metadata goes, and a tracked one still never goes."""
        repo = self.setup_astro()
        self.put(repo, 'README.md', b'edited\n')
        out = self.cli('reap', '--job', 'J-0080')
        self.assertEqual(out['deleted'], 1, out)
        self.assertFalse((repo / '.astro').exists())
        self.assertEqual((repo / 'README.md').read_bytes(), b'edited\n')
        self.assertTrue((repo / '.git').exists())

    def test_protected_leaf_still_preserves_output(self):
        repo = self.setup_astro()
        self.put(repo, '.astro/.env.local', b'fixture\n')
        self.assertEqual(self.cli('reap', '--job', 'J-0080')['deleted'], 0)
        self.assertTrue((repo / '.astro').exists())


class ProtectedFlagAndLateRetention(Home):
    def test_system_nounlink_flag_refuses_before_entering_root(self):
        flag = getattr(stat, 'SF_NOUNLINK', 0x100000)
        with mock.patch.object(core.stat, 'SF_NOUNLINK', flag, create=True), \
             mock.patch.object(core.os, 'lstat', return_value=SimpleNamespace(st_flags=flag)), \
             mock.patch.object(core, 'reap_fd_walk') as walk:
            with self.assertRaisesRegex(core.ReapRefused, 'filesystem-protected-flags:root'):
                core.reap_protected_scan('/synthetic/owned-temp')
            walk.assert_not_called()

    def test_new_preserving_marker_blocks_file_expiry_parent(self):
        root = self.tmp / 'evidence'
        self.put(root, '.ut-retention-evidence', b'')
        log = self.put(root, 'large.log')
        fd = os.open(root, core.REAP_ROOT_FLAGS)
        try:
            parent = core.evidence_parent(fd, 'large.log', marked=True)
            os.close(parent)
            (root / '.ut-retention-evidence').rename(root / '.ut-retention-customer')
            with self.assertRaisesRegex(core.ReapRefused, 'evidence-retention-changed'):
                core.evidence_parent(fd, 'large.log', marked=True)
        finally:
            os.close(fd)
        self.assertTrue(log.exists())
