"""0.20.2 second-review fixes (R3): the ignored-ancestor fence for every pressure kind, a nested clone or linked worktree inside a candidate dir,
a pin released beside another reason to keep the job, git failing closed, and the audit's wall-clock bounds. Synthetic temporary homes only."""
from __future__ import annotations

import contextlib
import subprocess
import time
from pathlib import Path
from unittest import mock

from trimreaper import core

from helpers import Home
from test_pressure_cache_worktrees import CacheBase, IGNORE, TAG
from test_scratch_regen import ScratchBase

KINDS = ("dist", "target", ".next")
NEEDS = ("ignored", "untracked")


def derived(root: Path, base: str, kind: str, put) -> None:
    put(root, f"{base}/package.json" if base else "package.json", b"{}")
    put(root, f"{base}/{kind}/dep/index.js" if base else f"{kind}/dep/index.js")
    if kind == "target":
        put(root, f"{base}/{kind}/CACHEDIR.TAG" if base else f"{kind}/CACHEDIR.TAG", TAG)


class IgnoredAncestorFence(CacheBase):
    def test_every_pressure_kind_below_an_ignored_ancestor_is_kept_in_a_cache_worktree(self):
        for number, kind in enumerate(KINDS):
            job = f"J-09{number}1"
            self.job(job)
            root = self.worktree(job, ignore=IGNORE + (".scratch/", ".next/"), outputs=())
            derived(root, ".scratch/pkg", kind, self.put)
            derived(root, "pkg", kind, self.put)                              # control: no ignored ancestor
            with self.assertRaises(core.ReapRefused) as refused:
                core.reap_git_proof(str(root), str(root / ".scratch/pkg" / kind), NEEDS)
            self.assertEqual(str(refused.exception), "ignored-ancestor", kind)
            self.assertIn("git-no-ignored-ancestor", core.reap_git_proof(str(root), str(root / "pkg" / kind), NEEDS), kind)
            self.pressure_tick()
            self.assertFalse((root / "pkg" / kind).exists(), f"control {kind}")
            self.assertTrue((root / ".scratch/pkg" / kind).exists(), kind)


class IgnoredAncestorFenceScratch(ScratchBase):
    def test_every_pressure_kind_below_an_ignored_ancestor_is_kept_in_an_agent_scratch_checkout(self):
        self.quiet()
        for kind in KINDS:
            root = self.checkout(f"anc/k{kind.strip('.')}x", outputs=())
            (root / ".gitignore").write_text((root / ".gitignore").read_text() + ".scratch/\n")
            self.git(root, "commit", "-qam", "ignore scratch"); self.git(root, "push", "-q")
            derived(root, ".scratch/pkg", kind, self.put)
            derived(root, "pkg", kind, self.put)
            with self.assertRaises(core.ReapRefused) as refused:
                core.reap_git_proof(str(root), str(root / ".scratch/pkg" / kind), NEEDS)
            self.assertEqual(str(refused.exception), "ignored-ancestor", kind)
            self.assertIn("git-no-ignored-ancestor", core.reap_git_proof(str(root), str(root / "pkg" / kind), NEEDS), kind)
            out = self.regen()
            self.assertFalse((root / "pkg" / kind).exists(), f"control {kind}: {out['items']}")
            self.assertTrue((root / ".scratch/pkg" / kind).exists(), kind)


INSIDE = ("app.vsix", "tool.jar", "a.tar.xz", "a.tar.bz2", "a.tar.zst", "a.7z", "a.snap", "a.flatpak", "a.nupkg", "a.crx", "a.xpi", "a.blockmap",
          "latest-linux-arm64.yml", "beta-mac.yml", "mystery.dat", "data.bin", "LICENSE", "installer", "notes.md", "feed.yaml", "app.AppImage",
          "mac/App.app/Contents/Info.plist", "deep/er/er/pack.rar", "a.js.gz")
