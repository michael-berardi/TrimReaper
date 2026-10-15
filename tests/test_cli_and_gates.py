"""The command line, the configuration checks, the owner handoff, and the gates the README promises."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

from helpers import DAY, Home

from trimreaper import cli, core


class JobCommands(Home):
    def test_finish_reopen_and_duplicate(self):
        added = self.cli("job", "add", "J-0100", "--cwd", "~/dev/app-j0100", "--title", "t")
        self.assertEqual(added["cwd"], os.path.realpath(self.home / "dev" / "app-j0100"))
        self.assertIsNone(added["finished_at"])
        done = self.cli("job", "status", "J-0100", "done")
        self.assertIsNotNone(done["finished_at"])
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("INSERT INTO meta(key,value) VALUES('reaper:J-0100','{}')")
        reopened = self.cli("job", "status", "J-0100", "running")
        self.assertIsNone(reopened["finished_at"])
        with contextlib.closing(self.state()) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM meta WHERE key='reaper:J-0100'").fetchone())
        self.assertIn("already exists", self.cli("job", "add", "J-0100", expect=2)["error"])
        self.assertEqual([j["id"] for j in self.cli("job", "list")["jobs"]], ["J-0100"])

    def test_job_ids_must_map_to_folder_names(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                cli.main(["job", "add", "J-00042"])
        self.assertEqual(raised.exception.code, 2)

    def test_status_of_an_unknown_job_is_named(self):
        self.assertIn("no job J-0101", self.cli("status", "--job", "J-0101", expect=2)["error"])

    def test_expire_evidence_needs_a_job(self):
        self.assertIn("--job", self.cli("reap", "--scratch", "--expire-evidence", expect=2)["error"])


class ConfigCommands(Home):
    def test_wrong_policy_types_are_named(self):
        self.write_config({"policy": {"autoReap": "false"}})
        self.assertIn("autoReap must be true or false", self.cli("tick", expect=2)["error"])
        self.write_config({"policy": {"cloneExpiryDays": "3"}})
        self.assertIn("cloneExpiryDays", self.cli("config", expect=2)["error"])
        self.write_config({"policy": {"cloneProtectedCopies": "remove", "cloneArchive": "keep"}})
        self.assertIn("needs cloneArchive", self.cli("config", expect=2)["error"])
        self.write_config({"cacheRoot": "~/../elsewhere"})
        self.assertIn("must not contain '..'", self.cli("config", expect=2)["error"])
        self.write_config({"cloneBases": [{"path": "~/dev", "prefx": ".j"}]})
        self.assertIn("prefx", self.cli("config", expect=2)["error"])

    def test_policy_set_writes_only_valid_values(self):
        out = self.cli("policy-set", "evidenceExpiryDays=7", "autoReap=false")
        self.assertEqual(out["policy"]["evidenceExpiryDays"], 7)
        self.assertIs(out["policy"]["autoReap"], False)
        self.assertIn("diskWarnPercent", self.cli("policy-set", "diskWarnPercent=500", expect=2)["error"])
        self.assertEqual(json.loads(self.config_file.read_text())["policy"], {"autoReap": False, "evidenceExpiryDays": 7})

    def test_config_core_digest_and_version(self):
        out = self.cli("config")
        self.assertIsNone(out["scratchRoot"], "an unset root is reported as off")
        self.assertEqual(out["ownerJournal"], str(self.home / ".local/state/trimreaper/cleanup"))
        text = Path(core.__file__).read_text()
        body = re.search(r"# >>> trimreaper core >>>\n(.*)# <<< trimreaper core <<<\n", text, re.S).group(1)
        self.assertEqual(self.cli("core-digest")["sha256"], hashlib.sha256(body.encode()).hexdigest())
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed), self.assertRaises(SystemExit):
            cli.main(["--version"])
        from trimreaper import __version__
        self.assertEqual(printed.getvalue().strip(), f"trimreaper {__version__}")


class OwnerHandoff(Home):
    def journal(self) -> Path:
        return self.home / ".local" / "state" / "trimreaper" / "cleanup" / "disk-cleanup.lock"

    def test_a_stopped_run_blocks_until_acknowledged(self):
        self.assertEqual(self.cli("owner", "status")["phase"], "none")
        repo = self.clone("app-j0110")
        self.put(repo, "node_modules/pkg/index.js")
        self.add_job("J-0110", repo)
        self.cli("reap", "--job", "J-0110", "--dry-run")  # a dry run takes no lease
        folder = self.journal().parent
        folder.mkdir(parents=True, mode=0o700)
        os.chmod(folder, 0o700)
        with open(self.journal(), "w") as handle:  # what a run killed mid-sweep leaves: a started note and no end
            handle.write(json.dumps({"run": "0123456789ab", "phase": "started", "outcome": "in-progress"}) + "\n")
        os.chmod(self.journal(), 0o600)

        status = self.cli("owner", "status")
        self.assertEqual((status["phase"], status["expectedRun"]), ("started", "0123456789ab"))
        self.assertIn("cleanup-handoff-incomplete", self.cli("reap", "--job", "J-0110", expect=2)["error"])
        tick = self.cli_raw("tick")
        self.assertEqual(tick[0], 3, "a tick that could not take the owner reports it")
        self.assertTrue((repo / "node_modules").exists())

        report = self.tmp / "handoff.md"
        report.write_text("Cleanup owner run: 0123456789ab\nReceipts reviewed: yes\nUnresolved paths preserved: yes\n"
                          "Deleted paths: none\nRetained paths: all\nReceipt references: none written\n"
                          "Active consumers: none\nNext safe step: reap again\n")
        os.chmod(report, 0o644)
        self.assertIn("0600", self.cli("owner", "acknowledge", "--run", "0123456789ab", "--evidence", str(report), expect=2)["error"])
        os.chmod(report, 0o600)
        self.assertTrue(self.cli("owner", "acknowledge", "--run", "0123456789ab", "--evidence", str(report))["acknowledged"])
        self.assertEqual(self.cli("reap", "--job", "J-0110")["deleted"], 1)

    def cli_raw(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(list(args))
        return code, out.getvalue()


class NeverRemoved(Home):
    def test_every_protected_name_keeps_an_expired_clone(self):
        for number, name in enumerate((".dev.vars", ".wrangler/state/db.sqlite", ".vercel/project.json", ".env"), 120):
            with self.subTest(name=name):
                repo = self.clone(f"app-j0{number}", ignored=(".dev.vars*", ".wrangler/", ".vercel/", ".env*"))
                self.put(repo, name, b"local state\n")
                self.add_job(f"J-0{number}", repo, finished_ago=4 * DAY)
                out = self.cli("reap", "--job", f"J-0{number}")
                self.assertEqual(out["deleted"], 0, out)
                self.assertTrue(any(i["reason"].startswith(("protected-leaf", "git-unsafe-index:protected")) for i in out["items"]), out)
                self.assertEqual((repo / name).read_bytes(), b"local state\n")

    def test_unfinished_statuses_and_waiting_jobs_keep_everything(self):
        for number, status in enumerate(("queued", "approved", "blocked", "parked", "interrupted"), 130):
            with self.subTest(status=status):
                repo = self.clone(f"app-j0{number}")
                self.put(repo, "node_modules/pkg/index.js")
                self.add_job(f"J-0{number}", repo, status=status)
                self.assertIn(f"job-{status}", self.cli("reap", "--job", f"J-0{number}", expect=2)["error"])
                self.assertTrue((repo / "node_modules").exists())
        repo = self.clone("app-j0140")
        self.put(repo, "node_modules/pkg/index.js")
        self.add_job("J-0140", repo)
        self.cli("job", "add", "J-0141", "--waits-on", "J-0140")
        self.assertIn("awaited-by:J-0141", self.cli("reap", "--job", "J-0140", expect=2)["error"])
        self.assertTrue((repo / "node_modules").exists())

    def test_an_open_file_or_a_command_line_keeps_the_folder(self):
        repo = self.clone("app-j0150")
        held = self.put(repo, "node_modules/pkg/index.js")
        self.add_job("J-0150", repo)
        holder = subprocess.Popen([sys.executable, "-c", "import os,time; f=open(os.environ['HELD']); time.sleep(60)"], cwd="/",
                                  env={**os.environ, "HELD": str(held)})  # the path is in no command line: only the open file names it
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        time.sleep(0.5)
        out = self.cli("reap", "--job", "J-0150")
        self.assertTrue(out["items"][0]["reason"].startswith("in-use:"), out["items"][0])
        self.assertNotIn("command-line", out["items"][0]["reason"])
        holder.kill()
        holder.wait()
        naming = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", str(held.parent.parent)], cwd="/")
        time.sleep(0.5)
        self.addCleanup(naming.wait)
        self.addCleanup(naming.kill)
        out = self.cli("reap", "--job", "J-0150")
        self.assertIn("command-line", out["items"][0]["reason"], out["items"][0])
        self.assertTrue(held.exists())

    def test_a_marker_on_a_whole_clone_keeps_it(self):
        repo = self.clone("app-j0160", ignored=(".ut-retention-*",))
        self.cli("mark", str(repo), "customer")
        self.add_job("J-0160", repo, finished_ago=4 * DAY)
        out = self.cli("reap", "--job", "J-0160")
        self.assertEqual(out["items"], [], "a clone marked customer is not offered as an item")
        self.assertTrue((repo / "README.md").exists())


class ProtectedCopiesAndArchives(Home):
    config_extra = {"policy": {"cloneProtectedCopies": "remove"}}

    def test_secret_copies_are_archived_owner_only_then_the_clone_goes(self):
        repo = self.clone("app-j0170")
        self.put(repo, ".env.local", b"SECRET=1\n")
        self.add_job("J-0170", repo, finished_ago=4 * DAY)

        out = self.cli("reap", "--job", "J-0170")

        self.assertFalse(repo.exists(), out)
        archive = next((self.home / ".cache/trimreaper/archive/J-0170").iterdir())
        sealed = archive / "protected.tar.gz"
        self.assertEqual(stat.S_IMODE(sealed.stat().st_mode), 0o600)
        import tarfile
        with tarfile.open(sealed) as tar:
            self.assertEqual(tar.extractfile(".env.local").read(), b"SECRET=1\n")

    def test_an_archive_goes_once_it_was_the_only_copy_long_enough(self):
        repo = self.clone("app-j0171")
        (repo / "notes.txt").write_text("unsaved\n")
        self.add_job("J-0171", repo, finished_ago=4 * DAY)
        self.cli("reap", "--job", "J-0171")
        archive = next((self.home / ".cache/trimreaper/archive/J-0171").iterdir())
        self.assertEqual(self.cli("reap", "--archives")["deleted"], 0, "a fresh archive stays")
        record = archive / "sole-copy-since.json"
        data = json.loads(record.read_text())
        data["soleCopySince"] = time.time() - 31 * DAY
        record.write_text(json.dumps(data))

        self.assertEqual(self.cli("reap", "--archives")["deleted"], 1)
        self.assertFalse(archive.exists())


class MoreGates(Home):
    def test_a_tracked_env_template_goes_with_its_expired_clone(self):
        repo = self.clone("app-j0180", ignored=("node_modules/",), tracked={"README.md": "hi\n", ".env.example": "KEY=\n"})
        self.assertIn(".env.example", self.git(repo, "ls-files"))
        self.add_job("J-0180", repo, finished_ago=4 * DAY)
        out = self.cli("reap", "--job", "J-0180")
        self.assertFalse(repo.exists(), out)

    def test_disk_pressure_takes_build_output_of_parked_jobs_only(self):
        self.write_config({"policy": {"diskWarnPercent": 1}})  # every disk is "full"
        parked = self.clone("app-j0190")
        parked_output = self.put(parked, "node_modules/pkg/index.js").parent.parent
        self.put(parked, "dist/out.js")
        self.add_job("J-0190", parked, status="parked")
        running = self.clone("app-j0191")
        running_output = self.put(running, "node_modules/pkg/index.js").parent.parent
        self.add_job("J-0191", running, status="running")
        due = self.clone("app-j0192")
        self.put(due, "node_modules/pkg/index.js")
        self.add_job("J-0192", due)  # the pressure pass rides on a tick with a finished job due

        out = self.cli("tick")

        self.assertIn("reaped-under-pressure", [a["did"] for a in out["actions"]], out)
        self.assertFalse(parked_output.exists())
        self.assertTrue((parked / "dist" / "out.js").exists(), "dist is never taken under pressure")
        self.assertTrue(running_output.exists())

    def test_disk_volume_and_release_paths_are_checked_at_load(self):
        self.write_config({"diskVolume": "/no/such/volume"})
        self.assertIn("diskVolume", self.cli("config", expect=2)["error"])
        self.write_config({"release": {"installedApp": "Applications/X.app"}})
        self.assertIn("must be absolute", self.cli("config", expect=2)["error"])
        self.write_config({"home": "relative/home"})
        self.assertIn("home", self.cli("config", expect=2)["error"])

    def test_usage_errors_are_json(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            cli.main(["reap"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("usage:", json.loads(err.getvalue())["error"])


class OwnerCompact(Home):
    def test_compact_needs_the_current_token_of_a_settled_journal(self):
        self.assertTrue(self.cli("owner", "status")["phase"] == "none")
        repo = self.clone("app-j0195")
        self.put(repo, "node_modules/pkg/index.js")
        self.add_job("J-0195", repo)
        self.cli("reap", "--job", "J-0195")  # a real run writes the journal
        status = self.cli("owner", "status")
        self.assertEqual(status["phase"], "finished")
        self.assertIn("stale", self.cli("owner", "compact", "--run", "000000000000", expect=2)["error"])
        out = self.cli("owner", "compact", "--run", status["expectedRun"])
        self.assertTrue(out["compacted"])
        self.assertTrue(Path(out["archive"]).exists())
