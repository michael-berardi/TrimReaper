"""A git step that fails on an unchanged folder is remembered and not retried every pass (2026-11-04)."""
from __future__ import annotations

from unittest import mock

from test_clean import Home

from trimreaper import core


class RememberedArchiveFailures(Home):
    def test_a_bundle_create_failure_is_remembered_and_not_retried(self):
        item = {"path": str(self.home / "job" / "repo")}
        calls = []

        def failing(*args, **kwargs):
            calls.append(1)
            raise core.ReapRefused("archive-failed:bundle-create:128")

        with mock.patch.object(core, "reap_archive_create_owned", failing):
            for _ in range(3):
                with self.assertRaises(core.ReapRefused) as caught:
                    core.reap_archive_create(item, {}, [], [], None)
                self.assertIn("bundle-create:128", str(caught.exception))
        self.assertEqual(len(calls), 1)

    def test_an_unremembered_failure_is_retried(self):
        item = {"path": str(self.home / "job" / "other")}
        calls = []

        def failing(*args, **kwargs):
            calls.append(1)
            raise core.ReapRefused("archive-failed:synthetic")

        with mock.patch.object(core, "reap_archive_create_owned", failing):
            for _ in range(2):
                with self.assertRaises(core.ReapRefused):
                    core.reap_archive_create(item, {}, [], [], None)
        self.assertEqual(len(calls), 2)
