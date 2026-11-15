"""The reclaim rules of 0.20.0: superseded side-by-side releases of self-updating tools, build output in coordinator job folders,
release scratch, temp build caches, package caches over a size threshold and the guided VM disk trim.

Throwaway homes only: every root, tool, VM and temp folder the rules read is a fixture below the test's temporary folder."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from unittest import mock

from helpers import DAY, GIT_ENV, Home

from trimreaper import clean

HOUR = DAY / 24
TOOLS, FOLDERS = clean.CLEAN_TOOLS, clean.CLEAN_FOLDERS   # the shipped specs, before a fixture patches them out


def stamp(seconds_ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - seconds_ago))


class Reclaim(Home):
    def setUp(self):
        super().setUp()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.brew = self.tmp / "brew"
        self.shared, self.user = self.tmp / "tmp-shared", self.tmp / "tmp-user"
        for folder in (self.shared, self.user):
            folder.mkdir()
        env = {k: v for k, v in os.environ.items() if k not in ("XDG_DATA_HOME", "XWIN_CACHE_DIR")}
        env.update(PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}", COLIMA_HOME=str(self.tmp / "colima"))
        for patch in (mock.patch.dict(os.environ, env, clear=True),
                      mock.patch.object(clean, "CLEAN_BREW_PREFIXES", (str(self.brew),), create=True),
                      mock.patch.object(clean, "clean_temp_roots", lambda hours=None: [(str(self.shared), 24), (str(self.user), 72)])):
            patch.start()
            self.addCleanup(patch.stop)
        self.coord = self.home / ".cache" / "ut-coordinator"
        self.statuses = self.tmp / "statuses.json"
        self.statuses.write_text("{}")
        self.script("coord", f"#!{sys.executable} -I\nimport json, sys\njobs = json.load(open({str(self.statuses)!r}))\n"
                             "job = sys.argv[-1]\nif job not in jobs:\n    print(json.dumps({'ok': False, 'error': 'no job ' + job})); sys.exit(1)\n"
                             "print(json.dumps({'ok': True, 'job': {'id': job, **jobs[job]}}))\n")
        self.write_config({"policy": {"coordinatorJobCommand": [str(self.bin / "coord"), "job-get"]}})

    def script(self, name: str, text: str) -> Path:
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)
        return path

    def run_clean(self, *only: str, dry=False, expect=0, extra=()):
        return self.cli("clean", *(["--dry-run"] if dry else []), "--no-report", "--no-escalate", "--only", ",".join(only), *extra, expect=expect)

    def item(self, out, path: Path) -> dict:
        rel = "~" + str(path)[len(str(self.home)):] if str(path).startswith(str(self.home)) else str(path)
        found = [i for i in out["items"] if i.get("path") == rel]
        self.assertEqual(len(found), 1, f"{rel} not listed once: {[i.get('path') for i in out['items']]}")
        return found[0]

    def hold(self, cwd: Path, *argv: str) -> subprocess.Popen:
        process = subprocess.Popen(list(argv) or ["sleep", "60"], cwd=str(cwd))
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        time.sleep(0.3)
        return process

    def repo_at(self, path: Path, ignored=("node_modules/", "target/", "*-target/", "dist/", ".cloudflare/"), tracked=None) -> Path:
        remote = self.remotes / f"{path.name}-{uuid.uuid4().hex[:6]}.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
        path.mkdir(parents=True)
        self.git(path, "init", "-q", "-b", "main")
        self.git(path, "remote", "add", "origin", str(remote))
        (path / ".gitignore").write_text("".join(f"{line}\n" for line in ignored))
        for rel, text in (tracked or {"package.json": "{}\n"}).items():
            (path / rel).parent.mkdir(parents=True, exist_ok=True)
            (path / rel).write_text(text)
        self.git(path, "add", "-A")
        self.git(path, "commit", "-q", "-m", "first")
        self.git(path, "push", "-q", "-u", "origin", "main")
        return path

    def cargo_target(self, path: Path) -> Path:
        self.put(path, ".rustc_info.json", b"{}")
        self.put(path, "debug/app")
        return path


class CliReleases(Reclaim):
    """Rule 1: `<data home>/<tool>/releases/<semver>/`: the newest three stay, and every version something uses or names."""

    def releases(self, versions, tool="acme-cli", hours=5) -> Path:
        root = self.home / ".local" / "share" / tool / "releases"
        for version in versions:
            self.put(root / version, "package/bin/cli.js")
        self.age(root, hours / 24)
        return root

    def test_superseded_releases_go_and_used_or_named_ones_stay(self):
        names = [f"1.0.{n}" for n in range(11)]
        root = self.releases(names + ["tmp-unpack"])
        # named by the tool's settings (a dot-folder of the name after the hyphen), a wrapper, a link, a Cellar keg and processes
        self.put(self.home / ".cli" / "agent", "settings.json", json.dumps({"packages": [f"{root}/1.0.2/package"]}).encode())
        self.put(self.home / ".cli" / "agent", ".settings.before-update.json", json.dumps({"p": f"{root}/1.0.1/package"}).encode())
        wrapper = self.put(self.home / ".local" / "bin", "acme", f'#!/bin/sh\nexec node "{root}/1.0.3/package/bin/cli.js" "$@"\n'.encode())
        wrapper.chmod(0o755)
        os.symlink(root / "1.0.4", root.parent / "current")
        keg = self.brew / "Cellar" / "acme" / "1.0" / "bin"
        keg.mkdir(parents=True)
        os.symlink(root / "1.0.5" / "package" / "bin" / "cli.js", keg / "acme")
        self.hold(root / "1.0.6" / "package")
        self.hold(self.tmp, "/bin/sh", "-c", "sleep 60; :", "sh", f"{root}/1.0.7/package/bin/cli.js")
        planned = self.run_clean("cli-releases", dry=True)
        self.assertEqual(planned["byRule"]["cli-releases"]["candidates"], 2, planned["items"])
        reasons = {i["version"]: i.get("keep", "") for i in planned["items"]}
        self.assertNotIn("1.0.10", reasons)   # the newest three are never candidates: 1.0.10, 1.0.9, 1.0.8 (not by string order)
        self.assertNotIn("1.0.8", reasons)
        self.assertNotIn("tmp-unpack", reasons)
        self.assertTrue(reasons["1.0.2"].startswith("referenced-by:~/.cli/agent/settings.json"), reasons)
        self.assertTrue(reasons["1.0.3"].startswith("referenced-by:~/.local/bin/acme"), reasons)
        self.assertTrue(reasons["1.0.4"].startswith("referenced-by:~/.local/share/acme-cli/current"), reasons)
        self.assertTrue(reasons["1.0.5"].startswith("referenced-by:" + str(keg / "acme")), reasons)
        self.assertTrue(reasons["1.0.6"].startswith("in-use:cwd"), reasons)
        self.assertTrue(reasons["1.0.7"].startswith("in-use:command-line"), reasons)
        self.assertEqual(reasons["1.0.0"], "")
        self.assertEqual(reasons["1.0.1"], "")   # a settings backup names what was, not what is
        self.assertTrue(all((root / n).exists() for n in names))
        out = self.run_clean("cli-releases")
        self.assertEqual(out["summary"]["done"], 2, out)
        self.assertEqual(sorted(p.name for p in root.iterdir()), sorted(set(names + ["tmp-unpack"]) - {"1.0.0", "1.0.1"}))

    def test_few_releases_young_releases_and_unreadable_settings_keep_everything(self):
        root = self.releases(["2.0.0", "2.1.0", "2.2.0"])
        self.assertEqual(self.run_clean("cli-releases", dry=True)["items"], [])   # three or fewer: nothing to look at
        self.put(root / "1.9.0", "package/x")   # just installed (an hour has not passed)
        self.assertTrue(self.item(self.run_clean("cli-releases", dry=True), root / "1.9.0")["keep"].startswith("recent:"))
        self.age(root / "1.9.0", 1)
        settings = self.put(self.home / ".acme-cli", "config.json", b"{}")
        settings.chmod(0)
        self.addCleanup(settings.chmod, 0o600)
        out = self.run_clean("cli-releases")
        self.assertTrue(self.item(out, root / "1.9.0")["keep"].startswith("references-unproved:unreadable:"), out)
        self.assertTrue((root / "1.9.0").exists())
        settings.chmod(0o600)
        self.write_config({"policy": {"cliReleasesKeep": 0, "coordinatorJobCommand": [str(self.bin / "coord"), "job-get"]}})
        self.assertEqual(self.run_clean("cli-releases", dry=True)["items"], [])   # 0 turns the rule off
        self.write_config({"policy": {"coordinatorJobCommand": [str(self.bin / "coord"), "job-get"]}})
        self.assertEqual(self.run_clean("cli-releases")["summary"]["done"], 1)
        self.assertFalse((root / "1.9.0").exists())

    def test_semver_order(self):
        names = ["1.10.0", "1.9.9", "1.10.0-rc.2", "1.10.0-rc.10", "v1.2.3", "2.0.0-beta"]
        self.assertEqual(sorted(names, key=clean.clean_semver, reverse=True), ["2.0.0-beta", "1.10.0", "1.10.0-rc.10", "1.10.0-rc.2", "1.9.9", "v1.2.3"])
        self.assertIsNone(clean.clean_semver("latest"))


class CoordinatorOutputs(Reclaim):
    """Rule 2: build output in `<coordinator root>/J-NNNN/`: only for a finished job, or a parked/blocked one untouched 24 h."""

    def jobs(self, **statuses):
        self.statuses.write_text(json.dumps(statuses))

    def project(self, job: str, name="app", git=True, age_days=1.0) -> Path:
        root = self.coord / job / name
        if git:
            self.repo_at(root)
        else:
            self.put(root, "package.json", b"{}")
        self.put(root, "node_modules/left-pad/index.js")
        self.cargo_target(root / "target")
        self.cargo_target(root / "rust-test-target")
        self.put(root, "dist/index.html")
        self.put(root, ".cloudflare/state.json")
        self.age(self.coord / job, age_days)
        return root

    def test_finished_jobs_lose_their_build_output_and_nothing_else(self):
        self.jobs(**{"J-0101": {"status": "done", "finished_at": stamp(3 * HOUR)}, "J-0102": {"status": "running"},
                     "J-0103": {"status": "failed"}})
        done = self.project("J-0101")
        for keep in ("notes", "reports", "evidence", "handoffs"):
            self.put(self.coord / "J-0101" / keep / "node_modules", "x.js")
        self.put(done, "src/main.rs", b"fn main() {}\n")
        self.git(done, "add", "src/main.rs")
        self.git(done, "commit", "-q", "-m", "src")
        self.git(done, "push", "-q")
        live = self.project("J-0102")
        tracked = self.repo_at(self.coord / "J-0103" / "site", ignored=(), tracked={"package.json": "{}\n", "dist/index.html": "kept\n"})
        self.age(self.coord / "J-0103", 1)
        self.age(self.coord / "J-0101", 1)
        planned = self.run_clean("coordinator-outputs", dry=True)
        candidates = sorted(i["path"].split("/J-")[1] for i in planned["items"] if "keep" not in i)
        self.assertEqual(candidates, ["0101/app/.cloudflare", "0101/app/dist", "0101/app/node_modules", "0101/app/rust-test-target", "0101/app/target"])
        self.assertFalse([i for i in planned["items"] if "/notes/" in i["path"] or "/reports/" in i["path"] or "/evidence/" in i["path"]])
        self.assertTrue(self.item(planned, live / "node_modules")["keep"].startswith("job-live:running"))
        self.assertEqual(self.item(planned, tracked / "dist")["keep"], "not-git-ignored")
        self.assertGreater(planned["byRule"]["coordinator-outputs"]["candidateBytes"], 0)
        out = self.run_clean("coordinator-outputs")
        self.assertEqual(out["summary"]["done"], 5, out)
        self.assertEqual(sorted(p.name for p in done.iterdir()), [".git", ".gitignore", "package.json", "src"])
        self.assertTrue((self.coord / "J-0101" / "notes" / "node_modules" / "x.js").exists())
        self.assertTrue((live / "node_modules").exists() and (tracked / "dist" / "index.html").exists())

    def test_parked_jobs_wait_a_day_untouched_and_unknown_or_held_jobs_keep(self):
        self.jobs(**{"J-0201": {"status": "parked", "last_reported_at": stamp(2 * HOUR)}, "J-0202": {"status": "blocked", "last_reported_at": stamp(3 * DAY)},
                     "J-0203": {"status": "done"}, "J-0205": {"status": "cancelled"}})
        recent = self.project("J-0201", age_days=3)
        old = self.project("J-0202", age_days=3)
        held = self.project("J-0203")
        unknown = self.project("J-0204")
        broken = self.project("J-0205", git=False)
        (broken / ".git").write_text(f"gitdir: {self.tmp}/gone/worktrees/x\n")   # a worktree whose repository is gone
        self.put(broken, "build-cache/x")       # not a well-known name: never a candidate
        self.put(broken, "lib/node_modules/x")  # no manifest beside it: a bundled runtime, not an install
        self.age(self.coord / "J-0205", 1)
        self.hold(held)
        planned = self.run_clean("coordinator-outputs", dry=True)
        self.assertTrue(self.item(planned, recent / "node_modules")["keep"].startswith("job-parked:active-within-24h"))
        self.assertNotIn("keep", self.item(planned, old / "node_modules"))
        self.assertTrue(self.item(planned, held / "node_modules")["keep"].startswith("in-use:"))
        self.assertTrue(self.item(planned, unknown / "node_modules")["keep"].startswith("job-status-unknown:exit-1"))
        self.assertNotIn("keep", self.item(planned, broken / "node_modules"))
        self.assertEqual(self.item(planned, broken / "lib" / "node_modules")["keep"], "no-manifest-beside")
        self.assertFalse([i for i in planned["items"] if i["path"].endswith("build-cache")])
        out = self.run_clean("coordinator-outputs")
        self.assertFalse((old / "node_modules").exists() or (broken / "node_modules").exists() or (broken / "target").exists())
        for kept in (recent / "node_modules", held / "node_modules", unknown / "node_modules", broken / "lib" / "node_modules", broken / "build-cache"):
            self.assertTrue(kept.exists(), (kept, out["items"]))


class ReleaseScratch(Reclaim):
    """Rule 3: in `rel-*` release scratch: a merged, unused release worktree's `src-tauri/target`, `unz`, and older staged `out*`."""

    def test_merged_targets_unz_and_all_but_the_newest_two_outputs_go(self):
        scratch = self.home / ".cache" / "agent-scratch"
        merged = self.repo_at(scratch / "rel-1" / "wt", ignored=("target/",), tracked={"src-tauri/Cargo.toml": "[package]\n"})
        self.cargo_target(merged / "src-tauri" / "target")
        ahead = self.repo_at(scratch / "rel-2" / "wt", ignored=("target/",), tracked={"src-tauri/Cargo.toml": "[package]\n"})
        (ahead / "NOTES.md").write_text("unreleased\n")
        self.git(ahead, "add", "NOTES.md")
        self.git(ahead, "commit", "-q", "-m", "not on main")
        self.cargo_target(ahead / "src-tauri" / "target")
        for rel, hours in (("rel-1/out", 9), ("rel-2/out", 7), ("rel-3/out", 5), ("rel-3/out-2", 3), ("rel-1/unz", 2)):
            self.put(scratch / rel, "UltraApp.zip" if "out" in rel else "UltraApp.app/x")
            self.age(scratch / rel, hours / 24)
        self.put(scratch / "rel-3", "unz/fresh")   # changed just now
        os.symlink(scratch / "rel-1" / "out", scratch / "rel-2" / "out-linked")   # a link is never a candidate or followed
        self.age(merged, 1)
        self.age(ahead, 1)
        planned = self.run_clean("release-scratch", dry=True)
        verdict = {i["path"].split("agent-scratch/")[1]: i.get("keep", "") for i in planned["items"]}
        self.assertEqual(verdict["rel-1/wt/src-tauri/target"], "")
        self.assertEqual(verdict["rel-2/wt/src-tauri/target"], "release-head-not-merged")
        self.assertEqual(verdict["rel-1/unz"], "")
        self.assertTrue(verdict["rel-3/unz"].startswith("recent:"))
        self.assertEqual((verdict["rel-3/out-2"], verdict["rel-3/out"]), ("newest-2-staged-outputs",) * 2)
        self.assertEqual((verdict["rel-1/out"], verdict["rel-2/out"]), ("", ""))
        self.assertNotIn("rel-2/out-linked", verdict)
        self.hold(scratch / "rel-2")   # something works in rel-2: its old output stays
        out = self.run_clean("release-scratch")
        self.assertEqual(out["summary"]["done"], 3, out)
        self.assertFalse(any((scratch / rel).exists() for rel in ("rel-1/wt/src-tauri/target", "rel-1/unz", "rel-1/out")))
        for rel in ("rel-2/wt/src-tauri/target", "rel-2/out", "rel-3/out", "rel-3/out-2", "rel-3/unz", "rel-1/wt/src-tauri/Cargo.toml"):
            self.assertTrue((scratch / rel).exists(), rel)
        self.assertTrue(self.item(out, scratch / "rel-2" / "out")["keep"].startswith("in-use:"))


