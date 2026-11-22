"""Names the temp cleaner never offers (pure regex; no files)."""
import unittest

from trimreaper import clean


class TempSystemNames(unittest.TestCase):
    def test_jiti_transpile_cache_is_never_a_candidate(self):
        self.assertTrue(clean.CLEAN_TEMP_SYSTEM.match("jiti"))

    def test_lookalike_leftovers_stay_candidates(self):
        for name in ("jiti-old", "jitikit", "my-jiti", "tmp.abc123"):
            self.assertIsNone(clean.CLEAN_TEMP_SYSTEM.match(name), name)


if __name__ == "__main__":
    unittest.main()
