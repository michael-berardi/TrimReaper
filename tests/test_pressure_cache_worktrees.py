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
RUSTC_INFO = b'{"rustc_fingerprint": 1234567890123456789, "outputs": {}, "successes": {}}'   # what Cargo writes, trimmed
IGNORE = ("node_modules/", ".next/", "target/", "dist/", ".env*", ".dev.vars*", ".wrangler/", ".vercel/")
DERIVED = ("node_modules", ".next", "dist", "target")


class CacheBase(Home):
    core_disk_percent = 90

    def setUp(self):
        super().setUp()
        self.write_config({"policy": {"parkedReapHours": 0, "evidenceExpiryDays": 30}})
        self.cache = self.home / ".cache" / "trimreaper" / "jobs"

    def job(self, job: str, status: str = "parked", ago: float = 3600, waits_on: str | None = None, cwd=None):
        args = ["job", "add", job, "--status", "running"] + (["--waits-on", waits_on] if waits_on else []) + (["--cwd", str(cwd)] if cwd else [])
        self.cli(*args)
        self.cli("job", "status", job, status)
        stamp = time.time() - ago
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET created_at=?, started_at=? WHERE id=?", (stamp, stamp, job))
            if status in ("done", "failed", "cancelled"):
                conn.execute("UPDATE jobs SET finished_at=? WHERE id=?", (stamp, job))

    def worktree(self, job: str, name: str = "wt", ignore=IGNORE, outputs=DERIVED, linked: bool = False) -> Path:
        """A git clone (or linked worktree) of a pushed repository inside the job's cache folder, holding derived output."""
        base = self.clone(f"base-{job.lower()}-{name}", ignored=ignore, tracked={"README.md": "hello\n", "package.json": "{}\n"})
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
        self.put(root, "rs/target/.rustc_info.json", RUSTC_INFO)
        self.put(root, "rs/target/debug/app")
        self.put(root, "forged/target/.rustc_info.json", b"{}")   # not Cargo's metadata: valid JSON, wrong content
        self.put(root, "forged/target/debug/app")
        self.put(root, "text/target/.rustc_info.json", b"not json")
        self.put(root, "text/target/debug/app")
        self.pressure_tick()
        self.assertTrue((root / "plain/target/classes/A.class").exists())
        self.assertTrue((root / "forged/target/debug/app").exists() and (root / "text/target/debug/app").exists())
        self.assertFalse((root / "rs/target").exists())   # the positive control: the same layout with Cargo's own metadata goes
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
    def test_a_finished_jobs_cache_worktree_gives_up_derived_dirs_on_its_first_reap_under_pressure(self):
        self.job("J-0531", "done", ago=2 * 3600)
        root = self.worktree("J-0531")
        self.put(root, "notes.md", b"mine\n")
        out = self.cli("reap", "--job", "J-0531")
        for kind in DERIVED:
            self.assertFalse((root / kind).exists(), kind)
        self.assertTrue((root / "notes.md").exists())
        self.assertTrue(out["deleted"] >= 4, out)
        self.assertEqual(self.audit("J-0531")[str(root)]["state"], "blocked")  # unmarked evidence window still keeps it (the key must exist)


