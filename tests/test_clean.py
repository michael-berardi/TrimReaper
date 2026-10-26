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
        self.sharedtmp = self.tmp / "sharedtmp"
        self.clones = self.tmp / "X" / "com.example.Browser.code_sign_clone"
        self.sharedtmp.mkdir()
        self.clones.mkdir(parents=True)
        # Never the machine's own temp folders: every temp root is a fixture folder.
        for patch in (mock.patch.object(clean, "clean_leftover_roots", lambda: [(str(self.sessions), True), (str(self.buildtmp), False)]),
                      mock.patch.object(clean, "clean_temp_roots", lambda: [(str(self.sharedtmp), 24), (str(self.buildtmp), 72)]),
                      mock.patch.object(clean, "clean_clone_roots", lambda: [str(self.clones)])):
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
        repo = self.clone("recent-repo")
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
        main = self.clone("linked-main")
        worktree = self.home / "projects" / "linked"
        worktree.parent.mkdir(parents=True)
        self.git(main, "worktree", "add", "--detach", str(worktree))
        debug = self.put(worktree, "src-tauri/target/debug/app").parent
        self.write_config({"policy": {"worktreeIdleDays": 0}})
        out = self.run_target("rust-build-output")
        self.assertEqual(out["summary"]["done"], 1, out)
        self.assertFalse(debug.exists())
        self.assertTrue((worktree / ".git").exists())

    def test_linked_worktree_outside_home_is_discovered(self):
        main = self.clone("external-main")
        worktree = self.tmp / "external-worktree"
        self.git(main, "worktree", "add", "--detach", str(worktree))
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

    def test_idle_temp_folders_go_after_a_day_shared_and_three_days_per_user(self):
        shared_old = self.put(self.sharedtmp, "tool-scratch/data").parent
        shared_new = self.put(self.sharedtmp, "fresh-scratch/data").parent
        user_mid = self.put(self.buildtmp, "two-days/data").parent
        user_old = self.put(self.buildtmp, "four-days/data").parent
        system = self.put(self.buildtmp, "com.apple.something/data").parent
        for path, days in ((shared_old, 2), (user_mid, 2), (user_old, 4), (system, 30)):
            self.age(path, days)
        out = self.run_target("agent-leftovers")
        self.assertFalse(shared_old.exists())
        self.assertFalse(user_old.exists())
        self.assertTrue(shared_new.exists())
        self.assertTrue(user_mid.exists())
        self.assertTrue(system.exists())
        self.assertEqual(out["summary"]["done"], 2, out)

    def test_abandoned_browser_code_sign_clones_go_and_an_open_one_stays(self):
        old = self.put(self.clones, "code_sign_clone.AAAA/Browser.app/Contents/binary").parents[2]
        open_ = self.put(self.clones, "code_sign_clone.BBBB/Browser.app/Contents/binary").parents[2]
        fresh = self.put(self.clones, "code_sign_clone.CCCC/Browser.app/Contents/binary").parents[2]
        self.age(old, 1)
        self.age(open_, 1)
        table = {"files": [("123", "Browser", "txt", str(open_ / "Browser.app/Contents/binary"))], "commands": []}
        with mock.patch.object(clean, "reap_process_table", return_value=table):
            out = self.run_target("browser-clones")
        self.assertFalse(old.exists())
        self.assertTrue(open_.exists())
        self.assertTrue(fresh.exists())
        self.assertEqual(out["summary"]["done"], 1, out)

    def project(self, name="site", ignored=("node_modules/", ".next/")):
        repo = self.clone(name, ignored=ignored, tracked={"package.json": "{}"})
        self.put(repo, "node_modules/pkg/index.js")
        self.put(repo, ".next/cache/x")
        if ".next/" not in ignored:
            self.git(repo, "add", ".next")
            self.git(repo, "commit", "-q", "-m", "tracked output fixture")
            self.git(repo, "push", "-q")
        return repo

    def test_idle_project_dependencies_go_and_tracked_or_recent_ones_stay(self):
        idle = self.project("idle-site")
        tracked = self.project("tracked-site", ignored=("node_modules/",))
        recent = self.project("recent-site")
        bare = self.home / "dev" / "no-manifest"
        subprocess.run(["git", "init", "-q", str(bare)], check=True)
        (bare / ".gitignore").write_text("node_modules/\n")
        self.put(bare, "node_modules/pkg/index.js")
        for path in (idle, tracked, bare):
            self.age(path, 3)
        out = self.run_target("project-dependencies")
        self.assertFalse((idle / "node_modules").exists())
        self.assertFalse((idle / ".next").exists())
        self.assertTrue((idle / "package.json").exists())
        self.assertFalse((tracked / "node_modules").exists())
        self.assertTrue((tracked / ".next").exists())  # git does not ignore it: it may be committed work
        self.assertTrue((recent / "node_modules").exists())  # touched within projectIdleHours
        self.assertTrue((bare / "node_modules").exists())  # no package.json beside it: nothing could reinstall it
        kept = {i["path"].split("/dev/")[-1]: i.get("keep") for i in out["items"]}
        self.assertTrue(kept["tracked-site/.next"].startswith("not-git-ignored"), kept)
        self.assertTrue(kept["recent-site/node_modules"].startswith("recent:"), kept)

    def test_a_running_job_keeps_its_projects_dependencies(self):
        repo = self.project("job-site-j4242")
        self.age(repo, 3)
        with mock.patch.object(clean, "rows", side_effect=lambda conn, sql, args=(): [{"id": "J-4242", "status": "running", "cwd": str(repo)}]
                               if "NOT IN" in sql else [{"status": "running"}]):
            out = self.run_target("project-dependencies")
        self.assertTrue((repo / "node_modules").exists())
        self.assertTrue(all(i["keep"].startswith("in-use-by-running-job:J-4242") for i in out["items"]), out)

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
                disks = [{"usedBytes": 4900, "usedPercent": 98, "freeBytes": 100}, {"usedBytes": 4900, "usedPercent": 98, "freeBytes": 100 + delta}]
                with mock.patch.object(clean, "disk_state", return_value=disks[0]), \
                        mock.patch.object(clean, "clean_pass", return_value={"summary": {"done": 1, "freedBytes": 999}, "errors": [], "diskAfter": disks[1], "goalMet": False, "remainingBytes": 1}):
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
        gitdir = repo / ".git" / "worktrees" / path.name
        pointer = path / ".git"
        if pointer.is_file():  # the worktree's own gitdir (git suffixes the name when it is taken: tool, tool1 ...)
            gitdir = Path(pointer.read_text().split("gitdir:", 1)[1].strip())
            gitdir = gitdir if gitdir.is_absolute() else (path / gitdir).resolve()
        self.age(gitdir, days)

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
        patches = [mock.patch.object(clean, "disk_state", lambda: {"path": str(self.tmp), "usedPercent": used,
                   "usedBytes": 1 + int(free_gb * self.GB * min(used, 99.999) / (100 - min(used, 99.999))), "freeBytes": free_gb * self.GB})]
        if at is not None:
            patches.append(mock.patch.object(clean, "now", lambda: at))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            return self.cli("tick")

    def cleaned(self, out):
        return [a.get("reason") for a in out["actions"] if a.get("did") == "cleaned-at-disk-pressure"]

    def test_automatic_clean_starts_at_75_by_default(self):
        repo = self.clone("app")
        path = repo.parent / "app-old"
        self.git(repo, "worktree", "add", "-b", "old", str(path))
        self.age(path, 10)
        self.age(repo / ".git" / "worktrees" / "app-old", 10)
        t = time.time()
        self.assertEqual(self.cleaned(self.tick(74, at=t)), [], "below the default 75%")
        self.assertTrue(path.exists())
        self.assertEqual(self.cleaned(self.tick(75, at=t + 1)), ["crossed"])
        self.assertFalse(path.exists())

    def test_automatic_clean_retries_pressure_until_low_with_cooldown(self):
        self.write_config({"policy": {"cleanAtPercent": 85, "cleanToPercent": 80}})
        repo = self.clone("app")
        path = repo.parent / "app-old"
        self.git(repo, "worktree", "add", "-b", "old", str(path))
        self.age(path, 10)
        self.age(repo / ".git" / "worktrees" / "app-old", 10)
        t = time.time()
        self.assertEqual(self.cleaned(self.tick(84, at=t)), [], "below the default 85%")
        self.assertTrue(path.exists())
        self.assertEqual(self.cleaned(self.tick(85, at=t + 1)), ["crossed"])
        self.assertFalse(path.exists())
        self.assertEqual(self.cleaned(self.tick(99, at=t + 7200)), ["pressure"], "sustained high use retries without 5GB growth")
        self.assertEqual(self.cleaned(self.tick(99, free_gb=44, at=t + 7300)), [], "cooldown prevents a stampede")
        self.assertEqual(self.cleaned(self.tick(79, free_gb=60, at=t + 7400)), [], "below low watermark: re-armed")
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


