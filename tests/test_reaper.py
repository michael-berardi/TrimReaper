"""What the reaper removes, what it keeps, and that it says why: real clones, real git, real processes."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tarfile
import time
from contextlib import closing
from pathlib import Path
from unittest import mock

from trimreaper import core

from helpers import DAY, Home


class FinishedJobOutput(Home):
    def test_build_output_of_a_finished_job_goes_and_everything_else_stays(self):
        repo = self.clone("app-j0001")
        node = self.put(repo, "node_modules/pkg/index.js").parent.parent
        target = self.put(repo, "target/debug/app").parent.parent
        self.put(repo, "dist/out.js")
        self.put(repo, ".env.local", b"SECRET=1\n")
        self.add_job("J-0001", repo, finished_ago=3600)

        out = self.cli("reap", "--job", "J-0001")

        self.assertEqual(out["deleted"], 2, out)
        self.assertFalse(node.exists())
        self.assertFalse(target.exists())
        self.assertTrue((repo / "dist" / "out.js").exists(), "dist can hold a release: it waits for the clone's expiry")
        self.assertEqual((repo / ".env.local").read_bytes(), b"SECRET=1\n")
        self.assertTrue((repo / "README.md").exists())
        receipts = list((self.home / ".local/state/trimreaper/reaper").glob("receipts-*.jsonl"))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(stat.S_IMODE(receipts[0].stat().st_mode), 0o600)
        phases = [json.loads(line)["phase"] for line in receipts[0].read_text().splitlines()]
        results = [json.loads(line) for line in receipts[0].read_text().splitlines() if json.loads(line)["phase"] == "result"]
        self.assertEqual(phases.count("plan"), 2, "a plan is durable before each removal")
        self.assertEqual(sorted(r["outcome"] for r in results), ["deleted", "deleted", "refused"])
        refused = next(r for r in results if r["outcome"] == "refused")
        self.assertEqual(refused["kind"], "dist")

    def test_a_job_that_is_still_running_keeps_everything(self):
        repo = self.clone("app-j0002")
        self.put(repo, "node_modules/pkg/index.js")
        self.add_job("J-0002", repo, status="running")

        error = self.cli("reap", "--job", "J-0002", expect=2)

        self.assertIn("job-running", error["error"])
        self.assertTrue((repo / "node_modules" / "pkg" / "index.js").exists())

    def test_a_job_that_just_finished_is_left_to_settle(self):
        repo = self.clone("app-j0003")
        self.put(repo, "node_modules/pkg/index.js")
        self.add_job("J-0003", repo, finished_ago=60)

        error = self.cli("reap", "--job", "J-0003", expect=2)

        self.assertIn("settling", error["error"])
        self.assertTrue((repo / "node_modules").exists())

    def test_dry_run_runs_the_gates_and_writes_nothing(self):
        repo = self.clone("app-j0004")
        self.put(repo, "node_modules/pkg/index.js")
        self.add_job("J-0004", repo)

        out = self.cli("reap", "--job", "J-0004", "--dry-run")

        self.assertEqual([i["outcome"] for i in out["items"]], ["would-delete"])
        self.assertTrue((repo / "node_modules").exists())
        self.assertFalse((self.home / ".local/state/trimreaper/reaper").exists(), "a dry run writes no receipt and no lock")

    def test_a_folder_a_process_works_in_is_kept(self):
        repo = self.clone("app-j0005")
        node = self.put(repo, "node_modules/pkg/index.js").parent
        self.add_job("J-0005", repo)
        sleeper = subprocess.Popen(["sleep", "60"], cwd=node)
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)

        out = self.cli("reap", "--job", "J-0005")

        self.assertEqual(out["deleted"], 0)
        self.assertTrue(out["items"][0]["reason"].startswith("in-use:"), out["items"][0])
        self.assertTrue(node.exists())

    def test_build_output_git_does_not_ignore_is_kept(self):
        repo = self.clone("app-j0006", ignored=())
        self.put(repo, "node_modules/pkg/index.js")
        self.add_job("J-0006", repo)

        out = self.cli("reap", "--job", "J-0006")

        self.assertEqual(out["deleted"], 0)
        self.assertTrue((repo / "node_modules").exists())

    def test_a_symlinked_output_is_never_followed(self):
        outside = self.tmp / "outside"
        self.put(outside, "precious.txt")
        repo = self.clone("app-j0007")
        os.symlink(outside, repo / "node_modules")
        self.add_job("J-0007", repo)

        out = self.cli("reap", "--job", "J-0007")

        self.assertEqual(out["items"], [], "a symlink is never offered as an item")
        self.assertTrue((outside / "precious.txt").exists())
        self.assertTrue(os.path.islink(repo / "node_modules"))

    def test_a_retention_marker_keeps_build_output(self):
        repo = self.clone("app-j0008")
        node = self.put(repo, "node_modules/pkg/index.js").parent.parent
        self.cli("mark", str(node), "evidence")
        self.add_job("J-0008", repo)

        out = self.cli("reap", "--job", "J-0008")

        self.assertEqual(out["deleted"], 0)
        self.assertTrue(out["items"][0]["reason"].startswith("retention-preserved"), out["items"][0])
        self.assertTrue(node.exists())


class ExpiredClones(Home):
    def test_a_clean_pushed_clone_goes_whole_after_expiry(self):
        repo = self.clone("site-j0010")
        self.put(repo, "node_modules/pkg/index.js")
        self.add_job("J-0010", repo, finished_ago=4 * DAY)

        out = self.cli("reap", "--job", "J-0010")

        self.assertFalse(repo.exists(), out)
        self.assertTrue((self.remotes / "site-j0010.git").exists())

    def test_a_clone_holding_secrets_is_kept_by_default(self):
        repo = self.clone("site-j0011")
        self.put(repo, ".env.local", b"SECRET=1\n")
        self.add_job("J-0011", repo, finished_ago=4 * DAY)

        self.cli("reap", "--job", "J-0011")

        self.assertEqual((repo / ".env.local").read_bytes(), b"SECRET=1\n")

    def test_unsaved_work_is_archived_and_verified_before_the_clone_goes(self):
        repo = self.clone("site-j0012")
        (repo / "README.md").write_text("edited, never committed\n")
        (repo / "notes.txt").write_text("untracked notes\n")
        (repo / "local.txt").write_text("committed, never pushed\n")
        self.git(repo, "add", "local.txt")
        self.git(repo, "commit", "-q", "-m", "local only")
        unpushed = self.git(repo, "rev-parse", "HEAD").strip()
        self.add_job("J-0012", repo, finished_ago=4 * DAY)

        out = self.cli("reap", "--job", "J-0012")

        self.assertFalse(repo.exists(), out)
        archives = list((self.home / ".cache/trimreaper/archive/J-0012").iterdir())
        self.assertEqual(len(archives), 1, archives)
        archive = archives[0]
        self.assertEqual(stat.S_IMODE(archive.parent.stat().st_mode), 0o700)
        manifest = json.loads((archive / "manifest.json").read_text())
        self.assertEqual(manifest["path"], str(repo))
        # the README's restore recipe, step by step
        restored = self.tmp / "restored"
        subprocess.run(["git", "init", "-q", str(restored)], check=True, capture_output=True)
        self.git(restored, "fetch", "-q", str(archive / "repo-0.bundle"), "refs/heads/*:refs/heads/*")
        refs = manifest["repositories"][0]["bundle"]["refs"]
        head = next(ref for ref, saved in refs.items() if "HEAD" in saved["from"])
        self.git(restored, "checkout", "-q", head.removeprefix("refs/heads/"))
        self.assertEqual(self.git(restored, "rev-parse", "HEAD").strip(), unpushed)
        with tarfile.open(archive / "files.tar.gz") as tar:
            tar.extractall(restored, filter="data")
        self.assertEqual((restored / "README.md").read_text(), "edited, never committed\n")
        self.assertEqual((restored / "notes.txt").read_text(), "untracked notes\n")
        self.assertIsNotNone(json.loads((archive / "sole-copy-since.json").read_text())["soleCopySince"])

    def test_a_clone_inside_its_expiry_window_stays(self):
        repo = self.clone("site-j0013")
        self.add_job("J-0013", repo, finished_ago=1 * DAY)

        self.cli("reap", "--job", "J-0013")

        self.assertTrue(repo.exists())


class Boundaries(Home):
    def test_a_clone_base_that_is_the_home_itself_is_refused(self):
        self.write_config({"cloneBases": [{"path": "~"}]})
        repo = self.clone("app-j0020")
        moved = self.home / "app-j0020"
        os.rename(repo, moved)
        self.put(moved, "node_modules/pkg/index.js")
        self.add_job("J-0020", moved)

        out = self.cli("reap", "--job", "J-0020")

        self.assertEqual(out["deleted"], 0)
        self.assertIn("allowed-root-too-broad", out["items"][0]["reason"])
        self.assertTrue((moved / "node_modules").exists())

    def test_a_clone_base_that_is_a_symlink_is_not_followed(self):
        real = self.tmp / "elsewhere"
        real.mkdir()
        os.symlink(real, self.home / "linked")
        self.write_config({"cloneBases": [{"path": "~/linked"}]})
        repo = self.clone("app-j0022")
        os.rename(repo, real / "app-j0022")
        node = self.put(real / "app-j0022", "node_modules/pkg/index.js")
        self.add_job("J-0022", self.home / "linked" / "app-j0022")

        out = self.cli("reap", "--job", "J-0022")

        self.assertEqual(out["deleted"], 0, out)
        self.assertEqual([i["reason"] for i in out["items"]], ["outside-allowed-roots"], out)
        self.assertTrue(node.exists())

    def test_a_folder_not_named_for_the_job_is_not_touched(self):
        repo = self.clone("unrelated-app")
        self.put(repo, "node_modules/pkg/index.js")
        self.add_job("J-0021", repo)

        out = self.cli("reap", "--job", "J-0021")

        self.assertEqual(out["items"], [])
        self.assertTrue((repo / "node_modules").exists())

    def test_unknown_config_keys_are_named(self):
        self.write_config({"cloneBase": "~/dev"})
        error = self.cli("config", expect=2)
        self.assertIn("unknown keys: cloneBase", error["error"])


class ScratchAndEvidence(Home):
    config_extra = {"scratchRoot": "~/.cache/agent-scratch"}

    def test_idle_scratch_goes_recent_and_marked_scratch_stays(self):
        root = self.home / ".cache" / "agent-scratch"
        idle = self.put(root, "idle-task/out.log").parent
        busy = self.put(root, "busy-task/out.log").parent
        kept = self.put(root, "kept-task/out.log").parent
        self.cli("mark", str(kept), "evidence")
        self.age(idle, 20)
        self.age(kept, 20)

        out = self.cli("reap", "--scratch")

        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(idle.exists())
        self.assertTrue(busy.exists())
        self.assertTrue(kept.exists())

    def test_live_scratch_sibling_does_not_pin_idle_tasks(self):
        root = self.home / ".cache" / "agent-scratch"
        idle = self.put(root, "idle-task/out.log").parent
        live = self.put(root, "live-task/out.log").parent
        self.age(idle, 20)
        self.age(live, 20)
        self.add_job("J-0031", live, status="running")

        out = self.cli("reap", "--scratch")

        self.assertFalse(idle.exists(), out)
        self.assertTrue(live.exists(), out)
        held = next(i for i in out["items"] if i["path"] == str(live))
        self.assertEqual(held["reason"], "claimed-by:J-0031(running)")

    def test_running_and_parked_ancestor_or_descendant_claims_preserve_scratch(self):
        root = self.home / ".cache" / "agent-scratch"
        for s, status in enumerate(("running", "parked")):
            for c, claim in enumerate(("ancestor", "exact", "descendant")):
                with self.subTest(status=status, claim=claim):
                    task = self.put(root, f"task-{status}-{claim}/nested/out.log").parent.parent
                    self.age(task, 20)
                    cwd = root if claim == "ancestor" else task / "nested" if claim == "descendant" else task
                    job = f"J-{320 + s * 3 + c:04d}"
                    self.add_job(job, cwd, status=status)
                    out = self.cli("reap", "--scratch")
                    self.assertTrue(task.exists(), out)
                    held = next(i for i in out["items"] if i["path"] == str(task))
                    self.assertEqual(held["reason"], f"claimed-by:{job}({status})")
                    self.cli("job", "status", job, "done")

    def test_machine_recorded_scratch_claim_is_not_inferred_from_the_cwd(self):
        root = self.home / ".cache" / "agent-scratch"
        held = self.put(root, "held-task/out.log").parent
        sibling = self.put(root, "other-task/out.log").parent
        self.age(held, 20)
        self.age(sibling, 20)
        self.add_job("J-0035", self.dev / "unrelated", status="parked")
        with closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET meta=? WHERE id='J-0035'", (json.dumps({"paths": [str(held)]}),))
        out = self.cli("reap", "--scratch")
        self.assertTrue(held.exists(), out)
        self.assertFalse(sibling.exists(), out)
        self.assertEqual(next(i for i in out["items"] if i["path"] == str(held))["reason"],
                         "claimed-by:J-0035(parked)")

    def test_idle_probe_never_enters_a_protected_leaf(self):
        task = self.home / ".cache" / "agent-scratch" / "protected-task"
        self.put(task, ".wrangler/state/db")
        self.age(task, 20)
        original = os.scandir
        def checked(path):
            self.assertNotIn(".wrangler", str(path))
            return original(path)
        with mock.patch.object(core.os, "scandir", side_effect=checked):
            self.assertFalse(core.reap_scratch_idle(str(task)))

    def test_release_claim_is_per_version_and_keeps_ancestor_claims(self):
        root = self.home / ".cache" / "releases"
        old = self.put(root, "1.0.0/report.txt").parent
        live = self.put(root, "2.0.0/report.txt").parent
        self.add_job("J-0033", live, status="running")
        item = {"path": str(old), "root": str(root), "owner": "release-evidence",
                "jobs": [], "class": "release-evidence"}
        core.reap_authority(item)
        self.add_job("J-0034", root, status="parked")
        with self.assertRaisesRegex(core.ReapRefused, r"claimed-by:J-0034\(parked\)"):
            core.reap_authority(item)

    def test_old_screenshots_and_logs_expire_with_a_manifest_reports_stay(self):
        folder = self.home / ".cache" / "trimreaper" / "jobs" / "J-0030"
        shot = self.put(folder, "screens/home.png", b"\x89PNG" + b"0" * 2000)
        report = self.put(folder, "report.json", b"{}")
        self.age(folder, 20)
        self.add_job("J-0030", None, finished_ago=20 * DAY)

        out = self.cli("reap", "--job", "J-0030", "--expire-evidence")

        self.assertEqual(out["expired"], 1, out)
        self.assertFalse(shot.exists())
        self.assertTrue(report.exists())
        manifest = [json.loads(line) for line in (folder / "evidence-expired.jsonl").read_text().splitlines()]
        self.assertEqual([m["name"] for m in manifest], ["screens/home.png"])
        self.assertEqual(len(manifest[0]["sha256"]), 64)


class Tick(Home):
    def test_one_tick_reaps_every_due_job(self):
        repo = self.clone("app-j0040")
        self.put(repo, "node_modules/pkg/index.js")
        self.add_job("J-0040", repo, finished_ago=3600)

        out = self.cli("tick")

        self.assertEqual(out["errors"], [])
        self.assertEqual([a["did"] for a in out["actions"]], ["reaped"])
        self.assertFalse((repo / "node_modules").exists())

    def test_the_audit_names_what_is_kept_and_why(self):
        repo = self.clone("app-j0041")
        self.put(repo, "node_modules/pkg/index.js")
        self.add_job("J-0041", repo, status="running")

        out = self.cli("status", "--items")

        self.assertEqual(out["deleted"], 0)
        self.assertIn("job-running", out["summary"]["blockedByReason"])
        self.assertTrue((repo / "node_modules").exists())


class Marks(Home):
    def test_one_marker_per_folder(self):
        folder = self.tmp / "folder"
        folder.mkdir()
        self.cli("mark", str(folder), "customer")
        error = self.cli("mark", str(folder), "disposable", expect=2)
        self.assertIn("already carries", error["error"])
        self.assertTrue((folder / ".ut-retention-customer").exists())


class IdleJobOutput(Home):
    def idle_job(self, status="parked", ago=2 * DAY):
        repo = self.clone("idle-j0050", ignored=tuple(k + "/" for k in core.REAP_DERIVED_KINDS) + ("dist/",))
        for kind in core.REAP_DERIVED_KINDS:
            self.put(repo, kind + "/build.bin")
        self.put(repo, "dist/release.bin")
        self.put(self.home / ".cache/trimreaper/jobs/J-0050", "cache/keep.bin")
        self.add_job("J-0050", repo, status=status)
        with closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET created_at=?,started_at=? WHERE id='J-0050'", (time.time() - ago, time.time() - ago))
        return repo

    def tick_core(self, pressure=False, hours=24):
        policy = {**core.load_policy(), "parkedReapHours": hours}
        actions, errors = [], []
        with mock.patch.object(core, "load_policy", return_value=policy), \
                mock.patch.object(core, "disk_level", return_value="warn" if pressure else "ok"), core.db() as conn:
            core.reap_tick(conn, policy, [], actions, errors)
        self.assertEqual(errors, [], errors)
        return actions

    def assert_only_output_gone(self, repo):
        for kind in core.REAP_DERIVED_KINDS:
            self.assertFalse((repo / kind).exists(), kind)
        self.assertTrue((repo / "README.md").exists())
        self.assertTrue((repo / "dist/release.bin").exists())
        self.assertTrue((self.home / ".cache/trimreaper/jobs/J-0050/cache/keep.bin").exists())
        with closing(self.state()) as conn, conn:
            self.assertIsNone(conn.execute("SELECT value FROM meta WHERE key='reap:J-0050'").fetchone())

    def test_old_parked_output_expires_without_disk_pressure_or_finished_jobs(self):
        repo = self.idle_job()
        self.assertTrue(self.tick_core())
        self.assert_only_output_gone(repo)

    def test_old_blocked_output_expires_without_disk_pressure(self):
        repo = self.idle_job("blocked")
        self.tick_core()
        self.assert_only_output_gone(repo)

    def test_a_recent_report_or_park_keeps_an_old_jobs_output(self):
        repo = self.idle_job()
        with closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET meta=? WHERE id='J-0050'", (json.dumps({"lastReportAt": time.time() - 3600}),))
        self.assertEqual(self.tick_core(), [])
        self.assertTrue((repo / "target").exists())
        with closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET meta=? WHERE id='J-0050'",
                         (json.dumps({"parked": {"at": time.time() - 3600, "handoff": "/x"}}),))
        self.assertEqual(self.tick_core(), [])
        self.assertTrue((repo / "target").exists())

    def test_pressure_runs_without_any_finished_job_due(self):
        repo = self.idle_job(ago=60)
        self.tick_core(pressure=True, hours=0)
        self.assert_only_output_gone(repo)

    def test_pressure_also_reaps_recent_blocked_output(self):
        repo = self.idle_job("blocked", ago=60)
        self.tick_core(pressure=True, hours=0)
        self.assert_only_output_gone(repo)

    def test_interrupted_output_still_requires_disk_pressure(self):
        repo = self.idle_job("interrupted")
        self.assertEqual(self.tick_core(), [])
        self.assertTrue((repo / "target").exists())
        self.tick_core(pressure=True)
        self.assert_only_output_gone(repo)

    def test_audit_counts_only_derived_idle_output_as_reapable(self):
        repo = self.idle_job()
        with mock.patch.object(core, "disk_level", return_value="ok"):
            out = self.cli("status", "--job", "J-0050", "--items")
        entries = out["jobs"][0]["detail"]
        reapable = [entry for entry in entries if entry["state"] == "reapable"]
        self.assertEqual({entry["kind"] for entry in reapable}, set(core.REAP_DERIVED_KINDS), out)
        self.assertTrue((repo / "target").exists())
        self.assertFalse((self.home / ".local/state/trimreaper/reaper").exists())

    def test_recent_idle_job_and_zero_hours_keep_output_without_pressure(self):
        repo = self.idle_job(ago=60)
        self.assertEqual(self.tick_core(), [])
        self.assertTrue((repo / "target").exists())
        with closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET created_at=?,started_at=? WHERE id='J-0050'", (time.time() - 2 * DAY,) * 2)
        self.assertEqual(self.tick_core(hours=0), [])
        self.assertTrue((repo / "target").exists())

    def test_recent_report_resets_the_quiet_window(self):
        repo = self.idle_job("blocked")
        with closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET meta=? WHERE id='J-0050'", (json.dumps({"lastReportAt": time.time() - 60}),))
        self.assertEqual(self.tick_core(), [])
        self.assertTrue((repo / "target").exists())

    def test_running_job_keeps_even_old_output_under_pressure(self):
        repo = self.idle_job("running")
        self.assertEqual(self.tick_core(pressure=True), [])
        self.assertTrue((repo / "target").exists())

    def test_live_terminal_keeps_idle_output_even_under_pressure(self):
        repo = self.idle_job()
        with closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET terminal_id='live' WHERE id='J-0050'")
        with mock.patch.object(core, "live_all", return_value=[{"id": "live"}]):
            self.tick_core(pressure=True)
        self.assertTrue((repo / "target").exists())


class ExpiredJobCache(Home):
    config_extra = {"policy": {"evidenceExpiryDays": 1, "cloneArchive": "keep"}}

    def cache(self, ago=2 * DAY):
        self.add_job("J-0060", finished_ago=ago)
        root = self.home / ".cache/trimreaper/jobs/J-0060"
        child = self.put(root, "unmarked-tool-cache/cache.bin").parent
        return root, child

    def test_unmarked_finished_cache_expires_by_finished_time_not_mtime(self):
        _, child = self.cache()
        out = self.cli("reap", "--job", "J-0060")
        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(child.exists())

    def test_disposable_job_folder_no_longer_suppresses_child_expiry(self):
        root, child = self.cache()
        self.cli("mark", str(root), "disposable")
        out = self.cli("reap", "--job", "J-0060")
        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(child.exists())
        self.assertTrue((root / ".ut-retention-disposable").exists())

    def test_preserving_parent_markers_keep_all_children(self):
        root, child = self.cache()
        marker = root / ".ut-retention-evidence"
        for label in ("evidence", "customer", "release-runtime-rollback"):
            with self.subTest(label=label):
                marker.unlink(missing_ok=True)
                marker = self.put(root, ".ut-retention-" + label)
                out = self.cli("reap", "--job", "J-0060")
                self.assertEqual(out["deleted"], 0, out)
                self.assertTrue(child.exists())

    def test_preserving_child_markers_and_protected_archive_stay(self):
        root, _ = self.cache()
        for label in ("evidence", "customer", "release-runtime-rollback"):
            child = self.put(root, label + "/keep.bin").parent
            self.cli("mark", str(child), label)
        self.put(root, "protected-archive/keep.bin")
        out = self.cli("reap", "--job", "J-0060")
        self.assertEqual(out["deleted"], 1, out)
        for name in ("evidence", "customer", "release-runtime-rollback", "protected-archive"):
            self.assertTrue((root / name / "keep.bin").exists())

    def test_recent_finish_or_missing_finish_time_does_not_expire(self):
        _, child = self.cache(ago=3600)
        self.assertEqual(self.cli("reap", "--job", "J-0060")["deleted"], 0)
        with closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET finished_at=NULL WHERE id='J-0060'")
        self.assertIn("settling", self.cli("reap", "--job", "J-0060", expect=2)["error"])
        self.assertTrue(child.exists())


class VendoredProtectedTemplates(Home):
    config_extra = {"policy": {"evidenceExpiryDays": 1, "cloneArchive": "keep"}}

    def vendor(self):
        self.add_job("J-0070", finished_ago=2 * DAY)
        folder = self.home / ".cache/trimreaper/jobs/J-0070/vendor"
        for name in (".env", "nested/.env.local", ".dev.vars", ".envrc"):
            self.put(folder, name, b"TEMPLATE=value\n")
        self.manifest(folder)
        return folder

    def manifest(self, folder):
        names = (".env", "nested/.env.local", ".dev.vars", ".envrc")
        text = "".join(hashlib.sha256((folder / name).read_bytes()).hexdigest() + "  ./" + name + "\n" for name in names)
        self.put(folder, "FILES.sha256", text.encode())

    def reap(self):
        return self.cli("reap", "--job", "J-0070")

    def test_every_manifest_verified_protected_file_goes_with_vendor(self):
        folder = self.vendor()
        out = self.reap()
        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(folder.exists())

    def test_nested_manifest_covers_only_its_shipped_tree(self):
        folder = self.vendor()
        package = folder / "package"
        package.mkdir()
        for name in (".env", "nested", ".dev.vars", ".envrc", "FILES.sha256"):
            (folder / name).rename(package / name)
        self.assertEqual(self.reap()["deleted"], 1)

    def test_modified_or_unlisted_secret_keeps_the_entire_folder(self):
        folder = self.vendor()
        self.put(folder, ".env", b"LOCAL=secret\n")
        self.assertEqual(self.reap()["deleted"], 0)
        self.manifest(folder)
        self.put(folder, ".env.production", b"LOCAL=secret\n")
        self.assertEqual(self.reap()["deleted"], 0)
        self.assertTrue(folder.exists())

    def test_wrangler_or_vercel_prevents_the_manifest_exception(self):
        folder = self.vendor()
        for name in (".wrangler", ".vercel"):
            with self.subTest(name=name):
                state = folder / name
                state.mkdir()
                self.assertEqual(self.reap()["deleted"], 0)
                state.rmdir()
        self.assertTrue(folder.exists())

    def test_manifest_paths_must_be_strict_and_unique(self):
        folder = self.vendor()
        manifest = folder / "FILES.sha256"
        original = manifest.read_bytes()
        for suffix in (original.splitlines()[0] + b"\n", b"0" * 64 + b"  ./../outside\n", b"malformed\n"):
            with self.subTest(suffix=suffix[:8]):
                manifest.write_bytes(original + suffix)
                self.assertEqual(self.reap()["deleted"], 0)

    def test_symlinked_protected_file_is_not_followed(self):
        folder = self.vendor()
        outside = self.put(self.tmp, "secret", b"TEMPLATE=value\n")
        (folder / ".env").unlink()
        (folder / ".env").symlink_to(outside)
        self.assertEqual(self.reap()["deleted"], 0)
        self.assertTrue(outside.exists())

    def test_protected_directory_or_hardlinked_file_is_not_a_template(self):
        folder = self.vendor()
        os.link(folder / ".env", self.tmp / "hardlink")
        self.assertEqual(self.reap()["deleted"], 0)
        (self.tmp / "hardlink").unlink()
        (folder / ".env").unlink()
        (folder / ".env").mkdir()
        self.assertEqual(self.reap()["deleted"], 0)

    def test_missing_manifest_keeps_all_protected_files(self):
        folder = self.vendor()
        (folder / "FILES.sha256").unlink()
        self.assertEqual(self.reap()["deleted"], 0)
        self.assertTrue((folder / ".env").exists())

    def test_verified_vendor_in_ignored_build_output_passes_the_git_proof(self):
        repo = self.clone("vendor-j0071")
        folder = repo / "node_modules/package"
        for name in (".env", "nested/.env.local", ".dev.vars", ".envrc"):
            self.put(folder, name, b"TEMPLATE=value\n")
        self.manifest(folder)
        self.add_job("J-0071", repo)
        out = self.cli("reap", "--job", "J-0071")
        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse((repo / "node_modules").exists())
        self.assertTrue((repo / "README.md").exists())

    def test_manifest_symlink_is_not_trusted(self):
        folder = self.vendor()
        manifest = folder / "FILES.sha256"
        outside = self.put(self.tmp, "manifest", manifest.read_bytes())
        manifest.unlink()
        manifest.symlink_to(outside)
        self.assertEqual(self.reap()["deleted"], 0)

    def test_last_scan_rehashes_before_removal(self):
        folder = self.vendor()
        original = core.reap_holders
        def changed(*args, **kwargs):
            self.put(folder, ".env", b"LOCAL=changed\n")
            return original(*args, **kwargs)
        with mock.patch.object(core, "reap_holders", side_effect=changed):
            out = self.reap()
        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue(folder.exists())
