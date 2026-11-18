"""0.20.2: disk pressure reaches derived output inside job cache worktrees (and standalone Cargo target folders), a parked or
interrupted pinner no longer holds pure derived output, and every new reapable path keeps its fences: protected leaves, tracked files,
git-unignored dirs, dirty or untracked source, markers, and running, starting and approved jobs. Synthetic temporary homes only."""
from __future__ import annotations

import contextlib
import json
import subprocess
import time
from pathlib import Path
from unittest import mock

from trimreaper import core

from helpers import Home, DAY

TAG = b"Signature: 8a477f597d28d172789f06886806bc55\n# fixture\n"
IGNORE = ("node_modules/", ".next/", "target/", "dist/", ".env*", ".dev.vars*", ".wrangler/", ".vercel/")
DERIVED = ("node_modules", ".next", "dist", "target")


class CacheBase(Home):
    core_disk_percent = 90

    def setUp(self):
        super().setUp()
        self.write_config({"policy": {"parkedReapHours": 0, "evidenceExpiryDays": 30}})
        self.cache = self.home / ".cache" / "trimreaper" / "jobs"

    def job(self, job: str, status: str = "parked", ago: float = 3600, waits_on: str | None = None):
        args = ["job", "add", job, "--status", "running"] + (["--waits-on", waits_on] if waits_on else [])
        self.cli(*args)
        self.cli("job", "status", job, status)
        stamp = time.time() - ago
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET created_at=?, started_at=? WHERE id=?", (stamp, stamp, job))
            if status in ("done", "failed", "cancelled"):
                conn.execute("UPDATE jobs SET finished_at=? WHERE id=?", (stamp, job))

    def worktree(self, job: str, name: str = "wt", ignore=IGNORE, outputs=DERIVED, linked: bool = False) -> Path:
        """A git clone (or linked worktree) of a pushed repository inside the job's cache folder, holding derived output."""
        base = self.clone(f"base-{job.lower()}-{name}", ignored=ignore)
        root = self.cache / job / name
        root.parent.mkdir(parents=True, exist_ok=True)
        if linked:
            self.git(base, "worktree", "add", "-q", "-b", f"{job}-{name}", str(root))
        else:
            subprocess.run(["git", "clone", "-q", str(self.remotes / f"base-{job.lower()}-{name}.git"), str(root)], check=True, capture_output=True)
        for kind in outputs:
            self.put(root, f"{kind}/sub/build.bin", b"x" * 4096)
        if "target" in outputs:
            self.put(root, "target/CACHEDIR.TAG", TAG)
        self.age(root, 2)
        return root

    def pressure_tick(self, pressure: bool = True) -> list:
        policy = {**core.load_policy(), "parkedReapHours": 0}
        actions, errors = [], []
        with mock.patch.object(core, "load_policy", return_value=policy), \
                mock.patch.object(core, "disk_level", return_value="warn" if pressure else "ok"), core.db() as conn:
            core.reap_tick(conn, policy, [], actions, errors)
        self.assertEqual(errors, [], errors)
        return actions

    def audit(self, job: str) -> dict:
        out = self.cli("status", "--items", "--job", job)
        return {entry["path"]: entry for j in out["jobs"] for entry in (j["detail"] or [])}


