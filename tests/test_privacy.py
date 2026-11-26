"""Privacy regression: no telemetry dependency or endpoint, no network on default config, private bounded state."""
from __future__ import annotations

import ast
import os
import re
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from trimreaper import __version__, cli, clean, host

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = sorted((ROOT / "src" / "trimreaper").glob("*.py")) + sorted((ROOT / "scripts").glob("*.sh")) \
    + [ROOT / "scripts" / "install.ps1", ROOT / "pyproject.toml"]

FORBIDDEN_MODULES = frozenset({
    "requests", "httpx", "aiohttp", "urllib", "socket", "http", "ftplib", "smtplib",
    "sentry_sdk", "posthog", "mixpanel", "amplitude", "segment", "analytics", "statsd",
    "datadog", "bugsnag", "opentelemetry", "openai", "anthropic", "huggingface_hub",
})
FORBIDDEN_ENDPOINT = re.compile(
    r"(?i)https?://[^\s\"']*(?:sentry|posthog|mixpanel|amplitude|segment|analytics|telemetry|bugsnag|datadog|otlp|googletagmanager|google-analytics)[^\s\"']*")


class Privacy(unittest.TestCase):
    def test_version_matches_the_release_entry(self):
        self.assertEqual(__version__, "0.26.0")
        self.assertIn("## 0.26.0 (2026-11-26)", (ROOT / "CHANGELOG.md").read_text())

    def test_no_telemetry_package_or_endpoint_in_shipped_code(self):
        for path in SOURCE_FILES:
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(FORBIDDEN_ENDPOINT.search(text), f"{path.name} names a telemetry endpoint")
            if path.suffix != ".py":
                continue
            for node in ast.walk(ast.parse(text)):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or "").split(".")[0]]
                self.assertEqual(set(names) & FORBIDDEN_MODULES, set(), f"{path.name}:{getattr(node, 'lineno', 1)}")

    def test_tool_cleaners_disable_third_party_telemetry(self):
        expected = {"HOMEBREW_NO_ANALYTICS": "1", "HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_AUTOREMOVE": "1",
                    "DO_NOT_TRACK": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
                    "NPM_CONFIG_UPDATE_NOTIFIER": "false", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
        for key, value in expected.items():
            self.assertEqual(clean.CLEAN_TOOL_ENV.get(key), value, key)
        installer = (ROOT / "scripts" / "install.sh").read_text()
        for setting in ("DO_NOT_TRACK=1", "HOMEBREW_NO_ANALYTICS=1", "PIP_DISABLE_PIP_VERSION_CHECK=1",
                        "--disable-pip-version-check", "--no-index", "--no-deps"):
            self.assertIn(setting, installer, setting)

    def test_default_config_command_makes_no_network_call(self):
        def network(*args, **kwargs):
            raise AssertionError("a default command tried to open a network connection")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"TRIMREAPER_HOME": tmp, "TRIMREAPER_CONFIG": str(Path(tmp) / "missing.json")}), \
                    mock.patch("socket.socket.connect", network), \
                    mock.patch("socket.create_connection", network), \
                    mock.patch("urllib.request.urlopen", network):
                host.clear_config_cache()
                out = cli.cmd_config(None)
            self.assertIsNone(out["config"]["offload"])
            self.assertTrue(out["policy"]["autoReap"])
            self.assertEqual(out["config"]["scratchRoot"], None)

    def test_schedule_error_log_is_private_and_bounded(self):
        self.assertLessEqual(cli.SCHEDULE_LOG_BYTES, 1024 * 1024)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"TRIMREAPER_HOME": tmp, "TRIMREAPER_CONFIG": str(Path(tmp) / "missing.json")}):
                host.clear_config_cache()
                log = Path(tmp) / ".local" / "state" / "trimreaper" / "schedule-errors.log"
                log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                log.write_text("x" * (cli.SCHEDULE_LOG_BYTES + 10))
                log.chmod(0o644)
                cli.private_schedule_log()
            self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
            self.assertEqual(log.stat().st_size, 0)

    def test_receipts_age_out_only_after_a_settled_owner(self):
        self.assertLessEqual(cli.RECEIPT_RETENTION_DAYS, 30)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"TRIMREAPER_HOME": tmp, "TRIMREAPER_CONFIG": str(Path(tmp) / "missing.json")}):
                host.clear_config_cache()
                directory = Path(tmp) / ".local" / "state" / "trimreaper" / "reaper"
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                old = directory / "receipts-20200101.jsonl"
                fresh = directory / "receipts-20990101.jsonl"
                for path in (old, fresh):
                    path.write_text("{}\n")
                    path.chmod(0o600)
                stamp = time.time() - (cli.RECEIPT_RETENTION_DAYS + 1) * 86400
                os.utime(old, (stamp, stamp))
                with mock.patch.object(cli.core, "tool_reap_owner_status", return_value={"phase": "started"}):
                    self.assertEqual(cli.prune_old_receipts(), 0)
                self.assertTrue(old.exists())
                with mock.patch.object(cli.core, "tool_reap_owner_status", return_value={"phase": "finished"}):
                    self.assertEqual(cli.prune_old_receipts(), 1)
                self.assertFalse(old.exists())
                self.assertTrue(fresh.exists())


if __name__ == "__main__":
    unittest.main()