BESIDE = ("app.vsix", "a.7z", "a.tar.zst", "latest-linux-arm64.yml", "beta-mac.yml", "a.blockmap", "a.nupkg", "a.snap", "App.app/Contents/Info.plist", "a.zip")
WEB = ("index.html", "assets/app-1a2b.js", "assets/app.css", "assets/app.js.map", "assets/logo.svg", "assets/f.woff2", "types/index.d.ts", "data.json",
       "mod.wasm", "favicon.ico", "robots.txt", "site.webmanifest")


class DistAllowlist(CacheBase):
    def test_a_dist_goes_only_when_everything_in_it_is_web_output_and_nothing_release_shaped_is_in_or_beside_it(self):
        self.job("J-0921")
        plain = self.worktree("J-0921", name="plain", outputs=("dist",))
        for rel in WEB:
            self.put(plain, f"dist/{rel}", b"w")                                  # control: every recognised web type, nested
        kept = {}
        for label, rel in [(f"in-{n}", f"dist/{n}") for n in INSIDE] + [(f"beside-{n}", n) for n in BESIDE]:
            kept[label] = self.worktree("J-0921", name=label.replace("/", "_").replace(".", "_"), outputs=("dist",))
            self.put(kept[label], rel, b"artifact")
        self.pressure_tick()
        self.assertFalse((plain / "dist").exists(), "control: web output only goes")
        for label, root in kept.items():
            self.assertTrue((root / "dist/sub/build.js").exists(), label)

    def test_a_link_inside_dist_keeps_it(self):
        self.job("J-0922")
        root = self.worktree("J-0922", outputs=("dist",))
        (root / "dist" / "elsewhere.js").symlink_to(root / "README.md")
        self.pressure_tick()
        self.assertTrue((root / "dist/sub/build.js").exists())
        self.assertEqual(core.reap_dist_keep_reason(str(root / "dist")), "dist-not-web-output")

    def test_the_audit_names_the_reason(self):
        self.job("J-0923")
        root = self.worktree("J-0923", outputs=("dist",))
        self.put(root, "dist/a.vsix", b"x")
        entry = self.audit("J-0923")[str(root / "dist")]
        self.assertEqual(entry["state"], "blocked")
        self.assertEqual(core.reap_dist_keep_reason(str(root / "dist")), "dist-release-bearing")


class DistAllowlistScratch(ScratchBase):
    def test_scratch_dist_follows_the_same_allowlist(self):
        self.quiet()
        plain = self.checkout("al/plain", outputs=("dist",))
        for rel in WEB:
            self.put(plain, f"dist/{rel}", b"w")
        kept = {}
        for label, rel in [("in-vsix", "dist/a.vsix"), ("in-yml", "dist/latest-linux-arm64.yml"), ("in-dat", "dist/x.dat"), ("beside-xz", "a.tar.xz"),
                           ("beside-beta", "beta-mac.yml")]:
            kept[label] = self.checkout(f"al/{label}", outputs=("dist",))
            self.put(kept[label], rel, b"artifact")
        self.regen()
        self.assertFalse((plain / "dist").exists())
        for label, root in kept.items():
            self.assertTrue((root / "dist/sub/file.js").exists(), label)


class DisposableMarkerKeepsFences(CacheBase):
    """m7: a disposable marker on the derived dir decides retention, but never lifts the structural fences of the pressure branches."""

    def test_a_disposable_marker_does_not_release_a_release_bearing_dist_or_an_unrecognised_target(self):
        self.job("J-0931")
        dist = self.worktree("J-0931", name="d", outputs=("dist",))
        self.put(dist, "dist/a.vsix", b"x")
        self.put(dist, "dist/.ut-retention-disposable", b"")
        target = self.worktree("J-0931", name="t", outputs=())
        self.put(target, "target/classes/A.class")
        self.put(target, "target/.ut-retention-disposable", b"")
        self.put(target, "package.json", b"{}")
        self.pressure_tick()
        self.assertTrue((dist / "dist/a.vsix").exists())
        self.assertTrue((target / "target/classes/A.class").exists())