class CacheWorktreeDerived(CacheBase):
    def test_parked_jobs_derived_dirs_in_a_cache_worktree_go_under_pressure_and_nothing_else(self):
        self.job("J-0501")
        root = self.worktree("J-0501")
        self.put(root, "notes.md", b"mine\n")                   # untracked, not ignored: source
        self.put(root, "src/edit.txt", b"dirty\n")
        (root / "README.md").write_text("edited\n")             # dirty tracked source
        self.put(self.cache / "J-0501", "other/keep.bin")
        self.put(root, ".env.local", b"SECRET=1\n")             # protected leaf beside the outputs
        self.put(root, ".wrangler/state/db.sqlite", b"d1")
        actions = self.pressure_tick()
        self.assertEqual([a["did"] for a in actions], ["reaped-under-pressure"], actions)
        for kind in DERIVED:
            self.assertFalse((root / kind).exists(), kind)
        for kept in ("notes.md", "src/edit.txt", "README.md", ".env.local", ".wrangler/state/db.sqlite", ".git/HEAD"):
            self.assertTrue((root / kept).exists(), kept)
        self.assertEqual((root / "README.md").read_text(), "edited\n")
        self.assertTrue((self.cache / "J-0501" / "other" / "keep.bin").exists())

    def test_the_audit_lists_them_as_reapable_job_clone_regenerable_under_pressure(self):
        self.job("J-0502", "interrupted")
        root = self.worktree("J-0502")
        audited = self.audit("J-0502")
        for kind in DERIVED:
            entry = audited[str(root / kind)]
            self.assertEqual((entry["class"], entry["state"]), ("job-clone-regenerable", "reapable"), entry)
        self.assertEqual(audited[str(root)]["state"], "blocked")   # the worktree itself stays

    def test_without_pressure_a_recent_parked_job_keeps_them(self):
        self.job("J-0503")
        root = self.worktree("J-0503")
        self.assertEqual(self.pressure_tick(pressure=False), [])
        self.assertTrue(all((root / kind).exists() for kind in DERIVED))
        with mock.patch.object(core, "disk_level", return_value="ok"):
            self.assertEqual(self.audit("J-0503")[str(root / "node_modules")]["blocking"], "job-parked")

    def test_a_linked_worktree_in_the_cache_folder_is_scanned_too(self):
        self.job("J-0504", "blocked")
        root = self.worktree("J-0504", linked=True)
        self.pressure_tick()
        for kind in DERIVED:
            self.assertFalse((root / kind).exists(), kind)
        self.assertTrue((root / ".git").exists() and (root / "README.md").exists())

    def test_running_starting_and_approved_jobs_stay_blocked_even_under_pressure(self):
        roots = {}
        for number, status in ((511, "running"), (512, "approved"), (513, "queued")):
            self.job(f"J-0{number}", status)
            roots[status] = self.worktree(f"J-0{number}")
        self.assertEqual(self.pressure_tick(), [])
        for status, root in roots.items():
            self.assertTrue(all((root / kind).exists() for kind in DERIVED), status)
        for number, status in ((511, "running"), (512, "approved"), (513, "queued")):
            audited = self.audit(f"J-0{number}")
            self.assertEqual(audited[str(roots[status] / "node_modules")]["state"], "blocked", status)
            self.assertEqual(audited[str(roots[status] / "node_modules")]["blocking"], f"job-{status}")

    def test_a_job_that_declares_the_worktree_as_its_cwd_while_running_keeps_it(self):
        self.job("J-0514", "running")
        root = self.worktree("J-0514")
        self.cli("job", "add", "J-0515", "--cwd", str(root), "--status", "running")  # another, running job works in it
        self.cli("job", "status", "J-0514", "parked")
        self.assertEqual(self.pressure_tick(), [])
        self.assertTrue((root / "node_modules").exists())

    def test_target_needs_cargos_own_marks_and_rustc_info_counts(self):
        self.job("J-0516")
        root = self.worktree("J-0516", outputs=("node_modules",))
        self.put(root, "plain/target/classes/A.class")          # a folder named target that Cargo never wrote
        self.put(root, "rs/target/.rustc_info.json", b"{}")
        self.put(root, "rs/target/debug/app")
        self.pressure_tick()
        self.assertTrue((root / "plain/target/classes/A.class").exists())
        self.assertFalse((root / "rs/target").exists())
        self.assertFalse((root / "node_modules").exists())

    def test_a_forged_tag_file_is_not_cargos_mark(self):
        self.job("J-0517")
        root = self.worktree("J-0517", outputs=())
        self.put(root, "target/CACHEDIR.TAG", b"Signature: not-the-signature\n")
        self.put(root, "target/debug/app")
        self.pressure_tick()
        self.assertTrue((root / "target/debug/app").exists())

    def test_a_release_bearing_target_is_kept(self):
        self.job("J-0518")
        root = self.worktree("J-0518", outputs=())
        self.put(root, "target/CACHEDIR.TAG", TAG)
        self.put(root, "target/release/bundle/macos/App.app/Contents/Info.plist", b"<plist/>")
        self.put(root, "target/release/bundle/dmg/App_1.0.0_aarch64.dmg", b"dmg")
        self.pressure_tick()
        self.assertTrue((root / "target/release/bundle/dmg/App_1.0.0_aarch64.dmg").exists())