def core_job_ids(*names):
    from trimreaper import core
    return core.reap_job_ids(*names)


def lifetime(home, *jobs):
    """Give finished fixture jobs a real lifetime (created and started before they finished), as the host records them."""
    with contextlib.closing(home.state()) as conn, conn:
        for job in jobs:
            conn.execute("UPDATE jobs SET created_at=finished_at-60, started_at=finished_at-60 WHERE id=? AND finished_at IS NOT NULL", (job,))


class PileupRegressions(Worktrees):
    """The causes of the 2026-10-24 ~/dev pileup: each test failed on 0.7.3."""

    def test_git_status_rewriting_the_index_does_not_keep_an_abandoned_worktree(self):
        repo = self.clone("app")
        path = self.worktree(repo, "app-abandoned")  # backdated 10 days
        self.git(path, "status", "--porcelain")      # what every audit, IDE and agent does: rewrites <gitdir>/index
        os.utime(repo / ".git" / "worktrees" / "app-abandoned" / "index", None)

        item = self.items(self.cli("clean", "--dry-run", "--only", "worktrees"))["app-abandoned"]
        self.assertNotIn("keep", item, item)

    def test_a_recently_edited_file_deep_in_the_tree_keeps_the_worktree(self):
        repo = self.clone("app")
        path = self.worktree(repo, "app-active")
        self.put(path, "src/deep/feature.md")
        self.git(path, "add", "-A")
        self.git(path, "commit", "-q", "-m", "work")
        self.backdate(repo, path, 10)
        os.utime(path / "src" / "deep" / "feature.md", None)  # edited just now, committed earlier

        item = self.items(self.cli("clean", "--dry-run", "--only", "worktrees"))["app-active"]
        self.assertTrue(item.get("keep", "").startswith("recent:"), item)

    def test_a_job_numbered_worktree_outside_any_clone_base_is_not_left_to_the_job_reaper(self):
        repo = self.clone("app")
        nested = repo / ".worktrees"
        nested.mkdir()
        (repo / ".gitignore").write_text((repo / ".gitignore").read_text() + ".worktrees/\n")
        self.git(repo, "add", ".gitignore")
        self.git(repo, "commit", "-q", "-m", "ignore nested worktrees")
        self.git(repo, "push", "-q")
        path = nested / "J-1552"
        self.git(repo, "worktree", "add", "-b", "job/J-1552", str(path))
        self.age(path, 10)
        self.age(repo / ".git" / "worktrees" / "J-1552", 10)

        item = {Path(i["path"]).name: i for i in self.cli("clean", "--dry-run", "--only", "worktrees")["items"]
                if i["name"] == "worktrees"}["J-1552"]
        self.assertNotEqual(item.get("keep", ""), "job-folder:reaped-with-its-job", item)
        self.assertFalse(item.get("keep", "").startswith("recent:"), item)
        self.assertEqual(core_job_ids("J-1552"), ["J-1552"])  # the dashed name is a job name, so the ownership rule is what decides

    def test_a_top_level_job_clone_is_still_left_to_the_job_reaper(self):
        repo = self.clone("app")
        self.worktree(repo, "app-j0042")
        kept = self.items(self.cli("clean", "--dry-run", "--only", "worktrees"))["app-j0042"]
        self.assertEqual(kept.get("keep"), "job-folder:reaped-with-its-job")

    def scratch_worktree(self, repo: Path, name: str, quiet_days: float) -> Path:
        nested = self.home / "scratch"
        nested.mkdir(exist_ok=True)
        path = nested / name
        self.git(repo, "worktree", "add", "-b", "job/" + name, str(path))  # outside the clone base
        self.backdate(repo, path, quiet_days)
        return path

    def scratch_item(self, name: str) -> dict:
        return {Path(i["path"]).name: i for i in self.cli("clean", "--dry-run", "--only", "worktrees")["items"]
                if i["name"] == "worktrees"}[name]

    def test_a_worktree_named_for_a_long_finished_job_waits_hours_not_days(self):
        repo = self.clone("app")
        self.scratch_worktree(repo, "app-j0077", quiet_days=0.5)   # 12 hours quiet: past the 6h job window, short of 7 days
        self.add_job("J-0077", status="done", finished_ago=5 * DAY)
        lifetime(self, "J-0077")
        self.assertNotIn("keep", self.scratch_item("app-j0077"))

    def test_finished_job_work_still_needs_a_short_quiet_period(self):
        repo = self.clone("app")
        self.scratch_worktree(repo, "app-j0079", quiet_days=0.05)  # touched about an hour ago
        self.add_job("J-0079", status="done", finished_ago=5 * DAY)
        lifetime(self, "J-0079")  # eligible for the short window, so the 6-hour rule is what keeps it
        self.assertTrue(self.scratch_item("app-j0079").get("keep", "").startswith("recent:0.1d-of-0.25d"))

    def test_an_ancestor_job_name_never_shortens_the_wait(self):
        repo = self.clone("app")
        nested = self.home / "scratch" / "j0080-review"
        nested.mkdir(parents=True)
        path = nested / "app-copy"
        self.git(repo, "worktree", "add", "-b", "copy", str(path))
        self.backdate(repo, path, 0.5)
        self.add_job("J-0080", status="done", finished_ago=5 * DAY)
        lifetime(self, "J-0080")  # the parent's job would qualify: only the name rule keeps the full window
        self.assertTrue(self.scratch_item("app-copy").get("keep", "").startswith("recent:0.5d-of-7d"))

    def test_a_finished_job_with_a_live_successor_keeps_the_full_wait(self):
        repo = self.clone("app")
        self.scratch_worktree(repo, "app-j0081", quiet_days=0.5)
        self.add_job("J-0081", status="done", finished_ago=5 * DAY)
        lifetime(self, "J-0081")
        self.add_job("J-0082", status="running")
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("INSERT INTO meta(key,value) VALUES(?,?)", ("cleanup:successor:J-0081",
                         json.dumps({"job": "J-0081", "successor": "J-0082"})))
        self.assertTrue(self.scratch_item("app-j0081").get("keep", "").startswith("recent:"))

    def test_a_finished_job_someone_awaits_keeps_the_full_wait(self):
        repo = self.clone("app")
        self.scratch_worktree(repo, "app-j0083", quiet_days=0.5)
        self.add_job("J-0083", status="done", finished_ago=5 * DAY)
        lifetime(self, "J-0083")
        self.cli("job", "add", "J-0084", "--status", "parked", "--waits-on", "J-0083")
        self.assertTrue(self.scratch_item("app-j0083").get("keep", "").startswith("recent:"))

    def test_a_worktree_named_for_a_live_job_still_waits(self):
        repo = self.clone("app")
        nested = self.home / "scratch"
        nested.mkdir()
        path = nested / "app-j0078"
        self.git(repo, "worktree", "add", "-b", "job/j0078", str(path))
        self.add_job("J-0078", status="running")

        item = {Path(i["path"]).name: i for i in self.cli("clean", "--dry-run", "--only", "worktrees")["items"]
                if i["name"] == "worktrees"}["app-j0078"]
        self.assertTrue(item.get("keep", ""), item)