class NestedRepoInsideACandidate(CacheBase):
    """M2: a `dist` that is a linked worktree (gh-pages) with work in it, and a clone at node_modules/pkg/.git, are kept."""

    def linked_dist(self, root: Path, dirty: bool, unpushed: bool):
        main = self.clone(f"pages-{root.parent.name.lower()}-{root.name}", ignored=("node_modules/", "dist/"))
        self.git(main, "branch", "gh-pages")
        subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(root / "dist"), "gh-pages"], check=True, capture_output=True)
        if unpushed:
            (root / "dist" / "page.html").write_text("local\n")
            self.git(root / "dist", "add", "page.html")
            self.git(root / "dist", "commit", "-qm", "local only")
        if dirty:
            (root / "dist" / "wip.js").write_text("uncommitted\n")

    def test_a_dist_that_is_a_linked_worktree_with_work_in_it_is_kept(self):
        for number, (dirty, unpushed) in enumerate(((True, False), (False, True), (False, False))):
            job = f"J-095{number}"
            self.job(job)
            root = self.worktree(job, outputs=())
            self.linked_dist(root, dirty, unpushed)
            self.pressure_tick()
            self.assertTrue((root / "dist" / ".git").exists(), (dirty, unpushed))      # the clean, pushed one too: a repository is not build output
            self.assertTrue((root / "dist" / "README.md").exists())
            if dirty:
                self.assertEqual((root / "dist" / "wip.js").read_text(), "uncommitted\n")
            if unpushed:
                self.assertIn("local only", self.git(root / "dist", "log", "-1", "--format=%s"))

    def test_a_clone_nested_inside_node_modules_is_kept_with_everything_in_it(self):
        self.job("J-0961")
        root = self.worktree("J-0961", outputs=("node_modules",))
        nested = root / "node_modules" / "pkg"
        nested.mkdir(parents=True, exist_ok=True)
        self.git(nested, "init", "-q", "-b", "main")
        (nested / "work.txt").write_text("unsaved\n")
        self.pressure_tick()
        self.assertEqual((nested / "work.txt").read_text(), "unsaved\n")
        self.assertTrue((nested / ".git").exists())

    def test_a_pushed_clean_nested_clone_in_node_modules_is_still_proved_not_assumed(self):
        """The nested clone has no remote: nothing is pushed, so the all-refs proof refuses even though it is clean."""
        self.job("J-0962")
        root = self.worktree("J-0962", outputs=("node_modules",))
        nested = root / "node_modules" / "pkg"
        nested.mkdir(parents=True, exist_ok=True)
        self.git(nested, "init", "-q", "-b", "main")
        (nested / "a.txt").write_text("a\n")
        self.git(nested, "add", "-A"); self.git(nested, "commit", "-qm", "a")
        self.pressure_tick()
        self.assertTrue((nested / "a.txt").exists())


class NestedRepoInsideACandidateScratch(ScratchBase):
    def test_scratch_keeps_a_linked_worktree_dist_and_a_nested_clone(self):
        self.quiet()
        root = self.checkout("nest/one", outputs=("node_modules",))
        self.git(root, "branch", "gh-pages")
        subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", str(root / "dist"), "gh-pages"], check=True, capture_output=True)
        (root / "dist" / "wip.js").write_text("uncommitted\n")
        nested = root / "node_modules" / "pkg"
        nested.mkdir(parents=True, exist_ok=True)
        self.git(nested, "init", "-q", "-b", "main")
        (nested / "work.txt").write_text("unsaved\n")
        self.regen()
        self.assertEqual((root / "dist" / "wip.js").read_text(), "uncommitted\n")
        self.assertTrue((root / "dist" / ".git").exists())
        self.assertEqual((nested / "work.txt").read_text(), "unsaved\n")