class CacheWorktreeFences(CacheBase):
    def test_a_dir_git_does_not_ignore_is_source_and_stays(self):
        self.job("J-0521")
        root = self.worktree("J-0521", ignore=(".env*",), outputs=("node_modules", "dist"))  # no .gitignore entry: untracked source
        self.pressure_tick()
        self.assertTrue((root / "node_modules/sub/build.bin").exists() and (root / "dist/sub/build.bin").exists())

    def test_a_tracked_file_inside_a_derived_dir_keeps_it(self):
        self.job("J-0522")
        root = self.worktree("J-0522", outputs=("dist",))
        self.git(root, "add", "-f", "dist/sub/build.bin")
        self.pressure_tick()
        self.assertTrue((root / "dist/sub/build.bin").exists())

    def test_a_derived_dir_holding_a_protected_leaf_is_refused_whole(self):
        self.job("J-0523")
        root = self.worktree("J-0523", outputs=("node_modules", "dist", ".next"))
        self.put(root, "node_modules/pkg/.env", b"SECRET=1\n")
        self.put(root, "dist/.dev.vars", b"K=1\n")
        self.put(root, ".next/server/.vercel/project.json", b"{}")
        self.put(root, ".next/server/.wrangler/tmp/x.js", b"x")
        self.pressure_tick()
        for kept in ("node_modules/pkg/.env", "dist/.dev.vars", ".next/server/.vercel/project.json", ".next/server/.wrangler/tmp/x.js"):
            self.assertTrue((root / kept).exists(), kept)

    def test_a_protected_name_is_never_a_worktree_or_an_ancestor(self):
        self.job("J-0524")
        root = self.worktree("J-0524", name=".wrangler")
        self.worktree("J-0524", name="wt")
        self.pressure_tick()
        self.assertTrue((root / "node_modules").exists())

    def test_evidence_markers_keep_the_derived_dirs(self):
        self.job("J-0525")
        marked = self.worktree("J-0525", name="marked")
        self.put(marked, ".ut-retention-evidence", b"")
        self.job("J-0526")
        folder = self.worktree("J-0526")
        self.put(self.cache / "J-0526", ".ut-retention-customer", b"")
        self.pressure_tick()
        self.assertTrue((marked / "node_modules").exists())
        self.assertTrue((folder / "node_modules").exists())

    def test_a_process_with_its_cwd_inside_keeps_it(self):
        self.job("J-0527")
        root = self.worktree("J-0527")
        proc = subprocess.Popen(["sleep", "30"], cwd=str(root / "src") if (root / "src").exists() else str(root))
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        time.sleep(0.4)
        self.pressure_tick()
        self.assertTrue((root / "node_modules").exists())

    def test_an_item_outside_its_worktree_or_job_folder_is_refused_by_the_boundary(self):
        self.job("J-0528")
        root = self.worktree("J-0528")
        other = self.worktree("J-0528", name="wt2")
        item = {"path": str(other / "node_modules"), "root": str(root), "repo": str(root), "owner": "J-0528", "jobs": ["J-0528"],
                "class": "job-clone-regenerable", "kind": "node_modules", "ownership": "cache-worktree", "retention": "disposable"}
        with self.assertRaises(core.ReapRefused):
            core.reap_boundary(item)
        foreign = {**item, "path": str(root / "node_modules"), "owner": "J-0999", "jobs": ["J-0999"]}
        with self.assertRaises(core.ReapRefused):
            core.reap_boundary(foreign)
        self.assertTrue((other / "node_modules").exists())

    def test_dry_run_removes_nothing(self):
        self.job("J-0529")
        root = self.worktree("J-0529")
        with mock.patch.object(core, "disk_level", return_value="warn"), core.db() as conn:
            job = core.row(conn, "SELECT * FROM jobs WHERE id='J-0529'")
            out = core.reap_job_owned(conn, job, True, "t", pressure=True)
        self.assertEqual(out["deleted"], 0)
        self.assertTrue(all(i["outcome"] == "would-delete" for i in out["items"]), out["items"])
        self.assertTrue(all((root / kind).exists() for kind in DERIVED))


class FinishedJobs(CacheBase):
    def test_a_finished_jobs_cache_worktree_gives_up_derived_dirs_on_its_first_reap(self):
        self.job("J-0531", "done", ago=2 * 3600)
        root = self.worktree("J-0531")
        self.put(root, "notes.md", b"mine\n")
        out = self.cli("reap", "--job", "J-0531")
        for kind in DERIVED:
            self.assertFalse((root / kind).exists(), kind)
        self.assertTrue((root / "notes.md").exists())
        self.assertTrue(out["deleted"] >= 4, out)
        self.assertEqual(self.audit("J-0531").get(str(root), {}).get("state", "blocked"), "blocked")  # unmarked evidence window still keeps it