class PinnedTargets(CacheBase):
    """An `awaited-by` pin protects the PRODUCER's output for its consumer. Under pressure it is released only when the pinner itself rebuilds
    that output (it works inside the very checkout), never because the consumer is merely parked."""

    def standalone(self, job: str, name: str = "target-rust", tag: bool = True) -> Path:
        folder = self.cache / job / name
        self.put(folder, "debug/app", b"x" * 4096)
        self.put(folder, "debug/deps/lib.rlib", b"x" * 4096)
        if tag:
            self.put(folder, "CACHEDIR.TAG", TAG)
        self.age(folder, 2)
        return folder

    def producer(self, producer: str, name: str = "wt") -> Path:
        self.job(producer, "done", ago=3 * DAY)
        return self.worktree(producer, name=name, outputs=("node_modules",))

    def test_a_parked_consumer_that_only_reads_the_producers_output_keeps_it_under_pressure(self):
        """The producer is done and will not rebuild: the consumer would resume to a missing input."""
        reader = self.producer("J-0541")
        self.job("J-0542", "parked", waits_on="J-0541", cwd=self.tmp)         # works elsewhere: it only reads the output
        target = self.standalone("J-0541")
        self.assertEqual(self.pressure_tick(), [])
        self.assertTrue((reader / "node_modules").exists() and target.exists())
        for path in (reader / "node_modules", target):
            entry = self.audit("J-0541")[str(path)]
            self.assertEqual(entry["state"], "blocked", entry)
            self.assertTrue(entry["blocking"].startswith("awaited-by:J-0542"), entry)

    def test_a_pinner_that_works_inside_the_checkout_rebuilds_it_so_the_pin_is_released_under_pressure(self):
        """The positive control of the test above: the same fixture, the consumer working in the producer's checkout."""
        root = self.producer("J-0543")
        self.job("J-0544", "parked", waits_on="J-0543", cwd=root)
        self.assertEqual(self.pressure_tick(pressure=False), [])
        self.assertTrue((root / "node_modules").exists())                       # no pressure, no release
        self.assertEqual(self.audit("J-0543")[str(root / "node_modules")]["state"], "reapable")   # the audit disk is 90%
        actions = self.pressure_tick()
        self.assertEqual([a["did"] for a in actions], ["reaped-pinned-under-pressure"], actions)
        self.assertFalse((root / "node_modules").exists())
        self.assertTrue((root / "package.json").exists())

    def test_an_interrupted_pinner_working_inside_the_checkout_releases_it_without_a_continuation(self):
        root = self.producer("J-0545")
        self.job("J-0546", "interrupted", waits_on="J-0545", cwd=root)
        self.pressure_tick()
        self.assertFalse((root / "node_modules").exists())

    def test_an_interrupted_pinner_with_a_running_continuation_keeps_it(self):
        root = self.producer("J-0547")
        self.job("J-0548", "interrupted", waits_on="J-0547", cwd=root)
        self.job("J-0549", "running")
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", ("interrupt-heir:J-0548", "J-0549"))
        self.pressure_tick()
        self.assertTrue((root / "node_modules").exists())
        with contextlib.closing(self.state()) as conn, conn:   # control: the continuation finished, nothing carries the work on
            conn.execute("UPDATE jobs SET status='done', finished_at=? WHERE id='J-0549'", (time.time() - 3600,))
            conn.execute("DELETE FROM meta WHERE key LIKE 'reap:pressure:%'")
        self.pressure_tick()
        self.assertFalse((root / "node_modules").exists())

    def test_a_producer_pinned_by_a_parked_and_a_running_job_keeps_it_whichever_is_named_first(self):
        for order in ("parked-first", "running-first"):
            with self.subTest(order):
                number = 0 if order == "parked-first" else 1
                producer, one, two = f"J-06{number}1", f"J-06{number}2", f"J-06{number}3"
                root = self.producer(producer)
                parked, running = (one, two) if order == "parked-first" else (two, one)
                self.job(parked, "parked", waits_on=producer, cwd=root)
                self.job(running, "running", waits_on=producer, cwd=root)
                self.assertEqual(self.pressure_tick(), [])
                self.assertTrue((root / "node_modules").exists())
                entry = self.audit(producer)[str(root / "node_modules")]
                self.assertEqual(entry["state"], "blocked", entry)
                with contextlib.closing(self.state()) as conn, conn:   # control: the running pinner finishes, only the parked one is left
                    conn.execute("UPDATE jobs SET status='done', finished_at=? WHERE id=?", (time.time() - 3600, running))
                    conn.execute("DELETE FROM meta WHERE key LIKE 'reap:pressure:%'")
                self.pressure_tick()
                self.assertFalse((root / "node_modules").exists())

    def test_approved_running_queued_and_blocked_pinners_still_hold_it(self):
        for number, status in enumerate(("approved", "running", "queued", "blocked")):
            with self.subTest(status):
                producer, consumer = f"J-07{number}1", f"J-07{number}2"
                root = self.producer(producer)
                self.job(consumer, status, waits_on=producer, cwd=root)
                self.pressure_tick()
                self.assertTrue((root / "node_modules").exists(), status)
                entry = self.audit(producer)[str(root / "node_modules")]
                self.assertEqual(entry["state"], "blocked")
                self.assertTrue(entry["blocking"].startswith(f"awaited-by:{consumer}"), entry)

    def test_the_pressure_reading_is_taken_once_per_item_not_once_per_job_record(self):
        """A host asks reap_pin_released once for every job record it holds (4,000 live) for each item: a disk reading per call made one
        job's audit take two minutes instead of ten seconds."""
        root = self.producer("J-0541")
        self.job("J-0542", "parked", waits_on="J-0541", cwd=root)
        for number in range(25):
            self.job(f"J-08{number:02d}", "parked", waits_on="J-0541", cwd=root)
        readings = []
        real = core.reap_pressure_now

        def counted():
            readings.append(1)
            return real()

        item = {"path": str(root / "node_modules"), "root": str(root), "repo": str(root), "owner": "J-0541", "jobs": ["J-0541"],
                "class": "job-clone-regenerable", "kind": "node_modules", "ownership": "cache-worktree", "retention": "disposable"}
        with mock.patch.object(core, "reap_pressure_now", counted), core.db() as conn:
            core.REAP_PIN_ITEM.item, core.REAP_PIN_ITEM.verdict = item, None
            try:
                released = [core.reap_pin_released(conn, other) for other in core.rows(conn, "SELECT * FROM jobs WHERE status='parked'")]
            finally:
                core.REAP_PIN_ITEM.item = core.REAP_PIN_ITEM.verdict = None
        self.assertTrue(all(released) and len(released) == 26, released)
        self.assertEqual(len(readings), 1, len(readings))

    def test_a_pin_on_source_stays_even_for_a_pinner_inside_the_checkout(self):
        self.job("J-0551", "done", ago=40 * DAY)             # past the 30-day evidence window: only the pin holds it
        source = self.cache / "J-0551" / "inputs"
        self.put(source, "data.csv", b"important\n")
        self.age(source, 40)
        self.job("J-0552", "parked", waits_on="J-0551", cwd=source)
        self.pressure_tick()
        self.assertTrue((source / "data.csv").exists())
        with contextlib.closing(self.state()) as conn, conn:   # control: with no pinner the same folder is past its window
            conn.execute("UPDATE jobs SET status='done', finished_at=? WHERE id='J-0552'", (time.time() - 3600,))
        self.assertEqual(self.audit("J-0551")[str(source)]["state"], "reapable")

    def test_an_untagged_target_named_folder_is_not_pure_output(self):
        self.job("J-0561", "parked")
        tagged = self.standalone("J-0561", name="target-tagged")
        plain = self.standalone("J-0561", name="target-plain", tag=False)
        self.pressure_tick()
        self.assertFalse(tagged.exists())
        self.assertTrue(plain.exists())

    def test_a_target_folder_with_a_protected_leaf_is_refused(self):
        self.job("J-0562", "parked")
        clean = self.standalone("J-0562", name="target-a")
        target = self.standalone("J-0562", name="target-b")
        self.put(target, "debug/.env", b"SECRET=1\n")
        self.pressure_tick()
        self.assertFalse(clean.exists())
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
        self.job("J-0545", "parked")                           # control: the same folder of a parked job goes
        other = self.standalone("J-0545")
        self.pressure_tick()
        self.assertFalse(other.exists())


