"""Release checks, notices, opt-in auto-install, verified installs, rollback and debris bounds (no network)."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import TestCase, mock

from helpers import DAY, Home

from trimreaper import __version__, cli, update

NEW = "0.99.1"
BASE = "http://release.invalid/download/v" + NEW
SILENCERS = ("CI", "NO_COLOR", "TRIMREAPER_NO_UPDATE_CHECK", *update.AGENT_ENV)


class VersionRules(TestCase):
    def test_newer_equal_older_garbage_and_prerelease(self):
        self.assertEqual(update.parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(update.parse_version("1.2.3"), (1, 2, 3))
        for bad in ("1.2", "1.2.3-rc.1", "1.2.3.4", "nope", "", None, "01.2.3"):
            self.assertIsNone(update.parse_version(bad), bad)
        self.assertTrue(update.newer("0.26.0", "0.26.1"))
        self.assertTrue(update.newer("0.26.0", "v1.0.0"))
        self.assertTrue(update.newer("0.9.0", "0.10.0"), "numeric, not text order")
        for not_newer in ("0.26.0", "0.25.9", "0.26.1-rc.1", "junk", None):
            self.assertFalse(update.newer("0.26.0", not_newer), not_newer)
        self.assertFalse(update.newer("garbage", "9.9.9"))
        notes = update.release_notes("One.\n\n\x1b[31mTwo.\n" + "x" * 300 + "\n4\n5\n6\n7")
        self.assertEqual(notes.splitlines()[:2], ["One.", "[31mTwo."])
        self.assertEqual(len(notes.splitlines()), 5)
        self.assertTrue(all(len(line) <= 200 for line in notes.splitlines()))


class UpdateHome(Home):
    """A throwaway config, cache and release server for each test; nothing reaches the network."""

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"TRIMREAPER_UPDATE_API": "http://release.invalid/releases/latest"})
        env.start()
        self.addCleanup(env.stop)
        for name in SILENCERS:  # the developer's own shell (CI, an agent) must not decide these tests
            os.environ.pop(name, None)
        self.cache_file = self.home / "Library/Caches/trimreaper/update.json" if sys.platform == "darwin" \
            else self.home / ".cache/trimreaper/update.json"
        os.environ.pop("XDG_CACHE_HOME", None)
        self.wheel = b"PK fake wheel"
        self.script = b"#!/bin/sh\nexit 0\n"
        self.files = {}
        self.release = {"tag_name": "v" + NEW, "draft": False, "prerelease": False, "body": "Fixes.\nSecond line.",
                        "assets": [{"name": name, "browser_download_url": f"{BASE}/{name}"}
                                   for name in ("SHA256SUMS", "install.sh", f"trimreaper-{NEW}-py3-none-any.whl")]}
        self.requests = []

        def fake_fetch(url, timeout=update.CHECK_TIMEOUT, accept="*/*"):
            self.requests.append(url)
            if isinstance(self.release, Exception):
                raise self.release
            if url.endswith("/SHA256SUMS"):
                return self.files.get("SHA256SUMS", self.sums()).encode()
            if url.endswith("/install.sh"):
                return self.script
            if url.endswith(".whl"):
                return self.wheel
            return json.dumps(self.release).encode()

        self.real_fetch, self.real_spawn = update.fetch, update.spawn_check
        for patch in (mock.patch.object(update, "fetch", side_effect=fake_fetch), mock.patch.object(update, "spawn_check")):
            self.popen = patch.start()
            self.addCleanup(patch.stop)

    def sums(self):
        return (f"{hashlib.sha256(self.script).hexdigest()}  install.sh\n"
                f"{hashlib.sha256(self.wheel).hexdigest()}  trimreaper-{NEW}-py3-none-any.whl\n")

    def cache(self, status="update", version=NEW, age=0, **extra):
        update.write_cache({"checkedAt": time.time() - age, "status": status, "version": version, "cause": "", **extra})

    def raw(self, *args, tty=False, stdin=""):
        """Run the CLI in-process; `tty` makes stdin, stdout and stderr claim to be terminals."""
        out, err, fake_in = io.StringIO(), io.StringIO(), io.StringIO(stdin)
        for stream in (out, err, fake_in):
            stream.isatty = lambda: tty
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(sys, "stdin", fake_in):
            try:
                code = cli.main(list(args))
            except SystemExit as exit_code:
                code = exit_code.code
        return code, out.getvalue(), err.getvalue()

    def installer_kind(self, bin_dir=""):
        target = self.tmp / "installed"
        (target / "venv/bin").mkdir(parents=True, exist_ok=True)
        return mock.patch.object(update, "install_kind",
                                 return_value={"kind": "installer", "home": target, "binDir": bin_dir})


class ReleaseChecks(UpdateHome):
    def test_one_anonymous_get_with_a_short_timeout(self):
        seen = {}

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def urlopen(outgoing, timeout):
            seen.update(url=outgoing.full_url, headers=dict(outgoing.header_items()), data=outgoing.data, timeout=timeout)
            return Response(b"{}")

        with mock.patch.object(update.request, "urlopen", side_effect=urlopen):
            self.real_fetch("https://api.example.invalid/repos/o/r/releases/latest")
        self.assertEqual(seen["timeout"], update.CHECK_TIMEOUT)
        self.assertLessEqual(update.CHECK_TIMEOUT, 4)
        self.assertIsNone(seen["data"])
        self.assertNotIn("?", seen["url"])
        self.assertEqual({k.lower() for k in seen["headers"]}, {"user-agent", "accept"})
        self.assertEqual(seen["headers"]["User-agent"], "trimreaper-update-check")

    def test_the_endpoint_comes_from_the_package_metadata(self):
        os.environ.pop("TRIMREAPER_UPDATE_API")
        declared = next(line.split('"')[1] for line in (Path(__file__).parents[1] / "pyproject.toml").read_text().splitlines()
                        if line.startswith("Homepage"))
        missing = update.importlib.metadata.PackageNotFoundError
        with mock.patch.object(update.importlib.metadata, "metadata", side_effect=missing):
            url = update.api_url()
        self.assertEqual(url, declared.replace("https://github.com/", "https://api.github.com/repos/") + "/releases/latest")

    def test_stable_equal_prerelease_draft_and_garbage(self):
        for release in ({"tag_name": "v" + __version__}, {"tag_name": "v0.1.0"}, {"tag_name": "v9.0.0", "prerelease": True},
                        {"tag_name": "v9.0.0", "draft": True}, {"tag_name": "branch-head"}, {"tag_name": "v9.0.0-rc.1"}):
            self.release = release
            self.assertEqual(update.latest_release()["status"], "current", release)
        self.release = ["not", "an", "object"]
        self.assertEqual(update.latest_release()["status"], "failed")

    def test_failure_is_cached_with_its_cause_and_check_exit_codes(self):
        self.release = OSError("offline")
        code, out, _ = self.raw("update", "--check")
        self.assertEqual(code, 1)
        self.assertIn("offline", json.loads(out)["cause"])
        self.assertIn("offline", update.read_cache()["cause"])
        self.assertEqual(stat.S_IMODE(self.cache_file.stat().st_mode), 0o600)
        self.release = {"tag_name": "v" + NEW}
        self.assertEqual(self.raw("update", "--check")[0], 10)
        self.release = {"tag_name": "v" + __version__}
        self.assertEqual(self.raw("update", "--check")[0], 0)

    def test_daily_background_check_runs_at_most_once_and_only_interactively(self):
        self.raw("config", tty=True)
        self.popen.assert_called_once()
        self.raw("config", tty=True)
        self.assertEqual(self.popen.call_count, 1, "an attempt counts for 24 hours even if the check never reports")
        self.cache(age=DAY + 1)
        self.raw("config", tty=True)
        self.assertEqual(self.popen.call_count, 2)
        self.cache(age=DAY + 1)
        self.raw("config")
        for name, value in (("CI", "1"), ("CLAUDECODE", "1"), ("TRIMREAPER_AGENT", "1"), ("TRIMREAPER_NO_UPDATE_CHECK", "1")):
            with mock.patch.dict(os.environ, {name: value}):
                self.raw("config", tty=True)
        update.save_settings(checkAutomatically=False)
        self.raw("config", tty=True)
        self.assertEqual(self.popen.call_count, 2, "redirected, CI, agent and opted-out runs never check")
        self.assertEqual(self.requests, [], "the command itself never touches the network")


    def test_the_background_check_is_detached_and_runs_this_copy(self):
        with mock.patch.object(update.subprocess, "Popen") as popen:
            self.real_spawn()
        command, options = popen.call_args.args[0], popen.call_args.kwargs
        self.assertEqual(command, [sys.executable, "-m", "trimreaper", "update", "--check", "--background"])
        self.assertIs(options["start_new_session"], True)
        self.assertEqual(Path(options["cwd"]), Path(update.__file__).resolve().parents[1])
        self.assertEqual({options[k] for k in ("stdin", "stdout", "stderr")}, {subprocess.DEVNULL})


class NoticeRules(UpdateHome):
    def test_notice_follows_the_json_on_stderr_once_per_version_per_day(self):
        self.cache()
        code, out, err = self.raw("config", tty=True)
        self.assertEqual(code, 0)
        self.assertIn("configFile", json.loads(out), "stdout stays one JSON document")
        lines = err.splitlines()
        self.assertEqual(lines[0], f"trimreaper {NEW} available (you have {__version__}) · run: trimreaper update")
        self.assertEqual(lines[1], "\x1b[2mturn on automatic updates: trimreaper update --auto on · skip: trimreaper update --skip\x1b[0m")
        self.assertEqual(len(lines), 2)
        self.assertEqual(self.raw("config", tty=True)[2], "", "once per version per 24 hours")
        update.write_cache({**update.read_cache(), "shownAt": time.time() - DAY - 1})
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertTrue(self.raw("config", tty=True)[2].endswith("update --skip\n"), "a day later, plain with NO_COLOR")

    def test_silent_when_redirected_in_ci_agents_opted_out_or_already_current(self):
        self.cache()
        self.assertEqual(self.raw("config")[2], "", "redirected output")
        for name in ("CI", "CLAUDECODE", "TRIMREAPER_AGENT", "TRIMREAPER_NO_UPDATE_CHECK"):
            with mock.patch.dict(os.environ, {name: "1"}):
                self.assertEqual(self.raw("config", tty=True)[2], "", name)
        update.save_settings(checkAutomatically=False)
        self.assertEqual(self.raw("config", tty=True)[2], "", "config switch")
        update.save_settings(checkAutomatically=True)
        with mock.patch.object(update, "__version__", NEW):
            self.assertEqual(self.raw("config", tty=True)[2], "", "a stale record never offers the running version")
        self.assertIn("available", self.raw("config", tty=True)[2])

    def test_skip_hides_only_that_version(self):
        self.cache()
        out = self.cli("update", "--skip")
        self.assertEqual((out["status"], out["skippedVersion"]), ("skipped", NEW))
        self.assertEqual(self.raw("config", tty=True)[2], "")
        self.cache(version="0.99.2", skippedVersion=NEW)
        self.assertIn("0.99.2 available", self.raw("config", tty=True)[2], "a newer version notifies again")
        self.cache(status="current", version=__version__)
        self.release = {"tag_name": "v" + __version__}
        self.assertEqual(self.raw("update", "--skip")[0], 2, "nothing newer to skip is a named refusal")

    def test_the_check_and_notice_write_one_cache_file_and_nothing_else(self):
        before = {p for p in self.tmp.rglob("*")}
        self.raw("update", "--check")
        self.raw("config", tty=True)
        self.cli("update", "--skip")
        created = {p for p in self.tmp.rglob("*") if p.is_file()} - before
        self.assertEqual(created, {self.cache_file})
        self.assertLess(self.cache_file.stat().st_size, 1024)


class AutoInstall(UpdateHome):
    def test_auto_is_off_by_default_and_persists_in_the_config(self):
        self.assertIs(self.cli("update", "--auto", "status")["installAutomatically"], False)
        self.assertIs(self.cli("update", "--auto", "on")["installAutomatically"], True)
        self.assertIs(json.loads(self.config_file.read_text())["updates"]["installAutomatically"], True)
        self.assertIs(self.cli("update", "--auto", "status")["installAutomatically"], True)
        self.assertIs(self.cli("update", "--auto", "off")["installAutomatically"], False)
        self.assertIs(self.cli("update", "--auto", "status")["installAutomatically"], False)
        self.assertEqual(self.raw("update", "--auto", "maybe")[0], 2)

    def test_background_check_installs_only_when_opted_in_once_per_version(self):
        with mock.patch.object(update, "install") as install:
            self.raw("update", "--check", "--background")
            install.assert_not_called()
            update.save_settings(installAutomatically=True)
            install.side_effect = update.HostError("bad wheel")
            self.assertIn("bad wheel", self.raw("update", "--check", "--background")[1])
            self.raw("update", "--check", "--background")
        install.assert_called_once()
        self.assertEqual(update.read_cache()["autoTried"], NEW)
        self.assertIn("failed: bad wheel", update.read_cache()["autoResult"])
        self.assertIn("bad wheel", (self.home / ".local/state/trimreaper/update.log").read_text())
        self.assertIn("available", self.raw("config", tty=True)[2], "a failed auto-install falls back to the notice")

    def test_the_log_stays_one_bounded_file(self):
        log = self.home / ".local/state/trimreaper/update.log"
        for n in range(400):
            update.log(f"line {n} " + "x" * 1000)
        self.assertLessEqual(log.stat().st_size, update.LOG_LIMIT)
        self.assertTrue(log.read_text().endswith("x\n"))
        self.assertTrue(log.read_text().split(" ", 1)[0][:4].isdigit(), "cut at a line boundary")
        self.assertEqual(sorted(p.name for p in log.parent.iterdir()), ["update.log"])


class VerifiedInstall(UpdateHome):
    def temp_root(self):
        root = self.tmp / "tmp"
        root.mkdir()
        patch = mock.patch.object(tempfile, "tempdir", str(root))
        patch.start()
        self.addCleanup(patch.stop)
        return root

    def test_checksum_mismatch_refuses_before_anything_runs(self):
        root = self.temp_root()
        self.files["SHA256SUMS"] = self.sums().replace(hashlib.sha256(self.script).hexdigest(), "0" * 64)
        with self.installer_kind(), mock.patch.object(update.subprocess, "run", side_effect=AssertionError("ran")):
            code, _, err = self.raw("update")
        self.assertEqual(code, 2)
        self.assertIn("checksum mismatch for install.sh", err)
        self.assertEqual(list(root.iterdir()), [], "the download folder is removed on failure")

    def test_the_installer_gets_the_verified_script_and_its_own_folders(self):
        root = self.temp_root()
        record = self.tmp / "installer-env.txt"
        self.script = (f'#!/bin/sh\nprintf "%s|%s|%s|%s|%s\\n" "$TRIMREAPER_HOME" "$TRIMREAPER_RELEASE_BASE" '
                       f'"$TRIMREAPER_BIN_DIR" "${{TRIMREAPER_CONFIG:-none}}" "$(ls)" > {record}\n').encode()
        with self.installer_kind("/opt/bin"):
            code, out, err = self.raw("update")
        self.assertEqual((code, err), (0, ""), "no questions without a terminal")
        self.assertEqual(json.loads(out)["method"], "installer")
        self.assertEqual(record.read_text(), f"{self.tmp / 'installed'}|{BASE}|/opt/bin|none|install.sh\n",
                         "the runtime TRIMREAPER_HOME (the folder to clean) and config never reach the installer, "
                         "which runs in its download folder, never where a `python -m` could import another copy")
        self.assertEqual(list(root.iterdir()), [], "the download folder is removed on success")

    def test_package_managers_get_the_verified_wheel_never_a_package_name(self):
        for kind, head in (("pip", [sys.executable, "-m", "pip", "install"]), ("uv", ["uv", "tool", "install", "--force"]),
                           ("pipx", ["pipx", "install", "--force"])):
            seen = []

            def run(command, **kwargs):
                seen.append(command)
                self.assertEqual(Path(command[-1]).read_bytes(), self.wheel, "the verified file, present while it runs")
                return subprocess.CompletedProcess(command, 0, "done", "")

            path = {"pip": "/x/lib/python3.12/site-packages", "uv": "/x/uv/tools/trimreaper/lib/site-packages",
                    "pipx": "/x/pipx/venvs/trimreaper/lib/site-packages"}[kind]
            with mock.patch.object(update, "__file__", f"{path}/trimreaper/update.py"), \
                    mock.patch.object(update.subprocess, "run", side_effect=run):
                self.assertEqual(update.install_kind()["kind"], kind)
                result = update.install(update.latest_release())
            self.assertEqual((result["method"], seen[0][:len(head)]), (kind, head))
            self.assertTrue(seen[0][-1].endswith(f"trimreaper-{NEW}-py3-none-any.whl"))
            self.assertNotIn("trimreaper", seen[0], "never resolved by name from a package index")
            if kind == "pip":
                self.assertIn("--no-index", seen[0])
        self.wheel = b"tampered"
        self.files["SHA256SUMS"] = self.sums().replace(hashlib.sha256(b"tampered").hexdigest(), "1" * 64)
        with mock.patch.object(update, "__file__", "/x/lib/python3.12/site-packages/trimreaper/update.py"), \
                mock.patch.object(update.subprocess, "run", side_effect=AssertionError("ran")):
            with self.assertRaisesRegex(update.HostError, "checksum mismatch"):
                update.install(update.latest_release())
        with self.assertRaisesRegex(update.HostError, "source checkout"):
            update.install(update.latest_release())

    def test_terminal_shows_notes_asks_first_and_asks_about_auto_once(self):
        with mock.patch.object(update, "install", return_value={"method": "stub", "version": NEW, "output": ""}) as install:
            code, out, err = self.raw("update", tty=True, stdin="n\n")
            self.assertEqual((code, json.loads(out)["status"]), (0, "declined"))
            self.assertIn(f"trimreaper {__version__} → {NEW}\nFixes.\nSecond line.\nInstall {NEW} now? [Y/n] ", err)
            install.assert_not_called()
            code, out, err = self.raw("update", tty=True, stdin="\nn\n")
            self.assertEqual((code, json.loads(out)["status"]), (0, "installed"))
            self.assertIn("Install future updates automatically? [y/N] ", err)
            code, out, err = self.raw("update", tty=True, stdin="y\n")
            self.assertNotIn("automatically?", err, "a no is remembered and never asked again")
        self.assertEqual(install.call_count, 2)
        stored = json.loads(self.config_file.read_text())["updates"]
        self.assertEqual(stored, {"autoPrompted": True, "installAutomatically": False})


class RollbackAndDebris(UpdateHome):
    def setUp(self):
        super().setUp()
        self.target = self.tmp / "installed"
        for name in ("venv", "venv.previous"):
            (self.target / name / "bin").mkdir(parents=True)
            (self.target / name / "version").write_text(name)
        python = self.target / "venv.previous/bin/python"
        python.write_text("#!/bin/sh\necho trimreaper 0.1.0\n")
        python.chmod(0o755)
        patch = mock.patch.object(update, "installer_home", return_value=self.target)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(update, "newest_running", return_value=True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_rollback_restores_the_previous_venv_and_skips_the_bad_version(self):
        out = self.cli("update", "--rollback")
        self.assertEqual((out["status"], out["from"], out["to"]), ("rolled-back", __version__, "0.1.0"))
        self.assertEqual((self.target / "venv/version").read_text(), "venv.previous")
        self.assertEqual((self.target / "venv.previous/version").read_text(), "venv", "one rollback copy, the newer one")
        self.assertEqual(sorted(p.name for p in self.target.iterdir()), ["venv", "venv.previous"])
        self.assertEqual(update.read_cache()["skippedVersion"], __version__)
        (self.target / "venv.previous").rename(self.tmp / "gone")
        self.assertEqual(self.raw("update", "--rollback")[0], 2, "nothing kept is a named refusal")

    def test_the_rollback_copy_goes_after_the_first_successful_run_or_seven_days(self):
        self.raw("update", "--auto", "status")
        self.assertTrue((self.target / "venv.previous").is_dir(), "update commands keep it for --rollback")
        update.prune(False)
        self.assertTrue((self.target / "venv.previous").is_dir(), "a failed run keeps it")
        old = time.time() - update.PREVIOUS_DAYS * DAY - 60
        os.utime(self.target / "venv.previous", (old, old))
        update.prune(False)
        self.assertFalse((self.target / "venv.previous").exists(), "at most seven days")
        (self.target / "venv.previous").mkdir()
        self.assertEqual(self.raw("config")[0], 0)
        self.assertFalse((self.target / "venv.previous").exists(), "removed after the new version's first successful run")

    def test_interrupted_install_leftovers_go_after_an_hour_and_old_processes_never_prune(self):
        for name in ("venv.new.123", "venv.old.123", "venv.rollback.123"):
            (self.target / name).mkdir()
        fresh = self.target / "venv.new.456"
        fresh.mkdir()
        old = time.time() - 7200
        for name in ("venv.new.123", "venv.old.123", "venv.rollback.123"):
            os.utime(self.target / name, (old, old))
        with mock.patch.object(update, "newest_running", return_value=False):
            update.prune(True)
        self.assertTrue((self.target / "venv.previous").is_dir(), "an older process left running after a swap prunes nothing")
        update.prune(True)
        self.assertEqual(sorted(p.name for p in self.target.iterdir()), ["venv", "venv.new.456"])
