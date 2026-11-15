"""Ownership of synthetic state connections and transactions."""
import contextlib
import sqlite3
import unittest

from helpers import Home


class FixtureResources(Home):
    def test_transaction_context_commits_and_closes(self):
        self.cli("job", "list")
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("INSERT INTO meta(key, value) VALUES('fixture-test', 'committed')")
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
        with contextlib.closing(self.state()) as check:
            self.assertEqual(check.execute("SELECT value FROM meta WHERE key='fixture-test'").fetchone()[0], "committed")

    def test_transaction_context_rolls_back_and_closes_on_failure(self):
        self.cli("job", "list")
        with self.assertRaisesRegex(ValueError, "abort"):
            with contextlib.closing(self.state()) as conn, conn:
                conn.execute("INSERT INTO meta(key, value) VALUES('fixture-test', 'rolled back')")
                raise ValueError("abort")
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
        with contextlib.closing(self.state()) as check:
            self.assertIsNone(check.execute("SELECT value FROM meta WHERE key='fixture-test'").fetchone())

    def test_fixture_cleanup_closes_bare_connection_on_failure(self):
        fixture = Home(methodName="runTest")
        fixture.setUp()
        try:
            fixture.cli("job", "list")
            conn = fixture.state()
            with self.assertRaisesRegex(ValueError, "abort"):
                with conn:
                    conn.execute("INSERT INTO meta(key, value) VALUES('fixture-test', 'rolled back')")
                    raise ValueError("abort")
        finally:
            fixture.doCleanups()
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