class Placement(CacheBase):
    """A dir counts as derived only when its OWN path is what git ignores and it sits where a package's output does. Every test holds a
    positive control: the same worktree's ordinary output goes, so a broken scan cannot pass."""

    def stay(self, root: Path, *names: str):
        for name in names:
            self.assertTrue((root / name).exists(), name)

    def test_a_vendored_runtimes_node_modules_is_not_build_output(self):
        """The live dry run listed resources/node/lib/node_modules (npm inside a vendored Node runtime): ignored by the same `node_modules/`
        pattern, but nothing rebuilds it and no package.json sits beside it."""
        self.job("J-0801")
        root = self.worktree("J-0801", outputs=("node_modules",))
        self.put(root, "resources/node/bin/node", b"#!runtime\n")
        self.put(root, "resources/node/lib/node_modules/npm/package.json", b"{}")
        self.put(root, "resources/node/lib/node_modules/corepack/dist/lib.js")
        self.pressure_tick()
        self.assertFalse((root / "node_modules").exists())                # control
        self.stay(root, "resources/node/lib/node_modules/npm/package.json", "resources/node/bin/node")

    def test_a_vendored_copy_below_a_vendor_folder_is_kept_even_beside_a_package_json(self):
        self.job("J-0802")
        root = self.worktree("J-0802", outputs=("node_modules",))
        for vendor in (".vendor", "vendor", "third_party"):
            self.put(root, f"steak-dsh/.scratch/{vendor}/package.json", b"{}")
            self.put(root, f"steak-dsh/.scratch/{vendor}/node_modules/dep/index.js")
        self.pressure_tick()
        self.assertFalse((root / "node_modules").exists())                # control
        for vendor in (".vendor", "vendor", "third_party"):
            self.stay(root, f"steak-dsh/.scratch/{vendor}/node_modules/dep/index.js")

    def test_a_derived_dir_below_an_ignored_ancestor_is_refused_by_the_git_proof(self):
        """The ancestor is what git ignores, not the dir: `check-ignore` on the dir alone says yes."""
        self.job("J-0803")
        root = self.worktree("J-0803", ignore=IGNORE + (".scratch/",), outputs=("node_modules",))
        self.put(root, ".scratch/pkg/package.json", b"{}")
        self.put(root, ".scratch/pkg/node_modules/dep/index.js")
        self.put(root, "pkg/package.json", b"{}")
        self.put(root, "pkg/node_modules/dep/index.js")                   # control: not below an ignored ancestor
        needs = ("ignored", "untracked")
        with self.assertRaises(core.ReapRefused) as refused:
            core.reap_git_proof(str(root), str(root / ".scratch/pkg/node_modules"), needs)
        self.assertEqual(str(refused.exception), "ignored-ancestor")
        self.assertIn("git-no-ignored-ancestor", core.reap_git_proof(str(root), str(root / "pkg/node_modules"), needs))
        self.pressure_tick()
        self.assertFalse((root / "pkg/node_modules").exists() or (root / "node_modules").exists())
        self.stay(root, ".scratch/pkg/node_modules/dep/index.js")

    def test_the_ancestor_rule_also_guards_a_named_clone(self):
        """Not only cache worktrees: the proof the named job clones use asks the same question."""
        repo = self.clone("site-j0805", ignored=("node_modules/", "vendored/"))
        self.put(repo, "vendored/pkg/node_modules/x.js")
        self.put(repo, "node_modules/y.js")
        needs = ("ignored", "untracked")
        with self.assertRaises(core.ReapRefused):
            core.reap_git_proof(str(repo), str(repo / "vendored/pkg/node_modules"), needs)
        self.assertIn("git-ignored", core.reap_git_proof(str(repo), str(repo / "node_modules"), needs))
        self.put(repo, "node_modules/.bin/tool")                          # a folder INSIDE derived output is ignored through its parent by design
        self.assertIn("git-ignored", core.reap_git_proof(str(repo), str(repo / "node_modules/.bin"), needs))

    def test_node_modules_must_sit_beside_a_package_json_and_dist_beside_one_or_a_build_config(self):
        self.job("J-0804")
        root = self.worktree("J-0804", outputs=("node_modules",))
        self.put(root, "packages/a/package.json", b"{}")
        self.put(root, "packages/a/node_modules/x.js")                    # control: a workspace package's own output
        self.put(root, "packages/b/node_modules/x.js")                    # no package.json beside it
        self.put(root, "packages/b/dist/index.js")                        # no package.json, no build config
        self.put(root, "site/vite.config.ts", b"export default {}\n")
        self.put(root, "site/dist/index.html")                            # control: beside a recognised build config
        self.pressure_tick()
        self.assertFalse((root / "packages/a/node_modules").exists() or (root / "node_modules").exists() or (root / "site/dist").exists())
        self.stay(root, "packages/b/node_modules/x.js", "packages/b/dist/index.js")

    def test_a_symlinked_derived_dir_or_intermediate_folder_never_reaches_outside_the_worktree(self):
        self.job("J-0806")
        root = self.worktree("J-0806", outputs=("node_modules",))
        outside = self.tmp / "outside"
        self.put(outside, "node_modules/precious.js")
        self.put(outside, "package.json", b"{}")
        self.put(outside, "target/CACHEDIR.TAG", TAG)
        self.put(outside, "target/debug/app")
        (root / "linked").symlink_to(outside)                                  # an intermediate folder
        self.put(root, "pkg/package.json", b"{}")
        (root / "pkg/node_modules").symlink_to(outside / "node_modules")       # a derived name that is a link
        (root / "target").symlink_to(outside / "target")
        self.pressure_tick()
        self.assertFalse((root / "node_modules").exists())                     # control
        self.assertTrue((outside / "node_modules/precious.js").exists() and (outside / "target/debug/app").exists())
        self.assertTrue((root / "pkg/node_modules").is_symlink() and (root / "target").is_symlink() and (root / "linked").is_symlink())

    def test_a_dotdot_path_or_a_link_swapped_in_after_listing_is_refused(self):
        self.job("J-0807", "done", ago=2 * 3600)
        root = self.worktree("J-0807", outputs=("node_modules",))
        other = self.worktree("J-0807", name="other", outputs=("node_modules",))
        item = {"path": str(root / "src" / ".." / ".." / "other" / "node_modules"), "root": str(root), "repo": str(root), "owner": "J-0807",
                "jobs": ["J-0807"], "class": "job-clone-regenerable", "kind": "node_modules", "ownership": "cache-worktree", "retention": "disposable"}
        (root / "src").mkdir(exist_ok=True)
        with mock.patch.object(core, "disk_level", return_value="warn"):
            result = core.reap_item_strict(item, "dry-run")
        self.assertNotEqual(result["outcome"], "would-delete", result)
        self.assertTrue((other / "node_modules").exists())
        genuine = {**item, "path": str(root / "node_modules")}             # control: the same item, honest path
        with mock.patch.object(core, "disk_level", return_value="warn"):
            self.assertEqual(core.reap_item_strict(genuine, "dry-run")["outcome"], "would-delete")
        # a link swapped in for the real folder after the item was listed
        stash = self.tmp / "moved"
        (root / "node_modules").rename(stash)
        (root / "node_modules").symlink_to(stash)
        with mock.patch.object(core, "disk_level", return_value="warn"):
            self.assertNotEqual(core.reap_item_strict(genuine, "delete")["outcome"], "deleted")
        self.assertTrue((stash / "sub/build.bin").exists())

    def test_a_partial_ignore_keeps_the_folder(self):
        """`dist/*` ignores what is in dist but not dist; `!dist/keep` unignores a folder: untracked source lives there."""
        self.job("J-0808")
        root = self.worktree("J-0808", ignore=("node_modules/", "dist/*", "!dist/keep"), outputs=("node_modules", "dist"))
        self.put(root, "dist/keep/notes.md", b"mine\n")
        self.pressure_tick()
        self.assertFalse((root / "node_modules").exists())                 # control
        self.stay(root, "dist/keep/notes.md", "dist/sub/build.bin")

    def test_a_protected_ancestor_inside_the_worktree_keeps_everything_below_it(self):
        self.job("J-0809")
        root = self.worktree("J-0809", outputs=("node_modules",))
        self.put(root, ".vercel/output/functions/x.func/package.json", b"{}")
        self.put(root, ".vercel/output/functions/x.func/node_modules/dep/index.js")
        self.pressure_tick()
        self.assertFalse((root / "node_modules").exists())                 # control
        self.stay(root, ".vercel/output/functions/x.func/node_modules/dep/index.js")


