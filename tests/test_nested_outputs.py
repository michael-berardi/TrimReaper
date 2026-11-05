"""Dependency and build output nested anywhere inside a job's cache folder (`project-dependencies`).

A finished, failed, cancelled, interrupted, parked or blocked job's output needs only six quiet hours; a live job's output is kept.
Throwaway homes only."""
from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path
from unittest import mock

from helpers import DAY, Home

from trimreaper import clean

HOUR = DAY / 24
EMPTY = {"files": [], "commands": []}
NAMES = ("node_modules", ".next", ".astro", ".turbo", ".svelte-kit")


class Nested(Home):
    def setUp(self):
        super().setUp()
        self.jobs = self.home / ".cache" / "trimreaper" / "jobs"
        for patch in (mock.patch.object(clean, "clean_running", return_value=set()),
                      mock.patch.object(clean, "reap_process_table", return_value=EMPTY),
                      mock.patch.object(clean, "clean_temp_roots", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)

    def job_repo(self, job: str, *where: str, name="proj", hours=10, ignored=NAMES) -> Path:
        """A pushed clone moved to `cacheRoot/<job>/<where...>/<name>`, holding every output name, aged `hours`."""
        repo = self.clone(name + "-" + job.lower(), ignored=tuple(n + "/" for n in ignored),
                          tracked={"package.json": "{}\n", "src/index.js": "1\n"})
        dest = self.jobs.joinpath(job, *where, name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(repo), str(dest))
        for output in NAMES:
            self.put(dest, f"{output}/a/b.js")
        self.age(dest, hours / 24)
        return dest

    def run_clean(self, dry=False):
        return self.cli("clean", *(["--dry-run"] if dry else []), "--only", "project-dependencies")

    def outputs(self, repo: Path):
        return [repo / n for n in NAMES]

    def test_outputs_nested_deeper_than_the_old_search_are_found_in_a_job_folder(self):
        self.add_job("J-0042", status="parked")
        shallow = self.job_repo("J-0042", name="top")
        deep = self.job_repo("J-0042", "a", "b", "c", "d", "e", name="deep")
        found = {Path(p) for p, _ in clean.clean_project_outputs([])}
        for repo in (shallow, deep):
            self.assertTrue(set(self.outputs(repo)) <= found, repo)

    def test_a_parked_blocked_interrupted_or_finished_job_gives_up_its_outputs_after_six_hours(self):
        for number, status in enumerate(("parked", "blocked", "interrupted", "done", "failed", "cancelled"), 10):
            with self.subTest(status=status):
                job = f"J-{number:04d}"
                self.add_job(job, status=status, finished_ago=2 * DAY)
                repo = self.job_repo(job, "integration", "web")
                out = self.run_clean()
                self.assertEqual(out["summary"]["done"], len(NAMES), out)
                for path in self.outputs(repo):
                    self.assertFalse(path.exists(), path)
                self.assertTrue((repo / "package.json").exists())
                self.assertTrue((repo / ".git").exists())

    def test_less_than_six_quiet_hours_keeps_them(self):
        self.add_job("J-0020", status="parked")
        repo = self.job_repo("J-0020", "x", "y", hours=3)
        out = self.run_clean()
        self.assertEqual(out["summary"]["done"], 0, out)
        self.assertTrue(all(i["keep"].startswith("recent:") for i in out["items"]), out)
        self.assertTrue(all(p.exists() for p in self.outputs(repo)))

    def test_a_running_or_starting_job_keeps_its_nested_outputs(self):
        self.add_job("J-0021", status="running")
        repo = self.job_repo("J-0021", "x", "y", "z", "w", "v", hours=20 * 24)   # quiet past every window: only the live job keeps it
        self.age(self.jobs / "J-0021", 20)
        out = self.run_clean()
        self.assertEqual(out["summary"]["done"], 0, out)
        self.assertTrue(all(i["keep"] == "in-use-by-running-job:J-0021" for i in out["items"]), out)
        self.assertEqual(len(out["items"]), len(NAMES), out)
        with contextlib.closing(self.state()) as conn, conn:
            conn.execute("UPDATE jobs SET status='starting' WHERE id='J-0021'")
        out = self.run_clean()
        self.assertEqual(out["summary"]["done"], 0, out)
        self.assertTrue(all(p.exists() for p in self.outputs(repo)))

    def test_the_short_window_is_for_job_folders_not_for_every_parked_project(self):
        """A parked job's clone in a clone base still waits the full project window (48 hours)."""
        self.add_job("J-0022", status="parked")
        clone = self.clone("app-j0022", ignored=("node_modules/",), tracked={"package.json": "{}\n"})
        self.put(clone, "node_modules/a/b.js")
        self.age(clone, 10 / 24)
        out = self.run_clean()
        self.assertEqual(out["summary"]["done"], 0, out)
        self.assertTrue((clone / "node_modules").exists())

    def test_the_git_proofs_still_apply_to_nested_outputs(self):
        self.add_job("J-0023", status="parked")
        repo = self.job_repo("J-0023", "a", "b")
        self.put(repo, "node_modules/tracked.js", b"keep\n")
        self.git(repo, "add", "-f", "node_modules/tracked.js")
        self.git(repo, "commit", "-qm", "tracked output")
        self.git(repo, "push", "-q")
        (repo / ".gitignore").write_text("")   # and the rest is not ignored either
        self.age(repo, 10 / 24)
        out = self.run_clean()
        self.assertTrue((repo / "node_modules/tracked.js").exists())
        self.assertEqual(out["summary"]["done"], 0, out)
        self.assertTrue(all(i["keep"].split(":")[0] in ("tracked-files", "not-git-ignored", "dirty", "unpushed") for i in out["items"]), out)

    def test_an_output_with_no_repository_inside_the_job_folder_is_never_listed(self):
        self.add_job("J-0024", status="parked")
        loose = self.put(self.jobs / "J-0024" / "copy", "node_modules/x/y.js")
        self.put(self.jobs / "J-0024" / "copy", "package.json", b"{}")
        self.age(self.jobs / "J-0024", 1)
        self.assertEqual([p for p, _ in clean.clean_project_outputs([])], [])
        self.run_clean()
        self.assertTrue(loose.exists())

    def test_outputs_need_their_manifest_beside_them(self):
        self.add_job("J-0025", status="parked")
        repo = self.job_repo("J-0025", "a")
        (repo / "package.json").unlink()
        self.git(repo, "rm", "-q", "--cached", "package.json")
        self.git(repo, "commit", "-qm", "no manifest")
        self.git(repo, "push", "-q")
        self.age(repo, 10 / 24)
        self.assertEqual(clean.clean_project_outputs([]), [])

    def test_protected_names_are_never_entered_and_dependency_stores_are_not_searched(self):
        self.add_job("J-0026", status="parked")
        hidden = self.job_repo("J-0026", ".wrangler", "state", name="inside")
        stored = self.job_repo("J-0026", "node_modules", "pkg", name="vendored")
        self.assertEqual([p for p, _ in clean.clean_project_outputs([]) if "inside" in p or "vendored" in p], [])
        self.assertTrue(hidden.exists() and stored.exists())

    def test_a_protected_leaf_in_the_repository_keeps_the_outputs(self):
        self.add_job("J-0027", status="parked")
        repo = self.job_repo("J-0027", "a", "b")
        self.put(repo, "node_modules/pkg/.env", b"SECRET=1\n")
        self.age(repo, 10 / 24)
        out = self.run_clean()
        self.assertEqual((repo / "node_modules/pkg/.env").read_bytes(), b"SECRET=1\n")
        self.assertTrue((repo / "node_modules").exists())

    def test_a_process_inside_keeps_the_outputs(self):
        self.add_job("J-0028", status="parked")
        repo = self.job_repo("J-0028", "a", "b")
        table = {"files": [("9", "node", "cwd", str(repo / "src"))], "commands": []}
        with mock.patch.object(clean, "reap_process_table", return_value=table):
            out = self.run_clean()
        self.assertEqual(out["summary"]["done"], 0, out)
        self.assertTrue(all(p.exists() for p in self.outputs(repo)))

    def test_the_discovery_entry_cap_is_honoured_and_reported(self):
        self.add_job("J-0029", status="parked")
        for n in range(30):
            self.put(self.jobs / "J-0029" / "wide", f"d{n}/f")
        errors = []
        with mock.patch.object(clean, "CLEAN_RUST_SCAN_ENTRIES", 20):
            self.assertEqual(clean.clean_job_outputs(errors, clean.CLEAN_PROJECT_OUTPUTS), [])
        self.assertTrue(any("discovery-incomplete" in e["error"] and e["clean"] == "project-dependencies" for e in errors), errors)

    def test_discovery_does_not_leave_the_job_folders(self):
        self.add_job("J-0030", status="parked")
        repo = self.job_repo("J-0030", "a")
        elsewhere = self.put(self.jobs / "not-a-job" / "r", "node_modules/z/y.js")
        self.put(self.jobs / "not-a-job" / "r", ".git/HEAD", b"ref: refs/heads/main\n")
        self.put(self.jobs / "not-a-job" / "r", "package.json", b"{}")
        self.assertEqual(clean.clean_job_folder_of(str(repo / "node_modules")), "J-0030")
        self.assertIsNone(clean.clean_job_folder_of(str(elsewhere)))
        self.assertNotIn(str(elsewhere), [p for p, _ in clean.clean_job_outputs([], clean.CLEAN_PROJECT_OUTPUTS)])