class ReleaseKeepLapse(Home):
    def release(self, source: str, by: str, keep: list):
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("INSERT INTO meta(key,value) VALUES(?,?)", ("cleanup:released:" + source,
                         json.dumps({"by": by, "keep": keep, "at": time.time() - 20 * DAY})))

    def lapsed(self, source: str) -> bool:
        from trimreaper import core, host
        with host.db() as conn:  # the engine's own store and helpers, nothing mocked
            value = host.row(conn, "SELECT value FROM meta WHERE key=?", ("cleanup:released:" + source,))["value"]
            return core.reap_release_keep_lapsed(conn, source, value)

    def test_keep_lapses_once_every_job_involved_ended_long_ago(self):
        self.cli("status")
        for job in ("J-0262", "J-0349", "J-0327"):
            self.add_job(job, status="cancelled", finished_ago=15 * DAY)
        lifetime(self, "J-0262", "J-0349", "J-0327")
        self.release("J-0262", "J-0349", [str(self.dev / "dubhaven-j0327-media")])
        self.assertTrue(self.lapsed("J-0262"))

    def test_keep_holds_while_a_kept_path_names_a_live_job(self):
        self.cli("status")
        for job in ("J-0262", "J-0349"):
            self.add_job(job, status="cancelled", finished_ago=15 * DAY)
        self.add_job("J-0327", status="running")
        self.release("J-0262", "J-0349", [str(self.dev / "dubhaven-j0327-media")])
        self.assertFalse(self.lapsed("J-0262"))

    def test_keep_whose_owner_is_not_a_job_never_lapses(self):
        self.cli("status")
        self.add_job("J-0262", status="cancelled", finished_ago=15 * DAY)
        self.release("J-0262", "operator", [str(self.dev / "dubhaven-other")])
        self.assertFalse(self.lapsed("J-0262"))

    def test_keep_holds_while_a_successor_of_the_source_is_live(self):
        self.cli("status")
        for job in ("J-0262", "J-0349"):
            self.add_job(job, status="cancelled", finished_ago=15 * DAY)
        lifetime(self, "J-0262", "J-0349")
        self.add_job("J-0400", status="running")
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("INSERT INTO meta(key,value) VALUES(?,?)", ("cleanup:successor:J-0262",
                         json.dumps({"job": "J-0262", "successor": "J-0400"})))
        self.release("J-0262", "J-0349", [str(self.dev / "x")])
        self.assertFalse(self.lapsed("J-0262"))

    def test_keep_holds_inside_the_expiry_window_and_for_unknown_jobs(self):
        self.cli("status")
        self.add_job("J-0262", status="done", finished_ago=1 * DAY)
        self.add_job("J-0349", status="done", finished_ago=15 * DAY)
        self.release("J-0262", "J-0349", [])
        self.assertFalse(self.lapsed("J-0262"))
        self.add_job("J-0500", status="done", finished_ago=15 * DAY)
        self.release("J-0500", "J-0349", [str(self.dev / "x-j0999")])
        self.assertFalse(self.lapsed("J-0500"))


