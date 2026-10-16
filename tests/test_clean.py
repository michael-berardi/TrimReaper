"""`trimreaper clean`: tool caches through their own cleaners, idle agent worktrees, the report, the threshold and the schedule.

Every tool here is a fake script in the test folder; nothing on this machine is cleaned."""
from __future__ import annotations

import contextlib
import json
import os
import plistlib
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

from helpers import DAY, Home as FixtureHome

from trimreaper import clean, cli, mcp


class Home(FixtureHome):
    def setUp(self):
        super().setUp()
        self.sessions = self.tmp / "claude-sessions"
        self.buildtmp = self.tmp / "buildtmp"
        self.sessions.mkdir()
        self.buildtmp.mkdir()
        patch = mock.patch.object(clean, "clean_leftover_roots", lambda: [(str(self.sessions), True), (str(self.buildtmp), False)])
        patch.start()
        self.addCleanup(patch.stop)


class Debris(Home):
    def setUp(self):
        super().setUp()
        # The probes are fixture snapshots, never real cleanup paths or machine processes.
        for patch in (mock.patch.object(clean, "clean_running", return_value=set()),
                      mock.patch.object(clean, "reap_process_table", return_value={"files": [], "commands": []})):
            patch.start()
            self.addCleanup(patch.stop)

    def run_target(self, target, dry=False):
        return self.cli("clean", *( ["--dry-run"] if dry else []), "--only", target)

    def rust(self):
        repo = self.home / "projects" / "recent-repo"
        (repo / ".git").mkdir(parents=True)
        self.put(repo, "target/debug/app")
        self.put(repo, "target/release/app")
        self.put(repo, "target/release/bundle/App.app/Contents/binary")
        self.put(repo, "target/release/incremental/state")
        self.put(repo, "target/aarch64-apple-darwin/incremental/state")
        self.put(repo, "target/debug/.cargo-lock", b"")
        return repo, repo / "target"

    def test_rust_removes_recent_debug_and_incremental_but_keeps_release_and_bundles(self):
        repo, target = self.rust()
        planned = self.run_target("rust-build-output", True)
        self.assertEqual(planned["summary"]["candidates"], 3, planned)
        self.assertTrue((target / "debug").exists())
        out = self.run_target("rust-build-output")
        self.assertEqual(out["summary"]["done"], 3, out)
        self.assertFalse((target / "debug").exists())
        self.assertFalse((target / "release/incremental").exists())
        self.assertFalse((target / "aarch64-apple-darwin/incremental").exists())
        self.assertTrue((target / "release/app").exists())
        self.assertTrue((target / "release/bundle/App.app/Contents/binary").exists())
        self.assertTrue(repo.exists())

    def test_rust_discovery_skips_tool_caches_and_virtualenvs_and_finishes(self):
        # A real machine's ~/dev and ~/.cache held 2.3 million entries, mostly tool caches and .venv trees, and the walk gave up
        # (discovery-incomplete) after finding one target. Those trees are never entered now; a target in a job clone is still found.
        for n in range(30):
            self.put(self.home / ".cache" / "uv" / "archive-v0" / f"pkg{n}", "file.py", b"x")
            self.put(self.home / "dev" / "app" / ".venv" / "lib" / f"mod{n}", "m.py", b"x")
        self.put(self.home / ".cache" / "uv" / "archive-v0" / "pkg0" / ".git", "HEAD", b"ref")
        self.put(self.home / ".cache" / "uv" / "archive-v0" / "pkg0" / "target" / "debug", "never", b"x")
        self.put(self.home / ".cache" / "agent-scratch" / "job" / "clone", ".git", b"gitdir: /unused/fixture\n")
        debug = self.put(self.home / ".cache" / "agent-scratch" / "job" / "clone", "target/debug/app").parent
        errors = []
        with mock.patch.object(clean, "CLEAN_RUST_SCAN_ENTRIES", 120):
            found = clean.clean_rust_targets(errors)
        self.assertEqual(errors, [])
        self.assertEqual([Path(target).parent.name for target, _ in found], ["clone"])
        self.assertTrue(debug.exists())

    def test_rust_in_a_recent_linked_worktree_is_independent_of_idle_policy(self):
        worktree = self.home / "projects" / "linked"
        self.put(worktree, ".git", b"gitdir: /unused/fixture\n")
        debug = self.put(worktree, "src-tauri/target/debug/app").parent
        self.write_config({"policy": {"worktreeIdleDays": 0}})
        out = self.run_target("rust-build-output")
        self.assertEqual(out["summary"]["done"], 1, out)
        self.assertFalse(debug.exists())
        self.assertTrue((worktree / ".git").exists())

    def test_linked_worktree_outside_home_is_discovered(self):
        worktree = self.tmp / "external-worktree"
        self.put(worktree, ".git", b"gitdir: /unused/fixture\n")
        debug = self.put(worktree, "target/debug/app").parent
        with mock.patch.object(clean, "clean_worktree_entries", return_value=[{"path": str(worktree), "kind": "worktree"}]):
            out = self.run_target("rust-build-output")
        self.assertEqual(out["summary"]["done"], 1, out)
        self.assertFalse(debug.exists())

    def test_every_rust_producer_keeps_output(self):
        _, target = self.rust()
        for name in clean.CLEAN_RUST_BUSY:
            with mock.patch.object(clean, "clean_running", return_value={name}):
                out = self.run_target("rust-build-output")
            self.assertEqual(out["summary"]["done"], 0, out)
            self.assertTrue(all(item["keep"] == "in-use:" + name for item in out["items"]))
        self.assertTrue((target / "debug").exists())

    def test_cargo_lock_is_nonblocking_and_held_through_removal(self):
        _, target = self.rust()
        lock = target / "debug/.cargo-lock"
        with clean.reap_locks([lock], create=False):
            out = self.run_target("rust-build-output")
        self.assertEqual(out["summary"]["done"], 0, out)
        self.assertTrue(all(item["keep"].startswith("lock-busy:") for item in out["items"]))
        remove = clean.reap_remove_and_verify
        checked = []
        def verify(*args, **kwargs):
            if lock.exists():
                with self.assertRaises(clean.ReapRefused):
                    with clean.reap_locks([lock], create=False):
                        pass
                checked.append(True)
            return remove(*args, **kwargs)
        with mock.patch.object(clean, "reap_remove_and_verify", side_effect=verify):
            out = self.run_target("rust-build-output")
        self.assertEqual(out["summary"]["done"], 3, out)
        self.assertTrue(checked)

    def test_rust_open_files_and_protected_leaves_keep_output(self):
        _, target = self.rust()
        table = {"files": [("123", "editor", "7r", str(target / "release/app"))], "commands": []}
        with mock.patch.object(clean, "reap_process_table", return_value=table):
            out = self.run_target("rust-build-output")
        self.assertEqual(out["summary"]["done"], 0, out)
        self.put(target, "debug/.env.local", b"secret")
        out = self.run_target("rust-build-output")
        self.assertEqual(out["summary"]["done"], 0, out)
        self.assertTrue((target / "debug/.env.local").exists())

    def test_stale_sessions_and_named_tmp_builds_only(self):
        old = self.put(self.sessions, "session-old/nested/log").parent.parent
        recent = self.put(self.sessions, "session-recent/nested/log").parent.parent
        paths = [self.put(self.buildtmp, name + "/data").parent for name in
                 ("app-verify-abc", "ultraterm-release.123", "ultraterm-publish-check.123", "app-rust-target-123")]
        unrelated = self.put(self.buildtmp, "unrelated/data").parent
        for path in [old, recent, unrelated, *paths]:
            self.age(path, 1)
        os.utime(recent / "nested/log", None)
        planned = self.run_target("agent-leftovers", True)
        self.assertEqual(planned["summary"]["candidates"], 5, planned)
        self.assertTrue(old.exists())
        out = self.run_target("agent-leftovers")
        self.assertEqual(out["summary"]["done"], 5, out)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(all(not path.exists() for path in paths))

    def test_session_open_file_cwd_and_command_line_are_kept(self):
        path = self.put(self.sessions, "session/data").parent
        self.age(path, 1)
        for table in ({"files": [("123", "agent", "cwd", str(path))], "commands": []},
                      {"files": [("123", "agent", "5r", str(path / "data"))], "commands": []},
                      {"files": [], "commands": [("123", "agent " + str(path))]}):
            with mock.patch.object(clean, "reap_process_table", return_value=table):
                out = self.run_target("agent-leftovers")
            self.assertTrue(out["items"][0]["keep"].startswith("in-use:"), out)
            self.assertTrue(path.exists())

    def test_sessions_recheck_freshness_before_deletion_and_keep_secrets(self):
        path = self.put(self.sessions, "session/data").parent
        self.age(path, 1)
        entry = clean.clean_extra_entries({"agent-leftovers"}, [])[0]
        os.utime(path / "data", None)
        with self.assertRaisesRegex(clean.ReapRefused, "recent"):
            clean.clean_act(entry, "fixture", 7)
        self.put(path, ".dev.vars", b"secret")
        self.age(path, 1)
        out = self.run_target("agent-leftovers")
        self.assertTrue(out["items"][0]["keep"].startswith("protected-leaf:"), out)
        self.assertTrue((path / ".dev.vars").exists())

    def browsers(self):
        root = self.home / "Library/Caches/ms-playwright"
        for name in ("chromium-99", "chromium-100", "firefox-8", "firefox-10", "chromium_headless_shell-12", "chromium_headless_shell-13", "unknown"):
            self.put(root, name + "/browser")
        return root

    def test_playwright_keeps_newest_numeric_build_of_each_browser(self):
        root = self.browsers()
        out = self.run_target("playwright")
        self.assertEqual(out["summary"]["done"], 3, out)
        for name in ("chromium-100", "firefox-10", "chromium_headless_shell-13", "unknown"):
            self.assertTrue((root / name).exists(), name)
        for name in ("chromium-99", "firefox-8", "chromium_headless_shell-12"):
            self.assertFalse((root / name).exists(), name)

    def test_running_old_browser_and_newest_after_replan_are_kept(self):
        root = self.browsers()
        table = {"files": [], "commands": [("321", str(root / "chromium-99/browser") + " --headless")]}
        with mock.patch.object(clean, "reap_process_table", return_value=table):
            out = self.run_target("playwright")
        item = next(item for item in out["items"] if item["path"].endswith("chromium-99"))
        self.assertTrue(item["keep"].startswith("in-use:"), out)
        entry = next(e for e in clean.clean_extra_entries({"playwright"}, []) if e["path"].endswith("chromium-99"))
        clean.shutil.rmtree(root / "chromium-100")
        with self.assertRaisesRegex(clean.ReapRefused, "newest-browser-build"):
            clean.clean_act(entry, "fixture", 7)

    def test_docker_build_gate_and_colima_trim_commands(self):
        for command in ("123 docker build .", "123 docker buildx build .", "123 /bin/docker-buildx build ."):
            with mock.patch.object(clean, "clean_docker_up", return_value=None), mock.patch.object(clean, "clean_run", return_value=(0, command)):
                self.assertEqual(clean.clean_docker_ready(), "in-use:docker-build")
        with mock.patch.dict(os.environ, {"DOCKER_HOST": "unix:///Users/test/.colima/default/docker.sock"}), \
                mock.patch.object(clean, "clean_run", return_value=(0, "")) as run:
            clean.clean_colima_trim()
            self.assertEqual(run.call_args.args[0], ["colima", "ssh", "--", "sudo", "fstrim", "-av"])

    def test_session_symlinks_and_protected_state_are_never_removed(self):
        outside = self.put(self.tmp, "outside/data").parent
        (self.sessions / "linked-session").symlink_to(outside, target_is_directory=True)
        for state in (".wrangler", ".vercel", ".env.production", ".dev.vars.local"):
            path = self.put(self.sessions, "session-" + state + "/" + state + "/state").parent.parent
            self.age(path, 1)
        out = self.run_target("agent-leftovers")
        self.assertEqual(out["summary"]["done"], 0, out)
        self.assertTrue((outside / "data").exists())
        self.assertTrue((self.sessions / "linked-session").is_symlink())
        self.assertEqual(len(out["items"]), 4, out)
        self.assertTrue(all(item["keep"].startswith("protected-leaf:") for item in out["items"]), out)

    def test_docker_prune_rechecks_builds_and_trims_only_after_success(self):
        spec = {"name": "docker-build-cache", "argv": ["docker", "builder", "prune", "-af"], "busy": ()}
        entry = {"name": spec["name"], "kind": "tool-cache", "spec": spec}
        calls = []
        def prune(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="pruned", stderr="")
        with mock.patch.object(clean, "clean_docker_ready", return_value=None) as ready, \
                mock.patch.object(clean.subprocess, "run", side_effect=prune), \
                mock.patch.object(clean, "clean_colima_trim", side_effect=lambda: (calls.append("trim") or (0, ""))), \
                mock.patch.object(clean, "clean_receipt"):
            clean.clean_run_tool(entry, "fixture")
        self.assertEqual(calls, [["docker", "builder", "prune", "-af"], "trim"])
        self.assertEqual(entry["colimaTrim"], "trimmed")
        ready.assert_called_once()
        entry = {"name": spec["name"], "kind": "tool-cache", "spec": spec}
        with mock.patch.object(clean, "clean_docker_ready", return_value="in-use:docker-build"), \
                mock.patch.object(clean.subprocess, "run") as run:
            clean.clean_run_tool(entry, "fixture")
        run.assert_not_called()
        self.assertEqual(entry["keep"], "in-use:docker-build")

    def test_auto_reports_actual_free_space_delta_even_when_zero(self):
        self.cli("status")  # initialize the fixture database
        for delta in (0, 123456):
            with contextlib.closing(self.state()) as conn:
                conn.execute("DELETE FROM meta WHERE key=?", (clean.CLEAN_AUTO_KEY,))
                conn.commit()
                actions, errors = [], []
                disks = [{"usedPercent": 98, "freeBytes": 100}, {"usedPercent": 98, "freeBytes": 100 + delta}]
                with mock.patch.object(clean, "disk_state", side_effect=disks), \
                        mock.patch.object(clean, "clean_pass", return_value={"summary": {"done": 1, "freedBytes": 999}, "errors": []}):
                    clean.clean_auto(conn, {"cleanAtPercent": 95}, actions, errors)
            self.assertEqual(actions[0]["freedBytes"], delta)
            self.assertEqual(errors, [])


