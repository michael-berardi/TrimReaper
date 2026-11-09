"""Disk-pressure rules for agent scratch: regenerable folders of a clean, fully pushed checkout, and linked worktrees merged into their
upstream, are removed at cleanAtPercent through the single deletion path (reap_item_strict, the cleanup owner, producer locks, per-item
open-handle checks, a plan receipt before and a result receipt after). Synthetic temporary homes and git remotes only."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from unittest import mock

from trimreaper import core

from helpers import Home

IGNORED = ["node_modules/", ".next/", ".open-next/", "target/", ".turbo/", ".astro/", "dist/", ".wrangler/", ".dev.vars", ".env*", ".vercel/"]
GONE = ["node_modules", ".astro", ".next", ".turbo", "target", ".open-next"]


class ScratchBase(Home):
    config_extra = {"scratchRoot": "~/.cache/agent-scratch"}

    def setUp(self):
        super().setUp()
        self.scratch = self.home / ".cache" / "agent-scratch"
        self.scratch.mkdir(parents=True)
        self.procs = []
        self.addCleanup(self.stop_procs)
        self.pressure = mock.patch.object(core, "reap_scratch_pressure_on", return_value=True)
        self.pressure.start()
        self.addCleanup(self.pressure.stop)

    def stop_procs(self):
        for proc in self.procs:
            proc.kill()
            proc.wait(timeout=20)

    def quiet(self):
        """Seven hours later for the idle proofs (2 h regenerable window, 6 h worktree window): ctime cannot be backdated, so the clock moves."""
        patcher = mock.patch.object(core, "now", lambda: time.time() + 7 * 3600)
        patcher.start()
        self.addCleanup(patcher.stop)

    def repo(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        self.git(path, "init", "-q", "-b", "main")
        (path / ".gitignore").write_text("\n".join(IGNORED) + "\n")
        (path / "src").mkdir()
        (path / "src" / "main.txt").write_text("source\n")
        self.git(path, "add", "-A")
        self.git(path, "commit", "-q", "-m", "init")
        bare = self.remotes / (path.name + ".git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
        self.git(path, "remote", "add", "origin", str(bare))
        self.git(path, "push", "-q", "-u", "origin", "main")
        return path

    def checkout(self, name="slot2/vitals", outputs=tuple(GONE) + ("dist",)) -> Path:
        root = self.repo(self.scratch / name)
        for out in outputs:
            self.put(root, f"{out}/sub/file.bin", b"x" * 2000)
        return root

    def receipts(self) -> list[dict]:
        directory = self.home / ".local/state/trimreaper/reaper"
        return [json.loads(line) for f in sorted(directory.glob("receipts-*.jsonl")) for line in f.read_text().splitlines()] if directory.exists() else []

    def regen(self, dry=False) -> dict:
        return core.reap_scratch_regen(dry)

    def hold(self, argv, cwd):
        self.procs.append(subprocess.Popen(argv, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(0.4)


class RegenerableDirs(ScratchBase):
    def test_clean_pushed_quiet_checkout_loses_its_regenerable_dirs_but_never_dist_or_source(self):
        root = self.checkout()
        self.quiet()
        out = self.regen()
        self.assertEqual(out["deleted"], len(GONE), out)
        for name in GONE:
            self.assertFalse((root / name).exists(), name)
        self.assertTrue((root / "dist" / "sub" / "file.bin").exists(), "dist can hold a release: never derived")
        self.assertTrue((root / "src" / "main.txt").exists())
        self.assertEqual(self.git(root, "status", "--porcelain"), "")
        records = [r for r in self.receipts() if r.get("class") == "scratch-regenerable"]
        self.assertEqual(sorted({r["phase"] for r in records}), ["plan", "result"], "a receipt before and after every item")
        self.assertTrue(all(r.get("cleanupOwnerRun") for r in records))

    def test_below_the_pressure_threshold_nothing_is_removed_and_the_reason_is_named(self):
        root = self.checkout()
        self.quiet()
        with mock.patch.object(core, "reap_scratch_pressure_on", return_value=False):
            out = self.regen()
        self.assertEqual(out["deleted"], 0)
        self.assertTrue(all(i["reason"] == "retention-preserved:below-pressure-threshold" for i in out["items"]), out["items"])
        for name in GONE:
            self.assertTrue((root / name).exists())

    def test_a_write_within_two_hours_keeps_it(self):
        root = self.checkout()  # real clock: everything was written a moment ago
        out = self.regen()
        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(all(i["reason"].startswith("retention-preserved:not-quiet") for i in out["items"]), out["items"])
        self.assertTrue((root / "node_modules").exists())

    def test_dirty_untracked_unpushed_and_stashed_checkouts_are_kept(self):
        self.quiet()
        dirty = self.checkout("a/dirty"); (dirty / "src" / "main.txt").write_text("changed\n")
        untracked = self.checkout("b/untracked"); (untracked / "notes.txt").write_text("mine\n")
        unpushed = self.checkout("c/unpushed"); (unpushed / "src" / "new.txt").write_text("n\n")
        self.git(unpushed, "add", "src/new.txt"); self.git(unpushed, "commit", "-q", "-m", "local only")
        stashed = self.checkout("d/stashed"); (stashed / "src" / "main.txt").write_text("wip\n")
        self.git(stashed, "stash", "push", "-q")
        out = self.regen()
        self.assertEqual(out["deleted"], 0, out)
        for root, reason in ((dirty, "dirty"), (untracked, "dirty"), (unpushed, "unpushed"), (stashed, "stash-present")):
            self.assertTrue((root / "node_modules").exists(), root)
            mine = [i for i in out["items"] if i["path"].startswith(str(root) + "/")]
            self.assertTrue(mine and all(i["reason"].split(":")[0] in (reason, "git-error") or reason in i["reason"] for i in mine), (root, [i["reason"] for i in mine]))

    def test_an_open_handle_or_cwd_keeps_it(self):
        root = self.checkout()
        self.quiet()
        self.hold(["sleep", "30"], cwd=str(root))
        out = self.regen()
        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(all(i["reason"].startswith("in-use") for i in out["items"]), [i["reason"] for i in out["items"]])
        self.assertTrue((root / "node_modules").exists())

    def test_protected_leaves_are_never_entered_deleted_or_listed(self):
        root = self.checkout()
        protected = {".wrangler/state/v3/d1/db.sqlite": b"d1", ".vercel/project.json": b"{}", ".env.local": b"SECRET=1\n", ".dev.vars": b"K=1\n"}
        for rel, data in protected.items():
            self.put(root, rel, data)
        before = {rel: ((root / rel).read_bytes(), (root / rel).stat().st_ino) for rel in protected}
        self.quiet()
        out = self.regen()
        self.assertEqual(out["deleted"], len(GONE), out)  # a protected sibling does not hold the regenerable dirs back
        for rel, state in before.items():
            self.assertEqual(((root / rel).read_bytes(), (root / rel).stat().st_ino), state, rel)
        self.assertFalse([i for i in out["items"] if any(p in str(i["path"]) for p in (".wrangler", ".vercel", ".env"))])

    def test_a_regenerable_dir_that_holds_a_protected_leaf_is_preserved_whole(self):
        root = self.checkout()
        kept = ("node_modules/pkg/.env", "target/.wrangler/tmp/x.js", ".next/server/.vercel/project.json")
        for rel in kept:
            self.put(root, rel, b"keep")
        self.quiet()
        out = self.regen()
        for rel in kept:
            self.assertEqual((root / rel).read_bytes(), b"keep", rel)
        self.assertEqual({n for n in GONE if not (root / n).exists()}, {".astro", ".turbo", ".open-next"}, out["items"])

    def test_tracked_content_a_non_git_folder_and_a_nested_checkout_are_not_removed(self):
        self.quiet()
        tracked = self.checkout("t/tracked", outputs=("node_modules",))
        self.git(tracked, "add", "-f", "node_modules/sub/file.bin"); self.git(tracked, "commit", "-q", "-m", "tracked deps"); self.git(tracked, "push", "-q")
        plain = self.scratch / "n" / "plain"
        self.put(plain, "node_modules/x", b"x")
        out = self.regen()
        self.assertTrue((tracked / "node_modules" / "sub" / "file.bin").exists())
        self.assertTrue((plain / "node_modules" / "x").exists(), "no .git: not a checkout, never an item")
        self.assertFalse([i for i in out["items"] if str(i["path"]).startswith(str(plain))])

    def test_dry_run_removes_nothing_and_writes_no_receipt(self):
        root = self.checkout()
        self.quiet()
        before = self.receipts()
        out = self.cli("reap", "--scratch-regen", "--dry-run")
        self.assertEqual(out["deleted"], 0)
        self.assertEqual([i["outcome"] for i in out["items"]].count("would-delete"), len(GONE), out["items"])
        self.assertEqual(self.receipts(), before)
        self.assertTrue((root / "node_modules").exists())

    def test_the_sweep_stops_once_the_disk_is_back_under_the_low_watermark(self):
        a, b = self.checkout("a/one", outputs=("node_modules",)), self.checkout("b/two", outputs=("node_modules",))
        self.quiet()
        readings = iter([{"usedPercent": 80}, {"usedPercent": 59}, {"usedPercent": 59}, {"usedPercent": 59}])
        with mock.patch.object(core, "disk_state", side_effect=lambda: next(readings, {"usedPercent": 59})):
            out = self.regen()
        self.assertEqual(out["deleted"], 1, out)
        self.assertEqual(sum(1 for r in (a, b) if (r / "node_modules").exists()), 1)

    def test_the_policy_keys_are_validated_and_zero_turns_the_rule_off(self):
        root = self.checkout()
        self.quiet()
        self.write_config({"policy": {"scratchRegenIdleHours": 0}})
        out = self.regen()
        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue((root / "node_modules").exists())
        self.write_config({"policy": {"scratchWorktreeIdleHours": "6"}})
        self.cli("reap", "--scratch-regen", "--dry-run", expect=2)


class MergedWorktrees(ScratchBase):
    def worktree(self, name="slot2/wt", push=True, branch="feat"):
        main = self.dev / "proj"
        if not main.exists():
            self.repo(main)
        wt = self.scratch / name
        wt.parent.mkdir(parents=True, exist_ok=True)
        self.git(main, "worktree", "add", "-q", "-b", branch, str(wt))
        (wt / "src" / "f.txt").write_text(f"feature {branch}\n")
        self.git(wt, "add", "-A"); self.git(wt, "commit", "-q", "-m", f"feature {branch}")
        if push:
            self.git(wt, "push", "-q", "-u", "origin", branch)
        self.put(wt, "node_modules/p/i.js", b"x" * 500)
        return main, wt

    def worktrees(self, main):
        return self.git(main, "worktree", "list", "--porcelain")

    def mine(self, out):
        return [i for i in out["items"] if i["class"] == "scratch-worktree"]

    def test_a_merged_idle_worktree_is_removed_with_git_worktree_remove_and_receipted(self):
        main, wt = self.worktree()
        self.quiet()
        out = self.regen()
        self.assertEqual([i["outcome"] for i in self.mine(out)], ["deleted"], out)
        self.assertFalse(wt.exists())
        self.assertNotIn(str(wt), self.worktrees(main))
        self.assertTrue(self.git(main, "rev-parse", "--verify", "feat").strip(), "the branch stays: only the checkout goes")
        records = [r for r in self.receipts() if r.get("class") == "scratch-worktree"]
        self.assertEqual([r["phase"] for r in records], ["plan", "result"])
        self.assertIn("git-worktree-remove", records[-1]["checks"])

    def test_never_force_removes_an_unclean_unmerged_unlocked_or_recent_worktree(self):
        main, wt = self.worktree("a/dirty"); (wt / "src" / "f.txt").write_text("dirty\n")
        _, nopush = self.worktree("b/nopush", push=False, branch="b")
        _, ahead = self.worktree("c/ahead", branch="c"); (ahead / "src" / "g.txt").write_text("g\n")
        self.git(ahead, "add", "-A"); self.git(ahead, "commit", "-q", "-m", "ahead of upstream")
        _, locked = self.worktree("d/locked", branch="d"); self.git(main, "worktree", "lock", str(locked))
        self.quiet()
        out = self.regen()
        mine = {Path(i["path"]).parent.name: i["reason"] for i in self.mine(out)}
        self.assertEqual([i["outcome"] for i in self.mine(out)], ["refused"] * 4, out)
        for path in (wt, nopush, ahead, locked):
            self.assertTrue(path.exists(), path)
        self.assertIn("dirty", mine["a"]); self.assertTrue(mine["b"].startswith(("unpushed", "no-remote-upstream")), mine["b"])
        self.assertTrue(mine["c"].startswith(("not-merged", "unpushed")), mine["c"]); self.assertIn("locked", mine["d"])
        self.assertEqual(self.git(wt, "status", "--porcelain").strip(), "M src/f.txt")

    def test_a_recent_write_a_busy_main_and_a_protected_leaf_keep_the_worktree(self):
        main, fresh = self.worktree("a/fresh")  # real clock: written a moment ago
        out = self.regen()
        self.assertTrue(fresh.exists()); self.assertIn("not-quiet", self.mine(out)[0]["reason"])
        self.quiet()
        (main / ".git" / "index.lock").write_bytes(b"")
        out = self.regen()
        self.assertTrue(fresh.exists()); self.assertIn("worktree-main-busy", self.mine(out)[0]["reason"])
        (main / ".git" / "index.lock").unlink()
        (fresh / ".env.local").write_bytes(b"SECRET=1\n")
        self.regen()
        self.assertEqual((fresh / ".env.local").read_bytes(), b"SECRET=1\n")
        self.assertTrue(fresh.exists())

    def test_the_protected_scan_runs_before_git_ever_lists_ignored_or_status(self):
        main, wt = self.worktree("a/order")
        (wt / ".env.local").write_bytes(b"SECRET=1\n")
        self.quiet()
        seen = []
        real = core.reap_git_clean_closed
        with mock.patch.object(core, "reap_git_clean_closed", side_effect=lambda *a, **k: (seen.append(a), real(*a, **k))[1]), \
                mock.patch.object(core, "reap_scratch_regen_items", return_value=([], [])):  # only the worktree item is under test
            out = self.regen()
        self.assertEqual(seen, [], "git status ran on a worktree that holds a protected leaf")
        self.assertTrue(wt.exists())
        self.assertEqual((wt / ".env.local").read_bytes(), b"SECRET=1\n")
        self.assertTrue(self.mine(out)[0]["reason"], out)

    def test_a_busy_queue_or_cargo_lock_keeps_the_worktree(self):
        import fcntl, os
        main, wt = self.worktree("a/locked-queue")
        self.quiet()
        for lock in (core.reap_queue_lock(str(wt)), wt / "target" / "debug" / ".cargo-lock"):
            lock.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o666)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                out = self.regen()
                self.assertTrue(wt.exists(), lock)
                self.assertIn("lock-busy", self.mine(out)[0]["reason"], out)
            finally:
                os.close(fd)
            if lock.name == ".cargo-lock":
                break
        out = self.regen()
        self.assertEqual([i["outcome"] for i in self.mine(out)], ["deleted"], out)
        self.assertIn("producer-locks", [c for r in self.receipts() if r.get("phase") == "result" for c in r["checks"]])

    def test_below_pressure_a_merged_worktree_stays(self):
        main, wt = self.worktree()
        self.quiet()
        with mock.patch.object(core, "reap_scratch_pressure_on", return_value=False):
            out = self.regen()
        self.assertTrue(wt.exists())
        self.assertEqual([i["reason"] for i in self.mine(out)], ["retention-preserved:below-pressure-threshold"])


class Pressure(ScratchBase):
    def test_pressure_is_the_clean_at_watermark_and_unknown_is_not_pressure(self):
        self.pressure.stop()
        policy = {"cleanAtPercent": 75}
        for percent, expected in ((74, False), (75, True), (97, True)):
            with mock.patch.object(core, "disk_state", return_value={"usedPercent": percent}):
                self.assertIs(core.reap_scratch_pressure_on(policy), expected, percent)
        with mock.patch.object(core, "disk_state", return_value=None):
            self.assertFalse(core.reap_scratch_pressure_on(policy))
        with mock.patch.object(core, "disk_state", return_value={"usedPercent": 99}):
            self.assertFalse(core.reap_scratch_pressure_on({"cleanAtPercent": 0}), "0 disables automatic cleaning")
        self.pressure.start()