class TempBuildCaches(Reclaim):
    """Rule 5: node-compile-cache, `*-publish-check.*` and `oss-leaks-*` in the temp folders, after an hour unused."""

    def test_idle_temp_build_caches_go_after_an_hour(self):
        old = [self.put(self.shared / "node-compile-cache" / "v22-abc", "cache.bin").parent, self.put(self.user, "app-publish-check.x1/pkg.tgz").parent,
               self.put(self.user, "oss-leaks-42/report.txt").parent]
        for path in old:
            self.age(path, 2 / 24)
        fresh = self.put(self.user, "oss-leaks-43/report.txt").parent
        held = self.put(self.user, "lib-publish-check.y2/pkg.tgz").parent
        self.age(held, 2 / 24)
        self.hold(held)
        other = self.put(self.user, "unrelated-folder/x").parent
        self.age(other, 2 / 24)
        planned = self.run_clean("temp-build-caches", "agent-leftovers", dry=True)
        self.assertEqual(sorted(Path(i["path"]).name for i in planned["items"] if i["name"] == "temp-build-caches" and "keep" not in i),
                         ["app-publish-check.x1", "oss-leaks-42", "v22-abc"])
        self.assertTrue(self.item(planned, fresh)["keep"].startswith("recent:less-than-1h"))
        self.assertTrue(self.item(planned, held)["keep"].startswith("in-use:"))
        self.assertEqual(self.item(planned, other)["name"], "agent-leftovers")   # its own three-day window, unchanged
        self.run_clean("temp-build-caches")
        self.assertFalse(any(p.exists() for p in old))
        self.assertTrue(fresh.exists() and held.exists() and other.exists())