class IdleWalk(Worktrees):
    def test_an_over_budget_walk_names_its_reason_and_keeps_the_worktree(self):
        repo = self.clone("app")
        path = self.worktree(repo, "app-huge", age=None)
        for i in range(30):
            self.put(path, f"f{i}.txt", b"x")
        self.backdate(repo, path, 10)
        with mock.patch.object(clean, "CLEAN_IDLE_WALK_ENTRIES", 5):
            clean.CLEAN_IDLE_CACHE.clear()
            item = self.items(self.cli("clean", "--dry-run", "--only", "worktrees"))["app-huge"]
        clean.CLEAN_IDLE_CACHE.clear()
        self.assertEqual(item.get("keep"), "idle-budget-exceeded:5-entries")

    def test_a_copy_that_preserved_mtimes_still_counts_as_a_change(self):
        repo = self.clone("app")
        path = self.worktree(repo, "app-copied")  # backdated 10 days: mtimes and reflog are old
        clean.CLEAN_IDLE_CACHE.clear()
        os.chmod(path / "README.md", 0o640)      # a metadata change (ctime now), like `cp -p` onto the tree
        item = self.items(self.cli("clean", "--dry-run", "--only", "worktrees"))["app-copied"]
        clean.CLEAN_IDLE_CACHE.clear()
        self.assertTrue(item.get("keep", "").startswith("recent:"), item)

    def test_the_removal_recheck_never_reuses_a_cached_idle_answer(self):
        repo = self.clone("app")
        path = self.worktree(repo, "app-raced")
        entry = {"path": str(path), "gitdir": str(repo / ".git" / "worktrees" / "app-raced")}
        clean.CLEAN_IDLE_CACHE.clear()
        self.assertGreater(clean.clean_idle_days(entry["path"], entry["gitdir"]), 9)  # the plan: idle
        (path / "README.md").write_text("edited after planning\n")                 # someone works in it now
        self.assertGreater(clean.clean_idle_days(entry["path"], entry["gitdir"]), 9)  # a planning re-read may reuse the walk
        self.assertLess(clean.clean_idle_days(entry["path"], entry["gitdir"], fresh=True), 0.01)

    def test_only_a_whole_wellformed_reflog_line_counts(self):
        import tempfile
        good = "0" * 40 + " " + "1" * 40 + " A B <a@b> 1700000000 -0300\n"
        tricky = "0" * 40 + " " + "1" * 40 + " A <a@b> 1700000000 -0300\tcommit: see <x> 1799999999 +0000\n"
        cases = {good: 1700000000.0, good.rstrip("\n"): None, tricky: 1700000000.0,
                 "0" * 40 + " " + "1" * 40 + " A <a@b> nan -0300\n": None,
                 "0" * 40 + " " + "1" * 40 + " A <a@b> 99999999999 +0000\n": None,
                 "garbage\n": None, "": None}
        for text, expected in cases.items():
            with self.subTest(text=text[:20]), tempfile.NamedTemporaryFile("w", delete=False) as handle:
                handle.write(text)
            try:
                self.assertEqual(clean.clean_reflog_last(handle.name), expected)
            finally:
                os.unlink(handle.name)