class PinPlusAnotherReason(CacheBase):
    """M3: a released pin must not hide another reason to keep the producer's output."""

    def producer(self, job: str):
        self.job(job, "done", ago=3 * 86400)
        return self.worktree(job, outputs=("node_modules",))

    def live_tick(self, live: list) -> list:
        policy = {**core.load_policy(), "parkedReapHours": 0}
        actions, errors = [], []
        with mock.patch.object(core, "load_policy", return_value=policy), mock.patch.object(core, "live_all", return_value=live), \
                mock.patch.object(core, "disk_level", return_value="warn"), core.db() as conn:
            core.reap_tick(conn, policy, live, actions, errors)
        self.assertEqual(errors, [], errors)
        return actions

    def test_a_pin_plus_a_still_open_worker_terminal_keeps_the_output(self):
        root = self.producer("J-0971")
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET terminal_id='live' WHERE id='J-0971'")
        self.job("J-0972", "parked", waits_on="J-0971", cwd=root)            # the pin alone would be released under pressure
        live = [{"id": "live", "idleSeconds": 0}]
        self.assertEqual(self.live_tick(live), [])
        self.assertTrue((root / "node_modules").exists())
        with mock.patch.object(core, "live_all", return_value=live):
            entry = self.audit("J-0971")[str(root / "node_modules")]
        self.assertEqual(entry["state"], "blocked", entry)
        self.assertTrue(entry["blocking"].startswith("worker-terminal-still-open"), entry)
        # control: the terminal is gone, only the pin is left, and the parked pinner works inside the checkout: released
        self.live_tick([])
        self.assertFalse((root / "node_modules").exists())

    def test_a_pin_plus_a_live_claim_on_the_producer_keeps_the_output(self):
        root = self.producer("J-0973")
        self.job("J-0974", "parked", waits_on="J-0973", cwd=root)
        self.job("J-0975", "running", cwd=root)                              # a second job, running, works in the same checkout
        self.assertEqual(self.pressure_tick(), [])
        self.assertTrue((root / "node_modules").exists())
        entry = self.audit("J-0973")[str(root / "node_modules")]
        self.assertEqual(entry["state"], "blocked", entry)
        with contextlib.closing(self.state()) as conn, conn:                 # control: the claimer finished
            conn.execute("UPDATE jobs SET status='done', finished_at=? WHERE id='J-0975'", (time.time() - 3600,))
            conn.execute("DELETE FROM meta WHERE key LIKE 'reap:pressure:%'")
        self.pressure_tick()
        self.assertFalse((root / "node_modules").exists())

    def test_the_audit_with_a_pin_released_still_asks_the_terminal_and_the_claims(self):
        """The audit's own path: the blocker is recomputed, not set to None, so a terminal is still asked."""
        root = self.producer("J-0976")
        self.job("J-0977", "parked", waits_on="J-0976", cwd=root)
        self.job("J-0978", "parked", cwd=root)                                # a parked claimer: releasable under pressure like the pinner
        self.job("J-0979", "running", cwd=root)
        entry = self.audit("J-0976")[str(root / "node_modules")]
        self.assertEqual(entry["state"], "blocked", entry)


class GitFailsClosed(CacheBase):
    """m5: the ancestor query is a git call like any other: a timeout or a non-zero exit refuses, it never reads as 'no ignored ancestor'."""

    def fixture(self, job: str):
        self.job(job)
        root = self.worktree(job, outputs=())
        derived(root, "pkg", "dist", self.put)
        derived(root, "pkg", "node_modules", self.put)
        return root

    def failing(self, how):
        real = core.reap_run

        def run(argv, timeout, env=None, data=None):
            if "check-ignore" in argv and "--stdin" in argv:
                if how == "timeout":
                    raise subprocess.TimeoutExpired(argv, timeout)
                return subprocess.CompletedProcess(argv, 128, "", "fatal: boom")
            return real(argv, timeout, env, data) if data is not None else real(argv, timeout, env)
        return mock.patch.object(core, "reap_run", run)

    def test_a_timeout_or_a_fatal_exit_on_the_ancestor_query_keeps_the_dir(self):
        root = self.fixture("J-0981")
        for how, expected in (("timeout", "probe-timeout:git"), ("exit", "git-error:check-ignore:128")):
            with self.failing(how), self.assertRaises(core.ReapRefused) as refused:
                core.reap_git_proof(str(root), str(root / "pkg/dist"), NEEDS)
            self.assertEqual(str(refused.exception), expected)
        self.assertIn("git-no-ignored-ancestor", core.reap_git_proof(str(root), str(root / "pkg/dist"), NEEDS))   # control
        with self.failing("timeout"):
            self.pressure_tick()
        self.assertTrue((root / "pkg/dist/dep/index.js").exists() and (root / "pkg/node_modules/dep/index.js").exists())
        with self.failing("exit"):
            self.pressure_tick()
        self.assertTrue((root / "pkg/dist/dep/index.js").exists() and (root / "pkg/node_modules/dep/index.js").exists())
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("DELETE FROM meta WHERE key LIKE 'reap:%'")                              # the failed passes' backoff is not under test
        out = self.pressure_tick()
        self.assertFalse((root / "pkg/dist").exists(), out)                                       # control: git answers, they go