class GitFailsClosed(CacheBase):
    def pair(self, job: str):
        good = self.worktree(job, name="good", outputs=("node_modules",))
        bad = self.worktree(job, name="bad", outputs=("node_modules",))
        return good, bad

    def test_a_git_file_pointing_to_a_missing_gitdir_keeps_the_folder(self):
        self.job("J-0811")
        good, bad = self.pair("J-0811")
        shutil_rm = subprocess.run(["rm", "-rf", str(bad / ".git")])
        self.assertEqual(shutil_rm.returncode, 0)
        (bad / ".git").write_text(f"gitdir: {self.tmp}/does-not-exist/.git\n")
        self.pressure_tick()
        self.assertFalse((good / "node_modules").exists())                 # control
        self.assertTrue((bad / "node_modules/sub/build.bin").exists())

    def test_a_corrupt_index_keeps_the_folder(self):
        self.job("J-0812")
        good, bad = self.pair("J-0812")
        (bad / ".git/index").write_bytes(b"not an index" * 50)
        self.pressure_tick()
        self.assertFalse((good / "node_modules").exists())                 # control
        self.assertTrue((bad / "node_modules/sub/build.bin").exists())

    def test_a_derived_dir_inside_a_submodule_with_a_broken_gitdir_is_kept_and_a_sound_one_is_judged_by_its_own_repository(self):
        self.job("J-0813")
        root = self.worktree("J-0813", outputs=("node_modules",))
        for name, sound in (("sound", True), ("broken", False)):
            inner = self.clone(f"inner-{name}", ignored=("node_modules/",), tracked={"README.md": "x\n", "package.json": "{}\n"})
            subprocess.run(["git", "-C", str(root), "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), f"mods/{name}"],
                           check=True, capture_output=True)
            self.put(root, f"mods/{name}/node_modules/dep/index.js")
            if not sound:
                (root / f"mods/{name}/.git").write_text("gitdir: ../../.git/modules/does-not-exist\n")
        self.pressure_tick()
        self.assertFalse((root / "node_modules").exists())                 # control
        self.assertTrue((root / "mods/broken/node_modules/dep/index.js").exists())
        # a submodule that is sound is an ordinary nested repository: its own ignore rules decide, and git said they ignore it
        self.assertTrue((root / "mods/sound/README.md").exists())

    def test_an_unsafe_ancestor_name_is_refused_not_skipped(self):
        self.job("J-0814")
        root = self.worktree("J-0814", ignore=IGNORE + ("weird*/",), outputs=())
        weird = root / "weird\x01dir"
        self.put(weird, "package.json", b"{}")
        self.put(weird, "node_modules/x.js")
        with self.assertRaises(core.ReapRefused):
            core.reap_git_proof(str(root), str(weird / "node_modules"), ("ignored", "untracked"))
        self.put(root, "plain/package.json", b"{}")
        self.put(root, "plain/node_modules/x.js")
        core.reap_git_proof(str(root), str(root / "plain/node_modules"), ("ignored", "untracked"))   # control


class InUseAndChanges(CacheBase):
    def test_a_process_inside_the_derived_dir_keeps_it_and_one_outside_does_not(self):
        self.job("J-0821")
        busy = self.worktree("J-0821", name="busy", outputs=("node_modules",))
        self.job("J-0822")
        idle = self.worktree("J-0822", name="idle", outputs=("node_modules",))
        proc = subprocess.Popen(["sleep", "30"], cwd=str(busy / "node_modules" / "sub"))
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        time.sleep(0.4)
        self.pressure_tick()
        self.assertFalse((idle / "node_modules").exists())                 # control
        self.assertTrue((busy / "node_modules/sub/build.bin").exists())

    def test_an_open_file_inside_the_derived_dir_keeps_it(self):
        self.job("J-0823")
        busy = self.worktree("J-0823", name="busy", outputs=("node_modules",))
        self.job("J-0824")
        idle = self.worktree("J-0824", name="idle", outputs=("node_modules",))
        proc = subprocess.Popen(["tail", "-f", str(busy / "node_modules/sub/build.bin")], stdout=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        time.sleep(0.4)
        self.pressure_tick()
        self.assertFalse((idle / "node_modules").exists())                 # control
        self.assertTrue((busy / "node_modules/sub/build.bin").exists())

    def during_size(self, mutate, path: Path):
        """Run the tick while `mutate` happens after the engine's first proofs and before its removal (the sizing step sits between)."""
        real = core.reap_metadata_size
        fired = []

        def sizing(target, *args, **kwargs):
            if target == str(path) and not fired:
                fired.append(1)
                mutate()
            return real(target, *args, **kwargs)

        with mock.patch.object(core, "reap_metadata_size", sizing):
            self.pressure_tick()
        self.assertEqual(fired, [1])

    def test_a_file_tracked_between_the_first_proof_and_the_removal_is_caught_by_the_last_look(self):
        self.job("J-0825")
        changed = self.worktree("J-0825", name="changed", outputs=("node_modules",))
        self.during_size(lambda: self.git(changed, "add", "-f", "node_modules/sub/build.bin"), changed / "node_modules")
        self.assertTrue((changed / "node_modules/sub/build.bin").exists())
        self.job("J-0826")
        steady = self.worktree("J-0826", name="steady", outputs=("node_modules",))   # control: the same run with no change goes
        self.pressure_tick()
        self.assertFalse((steady / "node_modules").exists())

    def test_the_dir_stops_being_ignored_between_the_first_proof_and_the_removal(self):
        self.job("J-0827")
        changed = self.worktree("J-0827", name="changed", outputs=("node_modules",))
        self.during_size(lambda: (changed / ".gitignore").write_text(".env*\n"), changed / "node_modules")
        self.assertTrue((changed / "node_modules/sub/build.bin").exists())

    def test_cargos_mark_removed_between_the_first_proof_and_the_removal_keeps_the_target(self):
        self.job("J-0828")
        changed = self.worktree("J-0828", name="changed", outputs=("target",))
        self.during_size(lambda: (changed / "target/CACHEDIR.TAG").unlink(), changed / "target")
        self.assertTrue((changed / "target/sub/build.bin").exists())
        self.job("J-0829")
        steady = self.worktree("J-0829", name="steady", outputs=("target",))
        self.pressure_tick()
        self.assertFalse((steady / "target").exists())

    def test_a_release_artifact_appearing_in_dist_between_the_first_proof_and_the_removal_keeps_it(self):
        self.job("J-0831")
        changed = self.worktree("J-0831", name="changed", outputs=("dist",))
        self.during_size(lambda: self.put(changed, "dist/App_1.0.0.dmg", b"dmg"), changed / "dist")
        self.assertTrue((changed / "dist/App_1.0.0.dmg").exists())

    def test_the_disk_leaving_pressure_between_the_first_proof_and_the_removal_keeps_dist(self):
        self.job("J-0832")
        changed = self.worktree("J-0832", name="changed", outputs=("dist",))
        real = core.reap_metadata_size
        level = {"v": "warn"}

        def sizing(target, *args, **kwargs):
            level["v"] = "ok"
            return real(target, *args, **kwargs)

        policy = {**core.load_policy(), "parkedReapHours": 0}
        actions, errors = [], []
        with mock.patch.object(core, "load_policy", return_value=policy), mock.patch.object(core, "reap_metadata_size", sizing), \
                mock.patch.object(core, "disk_level", lambda *a: level["v"]), core.db() as conn:
            core.reap_tick(conn, policy, [], actions, errors)
        self.assertTrue((changed / "dist/sub/build.bin").exists())


class DistRules(CacheBase):
    def test_a_dist_that_holds_or_sits_beside_a_release_is_kept_and_a_plain_one_goes(self):
        releases = {"dmg": "dist/App_1.0.0_aarch64.dmg", "pkg": "dist/App.pkg", "zip": "dist/downloads/app.zip", "tgz": "dist/app-1.0.0.tgz",
                    "app": "dist/mac/App.app/Contents/Info.plist", "sig": "dist/App.app.tar.gz.sig", "manifest": "dist/latest.json",
                    "beside": "latest-mac.yml", "folder": "dist/releases/1.0.0/notes.txt"}
        self.job("J-0841")
        plain = self.worktree("J-0841", name="plain", outputs=("dist",))
        roots = {}
        for number, (label, rel) in enumerate(releases.items()):
            roots[label] = self.worktree("J-0841", name=f"rel-{label}", outputs=("dist",))
            self.put(roots[label], rel, b"artifact")
        self.pressure_tick()
        self.assertFalse((plain / "dist").exists())                          # control: the same fixture without a release goes
        for label, root in roots.items():
            self.assertTrue((root / "dist/sub/build.bin").exists(), label)

    def test_a_cache_worktree_dist_goes_only_under_pressure_never_because_the_job_finished_or_a_timer_elapsed(self):
        self.write_config({"policy": {"parkedReapHours": 1, "evidenceExpiryDays": 30}})
        self.job("J-0842", "parked", ago=5 * 3600)                           # quiet for longer than parkedReapHours
        parked = self.worktree("J-0842", outputs=("node_modules", "dist"))
        self.job("J-0843", "done", ago=2 * 3600)
        finished = self.worktree("J-0843", outputs=("node_modules", "dist"))
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET meta=? WHERE id='J-0842'", (json.dumps({"lastReportAt": time.time() - 5 * 3600}),))
        policy = {**core.load_policy(), "parkedReapHours": 1}
        actions, errors = [], []
        with mock.patch.object(core, "load_policy", return_value=policy), mock.patch.object(core, "disk_level", return_value="ok"), core.db() as conn:
            core.reap_tick(conn, policy, [], actions, errors)
            for job_id in ("J-0842", "J-0843"):
                core.reap_job_owned(conn, core.row(conn, "SELECT * FROM jobs WHERE id=?", (job_id,)), False, "t")
        self.assertEqual(errors, [])
        self.assertFalse((finished / "node_modules").exists(), "control: a finished job's other derived output goes without pressure")
        self.assertFalse((parked / "node_modules").exists(), "control: so does a quiet parked job's, by its timer")
        self.assertTrue((finished / "dist/sub/build.bin").exists() and (parked / "dist/sub/build.bin").exists())
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("DELETE FROM meta WHERE key LIKE 'reap:pressure:%'")
        self.pressure_tick()
        self.assertFalse((parked / "dist").exists())                          # control: under pressure the parked job's dist goes
        with mock.patch.object(core, "disk_level", return_value="warn"), core.db() as conn:
            core.reap_job_owned(conn, core.row(conn, "SELECT * FROM jobs WHERE id='J-0843'"), False, "t")
        self.assertFalse((finished / "dist").exists())

    def test_the_audit_names_the_dist_reason(self):
        self.job("J-0844")
        root = self.worktree("J-0844", outputs=("dist",))
        with mock.patch.object(core, "disk_level", return_value="ok"):
            self.assertEqual(self.audit("J-0844")[str(root / "dist")]["state"], "blocked")
        self.assertEqual(self.audit("J-0844")[str(root / "dist")]["state"], "reapable")   # control: under the audit's 90% disk


class EvidenceExpiryExplainsSmallFolders(CacheBase):
    def test_a_finished_jobs_small_unmarked_folder_is_reapable_only_past_evidence_expiry_days(self):
        """The live list held J-3989/review (8 KB, done): the 0.20.0 evidence window, not a 0.20.2 rule. Pinned here so it is never a mystery."""
        self.write_config({"policy": {"evidenceExpiryDays": 1, "parkedReapHours": 0}})
        self.job("J-0851", "done", ago=30 * 3600)       # past the 1-day window
        self.put(self.cache / "J-0851" / "review", "port.diff", b"d" * 100)
        self.age(self.cache / "J-0851" / "review", 2)
        self.job("J-0852", "done", ago=3 * 3600)        # inside it
        self.put(self.cache / "J-0852" / "review", "port.diff", b"d" * 100)
        self.age(self.cache / "J-0852" / "review", 2)
        self.assertEqual(self.audit("J-0851")[str(self.cache / "J-0851" / "review")]["state"], "reapable")
        self.assertEqual(self.audit("J-0852")[str(self.cache / "J-0852" / "review")]["state"], "blocked")