class PackageCaches(Reclaim):
    """Rule 6: uv's cache gets `uv cache clean` over its threshold, npm's is left alone under its threshold, cargo-xwin's goes when big."""

    def tool(self, name: str, **over) -> dict:
        spec = dict(next(t for t in TOOLS if t["name"] == name))
        spec.update(over)
        return spec

    def test_thresholds_choose_the_cleaner(self):
        uv_spec, npm_spec = self.tool("uv"), self.tool("npm")
        self.assertEqual((uv_spec["argvLarge"], uv_spec["argv"]), (["uv", "cache", "clean"], ["uv", "cache", "prune"]))
        self.assertIn("uv", uv_spec["busy"])
        self.assertTrue(npm_spec["minBytes"] > 0)
        log = self.tmp / "calls.log"
        for name in ("uv", "npm"):
            self.script(name, f"#!/bin/sh\necho \"{name} $*\" >> {log}\n")
        cache = self.tmp / "uv-cache"
        self.put(cache, "wheels/a.whl", os.urandom(2 << 20))
        npm_cache = self.tmp / "npm-cache"
        self.put(npm_cache, "index", b"x" * 4096)
        tools = (self.tool("uv", where=lambda: [str(cache)], largeBytes=1 << 20), self.tool("npm", where=lambda: [str(npm_cache)]))
        with mock.patch.object(clean, "CLEAN_TOOLS", tools), mock.patch.object(clean, "clean_running", lambda: set()):
            planned = self.run_clean("uv", "npm", dry=True)
            shown = {i["name"]: i for i in planned["items"]}
            self.assertEqual(shown["uv"]["command"], "uv cache clean")
            self.assertTrue(shown["npm"]["keep"].startswith("below-threshold:"))
            self.run_clean("uv", "npm")
            self.assertEqual(log.read_text(), "uv cache clean\n")
        with mock.patch.object(clean, "CLEAN_TOOLS", (self.tool("uv", where=lambda: [str(cache)]),)), \
                mock.patch.object(clean, "clean_running", lambda: set()):
            self.run_clean("uv")   # under 2 GB: the routine prune
            self.assertEqual(log.read_text().splitlines()[-1], "uv cache prune")
        with mock.patch.object(clean, "CLEAN_TOOLS", tools), mock.patch.object(clean, "clean_running", lambda: {"uv"}):
            self.assertEqual({i["name"]: i.get("keep") for i in self.run_clean("uv", dry=True)["items"]}["uv"], "in-use:uv")

    def test_cargo_xwin_cache_goes_only_when_big_and_unused(self):
        spec = next(f for f in FOLDERS if f["name"] == "cargo-xwin")
        root = self.home / "Library" / "Caches" / "cargo-xwin"
        self.put(root, "xwin/crt/lib.a", os.urandom(2 << 20))
        with mock.patch.object(clean, "CLEAN_FOLDERS", (spec,)), mock.patch.object(clean, "clean_running", lambda: set()):
            self.assertTrue(self.item(self.run_clean("cargo-xwin", dry=True), root / "xwin")["keep"].startswith("below-threshold:2MB-of-512MB"))
        small = dict(spec, minBytes=1 << 20)
        with mock.patch.object(clean, "CLEAN_FOLDERS", (small,)), mock.patch.object(clean, "clean_running", lambda: {"cargo-xwin"}):
            self.assertEqual(self.item(self.run_clean("cargo-xwin", dry=True), root / "xwin")["keep"], "in-use:cargo-xwin")
        with mock.patch.object(clean, "CLEAN_FOLDERS", (small,)), mock.patch.object(clean, "clean_running", lambda: set()):
            self.assertEqual(self.run_clean("cargo-xwin")["summary"]["done"], 1)
        self.assertFalse((root / "xwin").exists())