class AuditWallClock(CacheBase):
    """m2: the worktree scan and the git proofs of an audit stop at its hard ceiling. m3: a non-tick audit never outlives the shell guard."""

    def test_a_scan_past_its_deadline_returns_what_it_has_marked_partial(self):
        self.job("J-0991")
        root = self.worktree("J-0991")
        self.assertEqual(core.reap_scan_clone(str(root), time.monotonic() - 1)[1], True)
        self.assertEqual(core.reap_scan_clone(str(root), time.monotonic() + 60)[1], False)
        self.assertEqual(core.reap_cache_worktree_items({"id": "J-0991"}, str(root), time.monotonic() - 1)[1], True)

    def test_the_audit_hands_its_ceiling_to_every_worktree_scan(self):
        self.job("J-0992")
        self.worktree("J-0992")
        deadlines = []
        real = core.reap_scan_clone

        def spy(root, deadline=None):
            deadlines.append(deadline)
            return real(root, deadline)
        with mock.patch.object(core, "reap_scan_clone", spy):
            core.reap_audit(None, 10, False, 5)
        self.assertTrue(deadlines and all(d is not None and d <= time.monotonic() + 5 * core.REAP_AUDIT_OVERRUN for d in deadlines), deadlines)

    def test_a_git_probe_inside_an_audit_never_outlives_the_ceiling_and_none_starts_after_it(self):
        self.job("J-0993")
        root = self.worktree("J-0993", outputs=("node_modules",))
        limits = []
        real = core.reap_run

        def spy(argv, timeout, env=None, data=None):
            if argv[0] == "git":
                limits.append(timeout)
            return real(argv, timeout, env, data) if data is not None else real(argv, timeout, env)
        with mock.patch.object(core, "reap_run", spy):
            core.reap_audit(None, 10, True, 5)
        self.assertTrue(limits and max(limits) <= 5 * core.REAP_AUDIT_OVERRUN + 0.5, limits)
        core.REAP_PROBE.ceiling = time.monotonic() - 1
        try:
            with self.assertRaises(core.ReapRefused) as refused:
                core.reap_git(str(root), "status")
        finally:
            core.REAP_PROBE.ceiling = None
        self.assertEqual(str(refused.exception), "audit-budget-exhausted")

    def test_a_huge_policy_budget_is_capped_under_the_shell_guard_outside_the_tick(self):
        self.write_config({"policy": {"reapAuditSeconds": 3600}})
        self.assertEqual(core.reap_audit_budget(), core.REAP_AUDIT_SHELL_SECONDS)
        self.assertLess(core.REAP_AUDIT_SHELL_SECONDS * core.REAP_AUDIT_OVERRUN, 120)
        seen = []
        with mock.patch.object(core, "reap_audit", lambda *a, **k: seen.append(a[3]) or {}):
            core.tool_reap_status({})
            core.tool_reap_status({"budgetSeconds": 900})
            core.tool_reap_status({"budgetSeconds": 7})
        self.assertEqual(seen, [core.REAP_AUDIT_SHELL_SECONDS, core.REAP_AUDIT_SHELL_SECONDS, 7])
        self.assertEqual(core.reap_audit_budget(persist=True), core.REAP_AUDIT_MAX_SECONDS if 3600 > core.REAP_AUDIT_MAX_SECONDS else 3600)