class Worktrees(Home):
    def worktree(self, repo: Path, name: str, *args: str, age: float | None = 10) -> Path:
        path = repo.parent / name
        self.git(repo, "worktree", "add", *(args or ("-b", name)), str(path))
        if age is not None:
            self.backdate(repo, path, age)
        return path

    def backdate(self, repo: Path, path: Path, days: float):
        self.age(path, days)
        self.age(repo / ".git" / "worktrees" / path.name, days)

    def items(self, out):
        return {Path(item["path"]).name: item for item in out["items"] if item["name"] == "worktrees"}

    def test_an_idle_clean_worktree_goes_and_its_branch_stays(self):
        repo = self.clone("app")
        path = self.worktree(repo, "app-feature", age=None)
        self.put(path, "notes.md")
        self.git(path, "add", "notes.md")
        self.git(path, "commit", "-q", "-m", "work on a branch no remote has")
        self.put(path, "node_modules/pkg/index.js")
        self.backdate(repo, path, 10)

        planned = self.cli("clean", "--dry-run", "--only", "worktrees")
        self.assertNotIn("keep", self.items(planned)["app-feature"], planned)
        self.assertTrue(path.exists(), "a dry run removes nothing")

        out = self.cli("clean", "--only", "worktrees")

        self.assertEqual(self.items(out)["app-feature"]["outcome"], "removed", out)
        self.assertFalse(path.exists())
        self.assertEqual(self.git(repo, "branch", "--list", "app-feature").strip(), "app-feature", "the branch stays")
        self.assertIn("work on a branch no remote has", self.git(repo, "log", "-1", "--format=%s", "app-feature"))
        self.assertNotIn("app-feature", self.git(repo, "worktree", "list"), "the repository forgot the worktree")
        receipts = list((self.home / ".local/state/trimreaper/reaper").glob("receipts-*.jsonl"))
        phases = [json.loads(line)["phase"] for line in receipts[0].read_text().splitlines()]
        self.assertEqual(phases, ["plan", "result"])

    def test_a_worktree_with_relative_pointers_goes_too(self):
        repo = self.clone("app")
        path = self.worktree(repo, "app-relative", "--relative-paths", "-b", "relative")
        self.assertFalse((path / ".git").read_text().split()[1].startswith("/"), "git wrote a relative pointer")

        self.assertEqual(self.items(self.cli("clean", "--only", "worktrees"))["app-relative"]["outcome"], "removed")
        self.assertFalse(path.exists())

    def test_every_reason_to_keep_a_worktree_is_named(self):
        repo = self.clone("app")
        self.worktree(repo, "app-recent", age=None)
        self.put(self.worktree(repo, "app-dirty"), "untracked.txt")
        self.backdate(repo, repo.parent / "app-dirty", 10)
        locked = self.worktree(repo, "app-locked")
        self.git(repo, "worktree", "lock", str(locked))
        detached = self.worktree(repo, "app-detached", "--detach", age=None)
        self.put(detached, "only-here.txt")
        self.git(detached, "add", "-A")
        self.git(detached, "commit", "-q", "-m", "a commit no branch holds")
        self.backdate(repo, detached, 10)
        secret = self.worktree(repo, "app-secret", age=None)
        self.put(secret, ".env.local", b"KEY=1\n")
        self.backdate(repo, secret, 10)
        self.worktree(repo, "app-j0042")
        evidence = self.worktree(repo, "app-evidence", age=None)
        self.cli("mark", str(evidence), "evidence")
        self.backdate(repo, evidence, 10)

        kept = {name: item.get("keep", "") for name, item in self.items(self.cli("clean", "--only", "worktrees")).items()}

        self.assertTrue(kept["app-recent"].startswith("recent:"), kept)
        self.assertEqual(kept["app-dirty"], "dirty")
        self.assertEqual(kept["app-locked"], "worktree-locked")
        self.assertEqual(kept["app-detached"], "unsaved-commits:only-this-worktree-holds-them")
        self.assertTrue(kept["app-secret"].startswith("protected-leaf:"), kept)
        self.assertEqual(kept["app-j0042"], "job-folder:reaped-with-its-job")
        self.assertEqual(kept["app-evidence"], "retention:evidence")
        for name in kept:
            self.assertTrue((repo.parent / name).exists(), name)

    def test_a_process_working_in_a_worktree_keeps_it(self):
        repo = self.clone("app")
        path = self.worktree(repo, "app-busy")
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=str(path))
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        time.sleep(0.5)

        item = self.items(self.cli("clean", "--only", "worktrees"))["app-busy"]

        self.assertTrue(item["keep"].startswith("in-use:"), item)
        self.assertTrue(path.exists())

    def test_worktrees_in_an_agent_home_are_found_through_their_repository(self):
        elsewhere = self.home / "projects"
        self.dev = elsewhere  # a repository outside the clone bases
        repo = self.clone("tool")
        codex = self.home / ".codex" / "worktrees" / "ab12"
        codex.mkdir(parents=True)
        path = codex / "tool"
        self.git(repo, "worktree", "add", "--detach", str(path))
        self.backdate(repo, path, 10)
        self.write_config({"worktreeHomes": ["~/.codex/worktrees"]})

        out = self.cli("clean", "--only", "worktrees")

        self.assertEqual(self.items(out)["tool"]["outcome"], "removed", out)
        self.assertFalse(path.exists())

    def test_a_vanished_worktree_is_forgotten_only_when_its_commits_are_saved(self):
        repo = self.clone("app")
        saved = self.worktree(repo, "app-gone", age=None)
        unsaved = self.worktree(repo, "app-gone-detached", "--detach", age=None)
        self.put(unsaved, "x.txt")
        self.git(unsaved, "add", "-A")
        self.git(unsaved, "commit", "-q", "-m", "only the worktree's HEAD holds this")
        fresh = self.worktree(repo, "app-gone-recently", age=None)
        for path in (saved, unsaved, fresh):
            subprocess.run(["rm", "-rf", str(path)], check=True)
        for name in ("app-gone", "app-gone-detached"):
            self.age(repo / ".git" / "worktrees" / name, 31)

        items = self.items(self.cli("clean", "--only", "worktrees"))

        self.assertEqual(items["app-gone"]["outcome"], "worktree-entry-forgotten", items)
        self.assertFalse((repo / ".git" / "worktrees" / "app-gone").exists())
        self.assertEqual(items["app-gone-detached"]["keep"], "unsaved-commits:only-this-worktree-holds-them")
        self.assertTrue(items["app-gone-recently"]["keep"].startswith("folder-missing:entry-0d"), items)

    def test_a_full_repository_at_an_old_worktree_path_is_never_a_worktree(self):
        repo = self.clone("app")
        path = self.worktree(repo, "app-feature", age=None)
        subprocess.run(["rm", "-rf", str(path)], check=True)
        self.git(repo.parent, "clone", "-q", str(self.remotes / "app.git"), str(path))
        self.git(path, "checkout", "-q", "-b", "local-only")
        self.put(path, "work.txt")
        self.git(path, "add", "-A")
        self.git(path, "commit", "-q", "-m", "a branch only this clone holds")
        self.backdate(repo, path, 10)

        item = self.items(self.cli("clean", "--only", "worktrees"))["app-feature"]

        self.assertTrue(item["keep"].startswith("not-a-linked-worktree"), item)
        self.assertEqual(self.git(path, "log", "-1", "--format=%s"), "a branch only this clone holds\n")

    def test_an_edit_hidden_by_skip_worktree_keeps_it(self):
        repo = self.clone("app")
        path = self.worktree(repo, "app-hidden", age=None)
        self.git(path, "update-index", "--skip-worktree", "README.md")
        (path / "README.md").write_text("local edit status does not show\n")
        self.backdate(repo, path, 10)

        item = self.items(self.cli("clean", "--only", "worktrees"))["app-hidden"]

        self.assertEqual(item["keep"], "git-unsafe-index:index-flag")
        self.assertTrue(path.exists())

    def test_a_folder_an_unfinished_job_declares_is_kept(self):
        repo = self.clone("app")
        path = self.worktree(repo, "app-claimed")
        self.cli("job", "add", "J-0300", "--worktree", str(path), "--status", "parked")

        item = self.items(self.cli("clean", "--only", "worktrees"))["app-claimed"]

        self.assertEqual(item["keep"], "declared-by-unfinished-job:J-0300")

    def test_a_lock_taken_after_the_plan_keeps_it(self):
        repo = self.clone("app")
        path = self.worktree(repo, "app-late-lock")
        entries = clean.clean_plan({"worktrees"}, False, [], [], 7)
        entry = next(e for e in entries if e["path"] == str(path))
        self.assertNotIn("keep", entry)
        self.git(repo, "worktree", "lock", str(path))
        with self.assertRaises(clean.ReapRefused) as refused:
            clean.clean_worktree_gates(entry, 7, None)
        self.assertEqual(str(refused.exception), "worktree-locked")

    def test_off_when_worktree_idle_days_is_zero(self):
        self.write_config({"policy": {"worktreeIdleDays": 0}})
        repo = self.clone("app")
        path = self.worktree(repo, "app-feature")
        self.assertEqual(self.items(self.cli("clean", "--only", "worktrees"))["app-feature"]["keep"], "off:worktreeIdleDays=0")
        self.assertTrue(path.exists())


