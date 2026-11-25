"""Regression tests for TrimReaper v0.25.0:
- Batch coordinator job status (`coordinatorJobListCommand`) eliminating N+1 subprocess hangs
- Verified TrimReaper offload symlink handling in git metadata & index safety checks
- Consistent git-tracked env template handling (`.env.local.example`, etc.) across scratch, job-cache, and worktrees
- Expanded generic macOS/dev-machine reclaim rules (`.build`, `.cloudflare`, `.open-next`, `.output`,
  `~/.cargo/git`, `swiftpm-cache`, `core-simulator-caches`, `dev-tool-caches`, `darwin-user-caches`,
  detached Cargo targets inside recent scratch folders, opt-in gzip `compressLogsMode`, `~/Models` inventory)
- `trimreaper offload prune-dangling`
- `trimreaper audit` (CLI + MCP)
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import stat
import sys
import time
from pathlib import Path
from unittest import mock

from helpers import DAY, Home, ScratchHome

from trimreaper import audit, clean, core, host, mcp, offload

HOUR = DAY / 24
EMPTY_TABLE = {"files": [], "commands": []}
CARGO_TAG = b"Signature: 8a477f597d28d172789f06886806bc55\n# This file is a cache directory tag created by cargo.\n"
REAL_CLEAN_FOLDERS = clean.CLEAN_FOLDERS
REAL_DARWIN_CACHE_DIR = clean.clean_darwin_cache_dir


class BatchCoordinatorStatus(Home):
    def setUp(self):
        super().setUp()
        self.coord = self.home / ".cache" / "coord"
        self.coord.mkdir(parents=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.statuses = self.tmp / "statuses.json"
        self.calls_log = self.tmp / "calls.log"
        script = self.bin / "coord"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"open({str(self.calls_log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
            f"data = json.load(open({str(self.statuses)!r}))\n"
            "if sys.argv[1] == 'list-jobs':\n"
            "    print(json.dumps({'jobs': [{'id': k, **v} for k, v in data.items()]}))\n"
            "elif sys.argv[1] == 'job-get':\n"
            "    job = data.get(sys.argv[2])\n"
            "    if not job: sys.exit(1)\n"
            "    print(json.dumps({'job': {'id': sys.argv[2], **job}}))\n"
        )
        script.chmod(0o755)
        self.write_config({
            "policy": {
                "coordinatorRoots": [str(self.coord)],
                "coordinatorJobCommand": [str(script), "job-get"],
                "coordinatorJobListCommand": [str(script), "list-jobs"],
            }
        })
        for patch in (
            mock.patch.object(clean, "clean_running", lambda: set()),
            mock.patch.object(clean, "reap_process_table", lambda: dict(EMPTY_TABLE)),
            mock.patch.object(core, "reap_process_table", lambda: dict(EMPTY_TABLE)),
            mock.patch.object(clean, "reap_holders", lambda *a, **kw: None),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_batch_list_avoids_n_plus_one_calls_and_reclaims_done_jobs(self):
        statuses = {}
        for idx in range(1, 16):
            jid = f"J-{idx:04d}"
            statuses[jid] = {"status": "done" if idx <= 10 else "running"}
            proj = self.coord / jid / "app"
            self.put(proj, "package.json", b"{}")
            self.put(proj, "node_modules/pkg/index.js", b"x")
            self.age(self.coord / jid, 1)
        # Non-manifest node_modules should fail regenerable check BEFORE any status call
        no_manifest = self.coord / "J-9999" / "bundle"
        self.put(no_manifest, "node_modules/x.js", b"x")
        self.age(self.coord / "J-9999", 1)
        self.statuses.write_text(json.dumps(statuses))

        planned = self.cli("clean", "--dry-run", "--only", "coordinator-outputs")
        self.assertEqual(planned["byRule"]["coordinator-outputs"]["candidates"], 10, planned)
        calls = self.calls_log.read_text().splitlines()
        self.assertEqual(calls, ["list-jobs"], f"expected single batch call, got: {calls}")

        out = self.cli("clean", "--only", "coordinator-outputs")
        self.assertEqual(out["summary"]["done"], 10, out)
        self.assertFalse((self.coord / "J-0001" / "app" / "node_modules").exists())
        self.assertTrue((self.coord / "J-0011" / "app" / "node_modules").exists())
        self.assertTrue((no_manifest / "node_modules").exists())


class OffloadSymlinkGitSafety(Home):
    def setUp(self):
        super().setUp()
        self.overflow = self.tmp / "Volumes" / "Drive" / "Overflow" / "home"
        self.overflow.mkdir(parents=True)
        patch = mock.patch.object(core, "reap_overflow_home", lambda: str(self.overflow))
        patch.start()
        self.addCleanup(patch.stop)

    def test_offload_hop_resolves_home_worktree_pointing_to_offloaded_repo(self):
        main_repo = self.home / ".cache" / "jobs" / "J-0010" / "base"
        main_repo.mkdir(parents=True)
        self.git(main_repo, "init", "-q", "-b", "main")
        (main_repo / "README.md").write_text("hi\n")
        self.git(main_repo, "add", "-A")
        self.git(main_repo, "commit", "-qm", "init")
        wt = self.home / "dev" / "linked-wt"
        self.git(main_repo, "worktree", "add", "-q", "--detach", str(wt), "HEAD")
        # Move main_repo to its exact overflow mirror and leave a verified TrimReaper offload symlink
        mirror = self.overflow / main_repo.relative_to(self.home)
        mirror.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(main_repo, mirror)
        shutil.rmtree(main_repo)
        os.symlink(mirror, main_repo)
        code, out = core.reap_git(str(wt), "rev-parse", "--verify", "HEAD")
        self.assertEqual(code, 0, out)

        # Unverified non-overflow symlink must still fail closed
        evil = self.tmp / "evil-link"
        os.symlink(mirror, evil)
        (wt / ".git").write_text(f"gitdir: {evil}/.git/worktrees/linked-wt\n")
        with self.assertRaisesRegex(core.ReapRefused, "^git-error:unsafe-metadata:symlink$"):
            core.reap_git_metadata_gate(str(wt))

    def test_tracked_dir_offloaded_symlink_does_not_fail_non_directory_ancestor(self):
        repo = self.clone("app-offload", tracked={"README.md": "hi\n", "assets/logo.txt": "svg\n"})
        assets = repo / "assets"
        mirror = self.overflow / assets.relative_to(self.home)
        mirror.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(assets, mirror)
        shutil.rmtree(assets)
        os.symlink(mirror, assets)
        # Should NOT raise git-unsafe-index:non-directory-ancestor (git status itself runs and sees typechange)
        code, out = core.reap_git_clean_closed(str(repo))
        self.assertEqual(code, 0)
        self.assertTrue(out.strip())  # git reports typechange/dirty safely

        # Arbitrary non-overflow symlink MUST still raise git-unsafe-index:non-directory-ancestor
        outside = self.tmp / "outside-dir"
        outside.mkdir()
        assets.unlink()
        os.symlink(outside, assets)
        with self.assertRaisesRegex(core.ReapRefused, "^git-unsafe-index:non-directory-ancestor$"):
            core.reap_git_clean_closed(str(repo))


class TrackedEnvTemplatesEverywhere(ScratchHome):
    config_extra = {"scratchRoot": "~/.cache/agent-scratch"}

    def setUp(self):
        super().setUp()
        for patch in (
            mock.patch.object(clean, "clean_running", lambda: set()),
            mock.patch.object(clean, "reap_process_table", lambda: dict(EMPTY_TABLE)),
            mock.patch.object(core, "reap_process_table", lambda: dict(EMPTY_TABLE)),
            mock.patch.object(core, "reap_item_holders", lambda *a, **kw: None),
            mock.patch.object(clean, "reap_holders", lambda *a, **kw: None),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_tracked_env_local_example_allowed_in_worktree_and_scratch_while_untracked_stays_protected(self):
        # 1. Linked worktree with tracked .env.local.example
        repo = self.clone("tpl-app", ignored=("node_modules/",), tracked={"README.md": "hi\n", ".env.local.example": "KEY=\n"})
        self.assertIn(".env.local.example", self.git(repo, "ls-files"))
        wt = self.home / ".codex" / "worktrees" / "tpl-wt"
        wt.parent.mkdir(parents=True, exist_ok=True)
        self.git(repo, "worktree", "add", "-q", "-b", "tpl-branch", str(wt))
        self.age(wt, 10)
        wt_gitdir = repo / ".git" / "worktrees" / "tpl-wt"
        if (wt_gitdir / "logs" / "HEAD").exists():
            (wt_gitdir / "logs" / "HEAD").unlink()
        self.age(wt_gitdir, 10)
        out = self.cli("clean", "--only", "worktrees")
        self.assertEqual(out["summary"]["done"], 1, out)
        self.assertFalse(wt.exists())

        # 2. Linked worktree with UNTRACKED .env.local.example stays protected
        wt2 = self.home / ".codex" / "worktrees" / "tpl-wt-untracked"
        self.git(repo, "worktree", "add", "-q", "-b", "tpl-branch-2", str(wt2))
        (wt2 / ".env.local.example").unlink()
        self.put(wt2, ".env.staging.example", b"UNTRACKED=1\n")
        self.age(wt2, 10)
        wt2_gitdir = repo / ".git" / "worktrees" / "tpl-wt-untracked"
        if (wt2_gitdir / "logs" / "HEAD").exists():
            (wt2_gitdir / "logs" / "HEAD").unlink()
        self.age(wt2_gitdir, 10)
        out2 = self.cli("clean", "--dry-run", "--only", "worktrees")
        item2 = next(i for i in out2["items"] if i["path"].endswith("tpl-wt-untracked"))
        self.assertTrue(item2["keep"].startswith("protected-leaf:"), item2)

        # 3. Scratch repo with tracked .env.local.example passes idle proof and protected scan
        scratch_root = self.home / ".cache" / "agent-scratch"
        scratch_root.mkdir(parents=True, exist_ok=True)
        s_repo = scratch_root / "old-git-task"
        self.git(repo, "worktree", "add", "-q", "-b", "tpl-scratch", str(s_repo))
        self.age(s_repo, 20)
        proof = core.reap_scratch_idle_proof(str(s_repo))
        self.assertTrue(proof["idle"], proof)


class ExpandedReclaimRules(ScratchHome):
    config_extra = {"scratchRoot": "~/.cache/agent-scratch"}

    def setUp(self):
        super().setUp()
        for patch in (
            mock.patch.object(clean, "clean_running", lambda: set()),
            mock.patch.object(clean, "reap_process_table", lambda: dict(EMPTY_TABLE)),
            mock.patch.object(core, "reap_process_table", lambda: dict(EMPTY_TABLE)),
            mock.patch.object(clean, "reap_holders", lambda *a, **kw: None),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_swift_build_cloudflare_opennext_output_reclaimed_when_ignored_and_idle(self):
        swift_repo = self.clone("swift-pkg", ignored=(".build/",), tracked={"Package.swift": "// swift\n"})
        self.put(swift_repo, ".build/debug/App", b"bin")
        self.age(swift_repo, 3)

        web_repo = self.clone("web-pkg", ignored=(".cloudflare/", ".open-next/", ".output/"), tracked={"package.json": "{}\n"})
        self.put(web_repo, ".cloudflare/worker.js", b"js")
        self.put(web_repo, ".open-next/server.js", b"js")
        self.put(web_repo, ".output/server/index.mjs", b"js")
        self.age(web_repo, 3)

        out = self.cli("clean", "--only", "project-dependencies")
        self.assertEqual(out["summary"]["done"], 4, out)
        self.assertFalse((swift_repo / ".build").exists())
        self.assertFalse((web_repo / ".cloudflare").exists())
        self.assertFalse((web_repo / ".open-next").exists())
        self.assertFalse((web_repo / ".output").exists())

    def test_cargo_git_swiftpm_dev_and_darwin_caches(self):
        # Cargo git checkouts & db
        cg = self.put(self.home / ".cargo" / "git" / "checkouts" / "crate-abc", "lib.rs", b"fn x(){}")
        # SwiftPM cache (and verify Library/org.swift.swiftpm security/sdks are untouched)
        spm_cache = self.put(self.home / "Library" / "Caches" / "org.swift.swiftpm" / "repositories", "repo.git", b"x")
        spm_sec = self.put(self.home / "Library" / "org.swift.swiftpm" / "security", "fingerprints.json", b"{}")
        # Dev tool cache (~/.cache/ruff)
        ruff_entry = self.put(self.home / ".cache" / "ruff" / "0.9.0", "cache.db", b"x")
        self.age(self.home / ".cache" / "ruff", 1)
        # Darwin user cache
        darwin_c = self.tmp / "darwin-C"
        clang_cache = self.put(darwin_c / "clang", "ModuleCache/x.pcm", b"x")
        self.age(darwin_c / "clang", 1)

        with mock.patch.object(clean, "CLEAN_FOLDERS", REAL_CLEAN_FOLDERS), \
                mock.patch.object(clean, "clean_darwin_cache_dir", lambda: str(darwin_c)):
            out = self.cli("clean", "--only", "cargo-registry,swiftpm-cache,dev-tool-caches,darwin-user-caches")
        self.assertEqual(out["summary"]["done"], 4, out)
        self.assertFalse(cg.parent.exists())
        self.assertFalse(spm_cache.parent.exists())
        self.assertTrue(spm_sec.exists(), "Library/org.swift.swiftpm/security must never be touched")
        self.assertFalse(ruff_entry.parent.exists())
        self.assertFalse(clang_cache.parent.parent.exists())

    def test_detached_cargo_target_in_recent_scratch_reclaims_debug_and_keeps_release(self):
        task = self.home / ".cache" / "agent-scratch" / "active-work"
        target = task / "custom-cargo-target"
        self.put(target, "debug/deps/lib.rlib", b"x")
        self.put(target, "release/myapp", b"bin")
        self.put(target, "release/incremental/state", b"inc")
        (target / "CACHEDIR.TAG").write_bytes(CARGO_TAG)
        (target / ".rustc_info.json").write_text("{}")
        self.age(target, 1)
        # Parent scratch task has a brand-new note touched now (so whole task is recent)
        self.put(task, "notes.md", b"fresh note\n")

        out = self.cli("clean", "--only", "rust-build-output")
        self.assertEqual(out["summary"]["done"], 2, out)
        self.assertFalse((target / "debug").exists())
        self.assertFalse((target / "release" / "incremental").exists())
        self.assertTrue((target / "release" / "myapp").exists(), "release binary must be preserved")
        self.assertTrue((task / "notes.md").exists())

    def test_compress_logs_gzip_mode_verifies_and_replaces_idle_logs(self):
        if not shutil.which("gzip"):
            self.skipTest("gzip not installed")
        logs = self.home / ".agent-history" / "archives" / "nested"
        payload = (b'{"event":"line","text":"' + b"a" * 200 + b'"}\n') * 1000
        idle_log = self.put(logs, "session.jsonl", payload)
        self.age(logs, 5)
        fresh_log = self.put(logs, "active.jsonl", payload)
        self.write_config({
            "policy": {
                "compressLogsRoots": ["~/.agent-history"],
                "compressLogsIdleDays": 2,
                "compressLogsMode": "gzip",
            }
        })
        with mock.patch.object(clean, "clean_compress_held", lambda p: False):
            out = self.cli("clean", "--only", "compress-logs")
        comp = out["compress"]
        self.assertEqual(comp["mode"], "gzip", comp)
        self.assertEqual(comp["files"], 1, comp)
        self.assertFalse(idle_log.exists())
        gz_path = Path(str(idle_log) + ".gz")
        self.assertTrue(gz_path.exists())
        self.assertEqual(gzip.decompress(gz_path.read_bytes()), payload)
        self.assertTrue(fresh_log.exists(), "fresh log must remain untouched")

    def test_darwin_cache_dir_rejects_arbitrary_tmpdir(self):
        fake_tmp = self.home / "tmp"
        fake_tmp.mkdir(parents=True)
        (self.home / "C").mkdir()
        (self.home / "T").mkdir()
        with mock.patch.object(os, "confstr", side_effect=OSError("no confstr")), \
                mock.patch.dict(os.environ, {"TMPDIR": str(fake_tmp)}):
            self.assertIsNone(REAL_DARWIN_CACHE_DIR())

    def test_scratch_idle_proof_skips_git_when_root_recent_or_no_template_hit(self):
        scratch_root = self.home / ".cache" / "agent-scratch"
        scratch_root.mkdir(parents=True, exist_ok=True)
        fresh = scratch_root / "fresh-task"
        self.put(fresh, ".git/HEAD", b"ref: refs/heads/main\n")
        self.put(fresh, "README.md", b"hi\n")
        with mock.patch.object(core, "reap_repo_templates", side_effect=AssertionError("must not run eagerly")):
            proof = core.reap_scratch_idle_proof(str(fresh))
        self.assertEqual(proof["reason"], "recent-change", proof)

    def test_models_dir_reported_in_inventory_not_deleted(self):
        model = self.put(self.home / "Models" / "demo-model", "weights.bin", b"0" * 4096)
        inv = clean.clean_inventory()
        names = [e["name"] for e in inv if e["kind"] == "model"]
        self.assertIn("demo-model", names, inv)
        self.assertTrue(model.exists())


class OffloadPruneDanglingAndAudit(Home):
    def setUp(self):
        super().setUp()
        self.overflow_vol = self.tmp / "Volumes" / "OverflowDisk"
        self.overflow_root = self.overflow_vol / "Overflow"
        self.overflow_root.mkdir(parents=True)
        self.source = self.home / ".cache" / "jobs"
        self.source.mkdir(parents=True)
        self.write_config({
            "offload": {
                "root": str(self.overflow_root),
                "minFreeGB": 1,
                "sources": [{"path": str(self.source), "children": True}],
            }
        })

    def test_prune_dangling_removes_only_broken_overflow_links_when_volume_mounted(self):
        live_target = self.overflow_root / "home" / ".cache" / "jobs" / "J-0001"
        live_target.mkdir(parents=True)
        missing_target = self.overflow_root / "home" / ".cache" / "jobs" / "J-0002"
        missing_parent_target = self.overflow_root / "home" / ".cache" / "missing-dir" / "J-0003"
        traversal_target = str(self.overflow_root / "home" / ".cache" / "jobs" / ".." / "escaped")
        live_link = self.source / "J-0001"
        broken_link = self.source / "J-0002"
        missing_parent_link = self.source / "J-0003"
        traversal_link = self.source / "J-0004"
        other_link = self.source / "other"
        os.symlink(live_target, live_link)
        os.symlink(missing_target, broken_link)
        os.symlink(missing_parent_target, missing_parent_link)
        os.symlink(traversal_target, traversal_link)
        os.symlink(self.tmp / "nonexistent-elsewhere", other_link)

        # Unmounted volume refuses/skips safely (real volume_ready refuses same-disk temp volume)
        skipped_unmounted = self.cli("offload", "prune-dangling")
        self.assertIn("skipped", skipped_unmounted)
        self.assertTrue(broken_link.is_symlink())

        # Missing README.txt marker on overflow root fails closed even when volume_ready passes
        with mock.patch.object(offload, "volume_ready", lambda r: None):
            uninit = self.cli("offload", "prune-dangling")
        self.assertIn("skipped", uninit)
        self.assertTrue(broken_link.is_symlink())

        # Initialize overflow root marker and re-test
        (self.overflow_root / "README.txt").write_text(offload.README)
        with mock.patch.object(offload, "volume_ready", lambda r: None), \
                mock.patch.object(core, "reap_process_table", lambda: dict(EMPTY_TABLE)), \
                mock.patch.object(core, "reap_holders", lambda *a, **kw: None):
            dry = self.cli("offload", "prune-dangling", "--dry-run")
            self.assertEqual(dry["prunedCount"], 1, dry)
            self.assertTrue(broken_link.is_symlink())

            real = self.cli("offload", "prune-dangling")
            self.assertEqual(real["prunedCount"], 1, real)
            kept_by_why = {k["why"]: k for k in real["kept"]}
            self.assertIn("target-parent-missing", kept_by_why, real)
            self.assertIn("unsafe-link-target", kept_by_why, real)
            self.assertFalse(broken_link.is_symlink())
            self.assertTrue(live_link.is_symlink())
            self.assertTrue(missing_parent_link.is_symlink())
            self.assertTrue(traversal_link.is_symlink())
            self.assertTrue(other_link.is_symlink())

    def test_audit_cli_and_mcp_prunes_protected_leaves_and_reports_hogs(self):
        self.put(self.home / "Models" / "local-weights", "model.bin", b"x" * 65536)
        self.put(self.home / "dev" / "demo" / ".wrangler" / "state", "secret.sqlite", b"do-not-enter")
        out = self.cli("audit", "--min-mb", "0", "--budget", "15", "--top", "20")
        self.assertIn("disk", out)
        self.assertIn("accounting", out)
        self.assertIn("topHogs", out)
        self.assertGreaterEqual(out["accounting"]["prunedProtectedLeaves"], 1, out)
        hog_paths = {h["path"]: h for h in out["topHogs"]}
        self.assertIn("~/Models", hog_paths, hog_paths)
        self.assertEqual(hog_paths["~/Models"]["coverage"], "report-only:model")
        # MCP tool smoke test
        reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "audit", "arguments": {"minMB": 0, "budgetSeconds": 10, "top": 10}}})
        self.assertFalse(reply["result"]["isError"], reply)
        self.assertIn("topHogs", reply["result"]["structuredContent"])