class InputPinRoute(CacheBase):
    """m1: a host's input pin (blocker text `input-pinned-by:J-N(parked)`, no wait record) is asked the same way by the audit and the removal.
    m4: the audit looks a job's pinners up once, not once per item."""

    def host_blocker(self, producer: str, pinner: str):
        real = core.reap_job_blocker

        def blocker(conn, job, *args, **kwargs):
            found = real(conn, job, *args, **kwargs)
            return f"input-pinned-by:{pinner}(parked)" if job["id"] == producer and found is None else found
        return mock.patch.object(core, "reap_job_blocker", blocker)

    def producer(self, job: str):
        self.job(job, "done", ago=3 * 86400)
        return self.worktree(job, outputs=("node_modules",))

    def test_an_input_pinner_that_works_inside_the_checkout_releases_it_in_the_audit_and_the_removal(self):
        root = self.producer("J-0911")
        self.job("J-0912", "parked", cwd=root)                                # no wait record: only the host's blocker text names it
        with self.host_blocker("J-0911", "J-0912"):
            self.assertEqual(self.audit("J-0911")[str(root / "node_modules")]["state"], "reapable")
            actions = self.pressure_tick()
        self.assertEqual([a["did"] for a in actions], ["reaped-pinned-under-pressure"], actions)
        self.assertFalse((root / "node_modules").exists())

    def test_an_input_pinner_that_only_reads_the_output_keeps_it_in_the_audit_and_the_removal(self):
        root = self.producer("J-0913")
        self.job("J-0914", "parked", cwd=self.tmp)                            # works elsewhere: it only reads the producer's output
        with self.host_blocker("J-0913", "J-0914"):
            entry = self.audit("J-0913")[str(root / "node_modules")]
            actions = self.pressure_tick()
        self.assertEqual(entry["state"], "blocked", entry)
        self.assertEqual(actions, [])
        self.assertTrue((root / "node_modules/sub/build.bin").exists())

    def test_the_audit_looks_the_pinners_up_once_per_job(self):
        self.job("J-0915", "done", ago=3 * 86400)
        root = self.worktree("J-0915", outputs=("node_modules", ".next", "dist"))
        self.job("J-0916", "parked", waits_on="J-0915", cwd=root)
        calls = []
        real = core.reap_pinners

        def spy(*args, **kwargs):
            calls.append(args[1])
            return real(*args, **kwargs)
        with mock.patch.object(core, "reap_pinners", spy):
            core.reap_audit("J-0915", 10, True, 30)
        self.assertEqual(calls, ["J-0915"], calls)


class PackagedBundlesAreVendored(CacheBase):
    """m6: a node_modules inside a packaged app bundle sits beside a package.json and is git-ignored by name, but it is a shipped copy."""

    def test_node_modules_and_dist_inside_an_app_bundle_or_framework_are_kept(self):
        self.job("J-0917")
        root = self.worktree("J-0917", outputs=("node_modules",))
        for bundle in ("Build/Foo.app/Contents/Resources/app", "Build/Bar.framework/Resources", "Out/Foo.AppEx/Contents/Resources"):
            self.put(root, f"{bundle}/package.json", b"{}")
            self.put(root, f"{bundle}/node_modules/dep/index.js")
            self.put(root, f"{bundle}/dist/index.js")
        self.pressure_tick()
        self.assertFalse((root / "node_modules").exists())                    # control: the checkout's own
        for bundle in ("Build/Foo.app/Contents/Resources/app", "Build/Bar.framework/Resources", "Out/Foo.AppEx/Contents/Resources"):
            self.assertTrue((root / bundle / "node_modules/dep/index.js").exists(), bundle)
            self.assertTrue((root / bundle / "dist/index.js").exists(), bundle)
