"""The engine's escalation hooks: shorter windows for one rung, pins released by finished or long-interrupted jobs, ACLs named."""
from __future__ import annotations

import json
import subprocess
import sys
import time
import unittest
from unittest import mock

from helpers import DAY, Home

from trimreaper import clean, core, host

DARWIN = sys.platform == "darwin"


class Windows(Home):
    def policy(self, **values):
        self.write_config({"policy": values})
        return host.load_policy()

    def test_without_a_rung_the_policy_is_the_hosts(self):
        self.policy(evidenceExpiryDays=14, cloneExpiryDays=3)
        self.assertEqual(core.reap_policy(), host.load_policy())

    def test_a_rung_only_shortens_a_window(self):
        self.policy(evidenceExpiryDays=14, cloneExpiryDays=3)
        with core.reap_escalated({"evidenceExpiryDays": 1, "cloneExpiryDays": 5}):
            policy = core.reap_policy()
        self.assertEqual((policy["evidenceExpiryDays"], policy["cloneExpiryDays"]), (1, 3), "5 days would lengthen the 3-day clone window")
        self.assertEqual(core.reap_policy()["evidenceExpiryDays"], 14, "the override ends with the rung")

    def test_a_window_the_operator_turned_off_stays_off(self):
        self.policy(evidenceExpiryDays=0, cloneExpiryDays=0)
        with core.reap_escalated({"evidenceExpiryDays": 1, "cloneExpiryDays": 1}):
            policy = core.reap_policy()
        self.assertEqual((policy["evidenceExpiryDays"], policy["cloneExpiryDays"]), (0, 0))

    def test_only_the_job_windows_can_be_shortened(self):
        self.policy()
        with core.reap_escalated({"cloneArchive": "keep", "archiveExpiryDays": 1, "cloneProtectedCopies": "remove", "autoReap": False}):
            policy = core.reap_policy()
        self.assertEqual({k: policy[k] for k in ("cloneArchive", "archiveExpiryDays", "cloneProtectedCopies", "autoReap")},
                         {k: host.load_policy()[k] for k in ("cloneArchive", "archiveExpiryDays", "cloneProtectedCopies", "autoReap")})

    def test_a_bad_override_changes_nothing(self):
        self.policy(evidenceExpiryDays=14)
        for bad in (True, 0, -1, 1.5, "1", None):
            with core.reap_escalated({"evidenceExpiryDays": bad}):
                self.assertEqual(core.reap_policy()["evidenceExpiryDays"], 14, bad)

    def test_a_rung_that_keeps_unsaved_work_turns_archiving_off_and_never_on(self):
        self.policy(cloneArchive="archive")
        with core.reap_escalated({}, None, True):
            self.assertEqual(core.reap_policy()["cloneArchive"], "keep")
        self.assertEqual(core.reap_policy()["cloneArchive"], "archive")
        self.policy(cloneArchive="keep")
        with core.reap_escalated({"cloneExpiryDays": 1}, None, False):
            self.assertEqual(core.reap_policy()["cloneArchive"], "keep")

    def test_nested_rungs_restore_the_outer_one(self):
        self.policy(evidenceExpiryDays=14)
        with core.reap_escalated({"evidenceExpiryDays": 5}):
            with core.reap_escalated({"evidenceExpiryDays": 2}):
                self.assertEqual(core.reap_policy()["evidenceExpiryDays"], 2)
            self.assertEqual(core.reap_policy()["evidenceExpiryDays"], 5)


class Pins(Home):
    def job(self, name, status, meta=None):
        self.add_job(name, status="running")
        stamp = time.time()
        with self.state() as conn, conn:
            conn.execute("UPDATE jobs SET status=?, meta=?, finished_at=? WHERE id=?",
                         (status, json.dumps(meta) if meta else None, stamp if status in core.REAP_TERMINAL else None, name))
        return {"id": name, "status": status, "meta": json.dumps(meta) if meta else None}

    def released(self, job, **rung):
        with host.db() as conn:
            if rung:
                with core.reap_escalated({}, rung.get("hours")):
                    return core.reap_pin_released(conn, job)
            return core.reap_pin_released(conn, job)

    def test_finished_jobs_release_a_pin_always(self):
        for status in core.REAP_TERMINAL:
            self.assertTrue(self.released(self.job("J-5000", status)) if status == "done" else self.released({"status": status}))

    def test_interrupted_jobs_release_a_pin_only_in_a_rung_and_only_when_the_interruption_is_old_and_proved(self):
        old = {"interrupted": {"at": time.time() - 30 * 3600}}
        job = self.job("J-5001", "interrupted", old)
        self.assertFalse(self.released(job), "outside a rung an interrupted job keeps what it waits on")
        self.assertTrue(self.released(job, hours=24))
        self.assertFalse(self.released(job, hours=48), "not yet two days")
        recent = self.job("J-5002", "interrupted", {"interrupted": {"at": time.time() - 3600}})
        self.assertFalse(self.released(recent, hours=24))
        unproved = self.job("J-5003", "interrupted")
        self.assertFalse(self.released(unproved, hours=24), "no interruption clock: the pin stays")

    def test_live_and_waiting_jobs_never_release_a_pin(self):
        for status in ("queued", "approved", "running", "blocked", "parked"):
            job = self.job("J-5100", status) if status == "queued" else {"status": status}
            self.assertFalse(self.released(job, hours=1), status)

    def test_the_engine_blocker_honours_the_release(self):
        producer = self.job("J-5200", "done")
        waiter = self.job("J-5201", "interrupted", {"interrupted": {"at": time.time() - 30 * 3600}, "waitingOn": {"job": "J-5200"}})
        with host.db() as conn:
            blocked = core.reap_job_blocker(conn, {**producer, "terminal_id": None, "finished_at": time.time() - 3 * DAY}, set())
            self.assertEqual(blocked, "awaited-by:J-5201(interrupted)")
            with core.reap_escalated({}, 24):
                self.assertIsNone(core.reap_job_blocker(conn, {**producer, "terminal_id": None, "finished_at": time.time() - 3 * DAY}, set()))


