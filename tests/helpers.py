"""Fixtures: a throwaway home, real git repositories with a bare remote, and the CLI run in-process."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from trimreaper import clean, cli

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
DAY = 86400


class Home(unittest.TestCase):
    """Everything happens below a temporary directory: the reap home, its clones, the remotes and the state."""

    config_extra: dict = {}

    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="trimreaper-test-")))
        self.home = self.tmp / "home"
        self.dev = self.home / "dev"
        self.remotes = self.tmp / "remotes"
        for path in (self.dev, self.remotes):
            path.mkdir(parents=True)
        self.config_file = self.tmp / "config.json"
        self.write_config({})
        env = mock.patch.dict(os.environ, {"TRIMREAPER_HOME": str(self.home), "TRIMREAPER_CONFIG": str(self.config_file),
                                           "HOME": str(self.home), **GIT_ENV})
        env.start()
        self.addCleanup(env.stop)
        # The clean pass never reaches this machine's tools, caches or disk level from a test: none by default, fakes per test.
        for patch in (mock.patch.object(clean, "CLEAN_TOOLS", ()), mock.patch.object(clean, "CLEAN_FOLDERS", ()),
                      mock.patch.object(clean, "disk_state", lambda: {"path": str(self.tmp), "usedPercent": 50, "freeBytes": 10 ** 12})):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.remove_tmp)

    def remove_tmp(self):
        subprocess.run(["chmod", "-R", "u+rwx", str(self.tmp)], capture_output=True)
        subprocess.run(["rm", "-rf", str(self.tmp)], capture_output=True)

    def write_config(self, extra: dict):
        config = {"cloneBases": [{"path": "~/dev"}], "worktreeHomes": [], **self.config_extra, **extra}
        self.config_file.write_text(json.dumps(config))

    # ---- the CLI ---------------------------------------------------------------------------------------------------

    def cli(self, *args: str, expect: int = 0):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(args))
        if code != expect:
            self.fail(f"trimreaper {' '.join(args)} exited {code}, expected {expect}\nstdout: {out.getvalue()}\nstderr: {err.getvalue()}")
        text = out.getvalue() if code in (0, 3) else err.getvalue()  # 3: the pass ran; its JSON (naming the errors) is on stdout
        return json.loads(text) if text.strip() else None

    def state(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.home / ".local" / "state" / "trimreaper" / "state.sqlite3"))

    def add_job(self, job: str, cwd: Path | None = None, status: str = "done", finished_ago: float = 3600):
        args = ["job", "add", job, "--status", "running"]
        if cwd is not None:
            args += ["--cwd", str(cwd)]
        self.cli(*args)
        if status != "running":
            self.cli("job", "status", job, status)
        if status in ("done", "failed", "cancelled"):
            with contextlib.closing(self.state()) as conn, conn:
                conn.execute("UPDATE jobs SET finished_at=? WHERE id=?", (time.time() - finished_ago, job))

    # ---- git -------------------------------------------------------------------------------------------------------

    def git(self, repo: Path, *args: str) -> str:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env={**os.environ, **GIT_ENV})
        if done.returncode:
            self.fail(f"git {' '.join(args)} failed: {done.stderr}")
        return done.stdout

    def clone(self, name: str, ignored=("node_modules/", "target/", "dist/", ".env*"), tracked=None) -> Path:
        """A clone with one pushed commit; `ignored` goes in its .gitignore."""
        remote = self.remotes / f"{name}.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
        repo = self.dev / name
        repo.mkdir(parents=True)
        self.git(repo, "init", "-q", "-b", "main")
        self.git(repo, "remote", "add", "origin", str(remote))
        (repo / ".gitignore").write_text("".join(f"{line}\n" for line in ignored))
        for rel, text in (tracked or {"README.md": "hello\n"}).items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(text)
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-q", "-m", "first")
        self.git(repo, "push", "-q", "-u", "origin", "main")
        return repo

    @staticmethod
    def put(root: Path, rel: str, data: bytes = b"x" * 4096) -> Path:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    @staticmethod
    def age(path: Path, days: float):
        """Backdate a tree's mtimes (files and folders)."""
        stamp = time.time() - days * DAY
        for current, dirs, files in os.walk(path, topdown=False):
            for name in files + dirs:
                os.utime(os.path.join(current, name), (stamp, stamp), follow_symlinks=False)
        os.utime(path, (stamp, stamp))