class PinnedTargets(CacheBase):
    def standalone(self, job: str, name: str = "target-rust", tag: bool = True) -> Path:
        folder = self.cache / job / name
        self.put(folder, "debug/app", b"x" * 4096)
        self.put(folder, "debug/deps/lib.rlib", b"x" * 4096)
        if tag:
            self.put(folder, "CACHEDIR.TAG", TAG)
        self.age(folder, 2)
        return folder

    def pinned(self, consumer_status: str, tag: bool = True, producer: str = "J-0541", consumer: str = "J-0542"):
        self.job(producer, "done", ago=3 * DAY)
        target = self.standalone(producer, tag=tag)
        self.job(consumer, consumer_status, waits_on=producer)
        return target

    def test_a_parked_pinner_does_not_hold_a_pure_target_under_pressure(self):
        target = self.pinned("parked")
        self.assertEqual(self.pressure_tick(pressure=False), [])
        self.assertTrue(target.exists())
        self.assertEqual(self.audit("J-0541")[str(target)]["state"], "reapable")   # the audit disk is 90%
        actions = self.pressure_tick()
        self.assertEqual([a["did"] for a in actions], ["reaped-pinned-under-pressure"], actions)
        self.assertFalse(target.exists())

    def test_an_interrupted_pinner_does_not_hold_it_either(self):
        target = self.pinned("interrupted")
        self.pressure_tick()
        self.assertFalse(target.exists())

    def test_approved_running_and_blocked_pinners_still_hold_it(self):
        for number, status in enumerate(("approved", "running", "queued", "blocked")):
            with self.subTest(status):
                producer, consumer = f"J-06{number}1", f"J-06{number}2"
                target = self.pinned(status, producer=producer, consumer=consumer)
                self.pressure_tick()
                self.assertTrue(target.exists(), status)
                entry = self.audit(producer)[str(target)]
                self.assertEqual(entry["state"], "blocked")
                self.assertTrue(entry["blocking"].startswith(f"awaited-by:{consumer}"), entry)

    def test_the_pressure_reading_is_taken_once_per_item_not_once_per_job_record(self):
        """A host asks reap_pin_released once for every job record it holds (4,000 live) for each item: a disk reading per call made one
        job's audit take two minutes instead of ten seconds."""
        target = self.pinned("parked")
        for number in range(25):
            self.job(f"J-07{number:02d}", "parked")
        readings = []
        real = core.reap_pressure_now

        def counted():
            readings.append(1)
            return real()

        with mock.patch.object(core, "reap_pressure_now", counted):
            self.assertEqual(self.audit("J-0541")[str(target)]["state"], "reapable")
        self.assertLessEqual(len(readings), 4, len(readings))

    def test_a_pin_on_source_stays_even_for_a_parked_pinner(self):
        self.job("J-0541", "done", ago=3 * DAY)
        source = self.cache / "J-0541" / "inputs"
        self.put(source, "data.csv", b"important\n")
        self.job("J-0542", "parked", waits_on="J-0541")
        self.pressure_tick()
        self.assertTrue((source / "data.csv").exists())

    def test_an_untagged_target_named_folder_is_not_pure_output(self):
        target = self.pinned("parked", tag=False)
        self.pressure_tick()
        self.assertTrue(target.exists())

    def test_a_target_folder_with_a_protected_leaf_is_refused(self):
        target = self.pinned("parked")
        self.put(target, "debug/.env", b"SECRET=1\n")
        self.pressure_tick()
        self.assertTrue((target / "debug/.env").exists())

    def test_a_parked_jobs_own_cargo_target_folder_goes_under_pressure(self):
        self.job("J-0543", "parked")
        target = self.standalone("J-0543", name="target-cargo")
        notes = self.cache / "J-0543" / "notes"
        self.put(notes, "log.txt")
        self.pressure_tick()
        self.assertFalse(target.exists())
        self.assertTrue((notes / "log.txt").exists())

    def test_a_running_jobs_cargo_target_folder_stays(self):
        self.job("J-0544", "running")
        target = self.standalone("J-0544")
        self.pressure_tick()
        self.assertTrue(target.exists())