class JobNamePattern(Home):
    def test_dashed_job_ids_are_recognised_and_lookalikes_are_not(self):
        from trimreaper import core
        self.assertEqual(core.reap_job_ids("J-1552", "australiatoargentina-J-2807", "app-j0042", ".j0262"),
                         ["J-1552", "J-2807", "J-0042", "J-0262"])
        # The dashed form is uppercase only: a lowercase j- prefix is too common in ordinary names.
        self.assertEqual(core.reap_job_ids("job-2024", "proj-12345", "dj-1234", "v-j12", "app-j-1234", "release-j-2024"), [])


class ReflogMaintenance(Worktrees):
    def test_gc_rewriting_every_reflog_does_not_make_old_worktrees_recent(self):
        repo = self.clone("app")
        path = self.worktree(repo, "app-old")  # committed and backdated 10 days
        old = time.time() - 10 * DAY
        log = repo / ".git" / "worktrees" / "app-old" / "logs" / "HEAD"
        lines = log.read_text().splitlines()
        rewritten = []
        for line in lines:  # entries written 10 days ago
            head, _, message = line.partition("\t")
            parts = head.split(" ")
            parts[-2] = str(int(old))
            rewritten.append(" ".join(parts) + "\t" + message)
        log.write_text("\n".join(rewritten) + "\n")  # the rewrite itself is what gc does: the file's mtime is now
        item = self.items(self.cli("clean", "--dry-run", "--only", "worktrees"))["app-old"]
        self.assertNotIn("keep", item, item)