class VmDisk(Reclaim):
    """Rule 4: a stopped Colima VM whose disk file occupies a lot: reported with the steps; `--trim-vm` runs them, only from stopped."""

    def setUp(self):
        super().setUp()
        lima = self.tmp / "colima" / "_lima"
        self.put(lima / "colima", "disk", os.urandom(1 << 20))
        self.put(lima / "_disks" / "colima", "datadisk", os.urandom(2 << 20))
        self.log, self.status = self.tmp / "colima.log", self.tmp / "colima.status"
        self.status.write_text("Stopped\n")
        self.script("colima", f"#!/bin/sh\necho \"$*\" >> {self.log}\n"
                              f"case \"$1\" in list) printf '{{\"name\":\"default\",\"status\":\"%s\"}}\\n' \"$(cat {self.status})\"; exit 0;;\n"
                              f"start) echo Running > {self.status};; stop) echo Stopped > {self.status};; esac\n"
                              f"case \"$*\" in *fstrim*) [ -f {self.tmp}/fail-trim ] && exit 1;; esac\nexit 0\n")
        patch = mock.patch.object(clean, "CLEAN_VM_TRIM_MIN_BYTES", 2 << 20, create=True)
        patch.start()
        self.addCleanup(patch.stop)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_reported_with_the_steps_and_run_only_behind_the_flag(self):
        out = self.cli("clean", "--dry-run", "--no-escalate", "--only", "temp-build-caches")
        vm = next(r for r in out["report"] if r["kind"] == "vm-disk")
        self.assertEqual((vm["name"], vm["trimSuggested"], vm["running"]), ("colima:default", True, False))
        self.assertGreaterEqual(vm["bytes"], 3 << 20)
        self.assertEqual(vm["guidedAction"]["steps"], ["colima start", "colima ssh -- docker image prune -a -f", "colima ssh -- docker volume prune -a -f",
                                                       "colima ssh -- docker builder prune -a -f", "colima ssh -- sudo fstrim -v /mnt/lima-colima", "colima stop"])
        self.assertEqual(self.calls(), [])   # reporting runs nothing
        planned = self.run_clean("temp-build-caches", dry=True, extra=("--trim-vm",))
        self.assertEqual(planned["vmTrim"][0]["skipped"], "dry-run")
        self.assertEqual(self.calls(), ["list --json"])
        self.log.unlink()
        out = self.run_clean("temp-build-caches", extra=("--trim-vm",))
        self.assertEqual(out["vmTrim"][0]["outcome"], "trimmed", out["vmTrim"])
        self.assertEqual(self.calls(), ["list --json", "start", "ssh -- docker image prune -a -f", "ssh -- docker volume prune -a -f",
                                        "ssh -- docker builder prune -a -f", "ssh -- sudo fstrim -v /mnt/lima-colima", "stop"])
        self.assertEqual(self.status.read_text(), "Stopped\n")

    def test_a_running_vm_is_never_touched_and_a_failed_step_still_stops(self):
        self.status.write_text("Running\n")
        self.assertEqual(clean.clean_vm_trim(False)[0]["skipped"], "vm-not-stopped:Running")
        self.assertEqual(self.calls(), ["list --json"])
        self.log.unlink()
        self.status.write_text("Stopped\n")
        (self.tmp / "fail-trim").write_text("")
        result = clean.clean_vm_trim(False)[0]
        self.assertEqual((result["outcome"], result["exitCodes"][-2:]), ("failed", [1, 0]))
        self.assertEqual(self.calls()[-1], "stop")
        with mock.patch.object(clean, "CLEAN_VM_TRIM_MIN_BYTES", 64 << 20):
            self.assertTrue(clean.clean_vm_trim(False)[0]["skipped"].startswith("below-threshold:"))


class Report(Reclaim):
    def test_dry_run_reports_each_rules_reclaimable_size(self):
        self.age(self.put(self.user, "oss-leaks-1/r.txt", b"x" * 8192).parent, 1)
        out = self.run_clean("temp-build-caches", "cli-releases", "release-scratch", "coordinator-outputs", dry=True)
        self.assertEqual(out["byRule"]["temp-build-caches"]["candidates"], 1)
        self.assertGreaterEqual(out["byRule"]["temp-build-caches"]["candidateBytes"], 8192)
        for name in ("cli-releases", "coordinator-outputs", "release-scratch", "temp-build-caches"):
            self.assertIn(name, clean.CLEAN_NAMES)
        self.assertEqual(out["byRule"]["cli-releases"], {"candidates": 0, "candidateBytes": 0, "kept": 0, "keptBytes": 0})   # listed, found nothing
