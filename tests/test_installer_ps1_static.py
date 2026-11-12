"""The Windows refusal script is piped to iex, so it must not change the caller's session."""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class InstallPs1Static(unittest.TestCase):
    def test_no_session_preference_changes(self):
        with open(os.path.join(ROOT, "scripts", "install.ps1"), encoding="utf-8") as f:
            text = f.read()
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        self.assertIsNone(re.search(r"\$(ErrorActionPreference|ProgressPreference|WarningPreference)\s*=", code, re.I))
        self.assertIn("throw ", code)


if __name__ == "__main__":
    unittest.main()