@unittest.skipUnless(DARWIN, "ACLs are a macOS filesystem feature")
class Acl(Home):
    def test_the_first_entry_whose_acl_denies_delete_is_named_relative_to_the_folder(self):
        folder = self.dev / "tree"
        (folder / "a" / "b").mkdir(parents=True)
        (folder / "plain").mkdir()
        self.assertIsNone(core.reap_acl_denies(str(folder)))
        subprocess.run(["chmod", "+a", "everyone deny delete", str(folder / "a" / "b")], check=True)
        self.addCleanup(lambda: subprocess.run(["chmod", "-RN", str(folder)], capture_output=True))
        self.assertEqual(core.reap_acl_denies(str(folder)), "a/b")

    def test_an_acl_that_only_allows_is_not_a_block(self):
        folder = self.dev / "allowed"
        folder.mkdir()
        subprocess.run(["chmod", "+a", "everyone allow list", str(folder)], check=True)
        self.addCleanup(lambda: subprocess.run(["chmod", "-RN", str(folder)], capture_output=True))
        self.assertIsNone(core.reap_acl_denies(str(folder)))

    def test_the_folder_itself_is_named_dot(self):
        folder = self.dev / "root-acl"
        folder.mkdir()
        subprocess.run(["chmod", "+a", "everyone deny delete", str(folder)], check=True)
        self.addCleanup(lambda: subprocess.run(["chmod", "-RN", str(folder)], capture_output=True))
        self.assertEqual(core.reap_acl_denies(str(folder)), ".")

    def test_an_unlistable_folder_finds_nothing(self):
        self.assertIsNone(core.reap_acl_denies(str(self.tmp / "missing")))


class NotMacos(Home):
    def test_a_platform_without_acls_finds_none(self):
        with mock.patch.object(core.os, "uname", return_value=mock.Mock(sysname="Linux")):
            self.assertIsNone(core.reap_acl_denies(str(self.dev)))


class Policy(unittest.TestCase):
    def test_the_ladder_keys_are_validated(self):
        host.check_policy({"cleanEscalate": False, "cleanEscalateRepoHours": 1, "cleanSteadyHours": 0})
        for bad in ({"cleanEscalate": "yes"}, {"cleanEscalateRepoHours": 0}, {"cleanEscalateWorktreeHours": True},
                    {"cleanEscalateJobHours": "24"}, {"cleanEscalatePinnedHours": -1}, {"cleanSteadyHours": -1},
                    {"cleanSteadyRepoDays": 0}, {"cleanSteadyJobHours": 1.5}, {"cleanSteadyRepoDays": 400}):
            with self.subTest(bad=bad), self.assertRaises(host.HostError):
                host.check_policy(bad)

    def test_a_host_whose_policy_predates_the_keys_gets_the_shipped_defaults(self):
        policy = {"cleanAtPercent": 85, "cleanToPercent": 80}
        self.assertEqual(clean.clean_ladder(policy, None), [0, 1, 2, 3, 4, 5])
        self.assertEqual(clean.clean_ladder({**policy, "cleanEscalate": False}, None), [0])
        self.assertEqual(clean.clean_ladder(policy, False), [0])
        profile = clean.clean_profile(policy, 1)
        self.assertEqual((profile["projectHours"], profile["temp"]), (2, {"shared": 6, "user": 6}))

    def test_every_rung_widens_the_one_before(self):
        profiles = [clean.clean_profile({}, rung) for rung in range(6)]
        self.assertEqual([p["name"] for p in profiles], list(clean.CLEAN_RUNGS))
        hours = [p["projectHours"] for p in profiles]
        self.assertEqual(hours, sorted(hours, reverse=True))
        worktree = [p["worktreeHours"] for p in profiles]
        self.assertEqual(worktree, sorted(worktree, reverse=True))
        self.assertEqual([p["archive"] for p in profiles], [False] * 5 + [True])
        self.assertEqual([p["job"] is not None for p in profiles], [False, False, True, True, True, True])
        self.assertEqual([p["job"]["filter"] for p in profiles[2:]], ["regenerable", "regenerable", "all", "all"])
        self.assertEqual([bool(p["job"]["pinned"]) for p in profiles[2:]], [False, True, True, True])
        self.assertEqual([p["job"]["clones"] for p in profiles[2:]], [True] * 4)
        self.assertEqual([p["job"]["keepUnsaved"] for p in profiles[2:]], [True, True, True, False], "only the last rung archives")

    def test_an_operator_who_turned_a_window_off_keeps_it_off_on_every_rung(self):
        for rung in range(6):
            profile = clean.clean_profile({"worktreeIdleDays": 0, "projectIdleHours": 0}, rung)
            self.assertEqual((profile["worktreeHours"], profile["projectHours"]), (0, 0), rung)