class Caches(Home):
    def setUp(self):
        super().setUp()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.cache = self.home / ".fakecache"
        self.put(self.cache, "blob")
        self.log = self.tmp / "tool.log"
        tool = self.bin / "faketool"
        tool.write_text('#!/bin/sh\nif [ "$1" = sleep ]; then sleep 60; exit 0; fi\n'
                        f'echo "$@" >> "{self.log}"\nrm -rf "{self.cache}"/*\n')
        tool.chmod(0o755)
        failing = self.bin / "failtool"
        failing.write_text("#!/bin/sh\necho 'cache locked by another process' >&2\nexit 2\n")
        failing.chmod(0o755)
        tools = ({"name": "npm", "argv": [str(tool), "cache", "clean"], "busy": ("faketool",), "where": lambda: [str(self.cache)]},
                 {"name": "uv", "argv": [str(failing)], "busy": ()},
                 {"name": "go-modules", "deep": True, "argv": [str(tool), "modcache"], "busy": ()},
                 {"name": "pnpm", "argv": [str(self.bin / "absent")], "busy": ()})
        cargo = self.home / "cargo-registry"
        self.registry = self.put(cargo, "cache/index-a/crate-1.0.crate").parent
        self.secret_src = self.put(cargo, "src/index-b/crate-2.0/.env", b"K=1\n").parent.parent
        # busy is not "cargo": a real build running on this machine would keep the fake cache too
        folders = ({"name": "cargo-registry", "busy": ("faketool-cargo",), "roots": lambda: [str(cargo / "cache"), str(cargo / "src")]},)
        for patch in (mock.patch.object(clean, "CLEAN_TOOLS", tools), mock.patch.object(clean, "CLEAN_FOLDERS", folders)):
            patch.start()
            self.addCleanup(patch.stop)

    def by_name(self, out):
        return {(item["name"], Path(item.get("path") or item["name"]).name): item for item in out["items"]}

    def test_tools_clean_their_own_caches_and_failures_are_named(self):
        out = self.cli("clean", "--only", "npm,uv,pnpm,go-modules", expect=3)
        items = self.by_name(out)

        self.assertEqual(items[("npm", "npm")]["outcome"], "cleaned")
        self.assertEqual(self.log.read_text().split(), ["cache", "clean"])
        self.assertEqual(list(self.cache.iterdir()), [])
        self.assertTrue(items[("uv", "uv")]["outcome"].startswith("failed:exit-2:cache locked"), items)
        self.assertEqual(out["errors"][0]["clean"], "uv")
        self.assertEqual(items[("pnpm", "pnpm")]["keep"], "tool-missing")
        self.assertEqual(items[("go-modules", "go-modules")]["keep"], "deep-only:add---deep")

    def test_a_running_tool_keeps_its_cache(self):
        runner = subprocess.Popen([str(self.bin / "faketool"), "sleep"])
        self.addCleanup(runner.wait)
        self.addCleanup(runner.kill)
        time.sleep(0.3)

        item = self.by_name(self.cli("clean", "--only", "npm"))[("npm", "npm")]

        self.assertEqual(item["keep"], "in-use:faketool")
        self.assertFalse(self.log.exists(), "the cleaner never ran")

    def test_folder_caches_go_through_the_guarded_removal(self):
        planned = self.by_name(self.cli("clean", "--dry-run", "--only", "cargo-registry"))
        self.assertTrue(planned[("cargo-registry", "index-b")].get("keep", "").startswith("protected-leaf:"), planned)
        out = self.cli("clean", "--only", "cargo-registry")
        items = self.by_name(out)

        self.assertEqual(items[("cargo-registry", "index-a")]["outcome"], "removed", items)
        self.assertFalse(self.registry.exists())
        self.assertTrue(items[("cargo-registry", "index-b")]["keep"].startswith("protected-leaf:"), items)
        self.assertEqual((self.secret_src / "crate-2.0" / ".env").read_bytes(), b"K=1\n")

    def test_unknown_targets_are_named(self):
        err = self.cli("clean", "--only", "npm,nope", expect=2)
        self.assertIn("nope", err["error"])
        self.assertIn("no clean targets", self.cli("clean", "--only", "", expect=2)["error"])

    def test_docker_is_cleaned_only_on_a_local_socket(self):
        with mock.patch.dict(os.environ, {"DOCKER_HOST": "tcp://build-server:2375"}):
            self.assertEqual(clean.clean_docker_up(), "docker-endpoint-not-local:tcp")
        with mock.patch.dict(os.environ, {"DOCKER_HOST": "ssh://user@elsewhere"}):
            self.assertEqual(clean.clean_docker_up(), "docker-endpoint-not-local:ssh")

    def test_a_script_run_by_its_interpreter_counts_as_running(self):
        script = self.bin / "brew.sh"
        script.write_text("sleep 60\n")
        runner = subprocess.Popen(["/bin/bash", str(script), "install", "x"])
        self.addCleanup(runner.wait)
        self.addCleanup(runner.kill)
        time.sleep(0.3)
        self.assertIn("brew", clean.clean_running())


