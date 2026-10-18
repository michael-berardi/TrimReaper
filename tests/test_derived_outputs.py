"""Synthetic exact-output fixtures only; no real caches, builders or user data are touched."""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from unittest import mock

from trimreaper import core
from helpers import Home


class DerivedOutputs(Home):
    def setUp(self):
        super().setUp()
        self.repo = self.clone("app-j0700")
        self.output = self.put(self.repo, "target/debug/app").parent.parent
        self.add_job("J-0700", self.repo)
        self.table = {"files": [], "commands": []}
        self.producers = {}
        self.register(self.repo, self.output, "J-0700", self.repo)
        self.register(self.repo, self.repo / "node_modules", "J-0700", self.repo)
        self.start(mock.patch.object(core, "reap_process_table", lambda: self.table))
        original = core.reap_probe
        self.start(mock.patch.object(core, "reap_probe", side_effect=lambda argv, *a, **kw:
                                    (1, "") if argv[0] == "lsof" else original(argv, *a, **kw)))
        self.start(mock.patch.object(core, "reap_output_ownership", self.ownership, create=True))
        self.start(mock.patch.object(core, "reap_output_registry_present", return_value=True))  # synthetic producer capability

    def start(self, patch):
        value = patch.start()
        self.addCleanup(patch.stop)
        return value

    def register(self, source, output, owner, root):
        # Synthetic producer registry populated independently of guard lookup arguments.
        # All fixture builds use the real canonical project locks; no all-writers=True guard.
        self.producers[(str(source), str(output))] = {
            "protocol": "trimreaper-project-locks-v1", "source": str(source), "output": str(output),
            "owner": owner, "kind": output.name, "claimInventory": "job-meta-paths-v1",
            "projects": core.reap_output_projects(str(source), str(output), str(root))}

    def ownership(self, source, output):
        return self.producers.get((source, output))

    def item(self):
        found = core.reap_output_item("J-0700", str(self.output))
        self.assertIsNotNone(found)
        return found

    def meta(self, job, value):
        with closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET meta=? WHERE id=?", (json.dumps(value), job))

    def detach(self):
        cache = core.reap_cache_root() / "J-0700"
        cache.mkdir(parents=True)
        source = cache / "source"
        self.repo.rename(source)
        self.repo = source
        # The exact target is a sibling, never inferred from arbitrary cache content.
        (source / "target").rename(cache / "target")
        self.output = cache / "target"
        self.register(source, self.output, "J-0700", cache)
        self.put(self.output, core.CACHEDIR_TAG, core.CACHEDIR_SIGNATURE + b"\n# synthetic Cargo cache\n")
        with closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET cwd=? WHERE id='J-0700'", (str(source),))
        return cache

    def test_untagged_detached_target_name_does_not_authorize_data_deletion(self):
        self.detach()
        (self.output / core.CACHEDIR_TAG).unlink()  # isolated synthetic fixture only
        result = core.reap_item(self.item())
        self.assertIn("retention-preserved:unmarked", result["reason"])
        self.assertTrue(self.output.exists())
        self.assertTrue((self.repo / "README.md").exists())

    def test_terminal_detached_target_deleted_source_and_evidence_preserved(self):
        cache = self.detach()
        evidence = self.put(cache, "evidence/report.json", b"keep")
        secret = self.put(self.repo, ".env.local", b"secret")
        result = core.reap_item(self.item())
        self.assertEqual(result["outcome"], "deleted", result)
        self.assertFalse(self.output.exists())
        self.assertTrue((self.repo / "README.md").exists())
        self.assertEqual(secret.read_bytes(), b"secret")
        self.assertEqual(evidence.read_bytes(), b"keep")
        self.assertIn("producer-locks", result["checks"])
        self.assertIn("plan-receipt-durable", result["checks"])

    def test_continuation_and_source_cwd_allow_only_output_under_guard(self):
        self.add_job("J-0701", self.repo, status="running")
        self.meta("J-0701", {"waitingOn": {"job": "J-0700"}})
        with closing(self.state()) as conn, conn:
            conn.execute("INSERT INTO meta(key,value) VALUES(?,?)", ("cleanup:successor:J-0700", json.dumps(
                {"job": "J-0700", "successor": "J-0701"})))
        self.table["files"] = [("8123", "zsh", "cwd", str(self.repo))]
        result = self.cli("reap", "--job", "J-0700")
        self.assertEqual(result["deleted"], 1, result)
        self.assertTrue((self.repo / "README.md").exists())
        self.assertTrue(all(i["class"] == "job-derived-output" for i in result["items"]))
        whole = {"path": str(self.repo), "root": str(self.repo.parent), "repo": str(self.repo),
                 "owner": "J-0700", "jobs": ["J-0700"], "class": "job-clone-root", "kind": "clone",
                 "ownership": "name-pattern", "retention": "disposable"}
        with self.assertRaisesRegex(core.ReapRefused, "inherited-by"):
            core.reap_authority(whole)

    def test_strict_output_refusal_retries_only_typed_route(self):
        self.table["files"] = [("8123", "zsh", "cwd", str(self.repo))]
        with core.db() as conn:
            job = core.row(conn, "SELECT * FROM jobs WHERE id='J-0700'")
        items, _ = core.reap_job_items(job, core.reap_clone_roots())
        item = next(i for i in items if i["path"] == str(self.output))
        result = core.reap_item(item)
        self.assertEqual(result["outcome"], "deleted", result)
        self.assertEqual(result["class"], "job-derived-output")

    def test_live_exact_and_descendant_declarations_block(self):
        self.add_job("J-0701", self.repo, status="running")
        for key, path in (("paths", self.output), ("paths", self.output / "debug"), ("paths", self.repo),
                          ("worktrees", self.output), ("worktrees", self.output / "debug"),
                          ("outputPaths", self.output), ("outputs", self.output / "debug"),
                          ("outputPaths", self.repo)):
            with self.subTest(key=key, path=path):
                self.meta("J-0701", {key: [str(path)]})
                result = core.reap_item(self.item())
                self.assertEqual(result["outcome"], "refused", result)
                self.assertIn("output-claimed-by", result["reason"])
                self.assertTrue(self.output.exists())

    def test_exact_live_cwd_claim_blocks(self):
        self.add_job("J-0701", self.output / "debug", status="running")
        result = core.reap_item(self.item())
        self.assertIn("output-claimed-by", result["reason"])

    def test_output_open_file_and_source_non_cwd_handle_block(self):
        for path, fd in ((self.output / "debug/app", "3r"), (self.output, "cwd"),
                         (self.repo / "README.md", "4r")):
            with self.subTest(path=path):
                self.table["files"] = [("8123", "reader", fd, str(path))]
                result = core.reap_item(self.item())
                self.assertEqual(result["outcome"], "refused", result)
                self.assertIn("in-use:", result["reason"])

    def test_running_builders_including_unattributed_relative_build_block(self):
        for command in ("cargo build", "node build.js", "npm install", "ninja -C relative"):
            with self.subTest(command=command):
                self.table["commands"] = [("8123", command)]
                result = core.reap_item(self.item())
                self.assertIn("in-use:output-command", result["reason"])
                self.assertTrue(self.output.exists())

    def test_missing_interface_unregistered_writer_and_bare_true_preserve(self):
        for value, reason in ((None, "host-ownership-interface-required"),
                              (lambda *args: None, "ownership-record-required"),
                              (lambda *args: True, "ownership-record-required")):
            with self.subTest(reason=reason), mock.patch.object(core, "reap_output_ownership", value):
                result = core.reap_item(self.item())
            self.assertIn(reason, result["reason"])
        @contextmanager
        def false_guard(*args):
            yield True
        with mock.patch.object(core, "reap_output_producer_guard", false_guard):
            result = core.reap_item(self.item())
        self.assertIn("ownership-record-mismatch", result["reason"])
        self.assertTrue(self.output.exists())

    def test_producer_and_cargo_locks_block(self):
        cargo = self.put(self.output, "debug/.cargo-lock", b"")
        queue = core.reap_queue_lock(str(self.repo))
        queue.parent.mkdir(parents=True, exist_ok=True)
        queue.touch()
        for path in (queue, cargo):
            with self.subTest(path=path), path.open("r+") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = core.reap_item(self.item())
                self.assertIn("lock-busy:", result["reason"])
                self.assertTrue(self.output.exists())

    def test_locks_and_guard_held_across_git_and_removal(self):
        cargo = self.put(self.output, "debug/.cargo-lock", b"")
        original_git, original_remove = core.reap_git_proof, core.reap_remove_and_verify
        def assert_held():
            for path in (core.reap_queue_lock(str(self.repo)), cargo):
                with path.open("r+") as stream:
                    with self.assertRaises(OSError):
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def git(*args, **kwargs):
            assert_held()
            return original_git(*args, **kwargs)
        def remove(*args, **kwargs):
            assert_held()
            return original_remove(*args, **kwargs)
        with mock.patch.object(core, "reap_git_proof", git), mock.patch.object(core, "reap_remove_and_verify", remove):
            result = core.reap_item(self.item())
        self.assertEqual(result["outcome"], "deleted", result)

    def test_protected_or_retained_output_never_removed(self):
        for rel in ("debug/.env.local", "debug/.wrangler/state", ".ut-retention-customer"):
            path = self.put(self.output, rel)
            result = core.reap_item(self.item())
            self.assertNotEqual(result["outcome"], "deleted", result)
            self.assertTrue(path.exists())
            path.unlink()  # isolated synthetic fixture only

    def test_retained_source_and_release_checkout_block_typed_route(self):
        marker = self.put(self.repo, ".ut-retention-release-runtime-rollback", b"")
        result = core.reap_item(self.item())
        self.assertIn("retention-preserved:", result["reason"])
        marker.unlink()  # isolated synthetic fixture only
        item = self.item()
        self.write_config({"releaseRoot": str(self.repo)})
        result = core.reap_item(item)
        self.assertIn("output-source-ambiguous", result["reason"])
        self.assertTrue(self.output.exists())

    def test_unsafe_source_and_ambiguous_detached_mapping_preserve(self):
        cache = self.detach()
        second = cache / "second-source"
        second.mkdir()
        (second / ".git").mkdir()
        self.meta("J-0700", {"worktrees": [str(second)]})
        self.assertIsNone(core.reap_output_item("J-0700", str(self.output)))
        self.meta("J-0700", {})
        item = self.item()
        source = self.repo
        source.rename(cache / "moved-source")
        source.symlink_to(cache / "moved-source", target_is_directory=True)
        result = core.reap_item(item)
        self.assertNotEqual(result["outcome"], "deleted", result)
        self.assertTrue(self.output.exists())

    def test_failed_process_probes_preserve(self):
        for reason in ("probe-failed:lsof-scan:127", "probe-failed:ps:127"):
            with self.subTest(reason=reason), mock.patch.object(core, "reap_process_table", side_effect=core.ReapRefused(reason)):
                result = core.reap_item(self.item())
                self.assertEqual(result["reason"], reason)
        self.assertTrue(self.output.exists())

    def test_interrupted_exact_output_discovery_without_source_expiry(self):
        self.cli("job", "status", "J-0700", "interrupted")
        self.meta("J-0700", {"interrupted": {"at": time.time() - 3600}})
        result = self.cli("reap", "--job", "J-0700")
        self.assertEqual(result["deleted"], 1, result)
        self.assertTrue(self.repo.exists())
        self.assertEqual(core.REAP_TERMINAL, ("done", "failed", "cancelled"))

    def test_interrupted_does_not_enable_evidence_or_source_expiry(self):
        self.cli("job", "status", "J-0700", "interrupted")
        self.meta("J-0700", {"interrupted": {"at": time.time() - 3600}})
        with self.assertRaisesRegex(core.HostError, "job-interrupted"):
            core.tool_reap({"job": "J-0700", "expireEvidence": True, "dryRun": True})
        self.assertTrue(self.repo.exists())
        self.assertTrue(self.output.exists())

    def test_malformed_live_claim_inventory_preserves(self):
        self.add_job("J-0701", self.repo, status="running")
        for value in ({"worktrees": "not-a-list"}, {"outputPaths": ["../target"]},
                      {"paths": {"cwd": str(self.repo)}}, {"paths": ["relative"]}, {"paths": [None]}):
            self.meta("J-0701", value)
            result = core.reap_item(self.item())
            self.assertIn("output-declarations-unverified", result["reason"])
        self.assertTrue(self.output.exists())

    def test_interrupted_without_terminal_timestamp_preserves(self):
        self.cli("job", "status", "J-0700", "interrupted")
        result = core.reap_item(self.item())
        self.assertIn("terminal-time-unverified", result["reason"])
        self.assertTrue(self.output.exists())

    def test_largest_fully_sized_disposable_outputs_first(self):
        small = self.item()
        node = self.put(self.repo, "node_modules/pkg/index.js", b"n" * 32768).parent.parent
        large = core.reap_output_item("J-0700", str(node))
        self.assertIsNotNone(large)
        ordered = core.reap_output_order([small, large])
        self.assertEqual([i["path"] for i in ordered], [str(node), str(self.output)])
        with mock.patch.object(core, "REAP_OUTPUT_SCHEDULE_ITEMS", 1):
            ordered = core.reap_output_order([small, large])
        self.assertEqual(ordered[0]["path"], str(self.output), "unverified sizes get no priority")

    def test_cross_job_largest_verified_output_first(self):
        repo = self.clone("app-j0702")
        self.put(repo, "target/debug/app", b"n" * 32768)
        self.add_job("J-0702", repo)
        with core.db() as conn:
            jobs = core.rows(conn, "SELECT * FROM jobs ORDER BY id")
        self.register(repo, repo / "target", "J-0702", repo)
        ordered = core.reap_output_job_order(jobs, core.reap_clone_roots())
        self.assertEqual([j["id"] for j in ordered], ["J-0702", "J-0700"])

    def test_blocked_large_output_gets_no_scheduling_priority(self):
        small = self.item()
        node = self.put(self.repo, "node_modules/pkg/index.js", b"n" * 32768).parent.parent
        large = core.reap_output_item("J-0700", str(node))
        self.table["files"] = [("8123", "reader", "3r", str(node / "pkg/index.js"))]
        ordered = core.reap_output_order([small, large])
        # The source handle also conservatively blocks small; neither gains a verified size priority.
        self.assertEqual(ordered[0]["path"], str(self.output))

    def test_release_keep_exact_output_and_terminal_host_unknown_preserve(self):
        with closing(self.state()) as conn, conn:
            conn.execute("INSERT INTO meta(key,value) VALUES(?,?)", ("cleanup:released:J-0700", json.dumps(
                {"keep": [str(self.output)]})))
        result = core.reap_item(self.item())
        self.assertIn("kept-by-release:", result["reason"])
        with closing(self.state()) as conn, conn:
            conn.execute("DELETE FROM meta WHERE key='cleanup:released:J-0700'")
        with mock.patch.object(core, "reap_live_ids", return_value=None):
            result = core.reap_item(self.item())
        self.assertEqual(result["reason"], "terminal-state-unknown")
        self.assertTrue(self.output.exists())

    def test_failed_and_cancelled_are_terminal_for_exact_output(self):
        for status in ("failed", "cancelled"):
            with self.subTest(status=status):
                self.cli("job", "status", "J-0700", status)
                with closing(self.state()) as conn, conn:
                    conn.execute("UPDATE jobs SET finished_at=? WHERE id='J-0700'", (time.time() - 3600,))
                self.put(self.output, "debug/app")
                result = core.reap_item(self.item())
                self.assertEqual(result["outcome"], "deleted", result)

    def test_authority_change_after_sizing_refuses(self):
        self.add_job("J-0701", self.repo, status="running")
        original = core.reap_metadata_size
        def size(*args, **kwargs):
            result = original(*args, **kwargs)
            self.meta("J-0701", {"outputPaths": [str(self.output)]})
            return result
        with mock.patch.object(core, "reap_metadata_size", size):
            result = core.reap_item(self.item())
        self.assertIn("output-claimed-by", result["reason"])
        self.assertTrue(self.output.exists())

    def test_interrupted_zoned_iso_and_invalid_clocks(self):
        self.cli("job", "status", "J-0700", "interrupted")
        old = datetime.fromtimestamp(time.time() - 3600, timezone.utc).isoformat()
        for value in (old, old.replace("+00:00", "Z")):
            self.meta("J-0700", {"interrupted": {"at": value}})
            result = core.reap_item(self.item(), "dry-run")
            self.assertEqual(result["outcome"], "would-delete", result)
        for value in (old.replace("+00:00", ""), "yesterday", "1234567890", True,
                      "2025-02-30T00:00:00Z", float("nan"), float("inf"), time.time() + 3600):
            with self.subTest(value=value):
                self.meta("J-0700", {"interrupted": {"at": value}})
                result = core.reap_item(self.item(), "dry-run")
                self.assertIn("terminal-time-unverified", result["reason"])
        self.assertNotIn("interrupted", core.REAP_TERMINAL)

    def test_host_paths_identify_source_but_do_not_replace_producer_proof(self):
        self.detach()
        with closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET cwd=NULL WHERE id='J-0700'")
        self.meta("J-0700", {"paths": [str(self.repo)]})
        self.assertEqual(core.reap_output_item("J-0700", str(self.output))["source"], str(self.repo))
        with mock.patch.object(core, "reap_output_ownership", None):
            self.assertIn("host-ownership-interface-required", core.reap_item(self.item())["reason"])

    def test_registered_node_control_agent_not_generic_node_is_exempt(self):
        node, agent = "/synthetic/bin/node", "/synthetic/pi/cli.js"
        self.table["commands"] = [("8123", node + " " + agent + " --control")]
        self.table["files"] = [("8123", "node", "txt", node), ("8123", "node", "cwd", "/synthetic/control")]
        with mock.patch.object(core, "REAP_OUTPUT_CONTROL_AGENTS", ((node, agent),), create=True):
            result = core.reap_item(self.item(), "dry-run")
            self.assertEqual(result["outcome"], "would-delete", result)
            for files in ([self.table["files"][0]],
                          [("8123", "node", "txt", node), ("8123", "node", "cwd", str(self.repo))],
                          self.table["files"] + [("8123", "node", "4r", str(self.repo / "README.md"))]):
                self.table["files"] = files
                result = core.reap_item(self.item(), "dry-run")
                self.assertIn("in-use:", result["reason"])
        self.table["files"] = [("8123", "node", "txt", node), ("8123", "node", "cwd", "/synthetic/control")]
        result = core.reap_item(self.item(), "dry-run")
        self.assertIn("output-command:", result["reason"])

    def test_release_profile_requires_explicit_output_disposable_marker(self):
        self.put(self.output, "aarch64-apple-darwin/release/app", b"signed-or-compiled")
        item = self.item()
        self.assertEqual(item["retention"], "release-runtime-rollback")
        result = core.reap_item(item)
        self.assertEqual(result["outcome"], "refused", result)
        self.put(self.output, ".ut-retention-disposable", b"")
        result = core.reap_item(self.item())
        self.assertEqual(result["outcome"], "deleted", result)

    def test_detached_cachedir_tag_does_not_dispose_release_profile(self):
        self.detach()
        self.put(self.output, "release/bundle/Customer.app/Contents/MacOS/app", b"retain")
        result = core.reap_item(self.item())
        self.assertIn("release-runtime-rollback", result["reason"])
        self.assertTrue(self.output.exists())

    def test_ownership_record_must_match_and_remain_stable(self):
        original = self.ownership(str(self.repo), str(self.output))
        for key, value in (("owner", "J-9999"), ("projects", []), ("claimInventory", "unknown")):
            with self.subTest(key=key), mock.patch.object(core, "reap_output_ownership", return_value={**original, key: value}):
                result = core.reap_item(self.item())
                self.assertIn("ownership-record-mismatch", result["reason"])
        sizes = core.reap_metadata_size
        def size(*args, **kwargs):
            result = sizes(*args, **kwargs)
            self.producers[(str(self.repo), str(self.output))] = {**original, "owner": "J-9999"}
            return result
        with mock.patch.object(core, "reap_metadata_size", size):
            result = core.reap_item(self.item())
        self.assertIn("ownership-record-mismatch", result["reason"])
        self.assertTrue(self.output.exists())

    def test_interrupted_recent_clock_overrides_stale_finished_at(self):
        self.cli("job", "status", "J-0700", "interrupted")
        with closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET finished_at=? WHERE id='J-0700'", (time.time() - 86400,))
        for meta in ({"interrupted": {}}, {"interrupted": {"at": datetime.now(timezone.utc).isoformat()}}):
            self.meta("J-0700", meta)
            result = core.reap_item(self.item(), "dry-run")
            self.assertIn("terminal-time-unverified", result["reason"])

    def test_host_redrive_numeric_finished_at_proves_interrupted_output_only(self):
        self.cli("job", "status", "J-0700", "interrupted")
        self.meta("J-0700", {"resume": {"interrupted": True, "at": time.time() - 3600}})
        stamp = time.time() - 3600
        with closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET finished_at=?,started_at=? WHERE id='J-0700'", (stamp, stamp - 60))
            conn.execute("INSERT INTO events(ts,kind,subject,actor,data) VALUES(?,?,?,?,?)",
                         (stamp, 'job.interrupted', 'J-0700', 'host', '{}'))
        result = core.reap_item(self.item(), "dry-run")
        self.assertEqual(result["outcome"], "would-delete", result)
        self.assertNotIn("interrupted", core.REAP_TERMINAL)

    def test_release_target_cannot_be_bypassed_by_enclosing_clone_scan(self):
        self.put(self.output, "release/app", b"retain")
        with self.assertRaisesRegex(core.ReapRefused, "retention-preserved:release-runtime-rollback"):
            core.reap_protected_scan(str(self.repo), allow_git=True, markers=True, nested_disposable=True)
        self.put(self.output, ".ut-retention-disposable", b"")
        core.reap_protected_scan(str(self.repo), allow_git=True, markers=True, nested_disposable=True)
