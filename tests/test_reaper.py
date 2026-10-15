"""What the reaper removes, what it keeps, and that it says why: real clones, real git, real processes."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tarfile
import time
from pathlib import Path

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

        self.cli("reap", "--job", "J-0007")

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
        restored = self.tmp / "restored"
        subprocess.run(["git", "init", "-q", str(restored)], check=True, capture_output=True)
        self.git(restored, "fetch", "-q", str(archive / "repo-0.bundle"), "refs/*:refs/restored/*")
        self.assertIn(unpushed, self.git(restored, "log", "--all", "--format=%H"))
        with tarfile.open(archive / "files.tar.gz") as tar:
            names = set(tar.getnames())
        self.assertIn("README.md", names)
        self.assertIn("notes.txt", names)
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