class Report(Home):
    def test_models_are_reported_and_never_removed(self):
        hub = self.home / "hf"
        model = self.put(hub, "models--acme--tiny/blobs/abc", b"w" * 8192).parent.parent
        with mock.patch.dict(os.environ, {"HF_HUB_CACHE": str(hub)}):
            out = self.cli("clean")
        found = [r for r in out["report"] if r["kind"] == "model"]
        self.assertEqual([r["name"] for r in found], ["acme/tiny"])
        self.assertGreaterEqual(found[0]["bytes"], 8192)
        self.assertTrue(model.exists())


class Threshold(Home):
    GB = 1024 ** 3

    def tick(self, used: int, free_gb: int = 50, at: float | None = None):
        patches = [mock.patch.object(clean, "disk_state", lambda: {"path": str(self.tmp), "usedPercent": used, "freeBytes": free_gb * self.GB})]
        if at is not None:
            patches.append(mock.patch.object(clean, "now", lambda: at))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            return self.cli("tick")

    def cleaned(self, out):
        return [a.get("reason") for a in out["actions"] if a.get("did") == "cleaned-at-disk-pressure"]

    def test_automatic_clean_is_event_driven_not_a_timer(self):
        repo = self.clone("app")
        path = repo.parent / "app-old"
        self.git(repo, "worktree", "add", "-b", "old", str(path))
        self.age(path, 10)
        self.age(repo / ".git" / "worktrees" / "app-old", 10)
        t = time.time()
        self.assertEqual(self.cleaned(self.tick(94, at=t)), [], "below the default 95%")
        self.assertTrue(path.exists())
        self.assertEqual(self.cleaned(self.tick(95, at=t + 1)), ["crossed"])
        self.assertFalse(path.exists())
        self.assertEqual(self.cleaned(self.tick(99, at=t + 7200)), [], "no new pressure two hours later: no repeat")
        self.assertEqual(self.cleaned(self.tick(99, free_gb=44, at=t + 7300)), ["growth"], "6 GB more used")
        self.assertEqual(self.cleaned(self.tick(90, free_gb=60, at=t + 7400)), [], "below again: re-armed")
        self.assertEqual(self.cleaned(self.tick(96, free_gb=40, at=t + 7500)), ["crossed"], "the next crossing runs again")
        self.assertEqual(self.cleaned(self.tick(96, free_gb=8, at=t + 7600)), [], "stampede guard: minutes apart")
        self.assertEqual(self.cleaned(self.tick(96, free_gb=8, at=t + 8300)), ["floor"])
        self.write_config({"policy": {"cleanAtPercent": 0}})
        self.assertEqual(self.cleaned(self.tick(100, free_gb=1, at=t + 9999)), [], "0 turns it off")

    def test_policy_values_are_checked(self):
        for policy in ({"cleanAtPercent": 101}, {"cleanAtPercent": "95"}, {"worktreeIdleDays": -1}):
            self.write_config({"policy": policy})
            err = self.cli("config", expect=2)
            self.assertIn(next(iter(policy)), err["error"])
        self.write_config({"worktreeHomes": "~/.codex/worktrees"})
        self.assertIn("worktreeHomes", self.cli("config", expect=2)["error"])


