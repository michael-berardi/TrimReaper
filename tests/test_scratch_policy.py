"""Synthetic policy checks for the opt-in scratch protected-copy rule."""
import unittest

from trimreaper import host


class ScratchPolicy(unittest.TestCase):
    def test_default_remains_keep(self):
        self.assertEqual(host.DEFAULT_POLICY["agentScratchProtectedCopies"], "keep")

    def test_archive_requires_archive_engine(self):
        host.check_policy({"agentScratchProtectedCopies": "archive", "cloneArchive": "archive"})
        with self.assertRaisesRegex(host.HostError, "needs cloneArchive"):
            host.check_policy({"agentScratchProtectedCopies": "archive", "cloneArchive": "keep"})

    def test_unknown_values_fail_closed(self):
        for value in (True, False, None, "remove", 1, [], {}):
            with self.subTest(value=value), self.assertRaises(host.HostError):
                host.check_policy({"agentScratchProtectedCopies": value})