class Schedule(Home):
    def test_install_status_and_remove(self):
        calls = []
        def launchctl(*args):
            calls.append(args)
            return (0, "") if args[0] != "print" else (0 if self.plist.exists() else 113, "")
        self.plist = self.home / "Library/LaunchAgents" / f"{cli.SCHEDULE_LABEL}.plist"
        with mock.patch.object(cli, "launchctl", launchctl), mock.patch.object(sys, "platform", "darwin"):
            out = self.cli("schedule", "install", "--minutes", "15")
            spec = plistlib.loads(self.plist.read_bytes())
            self.assertEqual(spec["StartInterval"], 900)
            self.assertEqual(spec["ProgramArguments"][-3:], ["trimreaper", "tick", "--record"])
            self.assertTrue(out["installed"] and out["loaded"])
            self.assertIn(("bootstrap", f"gui/{os.getuid()}", str(self.plist)), calls)
            out = self.cli("schedule", "remove")
            self.assertFalse(self.plist.exists())
            self.assertFalse(out["installed"])

    def test_tick_record_writes_the_last_result(self):
        self.cli("tick", "--record")
        last = json.loads((self.home / ".local/state/trimreaper/last-tick.json").read_text())
        self.assertEqual(last["errors"], [])


class Server(Home):
    def call(self, **arguments):
        out = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "clean", "arguments": arguments}})
        return out["result"]

    def test_clean_is_a_dry_run_unless_asked(self):
        repo = self.clone("app")
        path = repo.parent / "app-old"
        self.git(repo, "worktree", "add", "-b", "old", str(path))
        self.age(path, 10)
        self.age(repo / ".git" / "worktrees" / "app-old", 10)

        planned = self.call(only=["worktrees"])
        self.assertFalse(planned["isError"], planned)
        self.assertTrue(planned["structuredContent"]["dryRun"])
        self.assertTrue(path.exists())
        done = self.call(only=["worktrees"], dryRun=False, report=False)
        self.assertEqual(done["structuredContent"]["summary"]["done"], 1, done)
        self.assertFalse(path.exists())
        bad = self.call(only=["everything"])
        self.assertTrue(bad["isError"])
        self.assertIn("only entries must be among", bad["structuredContent"]["error"])
