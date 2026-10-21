"""TrimReaper core: the guarded reaper engine.

Everything between the `trimreaper core` markers below is the engine itself. A host runs it in one of two ways:

  * as this module, with the standalone host (`trimreaper.host`) supplying the names listed in HOST_NAMES;
  * embedded byte for byte in the host's own source, after the host has defined those names itself
    (see docs/EMBEDDING.md), the way UltraTerm vendors it.

Do not edit the engine in an embedding host: change it here, release, and re-vendor. `trimreaper core-digest`
prints the SHA-256 an embedded copy must match.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import plistlib
import pwd
import re
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import unicodedata
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from .host import (  # noqa: F401  (the engine reads these as module globals)
    REAP_CLI, REAP_OWNER_DIRS, VERSION, HostError, actor_of, db, disk_level, disk_state, iso, job_meta, job_wait, live_all,
    load_policy, log_event, now, reap_agent_scratch_root, reap_archive_root, reap_cache_root, reap_clone_bases,
    reap_home, reap_queue_state, reap_receipt_dir, reap_release_evidence_root, reap_release_marks, reap_release_root, require,
    row, rows, txn, whole_title, reap_output_ownership,
)

# The names an embedding host must define before the engine runs (checked by tests/test_embedding.py).
HOST_NAMES = (
    "REAP_CLI", "REAP_OWNER_DIRS", "VERSION", "HostError", "actor_of", "db", "disk_level", "disk_state", "iso", "job_meta",
    "job_wait", "live_all", "load_policy", "log_event", "now", "reap_agent_scratch_root", "reap_archive_root", "reap_cache_root",
    "reap_clone_bases", "reap_home", "reap_queue_state", "reap_receipt_dir", "reap_release_evidence_root",
    "reap_release_marks", "reap_release_root", "require", "row", "rows", "txn", "whole_title",
)

# >>> trimreaper core >>>
REAP_TERMINAL = ("done", "failed", "cancelled")
# Interrupted is terminal ONLY for the separately guarded, exact-output route.
REAP_OUTPUT_TERMINAL = REAP_TERMINAL + ("interrupted",)
REAP_OUTPUT_KINDS = ("target", "node_modules")
REAP_OUTPUT_SCHEDULE_ITEMS = 16
REAP_OUTPUT_SCHEDULE_SECONDS = 5
REAP_SETTLE_SECONDS = 900             # a job that just finished may still be corrected or resumed
REAP_CLOSED_SETTLE_SECONDS = 30       # ... unless its worker is already closed through the guarded cleanup: nothing can resume it
REAP_RETRY_SECONDS = (900, 3600, 6 * 3600)   # wait after a pass that preserved something, by attempt
REAP_TICK_JOBS = 2                    # jobs with artifacts one tick may reap
REAP_TICK_SECONDS = 60                # wall-clock budget of one tick's reaping
REAP_REGENERABLE = ("node_modules", ".next", ".open-next", "target", ".turbo", ".astro", "dist")
# Protected names are leaves the reaper NEVER enters (discovery, checks or removal): there is no carve-out below them (not
# .wrangler/tmp, not .vercel/output). An output that holds one anywhere is preserved whole.
REAP_PROTECTED_ROOTS = (".wrangler", ".vercel")        # local D1/KV/R2 state, the Vercel project link, env copies
REAP_PROTECTED_PREFIXES = (".env", ".dev.vars")        # secret-bearing files
REAP_RETENTION_CLASSES = ("disposable", "evidence", "customer", "release-runtime-rollback")
REAP_DISPOSABLE = "disposable"                         # the only retention class that may be removed; every other one, and any gap, is kept
REAP_MARKER_PREFIX = ".ut-retention-"                  # every candidate is disposable only through the marker file .ut-retention-disposable
# Self-cleaning: when only a build queue writes the disposable marker, installs, tests and private builds leave output that no
# gate can ever pass. Without a marker the reaper therefore DERIVES `disposable` in exactly three cases, and every other gate (git proofs, locks, protected scan,
# open handles, authority, receipts) still runs per item: (1) a dependency or compiler output of these names in a finished
# job's clone (dist is excluded: it can hold a release); (2) a job cache folder tagged as a tool cache by a valid CACHEDIR.TAG
# (Cargo, uv and ruff write one); (3) an unmarked job cache folder once its job finished evidenceExpiryDays ago, the same window
# the operator set for screenshots and logs. Any retention marker (evidence, customer, release...) still wins, a marker in
# the job folder preserves the whole job unless it is disposable, and `protected-archive` is never derived.
REAP_DERIVED_KINDS = ("node_modules", ".next", ".open-next", ".turbo", ".astro", "target")
# Astro writes these into .astro/ before any build output exists; an unignored .astro holding none of them is not its cache.
REAP_ASTRO_FILES = ("data-store.json", "content-assets.mjs", "content-modules.mjs", "types.d.ts")
# Disk pressure: clones of jobs that are parked or interrupted can hold a full disk while nothing runs in them. At or above
# diskWarnPercent, the regenerable build output of a job
# that is not starting or running is reapable too: such a job rebuilds it when it runs. Every per-item gate still applies (protected
# leaves, git proofs, locks, open handles, cwd), and nothing else of an unfinished job is touched.
# Parked/blocked jobs also surrender only derived output after parkedReapHours without a report; zero means pressure only.
# Nothing of a running, approved or queued job is touched, even under pressure.
REAP_PRESSURE_IDLE = ("parked", "blocked", "interrupted")
REAP_PRESSURE = threading.local()


def reap_idle_expired(job: dict, policy: dict) -> bool:
    """No report/activity for the parked window; absent timestamps fail closed."""
    hours = policy.get("parkedReapHours", 24)
    if job.get("status") not in ("parked", "blocked") or isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours <= 0:
        return False
    meta = job_meta(job)
    clocks = [source.get(key) for source in (job, meta) for key in
              ("created_at", "started_at", "updated_at", "last_reported_at", "last_report_at", "lastReportAt", "parked_at", "blocked_at")]
    for nested in ("parked", "resume", "waitingOn", "needsOperator"):  # the host records park/resume/wait/ask times inside meta
        value = meta.get(nested)
        if isinstance(value, dict):
            clocks += [value.get(key) for key in ("at", "since", "parkedAt")]
    clocks = [stamp for stamp in clocks if isinstance(stamp, (int, float)) and not isinstance(stamp, bool)]
    return bool(clocks) and now() - max(clocks) >= hours * 3600


def reap_pressure_allows(item: dict, job: dict) -> bool:
    """Only derived output of idle jobs, under pressure or after their quiet window."""
    return item.get("class") == "job-clone-regenerable" and item.get("kind") in REAP_DERIVED_KINDS \
        and job["status"] in REAP_PRESSURE_IDLE \
        and (bool(getattr(REAP_PRESSURE, "on", False)) or reap_idle_expired(job, load_policy())) \
        and reap_idle_terminal_allows(job)


def reap_idle_terminal_allows(job: dict) -> bool:
    terminal = job.get("terminal_id")
    if not terminal:
        return True
    live = reap_live_ids()
    if live is None or terminal in live:
        return False
    with db() as conn:
        record = row(conn, "SELECT status FROM cleanup_records WHERE terminal_id=?", (terminal,))
        siblings = rows(conn, "SELECT id,status FROM jobs WHERE terminal_id=? AND id!=?", (terminal, job["id"]))
    return not (record and record["status"] in ("verified", "closing", "awaiting-final-reply", "unknown")) \
        and not any(j["status"] not in REAP_TERMINAL for j in siblings)
CACHEDIR_TAG = "CACHEDIR.TAG"
CACHEDIR_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"   # bford.net/cachedir: the first 43 bytes of a valid tag
REAP_SCAN_DEPTH = 4                   # directory levels searched for regenerable dirs inside one clone
REAP_SCAN_ENTRIES = 100000            # entries one bounded walk may list before it reports itself incomplete
REAP_WALK_ENTRIES = 500000            # entries the names-only protected scan may list before it refuses
REAP_MAX_DEPTH = 128                  # directory levels a scan or the removal descends; deeper fails closed
REAP_AUDIT_SECONDS = 240              # default wall-clock budget of one reap-status sizing pass
REAP_RECEIPT_LOCK_SECONDS = 10        # bounded blocking wait for the receipt file lock; a longer wait is receipt-unavailable:lock-timeout
REAP_UNSAFE_CONFIG_SECTIONS = ("include", "includeif", "filter")   # Git config sections that read another file or run a command
REAP_JOB_NUMBER = re.compile(r"(?:^|[-_.])[jJ](\d{4,5})(?!\d)")
# An allowed root may not be, enclose or sit inside a system root, and may not be or enclose a system parent.
REAP_SYSTEM_ROOTS = ("/System", "/Library", "/Applications", "/usr", "/bin", "/sbin", "/etc", "/private/etc", "/opt",
                     "/cores", "/dev", "/Volumes", "/var/db", "/private/var/db")
REAP_SYSTEM_PARENTS = ("/Users", "/private", "/var", "/private/var", "/tmp", "/private/tmp")
REAP_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# macOS: no symlink in any component of the root path. The kernel rejects it combined with O_NOFOLLOW (EINVAL).
REAP_ROOT_FLAGS = (REAP_DIR_FLAGS & ~os.O_NOFOLLOW) | os.O_NOFOLLOW_ANY if hasattr(os, "O_NOFOLLOW_ANY") else REAP_DIR_FLAGS
# A protected leaf's identity is its own lstat and nothing below it (the leaf is never entered): see reap_identity.
REAP_IDENTITY_SCOPE = "root-lstat-only"
REAP_IDENTITY_KEYS = ("kind", "dev", "ino", "mode", "nlink", "size", "mtimeNs", "ctimeNs", "scope", "entries", "bytes", "unverified", "rootMetaDigest")
# The retention class fixes which proofs an item needs; a caller can add none and remove none. "retention" says where the
# class gets its retention from: ALL classes require an explicit owner marker freshly found on the item itself.
# A build name, gitignore or a superseded release is not evidence that customer/QA/rollback bytes may go.
REAP_CLASSES = {
    "job-derived-output": {"git": (), "lock": True, "retention": "marker"},
    # A finished job's regenerable output rebuilds from the source that stays in the clone, so the clone's own uncommitted
    # or stashed tracked work is not the output's business: derived kinds need derivedGit instead of clean/pushed. The
    # output is still proved git-ignored and untracked, and every protected leaf, lock, handle and retention gate stands.
    "job-clone-regenerable": {"git": ("ignored", "clean", "pushed", "untracked"), "derivedGit": ("ignored", "untracked"),
                              "lock": True, "retention": "marker"},
    "job-cache-subdir": {"git": (), "lock": False, "retention": "marker"},
    "release-target": {"git": ("ignored", "untracked"), "lock": True, "retention": "marker"},
    # <scratch root>/<task>: the one home for agent scratch outside a job (delegation staging, ad-hoc audits).
    # Each folder goes once nothing in it changed for evidenceExpiryDays; nested repos are proved like job cache folders.
    "agent-scratch": {"git": (), "lock": False, "retention": "marker"},
    "release-evidence": {"git": (), "lock": False, "retention": "marker"},
    # The whole clone of finished jobs: "clone-root" adds the whole-repository proof (standalone clone) or the worktree's own
    # HEAD reflog proof (linked worktree), and the known-build-output proof for every ignored entry.
    "job-clone-root": {"git": ("clean", "pushed", "clone-root"), "lock": True, "retention": "marker"},
    # <archive root>/<job>/<archive>: what the reaper saved before it removed an expired clone (cloneArchive). Each
    # archive goes archiveExpiryDays after it was written; a retention marker in it keeps it.
    "job-archive": {"git": (), "lock": False, "retention": "marker"},
}
# Ignored entries a removable clone may hold: regenerable build and tool output only. Anything else ignored (local data,
# downloads, logs, databases, notes) keeps the whole clone.
CLONE_IGNORED_OK = frozenset(REAP_REGENERABLE + ("build", "out", ".cloudflare", ".svelte-kit", "coverage", ".cache", ".parcel-cache", ".vite",
                                                 ".output", ".nuxt", "test-results", "playwright-report", "__pycache__", ".pytest_cache",
                                                 ".ruff_cache", ".mypy_cache", ".eslintcache", "storybook-static", ".venv", "venv",
                                                 ".DS_Store", ".ut-retention-disposable", ".build"))
CLONE_IGNORED_SUFFIXES = (".tsbuildinfo", ".pyc")
# ---- archive before removal (policy cloneArchive) -------------------------------------------------------------------------------
# Folders an archive never copies, by name, at any depth: dependency, compiler and tool output that a checkout regenerates
# (SwiftPM's .build holds its own bare package repositories). Protected names (.env*, .dev.vars*, .wrangler, .vercel) are never
# copied either, unless the clone goes whole under cloneProtectedCopies=remove: then they are saved owner-only (protected.tar.gz).
REAP_ARCHIVE_SKIP = frozenset(REAP_REGENERABLE + (".build", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
                                                  ".parcel-cache", ".svelte-kit", ".nuxt", ".output", ".vite", ".cache", "coverage",
                                                  "test-results", "playwright-report", "storybook-static", ".cloudflare", ".gradle",
                                                  "DerivedData", "Pods", ".dart_tool", ".expo", ".DS_Store"))
REAP_ARCHIVE_MAX_BYTES = 4 * 2 ** 30   # input bytes one archive may copy; more is `archive-too-large` (named, never a silent skip)
REAP_ARCHIVE_MAX_FILES = 200000        # files one archive may copy
REAP_ARCHIVE_REFLOG_TIPS = 5000        # reflog commits one linked worktree may name for its bundle
REAP_NONGIT_GRACE_DAYS = 2             # a git error is retried (tick backoff) this many days past cloneExpiryDays; then the folder is non-git
# Folders whose untracked or ignored screenshots, recordings and logs are genuine evidence (they expire past evidenceExpiryDays). A
# file git tracks, or an untracked file outside these folders that no .gitignore rule covers, is source: archived whatever its age.
REAP_EVIDENCE_DIRS = frozenset(("screenshots", "screenshot", "evidence", "logs", "log", "traces", "playwright-report", "test-results",
                                "test-artifacts", "e2e-results"))
REAP_WORKTREE_STALE_DAYS = 30          # a linked worktree whose folder is missing counts as moved (unknown) until its entry is this old
REAP_REVISIT_SECONDS = 6 * 3600        # a job that finished clean but still has a clone is looked at again this often (clone expiry)
REAP_LFS_DIR = os.path.join("lfs", "objects")   # below the git dir: Git LFS content that may exist nowhere else
# Refusals that mean "work no remote holds, or git cannot say": with cloneArchive=archive an expired folder is archived, then removed.
REAP_ARCHIVABLE = ("dirty", "unpushed", "stash-present", "unproved-tags", "unproved-refs", "unproved-git-objects", "ignored-unknown",
                   "unproved-git-dir", "nested-git-in-clone", "nested-git-boundary", "git-error", "git-unsafe-index", "git-untracked",
                   "protected-copies")
# ...of which these say git itself cannot be trusted or read here: retried, then archived as plain files (non-git).
REAP_NONGIT = ("git-error", "git-unsafe-index", "git-untracked")
AGENT_SCRATCH_OWNER = "agent-scratch"
# <release-evidence root>/<version>: the host app keeps the installed version, the single rollback and anything newer than
# installed; each component (component-1.2.3 ...) keeps its newest RELEASE_EVIDENCE_KEEP versions.
RELEASE_EVIDENCE_OWNER = "release-evidence"
RELEASE_EVIDENCE_KEEP = 2
RELEASE_EVIDENCE_NAME = re.compile(r"^(?:(?P<product>[a-z][a-z-]*?)-)?(?P<version>\d+\.\d+\.\d+)(?P<suffix>-[A-Za-z0-9.-]+)?$")
AGENT_SCRATCH_IDLE_MAX_ENTRIES = 200000   # entries one folder's idle walk may list; past it the walk stops and says so
AGENT_SCRATCH_IDLE_MAX_BYTES = 0          # bytes one folder's idle walk may see; 0 = no byte bound (bytes bound no work)
AGENT_SCRATCH_IDLE_SECONDS = 10           # wall-clock budget of one folder's idle walk; past it the walk stops and says so
AGENT_SCRATCH_TICK_SECONDS = 3600     # the tick sweeps agent scratch at most hourly


class ReapRefused(Exception):
    """An item is preserved. The message is the blocking reason that reap-status shows and the receipt records."""


def reap_fold(name: str) -> str:
    """The comparison form of a name or path: APFS and HFS+ ignore case and normalisation, so `.Wrangler`, `.VERCEL` and
    `.ENV.local` are the protected entries too."""
    return unicodedata.normalize("NFKC", name).casefold()


# Committed env TEMPLATES (.env.example, .env.local.template, .dev.vars.sample ...) carry no secret by convention and, once the
# clone is proved clean and pushed, an exact copy lives on the remote. Only for a whole clone, and only when git tracks the
# file, is such a plain file allowed through the protected scan and removed with the clone (otherwise a committed
# .env.example alone would keep a clone forever). .envrc, .env, .env.local, .dev.vars, .wrangler and .vercel stay protected.
REAP_ENV_TEMPLATE = re.compile(r"^\.(?:env|dev\.vars)(?:\.[a-z0-9_-]+)*\.(?:example|sample|template)$")


def reap_env_template(rel: str) -> bool:
    """A tracked-path name whose ONLY protected component is its last one, and that one is an env template."""
    parts = [p for p in rel.split("/") if p]
    return bool(parts) and not any(reap_protected(p) for p in parts[:-1]) and bool(REAP_ENV_TEMPLATE.match(reap_fold(parts[-1])))


def reap_tracked_templates(repo: str) -> frozenset:
    """Absolute paths of env templates the clone's index tracks (index listing only: the working tree is not read)."""
    code, out = reap_git(repo, "ls-files", "-z")
    if code != 0:
        raise ReapRefused(f"git-error:ls-files:{code}")
    return frozenset(os.path.join(repo, rel) for rel in out.split("\0") if rel and reap_env_template(rel))


def reap_protected(name: str) -> bool:
    """Names the reaper never enters or deletes: local secrets, the Vercel project link/config and Wrangler state."""
    name = reap_fold(name)
    return name in REAP_PROTECTED_ROOTS or name.startswith(REAP_PROTECTED_PREFIXES)


def reap_is_git(name: str) -> bool:
    return reap_fold(name) == ".git"


def reap_refuse_git_layout(rel: str, names: list) -> None:
    """A directory that lists HEAD, objects and refs is a Git directory whatever it is called: a bare repository, a
    `--separate-git-dir` target, a moved `.git`. Names only (nothing is stat'ed, opened or run, and the directory is refused
    before any entry in it is visited), so Git metadata this code did not name is never entered and never trusted. A
    Only `.git` itself is left to that repository's proof; nested metadata stores such as `.git/modules/*` remain independently protected."""
    if reap_is_git(rel.rpartition("/")[2]):
        return
    folded = {reap_fold(name) for name in names}
    if "objects" in folded and folded.intersection({"head", "refs", "packed-refs", "reftable"}):
        # Partial and reftable Git stores can still hold the only copy of local work.
        raise ReapRefused(f"unproved-git-dir:{rel or '.'}")


def reap_named(error: BaseException) -> str:
    code = getattr(error, "errno", None)
    return f"{error.__class__.__name__}:{errno.errorcode.get(code, code)}" if code else error.__class__.__name__


def reap_canon(path) -> str:
    return os.path.realpath(str(path))


def reap_inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def reap_inside_folded(path: str, root: str) -> bool:
    return reap_inside(reap_fold(path), reap_fold(root))


def reap_job_ids(*names: str) -> list[str]:
    """Job ids named by directory names: foo-j0349, j0349-foo, .j0349-foo."""
    found = []
    for name in names:
        for number in REAP_JOB_NUMBER.findall(name):
            job_id = f"J-{int(number):04d}"
            if job_id not in found:
                found.append(job_id)
    return found


def reap_free_bytes() -> int | None:
    for volume in ("/System/Volumes/Data", str(Path.home())):
        try:
            usage = os.statvfs(volume)
        except OSError:
            continue
        return usage.f_bavail * usage.f_frsize
    return None


def reap_disk_state(volume: str) -> dict | None:
    """APFS df Used is not shutil's total-minus-free (which includes shared container allocation).
    Exact goals use df's used/available columns. A failed probe keeps only a named conservative
    admission fallback; usedBytes is unknown, so cleanup never reports a met goal from it.
    """
    reason = "df-malformed-output"
    try:
        code, text = reap_probe(["df", "-kP", volume], 5, env={**os.environ, "LC_ALL": "C"})
        if code == 0:
            lines = text.strip().splitlines()
            fields = lines[-1].split(None, 5) if len(lines) == 2 else []
            if len(fields) == 6 and all(v.isdecimal() for v in fields[1:4]):
                used, free = int(fields[2]) * 1024, int(fields[3]) * 1024
                total = used + free
                if total > 0:
                    return {"path": volume, "usedPercent": -(-used * 100 // total), "usedBytes": used,
                            "freeBytes": free, "measurement": "df-kP"}
        else:
            reason = "df-exit:" + str(code)
    except (OSError, subprocess.SubprocessError, ReapRefused) as error:
        reason = "df-probe:" + reap_named(error)
    try:
        usage = shutil.disk_usage(volume)
    except OSError:
        return None
    total = usage.used + usage.free
    if total <= 0:
        return None
    return {"path": volume, "usedPercent": -(-usage.used * 100 // total), "usedBytes": None,
            "freeBytes": usage.free, "measurement": "statvfs-fallback", "measurementError": reason}


def reap_run(argv: list[str], timeout: float, env: dict | None = None, data: str | None = None):
    """Every external probe of the reaper goes through here."""
    if data is None:
        return subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout, env=env)
    with tempfile.TemporaryFile() as handle:  # an anonymous private file as stdin: no pipe to fill, no writer thread to stall
        handle.write(data.encode("utf-8"))
        handle.seek(0)
        return subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=timeout, env=env, stdin=handle)


def reap_probe(argv: list[str], timeout: float = 60, env: dict | None = None, data: str | None = None) -> tuple[int, str]:
    name = os.path.basename(argv[0])
    try:
        done = reap_run(argv, timeout, env) if data is None else reap_run(argv, timeout, env, data)
    except subprocess.TimeoutExpired:
        raise ReapRefused(f"probe-timeout:{name}")
    except OSError:
        raise ReapRefused(f"probe-unavailable:{name}")
    return done.returncode, done.stdout


def reap_fd_walk(top: str, visit, limit: int, deadline: float | None = None, incomplete: str = "walk-incomplete",
                 too_deep: str = "walk-too-deep", soft: list | None = None, blind=None, listing=None) -> int:
    """Names-only walk below `top`, descriptor-relative end to end: the top is opened with no symlink in any component and
    every child directory with O_NOFOLLOW, then compared by device and inode with the lstat that chose it, so a directory
    swapped for a symlink between the listing and the entry is never listed. `visit(rel, name, info, level, device)` gets
    each entry's lstat (None for a protected name, which is never stat'ed) and returns True to enter a directory; it raises
    ReapRefused to refuse. More than `limit` entries, a
    passed `deadline`, a tree deeper than REAP_MAX_DEPTH, a changed identity or an unreadable directory refuse; with `soft`
    an unreadable entry is noted there and skipped instead; `blind(name)` names more entries the visit sees by name only; `listing(rel, names)` sees every
    directory's entry names (still no stat) before any of them is visited or entered, and raises to refuse. Returns the number of entries listed."""
    top_info = os.lstat(top)
    if not stat.S_ISDIR(top_info.st_mode):
        raise ReapRefused("walk-root-not-a-directory")
    device, listed = top_info.st_dev, 0
    def walk(fd, rel, level):
        nonlocal listed
        try:
            with os.scandir(fd) as entries:
                names = [entry.name for entry in entries]
        except OSError:
            if soft is None:
                raise
            soft.append(rel or ".")
            return
        if listing is not None:
            listing(rel, names)
        for name in names:
            listed += 1
            if listed > limit or (deadline is not None and time.monotonic() > deadline):
                raise ReapRefused(incomplete)
            sub = rel + "/" + name if rel else name
            if reap_protected(name) or (blind is not None and blind(name)):
                visit(sub, name, None, level, device)  # named only: a protected (or blind) entry is never stat'ed, opened or entered
                continue
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                if soft is None:
                    raise
                soft.append(sub)
                continue
            if not visit(sub, name, info, level, device) or not stat.S_ISDIR(info.st_mode):
                continue
            if level + 1 > REAP_MAX_DEPTH:
                raise ReapRefused(too_deep)
            try:
                child = os.open(name, REAP_DIR_FLAGS, dir_fd=fd)
            except OSError:
                if soft is None:
                    raise
                soft.append(sub)
                continue
            try:
                opened = os.fstat(child)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise ReapRefused("walk-identity-changed")
                walk(child, sub, level + 1)
            finally:
                os.close(child)
    fd = os.open(top, REAP_ROOT_FLAGS)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (top_info.st_dev, top_info.st_ino):
            raise ReapRefused("walk-identity-changed")
        walk(fd, "", 0)
    finally:
        os.close(fd)
    return listed


def reap_git_env() -> dict:
    """The environment of every Git probe: no inherited GIT_* variable, no global or system config (so no home or XDG
    include), no optional locks, no prompts."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})
    return env


def reap_config_headers(text: str) -> None:
    """Every `[section]` header stands alone on its line (a trailing comment is fine). Git also reads several headers, or a
    header and a key, on one line (`[core] [include]`, `[core] [filter "x"]`), which a line-start check never sees."""
    for line in text.splitlines():
        body = line.lstrip()
        if not body.startswith("["):
            continue
        quoted, end = False, -1
        for index, char in enumerate(body):
            if char == '"':
                quoted = not quoted
            elif char == "]" and not quoted:
                end = index
                break
        tail = body[end + 1:].strip() if end >= 0 else None
        if tail is None or (tail and not tail.startswith(("#", ";"))):
            raise ReapRefused("git-error:unsafe-metadata:multiple-headers")


def reap_config_keys(path: str) -> list[str]:
    """The key names Git itself parses from one config file: `--no-includes` (nothing is followed), `--name-only` (no
    value), global/system config neutralised, hooks off, run from `/` so no repository config is read. Git's own parser
    decides what is a section, so the header forms a text check might miss are classified here."""
    argv = ["git", "-C", os.sep, "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false", "config", "--file", path,
            "--no-includes", "--null", "--name-only", "--list"]
    code, out = reap_probe(argv, 30, reap_git_env())
    if code != 0:
        raise ReapRefused("git-error:unsafe-metadata:config-unparsable")
    return [key for key in out.split("\0") if key]


def reap_config_gate(path: str, text: str) -> None:
    """Refuse a config that can read another file or run a program: any include, includeIf or filter key (case-insensitive,
    at any position, as Git parses them), any fsmonitor key, any fsck skipList key (Git opens the file it names at config
    load), a line continuation, several headers on one line."""
    if "\\" in text or "\0" in text:
        raise ReapRefused("git-error:unsafe-metadata:include-or-filter")
    for key in reap_config_keys(path):
        folded = key.casefold()
        if folded.partition(".")[0] in REAP_UNSAFE_CONFIG_SECTIONS or "fsmonitor" in folded:
            raise ReapRefused("git-error:unsafe-metadata:include-or-filter")
        if "fsck" in folded.split(".")[:-1] and folded.rpartition(".")[2] == "skiplist":
            raise ReapRefused("git-error:unsafe-metadata:fsck-skiplist")  # fsck.skipList (and receive./fetch.) makes Git read a named file at config load
    reap_config_headers(text)


def reap_config_stamp(roots: list[str]) -> list:
    """Identity and times of every config file Git reads for these roots; a difference means the gate's verdict is stale."""
    stamp = []
    for root in dict.fromkeys(roots):
        for name in ("config", "config.worktree"):
            try:
                info = os.lstat(os.path.join(root, name))
            except FileNotFoundError:
                stamp.append(None)
                continue
            stamp.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
    return stamp


def reap_git_metadata_gate(repo: str, allow_alternates: bool = False) -> dict:
    """Fence Git's metadata as well as its index: no protected paths, symlink metadata, include configs or executable
    clean filters. Linked worktree pointers are bounded, nofollow reads of safe metadata only. No native exemption. The
    metadata tree is walked descriptor-relative; the config files are classified by Git's own parser. Returns the roots and
    the config stamp the verdict holds for. `allow_alternates` (the archive only, which reads objects and never proves a
    removal from them) admits an objects/info/alternates whose every line names a real `objects` directory outside protected state."""
    def safe_path(path):
        absolute = os.path.abspath(path)
        if any(reap_protected(p) for p in Path(absolute).parts):
            raise ReapRefused("git-error:unsafe-metadata:protected-path")
        current = "/"
        for part in Path(absolute).parts[1:]:
            current = os.path.join(current, part)
            if stat.S_ISLNK(os.lstat(current).st_mode):
                raise ReapRefused("git-error:unsafe-metadata:symlink")
        return absolute
    def read_small(path):
        fd = os.open(safe_path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ReapRefused("git-error:unsafe-metadata:not-file")
            data = os.read(fd, 65537)
            if len(data) > 65536:
                raise ReapRefused("git-error:unsafe-metadata:oversized")
            return data.decode("utf-8", "strict")
        finally:
            os.close(fd)
    def visit(rel, name, info, level, device):
        if reap_protected(name):
            raise ReapRefused("git-error:unsafe-metadata:protected-name")
        if stat.S_ISLNK(info.st_mode) or info.st_dev != device:
            raise ReapRefused("git-error:unsafe-metadata:link-or-device")
        folded = reap_fold(rel)
        if folded == "objects/info/alternates":
            if not allow_alternates:
                raise ReapRefused("git-error:unsafe-metadata:object-alternates")
            alternates.append(os.path.join(current_root[0], rel))
        if folded == "info/grafts":  # a legacy graft file re-parents commits, so rev-list and fsck would not see the history it hides
            raise ReapRefused("git-error:unsafe-metadata:grafts")
        return True
    entry = safe_path(os.path.join(repo, ".git"))
    if stat.S_ISREG(os.lstat(entry).st_mode):
        pointer = read_small(entry).strip()
        if not pointer.startswith("gitdir: ") or "\n" in pointer:
            raise ReapRefused("git-error:unsafe-metadata:gitdir-pointer")
        entry = safe_path(os.path.join(repo, pointer[8:]))
    roots = [entry]
    common = os.path.join(entry, "commondir")
    if os.path.lexists(common):
        pointer = read_small(common).strip()
        if not pointer or "\n" in pointer:
            raise ReapRefused("git-error:unsafe-metadata:commondir-pointer")
        roots.append(safe_path(os.path.join(entry, pointer)))
    roots = list(dict.fromkeys(roots))
    listed, deadline, alternates, current_root = 0, time.monotonic() + 120, [], [None]
    for root in roots:
        current_root[0] = root
        listed += reap_fd_walk(root, visit, REAP_WALK_ENTRIES - listed, deadline, "git-error:metadata-scan-incomplete", "git-error:metadata-depth-limit")
    for path in alternates:  # one line per borrowed object store; every one must be a real objects directory, never protected
        objects = os.path.dirname(os.path.dirname(path))
        for line in read_small(path).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            target = os.path.normpath(line if os.path.isabs(line) else os.path.join(objects, line))
            if os.path.basename(target) != "objects" or not os.path.isdir(target) or reap_canon(target) != target:
                raise ReapRefused("git-error:unsafe-metadata:object-alternates-target")
            safe_path(target)
    stamp = reap_config_stamp(roots)  # taken before the files are read: a later change is a different stamp
    for root in roots:
        for name in ("config", "config.worktree"):
            path = os.path.join(root, name)
            if os.path.lexists(path):
                reap_config_gate(safe_path(path), read_small(path))
    return {"roots": roots, "stamp": stamp}


_REAP_GIT = threading.local()


def reap_git_gate(repo: str, allow_alternates: bool = False) -> dict:
    try:
        return reap_git_metadata_gate(repo, allow_alternates)
    except (OSError, UnicodeError):
        raise ReapRefused("git-error:metadata-unavailable")


@contextmanager
def reap_git_session(repo: str, allow_alternates: bool = False):
    """One metadata gate for all the probes of one proof, and one more when the proof is over. Inside, each probe only
    compares the config files' stamp with the gated one (cheap, and a config rewritten in between refuses), so a clean walk
    over thousands of directories is not thousands of metadata walks. Re-entrant per repository."""
    held = _REAP_GIT.__dict__.setdefault("held", {})
    if repo in held:
        yield
        return
    held[repo] = reap_git_gate(repo, allow_alternates)
    try:
        yield
    finally:
        held.pop(repo, None)
    reap_git_gate(repo, allow_alternates)


def reap_git(repo: str, *args: str) -> tuple[int, str]:
    return reap_git_input(repo, None, *args)


def reap_git_input(repo: str, data: str | None, *args: str) -> tuple[int, str]:
    """Run one Git probe. Outside a proof session the metadata is gated first; inside one the gated config is re-checked
    before the probe AND after it. That detects, and refuses on, a config rewritten before or during the probe; it cannot stop
    Git from having read a config rewritten inside that window, so it is not an immutability guarantee (see docs/SAFETY.md)."""
    held = _REAP_GIT.__dict__.setdefault("held", {}).get(repo)
    if held is None:
        reap_git_gate(repo)
    else:
        try:
            if reap_config_stamp(held["roots"]) != held["stamp"]:
                raise ReapRefused("git-error:unsafe-metadata:config-changed")
        except (OSError, UnicodeError):
            raise ReapRefused("git-error:metadata-unavailable")
    argv = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-c", "submodule.recurse=false",
            "-c", "core.excludesFile=" + os.devnull, "-c", "core.attributesFile=" + os.devnull, "-c", "core.worktree=" + repo, "-C", repo, *args]
    outcome = reap_probe(argv, 120, reap_git_env()) if data is None else reap_probe(argv, 120, reap_git_env(), data)
    if held is not None:  # fail closed per probe: a config rewritten while Git ran (a rewrite always moves ctime) discards this answer
        try:
            if reap_config_stamp(held["roots"]) != held["stamp"]:
                raise ReapRefused("git-error:unsafe-metadata:config-changed")
        except (OSError, UnicodeError):
            raise ReapRefused("git-error:metadata-unavailable")
    return outcome


def reap_safe_name(name: str) -> bool:
    """A name that may be handed to Git on stdin: printable, no newline or control character, valid text, and not a
    pathspec magic prefix (`:(...)`)."""
    return bool(name) and name.isprintable() and not name.startswith(":")


def reap_git_ignored(repo: str, names: list[str]) -> set:
    """Which of these repo-relative names Git ignores, from ONE `check-ignore --stdin -z`. Only safe names are asked; an
    unsafe name is never reported ignored, so the caller keeps the repository."""
    asked = [name for name in names if reap_safe_name(name)]
    if not asked:
        return set()
    code, out = reap_git_input(repo, "\0".join(asked) + "\0", "check-ignore", "-z", "--stdin")
    if code not in (0, 1):
        raise ReapRefused(f"git-error:check-ignore:{code}")
    return {name for name in out.split("\0") if name}


def reap_git_clean_closed(repo: str, protected_ok: bool = False) -> tuple[int, str]:
    with reap_git_session(repo):
        return reap_git_clean_walk(repo, protected_ok)


def reap_git_clean_walk(repo: str, protected_ok: bool = False) -> tuple[int, str]:
    """Close Git's traversal surface before status: index-only inventory, no protected/submodule index entries, no
    assume-unchanged / skip-worktree / fsmonitor-valid / unmerged index flag (they hide edits from status) and no
    symlink directory ancestors, then tracked-only status. Untracked proof is our bounded nofollow names walk;
    check-ignore receives ONLY safe names, once per directory (`--stdin -z`), and only for names that could be untracked:
    a tracked file and a directory holding tracked files are never asked. Git never enumerates a working tree or a
    protected subtree."""
    code, index = reap_git(repo, "ls-files", "-v", "-f", "--stage", "-z")
    if code:
        raise ReapRefused(f"git-error:index:{code}")
    tracked, tracked_dirs = set(), set()
    for record in index.split("\0"):
        if not record:
            continue
        meta, sep, rel = record.partition("\t")
        if not meta.startswith("H "):  # lowercase = assume-unchanged / fsmonitor-valid, S = skip-worktree, M = unmerged: status would not see an edit
            raise ReapRefused("git-unsafe-index:index-flag")
        meta = meta[2:]
        parts = Path(rel).parts
        if not sep or not parts or Path(rel).is_absolute() or ".." in parts:
            raise ReapRefused("git-unsafe-index:invalid-path")
        if any(reap_protected(part) for part in parts) and not reap_env_template(rel) and not protected_ok:
            raise ReapRefused("git-unsafe-index:protected-path")
        if meta.startswith("160000 "):
            raise ReapRefused("git-unsafe-index:submodule")
        current = repo
        for part in parts[:-1]:
            current = os.path.join(current, part)
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                break  # tracked-only status reports the missing file
            if not stat.S_ISDIR(info.st_mode):
                raise ReapRefused("git-unsafe-index:non-directory-ancestor")
        tracked.add(rel)
        tracked_dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
    code, out = reap_git(repo, "status", "--porcelain=v1", "--untracked-files=no", "--ignore-submodules=all")
    if code or out.strip():
        return code, out
    device, listed = os.lstat(repo).st_dev, 0
    deadline = time.monotonic() + 120
    def walk(fd, rel, level):
        nonlocal listed
        with os.scandir(fd) as entries:
            names = [entry.name for entry in entries]
        enter, asked = [], []
        for name in names:
            listed += 1
            if listed > REAP_WALK_ENTRIES or time.monotonic() > deadline:
                raise ReapRefused("git-untracked-scan-incomplete")
            if reap_protected(name):
                continue
            if reap_is_git(name):
                if rel:
                    return True
                continue
            sub = rel + "/" + name if rel else name
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if info.st_dev != device:
                raise ReapRefused("git-untracked-crosses-device")
            is_dir = stat.S_ISDIR(info.st_mode)
            if not is_dir and sub in tracked:
                continue
            if is_dir and sub in tracked_dirs:
                enter.append((name, sub, info))
            else:
                asked.append((name, sub, info))
        ignored = reap_git_ignored(repo, [sub for _, sub, _ in asked]) if asked else set()
        for name, sub, info in asked:
            if sub in ignored or (name == ".astro" and stat.S_ISDIR(info.st_mode) and sub not in tracked_dirs):
                continue  # Astro's untracked metadata output is regenerable even without a project ignore rule.
            if not stat.S_ISDIR(info.st_mode):
                return True
            enter.append((name, sub, info))
        for name, sub, info in enter:
            if level + 1 > REAP_MAX_DEPTH:
                raise ReapRefused("git-untracked-depth-limit")
            child = os.open(name, REAP_DIR_FLAGS, dir_fd=fd)
            try:
                opened = os.fstat(child)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise ReapRefused("git-untracked-identity-changed")
                if walk(child, sub, level + 1):
                    return True
            finally:
                os.close(child)
        return False
    fd = os.open(repo, REAP_ROOT_FLAGS)
    try:
        dirty = walk(fd, "", 0)
    finally:
        os.close(fd)
    return 0, "?? safe-untracked-path" if dirty else ""


def reap_git_repo_proof(root: str, needs: tuple) -> list[str]:
    if not os.path.lexists(os.path.join(root, ".git")):
        raise ReapRefused("git-error:no-git-entry")
    with reap_git_session(root):
        return reap_git_repo_checks(root, needs)


def reap_git_repo_checks(root: str, needs: tuple) -> list[str]:
    """Repository-level proofs, by exit code: a git entry exists, it is a git toplevel with its git dir present and HEAD
    resolved, and (as the class needs) its work tree is clean with no stash and HEAD is contained in a remote-tracking
    ref. Any git error, including a broken worktree, a missing git dir or an unresolved HEAD, preserves the item.
    `pushed` proves HEAD only (enough when the clone itself stays); `all-refs` is the whole-repository proof."""
    if not os.path.lexists(os.path.join(root, ".git")):
        raise ReapRefused("git-error:no-git-entry")
    code, top = reap_git(root, "rev-parse", "--show-toplevel")
    if code != 0 or reap_canon(top.strip()) != reap_canon(root):
        raise ReapRefused("git-error:not-a-git-toplevel")
    code, out = reap_git(root, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
    dirs = out.splitlines()
    if code != 0 or len(dirs) != 2 or not all(os.path.isdir(d) for d in dirs):
        raise ReapRefused("git-error:git-dir-missing")
    code, _ = reap_git(root, "rev-parse", "--verify", "-q", "HEAD^{commit}")
    if code != 0:
        raise ReapRefused("git-error:head-unresolved")
    passed = ["git-toplevel", "git-head"]
    if "clean" in needs:
        code, out = reap_git_clean_closed(root, "protected-ok" in needs)
        if code != 0:
            raise ReapRefused(f"git-error:status:{code}")
        if out.strip():
            raise ReapRefused("dirty:" + ",".join(sorted({line[:2].strip() or "?" for line in out.splitlines()})))
        code, _ = reap_git(root, "rev-parse", "--verify", "-q", "refs/stash")
        if code == 0:
            raise ReapRefused("stash-present")
        if code != 1:
            raise ReapRefused(f"git-error:stash:{code}")
        passed.append("git-clean")
    if "pushed" in needs:
        code, out = reap_git(root, "rev-list", "--count", "HEAD", "--not", "--remotes")
        if code != 0 or not out.strip().isdigit():
            raise ReapRefused(f"git-error:rev-list:{code}")
        if int(out) > 0:
            raise ReapRefused(f"unpushed:{int(out)}")
        passed.append("git-pushed")
    if "all-refs" in needs:
        passed.extend(reap_git_whole_repo_checks(root, dirs))
    if "clone-root" in needs:
        if reap_canon(dirs[0]) == reap_canon(dirs[1]):
            passed.extend(reap_git_whole_repo_checks(root, dirs))
        else:
            passed.extend(reap_git_worktree_checks(root))
        passed.extend(reap_git_ignored_known(root, "protected-ok" in needs))
    return passed


def reap_git_worktree_checks(root: str) -> list[str]:
    """A linked worktree takes its own HEAD reflog with it: every commit that reflog names must be on a remote. Branches,
    tags and the stash live in the main repository and stay."""
    code, out = reap_git(root, "reflog", "show", "--format=%H", "HEAD", "--")
    if code != 0:
        raise ReapRefused(f"git-error:worktree-reflog:{code}")
    commits = sorted({line.strip() for line in out.splitlines() if re.fullmatch(r"[0-9a-f]{40,64}", line.strip())})
    if commits:
        code, out = reap_git_input(root, "\n".join(commits) + "\n", "rev-list", "--count", "--stdin", "--not", "--remotes")
        if code != 0 or not out.strip().isdigit():
            raise ReapRefused(f"git-error:worktree-reflog-rev-list:{code}")
        if int(out) > 0:
            raise ReapRefused(f"unpushed:{int(out)}:worktree-reflog")
    return ["git-worktree-reflog-pushed"]


def reap_git_ignored_known(root: str, protected_ok: bool = False) -> list[str]:
    """Every ignored entry is known build or tool output (CLONE_IGNORED_OK). Runs after the protected scan proved the tree
    holds no protected name, so letting git list ignored paths cannot enter protected state."""
    code, out = reap_git(root, "status", "--porcelain=v1", "-z", "--ignored=matching", "--untracked-files=normal", "--ignore-submodules=all")
    if code != 0:
        raise ReapRefused(f"git-error:status-ignored:{code}")
    for record in out.split("\0"):
        if not record.startswith("!! "):
            continue
        parts = [part for part in record[3:].split("/") if part]
        if protected_ok and any(reap_protected(part) for part in parts):
            continue  # a secret copy or local state that goes with the clone (cloneProtectedCopies=remove)
        if not any(part in CLONE_IGNORED_OK for part in parts) and not (parts and parts[-1].endswith(CLONE_IGNORED_SUFFIXES)):
            raise ReapRefused("ignored-unknown:" + "/".join(parts)[:80])
    return ["git-ignored-only-build-output"]


def reap_git_whole_repo_checks(root: str, dirs: list[str]) -> list[str]:
    """Extra proofs when the WHOLE repository goes (a nested repository of a `job-cache-subdir`, `.git` included), so HEAD
    alone proves nothing: every local branch must be contained in a remote-tracking ref (HEAD and `refs/heads/*`, counted
    together), no other ref may exist, and no linked worktree may depend on this repository. Offline Git cannot prove a tag
    was pushed (a fetched tag and a local-only one look alike, and an annotated tag object carries its own message that a
    pushed commit does not), and a commit reachable from a remote branch says nothing about it, so ANY tag, and any ref
    that is not a local branch or a remote-tracking ref (notes, replace, stash, ...), keeps the repository. A repository
    that is a linked worktree, or has linked worktrees, is kept whole: removing it would break or strand the other side.
    Reflog-only commits and unreachable objects are local work too: they must be proved remote-reachable or the whole repository stays."""
    if reap_canon(dirs[0]) != reap_canon(dirs[1]):
        raise ReapRefused("linked-worktree")
    try:
        linked = [name for name in os.listdir(os.path.join(dirs[1], "worktrees"))]
    except FileNotFoundError:
        linked = []
    except OSError:
        raise ReapRefused("git-error:worktrees-unreadable")
    if linked:
        raise ReapRefused(f"linked-worktrees:{len(linked)}")
    code, out = reap_git(root, "for-each-ref", "--format=%(refname)")
    if code != 0:
        raise ReapRefused(f"git-error:for-each-ref:{code}")
    refs = out.splitlines()
    tags = [ref for ref in refs if ref.startswith("refs/tags/")]
    others = [ref for ref in refs if not ref.startswith(("refs/heads/", "refs/remotes/", "refs/tags/"))]
    if tags:
        raise ReapRefused(f"unproved-tags:{len(tags)}")
    if others:
        raise ReapRefused(f"unproved-refs:{len(others)}")
    code, out = reap_git(root, "rev-list", "--count", "HEAD", "--branches", "--reflog", "--not", "--remotes")
    if code != 0 or not out.strip().isdigit():
        raise ReapRefused(f"git-error:rev-list:{code}")
    if int(out) > 0:
        raise ReapRefused(f"unpushed:{int(out)}:local-refs-and-reflogs")
    code, out = reap_git(root, "fsck", "--connectivity-only", "--unreachable", "--no-reflogs", "--no-progress")
    if code != 0:
        raise ReapRefused(f"git-error:object-reachability:{code}")
    if out.strip():
        raise ReapRefused("unproved-git-objects")
    return ["git-all-branches-and-reflogs-pushed", "git-no-tags-or-extra-refs", "git-no-linked-worktrees", "git-no-unproved-objects"]


def reap_git_proof(root: str, path: str, needs: tuple, cache: dict | None = None) -> list[str]:
    if not os.path.lexists(os.path.join(root, ".git")):
        raise ReapRefused("git-error:no-git-entry")
    with reap_git_session(root):
        return reap_git_item_checks(root, path, needs, cache)


def reap_git_item_checks(root: str, path: str, needs: tuple, cache: dict | None = None) -> list[str]:
    """All git proofs one class needs for one item. `cache` (audits only) shares the per-repository verdict."""
    key = (root, tuple(n for n in needs if n not in ("ignored", "untracked")))
    if cache is not None and key in cache:
        if isinstance(cache[key], str):
            raise ReapRefused(cache[key])
        passed = list(cache[key])
    else:
        try:
            passed = reap_git_repo_proof(root, needs)
        except ReapRefused as refusal:
            if cache is not None:
                cache[key] = str(refusal)
            raise
        if cache is not None:
            cache[key] = list(passed)
    current = os.path.dirname(path)
    while current != root and reap_inside(current, root):
        if os.path.lexists(os.path.join(current, ".git")):
            raise ReapRefused("nested-git-boundary")
        current = os.path.dirname(current)
    rel = os.path.relpath(path, root)
    if "ignored" in needs:
        code, _ = reap_git(root, "check-ignore", "-q", "--", rel)
        known_astro = os.path.basename(path) == ".astro" and "untracked" in needs \
            and os.path.isdir(path) and not os.path.islink(path) and reap_astro_cache(path)
        if code == 1 and not known_astro:
            raise ReapRefused("not-git-ignored")
        if code not in (0, 1):
            raise ReapRefused(f"git-error:check-ignore:{code}")
        passed.append("known-regenerable-astro" if code == 1 else "git-ignored")
    if "untracked" in needs:
        # Case-folded and literal: a case-insensitive filesystem shows the index entry `Node_Modules/x` as `node_modules/x`,
        # and a case-sensitive pathspec would call that folder untracked and remove the tracked work living in it.
        code, out = reap_git(root, "ls-files", "-z", "--", ":(icase,literal)" + rel)
        if code != 0:
            raise ReapRefused(f"git-error:ls-files:{code}")
        if out:
            raise ReapRefused("tracked-files")
        passed.append("git-untracked")
    return passed


def reap_astro_cache(path: str) -> bool:
    """True when Astro's own generated cache files are in the folder: an unignored `.astro` is output only when Astro wrote it.
    A folder of the same name holding anything else is untracked user work and keeps the full proofs."""
    try:
        names = set(os.listdir(path))
    except OSError:
        return False
    return any(name in names for name in REAP_ASTRO_FILES)



def reap_linked_worktree(root: str) -> bool:
    """True for a `git worktree add` checkout: its git dir differs from the common dir. A main checkout never is."""
    code, out = reap_git(root, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
    lines = out.split()
    return code == 0 and len(lines) == 2 and reap_canon(lines[0]) != reap_canon(lines[1])


def reap_private_alias(name: str) -> str:
    return "/private" + name if name.startswith(("/var/", "/tmp/", "/etc/")) else name


def reap_process_table() -> dict:
    """Every open file (cwd included) and every command line, as of now. Names and pids only. The command lines leave out
    this process and its actual ancestors from the same `ps` snapshot (the wrapper that named a path to invoke the reaper);
    cwd and open handles are never skipped except this process's own."""
    code, out = reap_probe(["lsof", "-n", "-P", "-Fpcfn"], 60)
    if code not in (0, 1) or not out.strip():
        raise ReapRefused(f"probe-failed:lsof-scan:{code}")  # an empty listing means lsof is broken, never "nothing open"
    files, pid, command, fd = [], "", "", ""
    for line in out.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid, command, fd = value, "", ""
        elif tag == "c":
            command = value
        elif tag == "f":
            fd = value
        elif tag == "n" and value.startswith("/") and pid != str(os.getpid()):  # never count this CLI's own scans
            files.append((pid, command, fd, reap_canon(value) if fd == "cwd" else value))
    code, out = reap_probe(["ps", "-ww", "-axo", "pid=,ppid=,command="], 30)
    if code != 0 or not out.strip():
        raise ReapRefused(f"probe-failed:ps:{code}")
    table = {}
    for line in out.splitlines():
        fields = line.split(None, 2)
        if len(fields) >= 2 and fields[0].isdigit() and fields[1].isdigit():
            table[int(fields[0])] = (int(fields[1]), fields[2].strip() if len(fields) > 2 else "")
    me = os.getpid()
    if me not in table:
        raise ReapRefused("probe-failed:ps:self-missing")
    ancestors, current = set(), me
    while current in table and current not in ancestors:
        ancestors.add(current)
        current = table[current][0]
    return {"files": files, "commands": [(str(number), text) for number, (_, text) in sorted(table.items()) if number not in ancestors]}


def reap_holders(path: str, root: str, table: dict | None = None) -> None:
    """Preserve the item while any process has its cwd or an open handle inside the clone/scope, or names it in a
    command line. `lsof +D` is never used. With no table this runs fresh probes (the deletion path); an audit passes
    one shared snapshot instead. Comparisons fold case and see through /var and /tmp."""
    if table is None:
        code, out = reap_probe(["lsof", "-n", "-P", "-Fp", "--", path], 30)
        if code not in (0, 1):
            raise ReapRefused(f"probe-failed:lsof:{code}")
        if out.strip():
            raise ReapRefused("in-use:target-open:pid " + ",".join(sorted({line[1:] for line in out.splitlines() if line.startswith("p")})[:5]))
        table = reap_process_table()
    folded, hits = reap_fold(root), []
    for pid, command, fd, name in table["files"]:
        if reap_inside(reap_fold(reap_private_alias(name)), folded):
            hits.append(f"{'cwd' if fd == 'cwd' else 'open-handle'}:{pid}({command})")
    for pid, command in table["commands"]:
        if folded in reap_fold(command):
            hits.append(f"command-line:{pid}({os.path.basename(command.split()[0]) if command.split() else '?'})")
    if hits:
        raise ReapRefused("in-use:" + ";".join(hits[:4]))


def reap_metadata_size(path: str, seconds: float = 120, entry_limit: int = REAP_WALK_ENTRIES,
                  max_depth: int = REAP_MAX_DEPTH, templates: frozenset = frozenset(), protected_ok: bool = False) -> dict:
    """Bounded allocated-metadata lower bound. Protected roots are skipped BEFORE any stat/open; no file contents,
    symlink targets or other devices are read. Incomplete bytes are partial, never an estimate of skipped contents.
    Descriptor-relative directory opens fence replacement/symlink races. Named failures forbid removal."""
    result = {"bytes": None, "complete": False, "reason": None}
    if any(reap_protected(p) for p in Path(path).parts):
        result["reason"] = "size-protected-ancestor"
        return result
    deadline, listed, seen = time.monotonic() + seconds, 0, set()
    def walk(fd, level, rel=""):
        nonlocal listed
        with os.scandir(fd) as entries:
            for entry in entries:
                if time.monotonic() > deadline:
                    raise ReapRefused("size-time-limit")
                listed += 1
                if listed > entry_limit:
                    raise ReapRefused("size-entry-limit")
                if reap_protected(entry.name):
                    if not protected_ok and os.path.join(path, rel, entry.name) not in templates:  # not counted: never entered
                        result["reason"] = result["reason"] or "size-protected-skipped"
                    continue
                info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                if info.st_dev != device:
                    raise ReapRefused("size-crosses-device")
                key = (info.st_dev, info.st_ino)
                if key not in seen:
                    seen.add(key)
                    result["bytes"] += info.st_blocks * 512
                if stat.S_ISDIR(info.st_mode):
                    if level + 1 > max_depth:
                        raise ReapRefused("size-depth-limit")
                    child = os.open(entry.name, REAP_DIR_FLAGS, dir_fd=fd)
                    try:
                        opened = os.fstat(child)
                        if (opened.st_dev, opened.st_ino) != key:
                            raise ReapRefused("size-identity-changed")
                        walk(child, level + 1, os.path.join(rel, entry.name))
                    finally:
                        os.close(child)
    try:
        info = os.lstat(path)
        device = info.st_dev
        if not stat.S_ISDIR(info.st_mode):
            if stat.S_ISLNK(info.st_mode):
                raise ReapRefused("size-root-symlink")
            result["bytes"] = info.st_blocks * 512
        else:
            fd = os.open(path, REAP_ROOT_FLAGS)
            try:
                opened = os.fstat(fd)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise ReapRefused("size-identity-changed")
                seen.add((info.st_dev, info.st_ino))
                result["bytes"] = info.st_blocks * 512
                walk(fd, 0)
            finally:
                os.close(fd)
        result["complete"] = result["reason"] is None
    except ReapRefused as refusal:
        result["reason"] = str(refusal)
    except OSError as error:
        result["reason"] = "size-error:" + reap_named(error)
    return result


def reap_size(path: str, budget: float = 120) -> tuple[int | None, bool]:
    result = reap_metadata_size(path, budget)
    return result["bytes"], result["complete"]


def reap_vendored_file(fd: int, name: str, digest: bool = False):
    """Read a bounded, owned regular file without following links; protected bytes are only hashed."""
    leaf = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    try:
        before = os.fstat(leaf)
        limit = 64 * 1024 * 1024 if digest else 4 * 1024 * 1024
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1 or before.st_size > limit:
            raise ReapRefused("vendored-file-unsafe")
        hashed, chunks, total = hashlib.sha256(), [], 0
        while True:
            chunk = os.read(leaf, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ReapRefused("vendored-file-too-large")
            if digest:
                hashed.update(chunk)
            else:
                chunks.append(chunk)
        after = os.fstat(leaf)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != \
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ReapRefused("vendored-file-changed")
        return hashed.hexdigest() if digest else b"".join(chunks)
    finally:
        os.close(leaf)


def reap_vendored_templates(path: str) -> dict:
    """Manifest-verified env files only; a state directory, missing entry or mismatch invalidates its entire manifest."""
    manifests, protected, states = [], [], []
    def visit(rel, name, info, level, device):
        if reap_protected(name):
            (states if reap_fold(name) in REAP_PROTECTED_ROOTS else protected).append(rel)
            return False
        if reap_is_git(name):
            return False
        if name == "FILES.sha256" and stat.S_ISREG(info.st_mode):
            manifests.append(rel)
        if stat.S_ISDIR(info.st_mode) and info.st_dev != device:
            raise ReapRefused("vendored-crosses-filesystem")
        return True
    try:
        reap_fd_walk(path, visit, REAP_WALK_ENTRIES, blind=reap_is_git, listing=reap_refuse_git_layout)
    except (OSError, ReapRefused):
        return {}  # the ordinary protected/git scan still decides; no exception is granted by an incomplete walk
    verified = {}
    def read_relative(rel, digest=False):
        fd = os.open(path, REAP_ROOT_FLAGS)
        try:
            device = os.fstat(fd).st_dev
            for part in rel.split("/")[:-1]:
                child = os.open(part, REAP_DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
                if os.fstat(fd).st_dev != device:
                    raise ReapRefused("vendored-crosses-filesystem")
            return reap_vendored_file(fd, rel.rpartition("/")[2], digest)
        finally:
            os.close(fd)
    for manifest in manifests:
        base = manifest.rpartition("/")[0]
        prefix = base + "/" if base else ""
        leaves = [rel for rel in protected if rel.startswith(prefix)]
        if not leaves or any(rel.startswith(prefix) for rel in states):
            continue
        try:
            entries = {}
            for line in read_relative(manifest).decode("utf-8").splitlines():
                match = re.fullmatch(r"([0-9a-fA-F]{64})  \./(.+)", line)
                if not match:
                    raise ValueError("manifest syntax")
                digest, rel = match.groups()
                if rel.startswith("/") or any(part in ("", ".", "..") for part in rel.split("/")) or rel in entries:
                    raise ValueError("manifest path")
                entries[rel] = digest.lower()
            matched = {}
            for rel in leaves:
                expected = entries.get(rel[len(prefix):])
                if expected is None or read_relative(rel, True) != expected:
                    raise ValueError("manifest mismatch")
                matched[os.path.join(path, rel)] = expected
            verified.update(matched)
        except (OSError, ValueError, ReapRefused):
            continue  # leave every protected name blocked, without logging bytes
    return verified


def reap_protected_scan(path: str, allow_git: bool = False, markers: bool = False, budget: int = REAP_WALK_ENTRIES,
                        repos: list | None = None, nested_disposable: bool = False, templates: frozenset = frozenset(),
                        protected_ok: bool = False, git_dirs: list | None = None, secrets: list | None = None,
                        expiring_evidence: bool = False) -> int:
    """Names only, descriptor-relative (reap_fd_walk). Preserve the item if a protected name (.wrangler, .vercel, .env*,
    .dev.vars*) or, unless `allow_git`, a nested git repository sits anywhere inside it. The walk raises on seeing a
    protected name and never scans it, so it cannot enter the state it protects. Symlinks are named, not followed, and a
    directory swapped for a link is never entered. An unreadable directory, a mountpoint, a tree deeper than REAP_MAX_DEPTH
    or an exhausted budget is a refusal, never a pass. With `markers` a retention marker below the top of the item is
    ambiguous. With `allow_git`, `repos` receives the directory holding EVERY `.git` entry found, at any depth and under any
    name (node_modules and dist included): the caller must prove each of them or refuse. A Git directory with no `.git` name
    (HEAD, objects and refs together: a bare repository, a separate git dir) is refused at any depth, in either mode, as
    `unproved-git-dir:<rel>`; it is never proved, so it is never removed. With `git_dirs` (the archive pass, cloneArchive=archive)
    such a directory is named there instead of refused: the archive saves it whole before anything is removed. With `secrets`
    (a whole clone, protected_ok) every protected name that is not a tracked template is appended: the archive saves those too."""
    if any(reap_protected(part) for part in Path(path).parts):
        raise ReapRefused("protected-scan-ancestor")
    protected_flags = sum(getattr(stat, flag, 0) for flag in ("SF_NOUNLINK", "SF_IMMUTABLE", "SF_APPEND", "SF_RESTRICTED", "UF_NOUNLINK", "UF_IMMUTABLE", "UF_APPEND"))
    if getattr(os.lstat(path), "st_flags", 0) & protected_flags:
        raise ReapRefused("filesystem-protected-flags:root")
    vendored = reap_vendored_templates(path)
    def listing(rel, names):
        current = os.path.join(path, rel) if rel else path
        if expiring_evidence:
            if reap_cache_retention(current) not in ("unmarked", REAP_DISPOSABLE, "evidence"):
                raise ReapRefused("marked-evidence-preserving-marker:" + (rel or "."))
            if any(marked_evidence_keep(n) for n in names):
                raise ReapRefused("marked-evidence-kept-record:" + (rel or "."))
        if (rel or allow_git) and reap_fold(os.path.basename(current)) == "target" and reap_cache_retention(current) != REAP_DISPOSABLE \
                and reap_target_release_bearing(current):
            # Also protect a release-bearing target when an enclosing clone/cache is
            # considered: otherwise whole-clone cleanup would bypass output retention.
            raise ReapRefused("retention-preserved:release-runtime-rollback:" + (rel or "target"))
        try:
            reap_refuse_git_layout(rel, names)
        except ReapRefused as refusal:
            if git_dirs is None or not str(refusal).startswith("unproved-git-dir:"):
                raise
            git_dirs.append(rel or ".")
    def visit(rel, name, info, level, device):
        if reap_protected(name):
            if expiring_evidence:
                raise ReapRefused(f"protected-leaf:{rel}")
            if protected_ok:
                if secrets is not None and os.path.join(path, rel) not in templates:
                    secrets.append(rel)  # named for the archive (it copies them owner-only before the clone goes); never opened here
                return False  # a whole clone under cloneProtectedCopies=remove: named, never entered; it goes with the clone
            if os.path.join(path, rel) in templates or os.path.join(path, rel) in vendored:  # tracked or manifest-verified template
                try:
                    found = os.lstat(os.path.join(path, rel))  # metadata only: never opened, never followed
                except OSError:
                    raise ReapRefused(f"protected-leaf:{rel}")
                if stat.S_ISREG(found.st_mode) and found.st_nlink == 1 and found.st_uid == os.getuid():
                    return False
            raise ReapRefused(f"protected-leaf:{rel}")
        if reap_is_git(name):
            if not allow_git:
                raise ReapRefused(f"nested-git:{rel}")
            if repos is not None:
                parent = rel.rpartition("/")[0]
                repos.append(os.path.join(path, parent) if parent else path)
        if info is not None and getattr(info, "st_flags", 0) & protected_flags:
            raise ReapRefused("filesystem-protected-flags:" + rel)
        if markers and level > 0 and reap_fold(name).startswith(REAP_MARKER_PREFIX) \
                and not ((nested_disposable and name == REAP_MARKER_PREFIX + REAP_DISPOSABLE and not stat.S_ISDIR(info.st_mode))
                         or (expiring_evidence and name in (REAP_MARKER_PREFIX + "evidence", REAP_MARKER_PREFIX + REAP_DISPOSABLE)
                             and stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1)):
            raise ReapRefused(f"retention-ambiguous:nested-marker:{rel}")  # a whole clone may hold build output a build queue marked
        if stat.S_ISDIR(info.st_mode) and info.st_dev != device:
            raise ReapRefused(f"mountpoint:{rel}")
        return True
    try:
        return reap_fd_walk(path, visit, budget, incomplete="protected-scan-incomplete", too_deep="protected-scan-too-deep",
                            blind=None if allow_git else reap_is_git, listing=listing)
    except OSError:
        raise ReapRefused("protected-scan-incomplete")


def reap_kind(rel: tuple) -> str | None:
    """What an item is, from its path below its root alone; None means it is not a regenerable output. Nothing below a
    protected name is ever an item: there is no .wrangler/tmp or .vercel/output carve-out."""
    if rel and rel[-1] in REAP_REGENERABLE:
        if any(reap_fold(c) in REAP_REGENERABLE or reap_is_git(c) or reap_protected(c) for c in rel[:-1]):
            return None
        return rel[-1]
    return None


def reap_scan_clone(root: str) -> tuple[list[tuple[str, str]], bool]:
    """Regenerable dirs inside one clone: a bounded names-only, descriptor-relative walk that never enters .git, protected
    names, symlinks, other devices or the outputs it finds. Returns (path, kind) pairs and whether the walk was cut short."""
    if any(reap_protected(part) for part in Path(root).parts):
        return [], True
    found, soft = [], []
    def visit(rel, name, info, level, device):
        if reap_is_git(name) or reap_protected(name) or not stat.S_ISDIR(info.st_mode) or info.st_dev != device:
            return False  # a mountpoint is neither walked nor offered
        if name in REAP_REGENERABLE:
            found.append((os.path.join(root, rel), name))
            return False
        return level + 1 < REAP_SCAN_DEPTH
    try:
        reap_fd_walk(root, visit, REAP_SCAN_ENTRIES, soft=soft)
    except (ReapRefused, OSError):
        return found, True
    return found, bool(soft)


def reap_nested_repos(path: str) -> list[str]:
    """Every directory below (or at) `path` that holds a `.git` entry: vault clones in a cache subdirectory carry the clone
    proofs too. The same scan as the protected scan, so it covers every `.git` that scan permits, at any depth up to
    REAP_MAX_DEPTH and inside node_modules, dist and every other regenerable name."""
    repos: list = []
    reap_protected_scan(path, allow_git=True, repos=repos)
    return sorted(set(repos))


def reap_cache_retention(path: str) -> str:
    """The retention of any candidate, from marker NAMES at its top level only (a marker is never opened).
    `disposable` needs exactly one marker, `.ut-retention-disposable`, a plain file of this user. No marker is `unmarked`;
    another class's marker (evidence, customer, ...) is that class; several markers, a marker that is not a plain file of
    this user, a differently-cased disposable marker or an unknown label are `ambiguous`. Only `disposable` is ever removed."""
    found = []
    if any(reap_protected(part) for part in Path(path).parts):
        return "ambiguous"
    try:
        fd = os.open(path, REAP_ROOT_FLAGS)  # no symlink in any component, including a late swapped ancestor
        try:
            with os.scandir(fd) as entries:
                for listed, entry in enumerate(entries, 1):
                    if listed > REAP_WALK_ENTRIES:
                        return "ambiguous"
                    if reap_fold(entry.name).startswith(REAP_MARKER_PREFIX):
                        found.append(entry.name)
            if not found:
                return "unmarked"
            if len(found) > 1:
                return "ambiguous"
            marker = found[0]
            info = os.stat(marker, dir_fd=fd, follow_symlinks=False)
        finally:
            os.close(fd)
    except OSError:
        return "ambiguous"
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        return "ambiguous"
    label = reap_fold(marker)[len(REAP_MARKER_PREFIX):]
    if label == REAP_DISPOSABLE:
        return label if marker == REAP_MARKER_PREFIX + label else "ambiguous"
    return label if label in REAP_RETENTION_CLASSES else "ambiguous"


def reap_retention(item: dict, proof: dict | None = None) -> str:
    """The retention the reaper establishes itself, now. The item's own `retention` is only the builder's assertion."""
    spec = REAP_CLASSES.get(item.get("class")) or {}
    if spec.get("retention") == "marker":
        return reap_item_retention(item, proof)
    return "ambiguous"


def reap_target_release_bearing(path: str) -> bool:
    """Names-only check of Cargo's target/release and target/<triple>/release layouts.
    Signed/distributable artifacts at these levels also require an explicit output disposable marker.
    Unreadable or over-budget inventories preserve; protected directories and symlinks are never entered.
    """
    listed = 0
    artifacts = (".app", ".dmg", ".pkg", ".ipa", ".msix", ".sig", ".asc")
    def scan(fd, depth):
        nonlocal listed
        with os.scandir(fd) as entries:
            for entry in entries:
                listed += 1
                if listed > REAP_SCAN_ENTRIES:
                    return True
                name = reap_fold(entry.name)
                if name == "release" or name.endswith(artifacts):
                    return True
                if depth == 0 and not reap_protected(entry.name) and not reap_is_git(entry.name):
                    info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(entry.name, REAP_DIR_FLAGS, dir_fd=fd)
                        try:
                            opened = os.fstat(child)
                            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or scan(child, 1):
                                return True
                        finally:
                            os.close(child)
        return False
    try:
        fd = os.open(path, REAP_ROOT_FLAGS)
        try:
            return scan(fd, 0)
        finally:
            os.close(fd)
    except OSError:
        return True


def marked_evidence_expiry_days(policy: dict) -> int:
    value = policy.get("markedEvidenceExpiryDays", 5)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def marked_evidence_keep(name: str) -> bool:
    """Durable job records and retained material are never part of marked-evidence expiry."""
    # camelCase is the same class of record as a hyphenated name: "AppleCerts" is "apple certs", and
    # "SubscriptionKey_X.p8" is "subscription key x.p8", so a word boundary exists where the writer put a capital.
    name = reap_fold(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name))
    # Records in any position and any number: "final-report.md", "review-receipt.md" and "handoffs" are one record.
    records = r"reports?|summar(?:y|ies)|outcomes?|handoffs?|receipts?"
    # Retained classes, matched by prefix and after any non-alphanumeric edge so "Old Customer List" and
    # "customerdata" keep their data. Identity and signing material is in the same class (2026-10-19 review).
    retained = (r"release|runtime|rollback|customer|credential|secret|signing|entitlement|notariz|keychain|provisioning|"
                r"authkey|password|passwd|certificate|cert|token|keys?|ssh|gpg|gnupg|id_rsa|id_dsa|id_ecdsa|id_ed25519|"
                r"known_hosts|authorized_keys|netrc|npmrc|pypirc|htpasswd|keystore|kube|aws|service[-_ ]?accounts?|"
                r"adminsdk|gcloud|firebase|oauth")
    edge = r"(?:^|[^a-z0-9])"
    # A key, certificate or profile is never evidence whatever it is called: the extension is the class.
    secret_files = (".app", ".dmg", ".pkg", ".ipa", ".zip", ".tgz", ".xcarchive", ".sig", ".asc", ".p8", ".p12",
                    ".pem", ".pfx", ".cer", ".crt", ".key", ".csr", ".jks", ".keystore", ".p7b", ".p7c", ".srl",
                    ".mobileprovision", ".ovpn", ".ppk")
    return (bool(re.search(rf"{edge}(?:{records})(?:$|[^a-z0-9])", name))
            or name == "evidence-expired.jsonl" or name == "protected-archive"
            or bool(re.search(rf"{edge}(?:{retained})", name))
            or name.endswith(secret_files))


def marked_evidence_job(job_id: str) -> bool:
    """Only terminal jobs strictly older than the separate window, with a private surviving report."""
    days = marked_evidence_expiry_days(load_policy())
    if not days:
        return False
    with db() as conn:
        job = row(conn, "SELECT * FROM jobs WHERE id=?", (job_id,))
        clock = reap_job_finished_clock(conn, job) if job else None
    if not job or not clock or clock >= now() - days * 86400:
        return False
    try:
        fd = os.open(str(reap_cache_root() / job_id), REAP_ROOT_FLAGS)
        try:
            info = os.stat("report.md", dir_fd=fd, follow_symlinks=False)
            # A surviving report is the job's durable record: an empty placeholder is not one.
            return stat.S_ISREG(info.st_mode) and info.st_size > 0 and info.st_uid == os.getuid() and info.st_nlink == 1
        finally:
            os.close(fd)
    except OSError:
        return False


def reap_marked_evidence_item(item: dict) -> bool:
    """A bounded exception for job-cache children only; all ordinary authority and deletion proofs still apply."""
    if item.get("class") != "job-cache-subdir" or not item.get("owner") or marked_evidence_keep(os.path.basename(item["path"])):
        return False
    root = reap_canon(reap_cache_root() / item["owner"])
    if item.get("root") != root or os.path.dirname(item["path"]) != root or not marked_evidence_job(item["owner"]):
        return False
    parent, own = reap_cache_retention(root), reap_cache_retention(item["path"])
    return "evidence" in (parent, own) and all(c in ("unmarked", REAP_DISPOSABLE, "evidence") for c in (parent, own))


def reap_marked_evidence_locks(path: str) -> tuple[list, list]:
    """Discover cooperating repository/build and Cargo locks without entering Git or protected state."""
    projects, cargo = {path, os.path.dirname(path)}, set()
    def listing(rel, names):
        current = os.path.join(path, rel)
        if any(reap_is_git(n) for n in names):
            projects.add(current)
        for name in names:
            if name in (".cargo-lock", ".cargo-build-lock"):
                cargo.add(Path(current) / name)
    def visit(rel, name, info, level, device):
        if reap_protected(name):
            raise ReapRefused("protected-leaf:" + rel)
        if info is not None and info.st_dev != device:
            raise ReapRefused("mountpoint:" + rel)
        return not reap_is_git(name)
    reap_fd_walk(path, visit, REAP_WALK_ENTRIES, blind=reap_is_git, listing=listing)
    return [reap_queue_lock(p) for p in sorted(projects)], sorted(cargo)


def reap_evidence_marker_void(job_id: str | None) -> bool:
    """A cancelled job's `evidence` marker on its own cache has lost its premise: the acceptance or verification it was
    written for can never complete, so the marker keeps that cache only for the normal evidence window instead of forever.
    A done, failed, parked or running job keeps its marker as written."""
    days = evidence_expiry_days(load_policy())
    if not job_id or not days:
        return False
    with db() as conn:
        job = row(conn, "SELECT status, finished_at FROM jobs WHERE id=?", (job_id,))
    return (bool(job) and job["status"] == "cancelled" and bool(job["finished_at"])
            and now() - job["finished_at"] >= days * 86400)


def reap_no_retained_names(path: str) -> bool:
    """True only when no retained-class name (a record, signing or credential name, a key or certificate extension, an
    identity file) appears anywhere below a whole-folder candidate. A job-cache subdirectory is removed as a unit, so its
    top-level name alone cannot prove it holds no key material (2026-10-19 review). A walk that cannot complete, or a name
    it cannot judge, keeps the folder: over-retention is the safe answer."""
    hit = []
    def listing(rel, names):
        kept = [n for n in names if marked_evidence_keep(n)]
        if kept:
            hit.append(kept[0])
    try:
        reap_fd_walk(path, lambda rel, name, info, level, device: True, REAP_WALK_ENTRIES, listing=listing)
    except (OSError, ReapRefused):
        return False
    return not hit


def reap_item_retention(item: dict, proof: dict | None = None) -> str:
    """A marker decides; without one, `disposable` is derived only in the three self-cleaning cases (see REAP_DERIVED_KINDS).
    `proof` is an idle proof the caller already took for an agent-scratch folder: it is used as it is, never taken twice."""
    if item.get("class") == "job-cache-subdir" and marked_evidence_keep(os.path.basename(item["path"])):
        return "evidence"
    if reap_marked_evidence_item(item):
        return REAP_DISPOSABLE
    if item.get("class") in ("job-cache-subdir", "job-derived-output") and item.get("owner") \
            and reap_inside(item["path"], reap_canon(reap_cache_root() / item["owner"])):
        parent = reap_cache_retention(str(reap_cache_root() / item["owner"]))
        if parent not in ("unmarked", REAP_DISPOSABLE):
            if parent == "evidence" and reap_evidence_marker_void(item["owner"]):
                return REAP_DISPOSABLE
            return parent  # an explicit preserving job-folder marker applies to every child
    if item.get("class") == "job-derived-output":
        for parent in (item.get("source"), item["root"]):
            if not parent:
                return "ambiguous"
            kept = reap_cache_retention(parent)
            if kept not in ("unmarked", REAP_DISPOSABLE):
                return kept
        parent = os.path.dirname(item["path"])
        while reap_inside(parent, item["root"]):
            kept = reap_cache_retention(parent)
            if kept not in ("unmarked", REAP_DISPOSABLE):
                return kept
            if parent == item["root"]:
                break
            parent = os.path.dirname(parent)
    found = reap_cache_retention(item["path"])
    if found != "unmarked":
        return found
    cls = item.get("class")
    if os.path.basename(item["path"]) == "target" and cls in ("job-derived-output", "job-clone-regenerable", "job-cache-subdir"):
        # Cargo's release profile can contain either rebuildable binaries or retained signed releases.
        # Neither the target name, cache tag nor a parent disposable marker resolves that ambiguity.
        if reap_target_release_bearing(item["path"]):
            return "release-runtime-rollback"
    if cls == "job-derived-output" and item.get("kind") in REAP_OUTPUT_KINDS \
            and os.path.basename(item["path"]) == item["kind"]:
        if item.get("ownership") == "detached-job-output":
            # A sibling's name alone cannot turn arbitrary cache data into compiler output.
            # Explicit disposable markers already returned above; otherwise require the tool's cache tag.
            return REAP_DISPOSABLE if item["kind"] == "target" and reap_cachedir_tagged(item["path"]) else "unmarked"
        return REAP_DISPOSABLE
    if cls == "job-clone-regenerable" and item.get("kind") in REAP_DERIVED_KINDS \
            and os.path.basename(item["path"]) == item.get("kind"):
        return REAP_DISPOSABLE
    if cls == "job-cache-subdir" and reap_fold(os.path.basename(item["path"])) not in EVIDENCE_KEEP_DIRS and item.get("owner") \
            and reap_cache_retention(str(reap_cache_root() / item["owner"])) in ("unmarked", REAP_DISPOSABLE):  # only a preserving parent marker keeps it all
        if (reap_cachedir_tagged(item["path"]) or reap_scratch_expired(item["owner"])) and reap_no_retained_names(item["path"]):
            return REAP_DISPOSABLE
    if cls == "agent-scratch":
        # A marker decides; only an unmarked folder is judged by its own idle proof, and the proof (its reason, its counts
        # and the budget it spent) rides on the item, so the receipt says why the folder stayed instead of naming an
        # unmarked folder and nothing else.
        if found == "unmarked":
            proof = proof if isinstance(proof, dict) else reap_scratch_idle_proof(item["path"])
            item["idleProof"] = proof
            if proof["idle"]:
                return REAP_DISPOSABLE
        return found  # unmarked: no marker; the idle proof in the receipt says what kept it
    if cls == "release-evidence" and reap_release_evidence_superseded(os.path.basename(item["path"])):
        return REAP_DISPOSABLE
    if cls == "job-clone-root" and item.get("kind") == "clone" and reap_clone_expired(item.get("jobs") or [], item["path"]):
        return REAP_DISPOSABLE
    if cls == "job-clone-regenerable" and item.get("kind") == "dist" and os.path.basename(item["path"]) == "dist" \
            and reap_clone_expired(item.get("jobs") or [], item["root"]):
        return REAP_DISPOSABLE  # dist can hold a release while its job may still ship; not once every job finished cloneExpiryDays ago
    if cls == "job-archive" and reap_archive_expired(item["path"]):
        return REAP_DISPOSABLE
    return found


REAP_ARCHIVE_SOLE = "sole-copy-since.json"   # written into an archive the moment its clone starts to be removed (never before)


def reap_archive_read_small(path: str, limit: int) -> bytes | None:
    """One small file of an archive folder, never followed and never larger than `limit` (None when absent, unreadable or larger)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        return os.read(fd, limit + 1)
    except OSError:
        return None
    finally:
        os.close(fd)


def reap_archive_sole_since(path: str) -> float | None:
    """When this archive became the only copy of its clone (the clone's removal began), from the archive's own record."""
    data = reap_archive_read_small(os.path.join(path, REAP_ARCHIVE_SOLE), 4096)
    try:
        since = json.loads(data).get("soleCopySince") if data else None
    except (ValueError, AttributeError):
        return None
    return float(since) if isinstance(since, (int, float)) and not isinstance(since, bool) else None


def reap_archive_clone_of(path: str) -> str | None:
    """The folder this archive was made from (its manifest's `path`); None when there is no readable manifest to say."""
    data = reap_archive_read_small(os.path.join(path, "manifest.json"), 64 * 2 ** 20)
    try:
        clone = json.loads(data).get("path") if data else None
    except (ValueError, AttributeError):
        return None
    return clone if isinstance(clone, str) and os.path.isabs(clone) else None


def reap_archive_mark_sole(path: str, clone: str, run: str | None, observed: bool = False) -> float | None:
    """Record that the archive at `path` is the only copy of `clone` from now on. A valid record already there stands (the first
    one counts); an empty or corrupt one is as good as none and is replaced. Written to a private temporary file and renamed into
    place, so a crash never leaves a half-written record. `observed` marks a legacy archive found after its clone was already
    gone. Returns the time recorded, None if not written."""
    existing = reap_archive_sole_since(path)
    if existing is not None:
        return existing
    stamp = now()
    data = json.dumps({"soleCopySince": stamp, "at": iso(), "clone": clone, "run": run, "observed": observed}, sort_keys=True).encode()
    final, temporary = os.path.join(path, REAP_ARCHIVE_SOLE), os.path.join(path, f".{REAP_ARCHIVE_SOLE}.tmp-{os.getpid()}")
    try:
        os.unlink(temporary)  # a leftover of a crashed earlier write by this process id
    except OSError:
        pass
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        return None
    try:
        try:
            if os.write(fd, data) != len(data):
                raise OSError(errno.EIO, "short write")
            reap_durable(fd)
        finally:
            os.close(fd)
        os.rename(temporary, final)
        dfd = os.open(path, REAP_DIR_FLAGS)
        try:
            reap_durable(dfd)
        finally:
            os.close(dfd)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return reap_archive_sole_since(path)
    return stamp


def reap_archive_expired(path: str) -> bool:
    """The archive has been the only copy of its clone for at least archiveExpiryDays: counted from the clone's removal (its
    `sole-copy-since.json`), never from when the archive was written, so a removal that was refused and retried cannot shorten
    the time. While the clone still exists, or a legacy archive's removal time is not recorded yet (the tick records it), it does
    not expire. Only an archive with no readable manifest to ask falls back to its own folder's mtime."""
    days = load_policy().get("archiveExpiryDays", 30)
    if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
        return False
    try:
        since = reap_archive_sole_since(path)
        if since is None:
            clone = reap_archive_clone_of(path)
            if clone is not None:
                return False
            since = os.lstat(path).st_mtime
        return now() - since >= days * 86400
    except OSError:
        return False


def reap_archive_items() -> list[dict]:
    """Every archive the reaper wrote: <archive root>/<J-NNNN>/<name>. Names only; symlinks are not items."""
    root, items = reap_archive_root(), []
    try:
        jobs = sorted(os.scandir(root), key=lambda e: e.name)
    except FileNotFoundError:
        return []
    for job in jobs:
        if not re.fullmatch(r"J-\d{4,5}", job.name) or job.is_symlink() or not job.is_dir(follow_symlinks=False):
            continue
        try:
            entries = sorted(os.scandir(job.path), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue  # a staging folder (.partial-*) is the reaper's own unfinished write, never an item
            item = {"path": reap_canon(entry.path), "root": reap_canon(job.path), "owner": job.name, "jobs": [job.name],
                    "class": "job-archive", "kind": "archive", "ownership": "reaper-archive"}
            items.append({**item, "retention": reap_item_retention(item)})
    return items


REAP_PARTIAL_SECONDS = 86400   # a `.partial-*` staging folder this old is the leftover of a crashed archive


def reap_archive_job_dirs() -> list:
    root = reap_archive_root()
    try:
        return [e for e in sorted(os.scandir(root), key=lambda e: e.name)
                if re.fullmatch(r"J-\d{4,5}", e.name) and not e.is_symlink() and e.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def reap_archive_clean_partials() -> int:
    """Staging folders (`.partial-*`) older than a day in EVERY job folder of the archive root, whatever job is being reaped: the
    reaper's own unfinished writes of a crashed archive, never an item and never a saved archive. Returns how many went."""
    cleaned = 0
    for job in reap_archive_job_dirs():
        try:
            entries = sorted(os.scandir(job.path), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.name.startswith(".partial-") or entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                if now() - os.lstat(entry.path).st_mtime > REAP_PARTIAL_SECONDS:
                    shutil.rmtree(entry.path, ignore_errors=True)
                    cleaned += 0 if os.path.lexists(entry.path) else 1
            except OSError:
                continue
    return cleaned


def reap_archive_supersede(job_dir: str, final: str, clone: str, job: str) -> int:
    """After the archive at `final` verified: the older archives of the SAME clone (same job folder, the manifest names the same job
    and folder) are superseded and go, so a re-run does not leave one archive per attempt that never expires while the clone exists.
    Never `final`, never the only verified archive, never one that already became a clone's sole copy (it holds a removed
    incarnation of that path), never anything whose manifest does not say it is this clone's. Returns how many were removed."""
    manifest = reap_archive_read_small(os.path.join(final, "manifest.json"), 64 * 2 ** 20)
    try:
        newest = os.lstat(final).st_mtime
        current = json.loads(manifest) if manifest else None
    except (OSError, ValueError):
        return 0
    if not isinstance(current, dict) or current.get("kind") != "ut-overseer-reap-archive" or current.get("path") != clone or current.get("job") != job:
        return 0  # the successor must itself be a readable archive of this clone, or nothing is superseded
    removed = 0
    try:
        entries = sorted(os.scandir(job_dir), key=lambda e: e.name)
    except OSError:
        return 0
    for entry in entries:
        if entry.name.startswith(".") or entry.path == final or entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            continue
        try:
            info = os.lstat(entry.path)
            data = reap_archive_read_small(os.path.join(entry.path, "manifest.json"), 64 * 2 ** 20)
            older = json.loads(data) if data else None
            if info.st_uid != os.getuid() or info.st_mtime >= newest or not isinstance(older, dict) \
                    or older.get("kind") != "ut-overseer-reap-archive" or older.get("path") != clone or older.get("job") != job \
                    or reap_archive_sole_since(entry.path) is not None:
                continue
            shutil.rmtree(entry.path, ignore_errors=True)
            removed += 0 if os.path.lexists(entry.path) else 1
        except (OSError, ValueError):
            continue
    return removed


# ---- archive before removal: what it saves and how it is proved ---------------------------------------------------------------
# One archive per refused item: <archive root>/<job>/<name>-<stamp>-<run>/ holding
#   manifest.json   job, folder, the refusal it settles, per repository HEAD, branch, remotes (credentials masked), every saved tip
#                   and where it came from, the bundle and every archived file with its sha256, what was left out and why
#   repo-<n>.bundle per repository whose metadata gate passes: every ref, HEAD, reflog, stash and unreachable commit that no
#                   remote-tracking ref holds (a linked worktree: its own HEAD and HEAD reflog; branches stay in the main repository)
#   files.tar.gz    every file git does not hold unchanged: modified, untracked, ignored data, and the whole of a stray Git
#                   directory or of a repository git cannot be trusted to read (its .git included)
# Left out, named in the manifest: regenerable output (REAP_ARCHIVE_SKIP, and ignored CLONE_IGNORED_OK folders), protected names
# (never entered or copied: they go with the folder under cloneProtectedCopies=remove) and evidence files past evidenceExpiryDays.
# Built in a `.partial-*` folder, verified (`git bundle verify`, every saved tip reachable from the bundle heads or a remote-tracking
# ref, every tar member's sha256 against the manifest) and only then renamed into place; nothing is removed unless that passed.
REAP_ARCHIVE_SPACE_MARGIN = 2 ** 30    # free bytes an archive must leave on the volume besides its own input
REAP_ARCHIVE_LIST_MAX = 5000           # left-out entries the manifest names one by one (the rest are counted)


def reap_archive_on() -> bool:
    return load_policy().get("cloneArchive", "archive") == "archive"


def reap_jobs_finished_before(ids: list, seconds: float) -> bool:
    """Every job named finished (done/failed/cancelled, or interrupted with a durable continuation) at least `seconds` ago."""
    ids = [i for i in ids if i]
    if not ids:
        return False
    with db() as conn:
        found = [row(conn, "SELECT * FROM jobs WHERE id=?", (i,)) for i in ids]
        for job in found:
            if not job:
                return False
            clock = reap_job_finished_clock(conn, job)
            if not clock or now() - clock < seconds:
                return False
    return True


def reap_archive_eligible(item: dict) -> bool:
    """cloneArchive=archive, a whole clone or a job cache folder, and every job it is named for finished cloneExpiryDays ago."""
    days = load_policy().get("cloneExpiryDays", 3)
    if not reap_archive_on() or item.get("class") not in ("job-clone-root", "job-cache-subdir") \
            or not isinstance(days, int) or isinstance(days, bool) or days <= 0:
        return False
    ids = item.get("jobs") or [item.get("owner")]
    if not reap_jobs_finished_before(ids, days * 86400):
        return False
    return item.get("class") != "job-clone-root" or not reap_recent_users(item["path"], ids, days * 86400)


def reap_archive_settles(item: dict, refusal: str, eligible: bool) -> dict:
    """The plan when archiving first settles this refusal; otherwise the refusal stands (raised again, unchanged). A git error is
    retried (the tick's backoff) until REAP_NONGIT_GRACE_DAYS past cloneExpiryDays, then the folder is archived as plain files.
    Object alternates are not an error here: the archive reads them (validated) and bundles what the clone itself holds."""
    head = refusal.split(":")[0]
    nongit = head.startswith(REAP_NONGIT) and refusal != "git-error:unsafe-metadata:object-alternates"
    if not eligible or not (head in REAP_ARCHIVABLE or head.startswith(REAP_NONGIT)):
        if reap_archive_on() and item.get("class") in ("job-clone-root", "job-cache-subdir") and (head in REAP_ARCHIVABLE or head.startswith(REAP_NONGIT)):
            raise ReapRefused(f"{refusal};archive-after-expiry")  # named: archived, then removed, once every job is past cloneExpiryDays
        raise ReapRefused(refusal)
    if nongit:
        days = load_policy().get("cloneExpiryDays", 3) + REAP_NONGIT_GRACE_DAYS
        if not reap_jobs_finished_before(item.get("jobs") or [item.get("owner")], days * 86400):
            raise ReapRefused(f"{refusal};archive-after-retries:{days}d")
    return {"reason": refusal}


def reap_mask_url(url: str) -> str:
    """A remote URL without any credentials it carries (https://user:token@host -> https://***@host)."""
    return re.sub(r"(?<=://)[^/@\s]*@", "***@", url.strip())[:300]


def reap_read_pointer(path: str) -> str:
    """One small Git pointer file (gitdir, commondir), never followed and never larger than 4 KiB."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a file")
        return os.read(fd, 4096).decode("utf-8", "strict").strip()
    finally:
        os.close(fd)


def reap_archive_live_worktrees(common: str) -> list[str]:
    """Linked worktrees of this repository that may still exist: removing it would strand them, so the archive refuses
    (`linked-worktrees:<n>`) until they are reaped and pruned. An unreadable entry counts as existing, and so does one whose
    folder is missing: a moved or renamed worktree, or one on an unmounted volume, looks exactly like a deleted one. A missing
    folder is dead only when its entry is REAP_WORKTREE_STALE_DAYS old, is not locked and the folder's parent is mounted."""
    base = os.path.join(common, "worktrees")
    try:
        names = sorted(os.listdir(base))
    except FileNotFoundError:
        return []
    except OSError:
        raise ReapRefused("git-error:worktrees-unreadable")
    live = []
    for name in names:
        pointer_file = os.path.join(base, name, "gitdir")
        try:
            pointer = reap_read_pointer(pointer_file)
        except (OSError, UnicodeError):
            live.append(os.path.join(base, name))
            continue
        target = pointer if os.path.isabs(pointer) else os.path.join(base, name, pointer)
        if not pointer or os.path.lexists(target):
            live.append(os.path.dirname(target) if pointer else os.path.join(base, name))
            continue
        try:
            old = now() - os.lstat(pointer_file).st_mtime >= REAP_WORKTREE_STALE_DAYS * 86400
            mounted = os.path.isdir(os.path.dirname(os.path.dirname(target)))
            locked = os.path.lexists(os.path.join(base, name, "locked"))
        except OSError:
            old, mounted, locked = False, False, True
        if not (old and mounted) or locked:
            live.append(os.path.join(base, name))  # unknown: the repository stays until the entry is provably dead
    return live


def reap_archive_git_facts(repo: str) -> dict:
    """What one repository holds that no remote does, and which of its files git holds unchanged. Runs inside one gated
    session (object alternates allowed and validated). Raises ReapRefused on any git failure; `linked-worktrees` is final."""
    code, out = reap_git(repo, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
    dirs = out.splitlines()
    if code != 0 or len(dirs) != 2:
        raise ReapRefused(f"git-error:rev-parse:{code}")
    gitdir, common = reap_canon(dirs[0]), reap_canon(dirs[1])
    linked = gitdir != common
    if not linked:
        live = reap_archive_live_worktrees(common)
        if live:
            raise ReapRefused(f"linked-worktrees:{len(live)}:{live[0][-80:]}")
    code, head = reap_git(repo, "rev-parse", "--verify", "-q", "HEAD^{commit}")
    head = head.strip() if code == 0 and re.fullmatch(r"[0-9a-f]{40,64}", head.strip()) else None
    code, branch = reap_git(repo, "symbolic-ref", "-q", "--short", "HEAD")
    branch = branch.strip() if code == 0 else None
    code, out = reap_git(repo, "config", "--get-regexp", r"^remote\..*\.url$")
    remotes = {}
    for line in out.splitlines() if code == 0 else []:
        key, _, url = line.partition(" ")
        remotes[key[len("remote."):-len(".url")]] = reap_mask_url(url)
    code, out = reap_git(repo, "for-each-ref", "--format=%(objectname) %(objecttype) %(refname)")
    if code != 0:
        raise ReapRefused(f"git-error:for-each-ref:{code}")
    tips, refs, remote_refs, unbundled = {}, [], [], []
    def tip(sha, kind, source):
        entry = tips.setdefault(sha, {"type": kind, "from": []})
        if len(entry["from"]) < 8:
            entry["from"].append(source)
    if head:
        tip(head, "commit", "HEAD")
    for line in out.splitlines():
        sha, kind, name = (line.split(" ", 2) + ["", ""])[:3]
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            continue
        if name.startswith("refs/remotes/"):
            remote_refs.append((name, sha))
        elif linked:
            continue  # branches, tags and the stash live in the main repository and stay with it
        elif kind in ("commit", "tag"):
            refs.append({"ref": name, "sha": sha, "type": kind})
            tip(sha, kind, name)
        else:
            unbundled.append({"ref": name, "sha": sha, "type": kind})
    code, out = reap_git(repo, "reflog", "show", "--format=%H", "HEAD", "--") if linked else reap_git(repo, "rev-list", "--no-walk", "--reflog")
    if code != 0 and (linked or out.strip()):
        raise ReapRefused(f"git-error:reflog:{code}")
    for line in out.splitlines():
        if re.fullmatch(r"[0-9a-f]{40,64}", line.strip()):
            tip(line.strip(), "commit", "reflog")
    dangling = None
    if not linked:
        try:
            code, out = reap_git(repo, "fsck", "--connectivity-only", "--unreachable", "--no-reflogs", "--no-progress")
            dangling = 0 if code == 0 else f"unknown:fsck-exit-{code}"
            for line in out.splitlines() if code == 0 else []:
                parts = line.split()
                if len(parts) == 3 and parts[:2] == ["unreachable", "commit"]:
                    tip(parts[2], "commit", "unreachable")
                    dangling += 1
        except ReapRefused as refusal:  # unreachable commits are git garbage: an unknown count is recorded, never guessed
            dangling = f"unknown:{refusal}"
    negatives = "".join(f"^{sha}\n" for _, sha in remote_refs)
    unpushed = 0
    if tips:
        code, out = reap_git_input(repo, "".join(f"{sha}\n" for sha in tips) + negatives, "rev-list", "--count", "--stdin")
        if code != 0 or not out.strip().isdigit():
            raise ReapRefused(f"git-error:rev-list:{code}")
        unpushed = int(out)
    code, listing = reap_git(repo, "ls-files", "-v", "-z")
    if code != 0:
        raise ReapRefused(f"git-error:ls-files:{code}")
    tracked = {}
    for record in listing.split("\0"):
        if len(record) > 2 and record[1] == " ":
            tracked[record[2:]] = record[0]
    code, out = reap_git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=no", "--ignore-submodules=all")
    if code != 0:
        raise ReapRefused(f"git-error:status:{code}")
    changed, status_lines, records = set(), [], out.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        changed.add(record[3:])
        status_lines.append(record[:2] + " " + record[3:])
        if "R" in record[:2] or "C" in record[:2]:  # -z: a rename or copy names its source in the next record
            if index < len(records):
                changed.add(records[index])
            index += 1
    clean = frozenset(path for path, tag in tracked.items() if tag == "H" and path not in changed)
    tracked_dirs = frozenset("/".join(Path(path).parts[:i]) for path in tracked for i in range(1, len(Path(path).parts)))
    code, out = reap_git(repo, "status", "--porcelain=v1", "-z", "--ignored=matching", "--untracked-files=normal", "--ignore-submodules=all")
    if code != 0:
        raise ReapRefused(f"git-error:status-ignored:{code}")
    ignored_known, ignored_all, untracked = set(), set(), 0
    for record in out.split("\0"):
        if record.startswith("!! "):
            rel = record[3:].rstrip("/")
            if rel:
                ignored_all.add(rel)
            if rel and (rel.rpartition("/")[2] in CLONE_IGNORED_OK or rel.endswith(CLONE_IGNORED_SUFFIXES)):
                ignored_known.add(rel)
        elif record.startswith("?? "):
            untracked += 1
    lfs = None
    if not linked:  # a linked worktree shares the main repository's LFS store, which stays
        code, storage = reap_git(repo, "config", "--get", "lfs.storage")
        lfs = {"objects": os.path.isdir(os.path.join(common, REAP_LFS_DIR)) and not os.path.islink(os.path.join(common, REAP_LFS_DIR)),
               "storage": storage.strip()[:200] if code == 0 and storage.strip() else None}
    return {"gitDir": gitdir, "commonDir": common, "linked": linked, "head": head, "branch": branch, "remotes": remotes,
            "tips": tips, "remoteRefs": remote_refs, "refs": refs, "unbundled": unbundled, "unpushed": unpushed, "dangling": dangling,
            "clean": clean, "trackedFiles": frozenset(tracked), "trackedDirs": tracked_dirs, "ignoredKnown": frozenset(ignored_known),
            "ignoredAll": frozenset(ignored_all), "untracked": untracked, "statusLines": status_lines, "lfs": lfs}


def reap_archive_needs_save(unit: dict) -> bool:
    """A repository that holds anything a remote does not: a commit, a stash, a dirty tracked file or an untracked file."""
    return unit["kind"] == "git" and bool(unit["unpushed"] or unit["statusLines"] or unit["untracked"])


def reap_archive_protected(item: dict) -> bool:
    """Secret copies and local state (.env*, .dev.vars*, .wrangler, .vercel) of a whole clone are archived (owner-only) before
    cloneProtectedCopies=remove deletes them with the clone: nothing is irrecoverable."""
    return item["class"] == "job-clone-root" and load_policy().get("cloneProtectedCopies", "remove") == "remove"


def reap_archive_units(item: dict, repos: list, git_dirs: list) -> list[dict]:
    """What one archive saves: every repository found in the item (git when its metadata gate passes, else its files with its
    .git), every stray Git directory (whole), and for a whole clone its root (a plain folder when it holds no `.git`). A
    repository inside regenerable output (node_modules, dist, .cache, ...) is a unit when it holds anything no remote does
    (`forced`: the walk enters that output only on the way to it) and is dropped only when git proves it has nothing to lose; one
    git cannot read keeps the clone (`archive-failed:nested-repo-unreadable`). Bare repositories in regenerable output go whole
    with it (SwiftPM's .build holds its own bare package repositories)."""
    path, units = item["path"], []
    def regenerable(root):
        return root != path and any(part in REAP_ARCHIVE_SKIP for part in Path(root).relative_to(path).parts)
    for repo in sorted(set(repos)):
        nested = regenerable(repo)
        try:
            with reap_git_session(repo, allow_alternates=True):
                unit = {"root": repo, "kind": "git", **reap_archive_git_facts(repo)}
        except ReapRefused as refusal:
            if str(refusal).startswith("linked-worktrees"):
                raise
            if nested:
                raise ReapRefused(f"archive-failed:nested-repo-unreadable:{os.path.relpath(repo, path)[:120]}")
            units.append({"root": repo, "kind": "files", "gitError": str(refusal)})
            continue
        if nested and not reap_archive_needs_save(unit):
            continue
        unit["forced"] = repo != path and reap_archive_needs_save(unit)
        units.append(unit)
    for rel in sorted(set(git_dirs)):
        root = path if rel in (".", "") else os.path.join(path, rel)
        if not regenerable(root) and not any(u["root"] == root for u in units):
            units.append({"root": root, "kind": "git-dir"})
    if item["class"] == "job-clone-root" and not any(u["root"] == path for u in units):
        units.append({"root": path, "kind": "files"})
    return units


def reap_archive_evidence_why(unit: dict | None, in_unit: str | None, sub: str) -> str | None:
    """Why an old screenshot, recording or log may be dropped instead of saved; None when it is source. Git tracks it (here
    modified): never. Otherwise it must be genuine evidence: ignored by git, or untracked inside a known evidence folder
    (REAP_EVIDENCE_DIRS). An untracked file outside both (public/hero.png) is source and is archived whatever its age."""
    if unit is None:
        return None
    if unit["kind"] == "git":
        if in_unit in unit["trackedFiles"]:
            return None
        parts = Path(in_unit).parts
        if any("/".join(parts[:i]) in unit["ignoredAll"] for i in range(1, len(parts) + 1)):
            return "ignored"
    return "evidence-dir" if any(reap_fold(part) in REAP_EVIDENCE_DIRS for part in Path(sub).parts[:-1]) else None


def reap_archive_collect(item: dict, units: list, evidence_cutoff: float | None, protected: bool = False) -> dict:
    """The files the archive copies, from one names-and-lstat walk of the item (descriptor-relative, never following a symlink,
    never entering another device). Each file belongs to the innermost unit holding it; content of a job cache folder outside
    every repository is disposable and is not copied. `protected` (a whole clone under cloneProtectedCopies=remove) also
    collects the protected names (.env*, .dev.vars*, .wrangler, .vercel) into their own list. `stamp` hashes the name, type,
    size and mtime of EVERY entry the walk saw, left-out folders and clean files included (never inside a repository's own .git:
    the tips cover it), so the last look before removal sees any change anywhere in the tree."""
    path, by_root = item["path"], {u["root"]: u for u in units}
    forced = [u["root"] for u in units if u.get("forced")]
    files, secrets, skipped, expired, listed = [], [], [], [], 0
    stamp = hashlib.sha256()
    top = os.lstat(path)
    device = top.st_dev
    def left_out(rel, why, size=None):
        if len(skipped) < REAP_ARCHIVE_LIST_MAX:
            skipped.append({"path": rel, "why": why, **({"bytes": size} if size is not None else {})})
    def leads_to_forced(absolute):
        return any(root == absolute or root.startswith(absolute + "/") for root in forced)
    def secret_entry(sub, info):
        return {"path": sub, "kind": "link" if stat.S_ISLNK(info.st_mode) else "file", "dev": info.st_dev, "ino": info.st_ino,
                "bytes": info.st_size if stat.S_ISREG(info.st_mode) else 0, "mtimeNs": info.st_mtime_ns, "mode": stat.S_IMODE(info.st_mode)}
    swept = 0
    def sweep_dir(fd, name, info, sub, level, secret):
        if info.st_dev != device:
            raise ReapRefused(f"archive-crosses-device:{sub[:80]}")
        if level + 1 > REAP_MAX_DEPTH:
            raise ReapRefused("archive-too-deep")
        child = os.open(name, REAP_DIR_FLAGS, dir_fd=fd)
        try:
            opened = os.fstat(child)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ReapRefused("archive-identity-changed")
            sweep(child, sub, level + 1, secret)
        finally:
            os.close(child)
    def sweep(fd, rel, level, secret):
        """Names-only walk of a folder the archive leaves out (regenerable or ignored build output): ONLY protected names are kept
        (build/.env, out/.vercel/...), because the removal deletes them with the clone. A git directory is not entered (its unit
        bundles what no remote holds); an env template (.env.example) carries no secret and is only named."""
        nonlocal swept
        with os.scandir(fd) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            swept += 1
            if swept > REAP_WALK_ENTRIES:
                raise ReapRefused("archive-too-large:entries")
            if reap_is_git(name):
                continue
            sub, held = f"{rel}/{name}", secret or reap_protected(name)
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                sweep_dir(fd, name, info, sub, level, held)
            elif held and (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                if reap_env_template(sub):
                    left_out(sub, "env-template")
                    continue
                stamp.update(f"{sub}\0{stat.S_IFMT(info.st_mode)}\0{info.st_size}\0{info.st_mtime_ns}\n".encode("utf-8", "surrogateescape"))
                secrets.append(secret_entry(sub, info))
            elif held:
                left_out(sub, "special-file")
    def walk(fd, rel, level, unit, raw, narrow=False, secret=False):
        nonlocal listed
        with os.scandir(fd) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            listed += 1
            if listed > REAP_ARCHIVE_MAX_FILES * 5:
                raise ReapRefused("archive-too-large:entries")
            sub = f"{rel}/{name}" if rel else name
            absolute = os.path.join(path, sub)
            hidden = reap_protected(name) and not (protected or secret)
            if hidden:
                left_out(sub, "protected")  # named only, never stat'ed, entered or copied (not a whole clone under =remove)
                continue
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            in_unit = os.path.relpath(absolute, unit["root"]) if unit else None
            own_git = unit is not None and unit["kind"] == "git" and reap_is_git(name) and os.path.dirname(absolute) == unit["root"]
            if not own_git:
                stamp.update(f"{sub}\0{stat.S_IFMT(info.st_mode)}\0{0 if stat.S_ISDIR(info.st_mode) else info.st_size}\0{info.st_mtime_ns}\n".encode("utf-8", "surrogateescape"))
            if stat.S_ISDIR(info.st_mode):
                if info.st_dev != device:
                    raise ReapRefused(f"archive-crosses-device:{sub[:80]}")
                child_unit, child_raw, child_narrow = unit, raw, False
                if absolute in by_root:
                    child_unit = by_root[absolute]
                    child_raw = child_unit["kind"] == "git-dir"
                    child_unit["visited"] = True
                elif own_git:
                    continue  # the repository's own metadata: its bundle holds what no remote does
                elif narrow and not leads_to_forced(absolute):
                    if protected:
                        sweep_dir(fd, name, info, sub, level, secret or reap_protected(name))
                    continue  # a sibling of the way to a nested repository: part of the output already named as left out
                elif narrow:
                    child_narrow = True
                elif not raw and name in REAP_ARCHIVE_SKIP and not (unit and unit["kind"] == "git" and in_unit in unit["trackedDirs"]):
                    if not leads_to_forced(absolute):
                        left_out(sub, "regenerable")
                        if protected:
                            sweep_dir(fd, name, info, sub, level, secret or reap_protected(name))
                        continue
                    left_out(sub, "regenerable-except-nested-repository")
                    child_narrow = True  # a repository with unsaved work lives below: only the way to it is entered
                elif not raw and unit and unit["kind"] == "git" and in_unit in unit["ignoredKnown"]:
                    if not leads_to_forced(absolute):
                        left_out(sub, "ignored-build-output")
                        if protected:
                            sweep_dir(fd, name, info, sub, level, secret or reap_protected(name))
                        continue
                    left_out(sub, "ignored-build-output-except-nested-repository")
                    child_narrow = True
                if level + 1 > REAP_MAX_DEPTH:
                    raise ReapRefused("archive-too-deep")
                child = os.open(name, REAP_DIR_FLAGS, dir_fd=fd)
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise ReapRefused("archive-identity-changed")
                    walk(child, sub, level + 1, child_unit, child_raw, child_narrow, secret or reap_protected(name))
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                if narrow and protected and (secret or reap_protected(name)) and not reap_env_template(sub):
                    secrets.append(secret_entry(sub, info))  # a protected file on the way to a nested repository goes with the clone: kept
                    continue
                if unit is None or own_git or narrow:
                    continue  # cache content outside every repository; a linked worktree's own `.git` pointer; the way to a nested repository
                held = secret or reap_protected(name)
                if unit["kind"] == "git" and (in_unit in unit["clean"] or (in_unit in unit["ignoredKnown"] and not held)):
                    continue  # git holds it unchanged, or it is ignored build output
                if not raw and not held and evidence_cutoff and stat.S_ISREG(info.st_mode) and evidence_name(name) and info.st_mtime <= evidence_cutoff:
                    why = reap_archive_evidence_why(unit, in_unit, sub)
                    if why:
                        expired.append({"path": sub, "bytes": info.st_size, "why": why})
                        continue
                (secrets if held else files).append({"path": sub, "kind": "link" if stat.S_ISLNK(info.st_mode) else "file", "dev": info.st_dev,
                                                     "ino": info.st_ino, "bytes": info.st_size if stat.S_ISREG(info.st_mode) else 0,
                                                     "mtimeNs": info.st_mtime_ns, "mode": stat.S_IMODE(info.st_mode)})
            else:
                left_out(sub, "special-file")
    fd = os.open(path, REAP_ROOT_FLAGS)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (top.st_dev, top.st_ino):
            raise ReapRefused("archive-identity-changed")
        root_unit = by_root.get(path)
        if root_unit:
            root_unit["visited"] = True
        walk(fd, "", 0, root_unit, bool(root_unit and root_unit["kind"] == "git-dir"))
    except OSError as error:
        raise ReapRefused("archive-scan-unreadable:" + reap_named(error))
    finally:
        os.close(fd)
    return {"files": files, "protected": secrets, "skipped": skipped, "skippedCount": None, "expired": expired, "stamp": stamp.hexdigest()}


class _ReapHashingReader:
    """The bytes tarfile copies, hashed on the way through."""
    def __init__(self, handle):
        self.handle, self.digest, self.count = handle, hashlib.sha256(), 0

    def read(self, size=-1):
        data = self.handle.read(size)
        self.digest.update(data)
        self.count += len(data)
        return data


def reap_archive_tar(path: str, files: list, target: str, level: int = 6) -> dict:
    """files.tar.gz of the collected files, each opened without following a symlink and checked against the walk's lstat."""
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as raw:
        with tarfile.open(fileobj=raw, mode="w:gz", compresslevel=level, format=tarfile.PAX_FORMAT) as tar:
            for entry in files:
                reap_archive_remaining()  # the archive's own wall-clock bound
                absolute = os.path.join(path, entry["path"])
                member = tarfile.TarInfo(entry["path"])
                member.mtime, member.mode = entry["mtimeNs"] // 10 ** 9, entry["mode"]
                if entry["kind"] == "link":
                    member.type, member.linkname = tarfile.SYMTYPE, os.readlink(absolute)
                    entry["sha256"] = hashlib.sha256(os.fsencode(member.linkname)).hexdigest()
                    tar.addfile(member)
                    continue
                source = os.open(absolute, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
                with os.fdopen(source, "rb") as handle:
                    info = os.fstat(handle.fileno())
                    if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != \
                            (entry["dev"], entry["ino"], entry["bytes"], entry["mtimeNs"]):
                        raise ReapRefused("archive-source-changed:" + entry["path"][:80])
                    member.size = info.st_size
                    reader = _ReapHashingReader(handle)
                    tar.addfile(member, reader)
                    if reader.count != info.st_size:
                        raise ReapRefused("archive-source-changed:" + entry["path"][:80])
                    entry["sha256"] = reader.digest.hexdigest()
        raw.flush()
        reap_durable(raw.fileno())
    return {"file": os.path.basename(target), "sha256": reap_file_sha256(target), "bytes": os.lstat(target).st_size, "count": len(files),
            "inputBytes": sum(e["bytes"] for e in files)}


def reap_file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def reap_archive_verify_tar(target: str, files: list) -> None:
    """Every member, and only those, with the sha256 the manifest records (contents read back from the compressed archive)."""
    expected, seen = {e["path"]: e for e in files}, set()
    try:
        with tarfile.open(target, "r:gz") as tar:
            for member in tar:
                reap_archive_remaining()
                entry = expected.get(member.name)
                if entry is None or member.name in seen:
                    raise ReapRefused("archive-verify-failed:tar-unexpected-member")
                if member.issym():
                    digest = hashlib.sha256(os.fsencode(member.linkname)).hexdigest()
                elif member.isreg():
                    handle, digest = tar.extractfile(member), hashlib.sha256()
                    while chunk := handle.read(1 << 20):
                        digest.update(chunk)
                    digest = digest.hexdigest()
                else:
                    raise ReapRefused("archive-verify-failed:tar-member-type")
                if digest != entry.get("sha256"):
                    raise ReapRefused("archive-verify-failed:tar-sha256:" + member.name[:80])
                seen.add(member.name)
    except (OSError, tarfile.TarError, EOFError) as error:
        raise ReapRefused("archive-verify-failed:tar-unreadable:" + error.__class__.__name__)
    if seen != set(expected):
        raise ReapRefused("archive-verify-failed:tar-missing-members")


def reap_archive_git(gitdir: str, *args: str, data: str | None = None, timeout: float = 600) -> tuple[int, str]:
    """Git on the archive's own scratch repository (created here, never a job's): hooks, fsmonitor and global config off."""
    argv = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false", "--git-dir", gitdir, *args]
    return reap_probe(argv, timeout, reap_git_env(), data) if data is not None else reap_probe(argv, timeout, reap_git_env())


REAP_ARCHIVE_SECONDS = 900              # wall-clock bound of ONE archive: a longer one is refused (`archive-failed:time-limit`), never held on the tick
REAP_ARCHIVE_REFUSED_SECONDS = 86400    # a bundle refused for size, space or time is not attempted again for this long
REAP_ARCHIVE_BUNDLE_FACTOR = 1.1        # the bundle's estimated bytes are scaled by this before they are compared with the bounds
REAP_ARCHIVE_REMEMBERED = ("archive-too-large:bundle", "archive-no-space:", "archive-failed:time-limit", "probe-timeout:")
REAP_ARCHIVE_CLOCK = threading.local()


def reap_archive_remaining(cap: float = 600) -> float:
    """Seconds one git/tar step of the archive in progress may still take (at most `cap`); refuses once the archive's own budget is spent."""
    deadline = getattr(REAP_ARCHIVE_CLOCK, "deadline", None)
    if deadline is None:
        return cap
    left = deadline - time.monotonic()
    if left <= 0:
        raise ReapRefused(f"archive-failed:time-limit:{REAP_ARCHIVE_SECONDS}s")
    return min(cap, left)


def reap_archive_refusal_key(path: str) -> str:
    return "reap:archive-refused:" + hashlib.sha256(os.fsencode(path)).hexdigest()[:24]


def reap_archive_refused_for(path: str) -> str | None:
    """The remembered size/space/time refusal of this folder's archive while it still stands (REAP_ARCHIVE_REFUSED_SECONDS), else None."""
    with db() as conn:
        stored = row(conn, "SELECT value FROM meta WHERE key=?", (reap_archive_refusal_key(path),))
    try:
        note = json.loads(stored["value"]) if stored else None
        return str(note["reason"])[:300] if note and note["path"] == path and float(note["until"]) > now() else None
    except (ValueError, KeyError, TypeError):
        return None


def reap_archive_refuse_remember(path: str, reason: str) -> None:
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (reap_archive_refusal_key(path), json.dumps(
            {"path": path, "reason": reason[:300], "at": now(), "until": now() + REAP_ARCHIVE_REFUSED_SECONDS})))


def reap_archive_bundle_estimate(unit: dict, work: str, revisions: str, cap: int) -> int:
    """Bytes the bundle will take, BEFORE anything is written: the repository's own object store (`count-objects -v`: loose + packed),
    refined with `rev-list --objects --disk-usage` of exactly the bundled revisions only when that cheap upper bound exceeds `cap`
    (a linked worktree shares a larger store than it bundles)."""
    code, out = reap_archive_git(unit["commonDir"], "count-objects", "-v", timeout=reap_archive_remaining(60))
    sizes = {k: int(v) for k, _, v in (line.partition(": ") for line in out.splitlines()) if k in ("size", "size-pack") and v.strip().isdigit()}
    estimate = (sizes.get("size", 0) + sizes.get("size-pack", 0)) * 1024 if code == 0 and sizes else None
    if estimate is None or estimate * REAP_ARCHIVE_BUNDLE_FACTOR > cap:
        code, out = reap_archive_git(work, "rev-list", "--objects", "--disk-usage", "--stdin", data=revisions, timeout=reap_archive_remaining(300))
        if code == 0 and out.strip().isdigit():
            estimate = int(out.strip())
    if estimate is None:
        raise ReapRefused("archive-failed:bundle-size-unknown")
    return estimate


def reap_archive_bundle(unit: dict, stage: str, number: int, reserve: int = 0) -> dict | None:
    """repo-<n>.bundle, SELF-CONTAINED: every saved tip and every remote-tracking ref of the repository, no prerequisite commits. A
    thin bundle (tips minus what a remote-tracking ref holds) cannot restore once that remote branch is deleted (squash-merge) or
    force-pushed, and nothing here can prove it still exists upstream. Built in a scratch bare repository that borrows the
    repository's objects (alternates) and holds one ref per tip. Verified with `git bundle verify` there AND in a second, empty
    repository that borrows nothing (a missing prerequisite fails it), and every tip must be reachable from the bundle's heads.
    The bundle's size is ESTIMATED first and refused before a byte is written when it (x1.1) exceeds the 4 GiB bound or the free
    space left after the margin and `reserve` (the archive's other files). None when no remote lacks anything."""
    if not unit["unpushed"]:
        return None
    work, empty = os.path.join(stage, f".bundle-work-{number}"), os.path.join(stage, f".bundle-empty-{number}")
    code, _ = reap_probe(["git", "init", "--bare", "-q", "--template=", work], 60, reap_git_env())
    if code != 0:
        raise ReapRefused(f"archive-failed:bundle-init:{code}")
    with open(os.path.join(work, "objects", "info", "alternates"), "w") as handle:
        handle.write(os.path.join(unit["commonDir"], "objects") + "\n")
    lines, names = [], {}
    for index, (sha, tip) in enumerate(sorted(unit["tips"].items())):
        ref = f"refs/tags/ut-archive/{index}" if tip["type"] == "tag" else f"refs/heads/ut-archive/{index}"
        names[ref] = {"sha": sha, "from": tip["from"]}
        lines.append(f"create {ref} {sha}\n")
    for index, (name, sha) in enumerate(unit["remoteRefs"]):
        lines.append(f"create refs/remotes/ut-archive/{index} {sha}\n")
    code, _ = reap_archive_git(work, "update-ref", "--stdin", data="".join(lines))
    if code != 0:
        raise ReapRefused(f"archive-failed:bundle-refs:{code}")
    target = os.path.join(stage, f"repo-{number}.bundle")
    # explicit refs on stdin: `--branches --tags --remotes` globs are refused by some git builds as an "empty bundle"; no `^` lines: self-contained
    revisions = "".join(f"{ref}\n" for ref in names) + "".join(f"refs/remotes/ut-archive/{index}\n" for index in range(len(unit["remoteRefs"])))
    free = reap_free_bytes()  # before one byte is written: the estimate must fit the bound and the volume (with the margin and the files still to come)
    room = REAP_ARCHIVE_MAX_BYTES if free is None else min(REAP_ARCHIVE_MAX_BYTES, free - REAP_ARCHIVE_SPACE_MARGIN - reserve)
    estimate = reap_archive_bundle_estimate(unit, work, revisions, max(room, 0))
    if estimate * REAP_ARCHIVE_BUNDLE_FACTOR > REAP_ARCHIVE_MAX_BYTES:
        raise ReapRefused(f"archive-too-large:bundle:~{estimate}B")
    if free is None or estimate * REAP_ARCHIVE_BUNDLE_FACTOR > free - REAP_ARCHIVE_SPACE_MARGIN - reserve:
        raise ReapRefused(f"archive-no-space:{free}B-free:bundle-~{estimate}B")
    code, _ = reap_archive_git(work, "bundle", "create", target, "--stdin", data=revisions, timeout=reap_archive_remaining())
    if code != 0 or not os.path.isfile(target):
        raise ReapRefused(f"archive-failed:bundle-create:{code}")
    os.chmod(target, 0o600)  # git writes it under the umask (the staging folder is 0700 meanwhile): owner-only like every other archive file
    size, free = os.lstat(target).st_size, reap_free_bytes()
    if size > REAP_ARCHIVE_MAX_BYTES:  # the whole history is bundled: bounded like the files are
        raise ReapRefused(f"archive-too-large:bundle:{size}B")
    if free is None or free < REAP_ARCHIVE_SPACE_MARGIN:
        raise ReapRefused(f"archive-no-space:{free}B-free:bundle-{size}B")
    code, _ = reap_archive_git(work, "bundle", "verify", target, timeout=reap_archive_remaining())
    if code != 0:
        raise ReapRefused(f"archive-verify-failed:bundle-verify:{code}")
    code, _ = reap_probe(["git", "init", "--bare", "-q", "--template=", empty], 60, reap_git_env())
    if code != 0:
        raise ReapRefused(f"archive-failed:bundle-init:{code}")
    code, _ = reap_archive_git(empty, "bundle", "verify", target, timeout=reap_archive_remaining())  # no alternates, no remote: a prerequisite the bundle lacks fails here
    if code != 0:
        raise ReapRefused(f"archive-verify-failed:bundle-not-self-contained:{code}")
    code, out = reap_archive_git(work, "bundle", "list-heads", target)
    heads = [line.split()[0] for line in out.splitlines() if line.split() and re.fullmatch(r"[0-9a-f]{40,64}", line.split()[0])]
    if code != 0 or not heads:
        raise ReapRefused(f"archive-verify-failed:bundle-heads:{code}")
    probe = "".join(f"{sha}\n" for sha in unit["tips"]) + "".join(f"^{sha}\n" for sha in heads)
    code, out = reap_archive_git(work, "rev-list", "--count", "--stdin", data=probe)
    if code != 0 or out.strip() != "0":
        raise ReapRefused(f"archive-verify-failed:bundle-coverage:{out.strip() or code}")
    shutil.rmtree(work)  # the scratch repositories this function created (refs and an alternates file only)
    shutil.rmtree(empty)
    return {"file": os.path.basename(target), "sha256": reap_file_sha256(target), "bytes": os.lstat(target).st_size,
            "heads": len(heads), "refs": names, "selfContained": True, "remoteTrackingRefs": len(unit["remoteRefs"])}


def reap_archive_staged_patch(unit: dict, stage: str, number: int) -> dict | None:
    """`git diff --cached --binary` of a repository whose INDEX differs from HEAD (a file staged, then edited or reverted in the working
    tree: files.tar.gz copies the working file, so the staged content would go with the clone). Written by git itself (--output:
    byte-exact), owner-only, hashed in the manifest. None when nothing is staged."""
    if not any(line[:1] not in (" ", "?", "!", "") for line in unit["statusLines"]):
        return None
    target = os.path.join(stage, f"repo-{number}.staged.patch")
    with reap_git_session(unit["root"], allow_alternates=True):
        code, _ = reap_git(unit["root"], "diff", "--cached", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "--no-color",
                           "--ignore-submodules=all", "--output=" + target)
    if code != 0:
        raise ReapRefused(f"archive-failed:staged-patch:{code}")
    try:
        os.chmod(target, 0o600)
        size = os.lstat(target).st_size
    except OSError:
        return None
    if size == 0:
        os.unlink(target)
        return None
    free = reap_free_bytes()
    if size > REAP_ARCHIVE_MAX_BYTES:
        raise ReapRefused(f"archive-too-large:staged-patch:{size}B")
    if free is None or free < REAP_ARCHIVE_SPACE_MARGIN:
        raise ReapRefused(f"archive-no-space:{free}B-free:staged-patch-{size}B")
    return {"file": os.path.basename(target), "sha256": reap_file_sha256(target), "bytes": size,
            "restore": "git apply --index <file> on a checkout of the recorded head"}


def reap_archive_lfs_scan(unit: dict) -> list[dict]:
    """Git LFS content of a repository with commits no remote holds: <git dir>/lfs/objects (the only local copy of an LFS object
    that was never uploaded), as tar entries relative to that folder. A linked worktree shares the main repository's store, which
    stays. Custom `lfs.storage` and a store over the archive bounds refuse by name instead of deleting the content."""
    lfs = unit.get("lfs")
    if unit["kind"] != "git" or unit.get("linked") or not lfs or not unit["unpushed"]:
        return []
    if lfs["storage"]:
        raise ReapRefused("archive-failed:lfs-custom-storage")
    if not lfs["objects"]:
        return []
    base, entries, total = os.path.join(unit["commonDir"], REAP_LFS_DIR), [], 0
    def walk(directory, rel, level):
        nonlocal total
        if level > 6:
            raise ReapRefused("archive-failed:lfs-too-deep")
        with os.scandir(directory) as listing:
            for entry in sorted(listing, key=lambda e: e.name):
                sub = f"{rel}/{entry.name}" if rel else entry.name
                info = os.lstat(entry.path)
                if stat.S_ISDIR(info.st_mode):
                    walk(entry.path, sub, level + 1)
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                    if len(entries) >= REAP_ARCHIVE_MAX_FILES or total > REAP_ARCHIVE_MAX_BYTES:
                        raise ReapRefused(f"archive-too-large:lfs:{total}B")
                    entries.append({"path": sub, "kind": "file", "dev": info.st_dev, "ino": info.st_ino, "bytes": info.st_size,
                                    "mtimeNs": info.st_mtime_ns, "mode": stat.S_IMODE(info.st_mode)})
                else:
                    raise ReapRefused("archive-failed:lfs-special-file")
    try:
        walk(base, "", 0)
    except OSError as error:
        raise ReapRefused("archive-scan-unreadable:lfs:" + reap_named(error))
    return entries


def reap_archive_fingerprint(units: list, collected: dict) -> str:
    """What the archive would hold: the tips each repository saves and every collected file's name, size and mtime (protected
    copies and LFS objects too). Format 2: self-contained bundles, so an archive of an older format is never reused."""
    shape = [["archive-format", 2]] + [[u["root"], u["kind"], sorted(u.get("tips") or {})] for u in units if u.get("visited")]
    shape.append([[f["path"], f["bytes"], f["mtimeNs"]] for f in collected["files"]])
    if collected.get("protected"):
        shape.append(["protected", [[f["path"], f["bytes"], f["mtimeNs"]] for f in collected["protected"]]])
    for unit in units:
        if unit.get("lfsFiles"):
            shape.append(["lfs", unit["root"], [[f["path"], f["bytes"], f["mtimeNs"]] for f in unit["lfsFiles"]]])
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode("utf-8", "surrogateescape")).hexdigest()


def reap_archive_reuse(job_dir: str, path: str, fingerprint: str) -> dict | None:
    """An archive of the same folder with the same fingerprint (a removal that was refused after its archive): reused when its
    files still match the manifest's sha256s, so a retried removal never stacks duplicate archives."""
    try:
        entries = sorted(os.scandir(job_dir), key=lambda e: e.name)
    except OSError:
        return None
    for entry in entries:
        if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            continue
        manifest_path = os.path.join(entry.path, "manifest.json")
        try:
            with open(manifest_path, "rb") as handle:
                data = handle.read()
            manifest = json.loads(data)
        except (OSError, ValueError):
            continue
        if manifest.get("path") != path or manifest.get("fingerprint") != fingerprint:
            continue
        stored = [r["bundle"] for r in manifest.get("repositories") or [] if r.get("bundle")] \
            + [r["lfs"] for r in manifest.get("repositories") or [] if r.get("lfs")] \
            + [manifest[key] for key in ("files", "protectedFiles") if manifest.get(key)]
        try:
            if all(reap_file_sha256(os.path.join(entry.path, s["file"])) == s["sha256"] for s in stored):
                return {"path": reap_canon(entry.path), "manifestSha256": hashlib.sha256(data).hexdigest(), "reused": True}
        except OSError:
            continue
    return None


def reap_archive_state(item: dict, repos: list, git_dirs: list) -> tuple[list, dict]:
    """The units and the collected files an archive of this item would hold right now (names, sizes and tips; nothing written)."""
    units = reap_archive_units(item, repos, git_dirs)
    days = evidence_expiry_days(load_policy())
    cutoff = now() - days * 86400 if days and reap_jobs_finished_before(item.get("jobs") or [item.get("owner")], days * 86400) else None
    collected = reap_archive_collect(item, units, cutoff, reap_archive_protected(item))
    for unit in units:
        if unit["kind"] == "git" and unit.get("visited"):
            unit["lfsFiles"] = reap_archive_lfs_scan(unit)
    return units, collected


def reap_archive_create(item: dict, plan: dict, repos: list, git_dirs: list, run: str | None) -> dict:
    """Archive what the refused item holds that no remote does, verify it, and only then report it saved. Raises ReapRefused
    (archive-*) and leaves no partial archive behind; the item is then kept and retried later. One archive may take
    REAP_ARCHIVE_SECONDS at most (`archive-failed:time-limit`). A refusal for size, space or time is remembered for
    REAP_ARCHIVE_REFUSED_SECONDS: the folder is not walked, bundled or measured again until then."""
    held = reap_archive_refused_for(item["path"])
    if held:
        raise ReapRefused(held)
    REAP_ARCHIVE_CLOCK.deadline = time.monotonic() + REAP_ARCHIVE_SECONDS
    try:
        return reap_archive_create_owned(item, plan, repos, git_dirs, run)
    except ReapRefused as refusal:
        if str(refusal).startswith(REAP_ARCHIVE_REMEMBERED):
            reap_archive_refuse_remember(item["path"], str(refusal))
        raise
    finally:
        REAP_ARCHIVE_CLOCK.deadline = None


def reap_archive_create_owned(item: dict, plan: dict, repos: list, git_dirs: list, run: str | None) -> dict:
    units, collected = reap_archive_state(item, repos, git_dirs)
    files, secrets = collected["files"], collected["protected"]
    lfs_files = [f for u in units for f in u.get("lfsFiles") or []]
    total = sum(f["bytes"] for f in files) + sum(f["bytes"] for f in secrets) + sum(f["bytes"] for f in lfs_files)
    if len(files) + len(secrets) + len(lfs_files) > REAP_ARCHIVE_MAX_FILES or total > REAP_ARCHIVE_MAX_BYTES:
        raise ReapRefused(f"archive-too-large:{len(files) + len(secrets) + len(lfs_files)}files:{total}B")
    for unit in units:  # every repository that holds work no remote has is reachable by the walk, or the clone stays
        if unit["kind"] == "git" and unit["unpushed"] and not unit.get("visited"):
            raise ReapRefused("archive-failed:bundle-missing:" + os.path.relpath(unit["root"], item["path"])[:80])
    free = reap_free_bytes()
    if free is None or free < total + REAP_ARCHIVE_SPACE_MARGIN:
        raise ReapRefused(f"archive-no-space:{free}B-free:{total}B-needed")
    root = reap_archive_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = os.lstat(root)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ReapRefused("archive-failed:archive-root-not-private")
    job_dir = root / item["owner"]
    job_dir.mkdir(exist_ok=True, mode=0o700)
    if stat.S_ISLNK(os.lstat(job_dir).st_mode):
        raise ReapRefused("archive-failed:job-dir-symlink")
    for private in (root, job_dir):  # the archive may hold secret copies: its folders are owner-only whatever they were before
        if stat.S_IMODE(os.lstat(private).st_mode) & 0o077:
            os.chmod(private, 0o700)
    fingerprint = reap_archive_fingerprint(units, collected)
    linked = next((u for u in units if u["root"] == item["path"] and u["kind"] == "git" and u.get("linked")), None)
    common = {"fingerprint": fingerprint, "stamp": collected["stamp"], "gitCommonDir": linked["commonDir"] if linked else None,
              "gitDir": linked["gitDir"] if linked else None, "files": len(files) + len(secrets) + len(lfs_files), "inputBytes": total}
    reused = reap_archive_reuse(str(job_dir), item["path"], fingerprint)
    if reused:
        return {**reused, **common}
    reap_archive_clean_partials()  # crashed archives of earlier passes (every job folder; the tick does this too)
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(item["path"])).lstrip(".-")[:80] or "folder"
    tag = (run or uuid.uuid4().hex)[:12]
    stage = str(job_dir / f".partial-{name}-{tag}")
    os.mkdir(stage, 0o700)
    try:
        repositories = []
        for number, unit in enumerate(units):
            entry = {"root": unit["root"], "relative": os.path.relpath(unit["root"], item["path"]), "mode": unit["kind"],
                     "visited": bool(unit.get("visited"))}
            if unit["kind"] == "git":
                entry.update({k: unit[k] for k in ("head", "branch", "remotes", "linked", "commonDir", "unpushed", "dangling")})
                entry.update(tips=len(unit["tips"]), refs=unit["refs"][:500], unbundled=unit["unbundled"][:100],
                             statusLines=unit["statusLines"][:2000], untracked=unit["untracked"],
                             lfsStore=bool(unit["lfs"] and unit["lfs"]["objects"]))
                entry["bundle"] = reap_archive_bundle(unit, stage, number, total) if unit.get("visited") else None
                if entry["bundle"] is None and unit["unpushed"]:
                    raise ReapRefused("archive-failed:bundle-missing:" + entry["relative"][:80])
                entry["stagedPatch"] = reap_archive_staged_patch(unit, stage, number) if unit.get("visited") else None
                if unit.get("lfsFiles"):
                    target = os.path.join(stage, f"repo-{number}.lfs.tar.gz")
                    entry["lfs"] = {**reap_archive_tar(os.path.join(unit["commonDir"], REAP_LFS_DIR), unit["lfsFiles"], target, level=1),
                                    "restore": "tar -xzf repo-<n>.lfs.tar.gz -C <repository>/.git/lfs/objects"}
                    reap_archive_verify_tar(target, unit["lfsFiles"])
            elif unit.get("gitError"):
                entry["gitError"] = unit["gitError"]
            repositories.append(entry)
        tar = reap_archive_tar(item["path"], files, os.path.join(stage, "files.tar.gz")) if files else None
        if tar:
            reap_archive_verify_tar(os.path.join(stage, "files.tar.gz"), files)
        sealed = reap_archive_tar(item["path"], secrets, os.path.join(stage, "protected.tar.gz")) if secrets else None
        if sealed:
            reap_archive_verify_tar(os.path.join(stage, "protected.tar.gz"), secrets)
        manifest = {"schema": 2, "kind": "ut-overseer-reap-archive", "reaperVersion": VERSION, "createdAt": iso(), "run": run,
                    "job": item["owner"], "jobs": item.get("jobs"), "class": item["class"], "path": item["path"], "reason": plan["reason"],
                    "fingerprint": fingerprint, "repositories": repositories, "soleCopySince": None,
                    "files": {**tar, "entries": [{k: f[k] for k in ("path", "kind", "bytes", "sha256", "mode")} for f in files]} if tar else None,
                    "protectedFiles": {**sealed, "entries": [{k: f[k] for k in ("path", "kind", "bytes", "sha256", "mode")} for f in secrets]} if sealed else None,
                    "leftOut": collected["skipped"], "expiredEvidence": {"count": len(collected["expired"]), "bytes": sum(e["bytes"] for e in collected["expired"]),
                                                                       "entries": collected["expired"][:REAP_ARCHIVE_LIST_MAX]},
                    "verification": {"bundles": "git bundle verify here and in an empty repository with no remote (self-contained); every saved tip reachable from the bundle heads",
                                     "files": "tar read back: every member and only those, each sha256 equal to this manifest"},
                    "restore": "git init; git fetch <repo-n.bundle> 'refs/*:refs/ut-archive/*' (the bundle holds the whole history, no remote needed); "
                               "tar -xzf files.tar.gz -C <folder>; protected.tar.gz holds the .env*/.dev.vars*/.wrangler/.vercel copies (owner-only, extract with the same care)"}
        data = json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8", "surrogateescape")
        fd = os.open(os.path.join(stage, "manifest.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            if os.write(fd, data) != len(data):
                raise ReapRefused("archive-failed:manifest-short-write")
            reap_durable(fd)
        finally:
            os.close(fd)
        final = str(job_dir / f"{name}-{time.strftime('%Y%m%dT%H%M%S', time.localtime(now()))}-{tag}")
        os.rename(stage, final)
        dfd = os.open(job_dir, REAP_DIR_FLAGS)
        try:
            reap_durable(dfd)
        finally:
            os.close(dfd)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)  # this function's own staging folder: no partial archive is ever left as saved
        raise
    superseded = reap_archive_supersede(str(job_dir), final, item["path"], item["owner"])  # the new one verified: its older siblings are redundant
    return {"path": final, "superseded": superseded, "manifestSha256": hashlib.sha256(data).hexdigest(), "bytes": sum(os.lstat(os.path.join(final, n)).st_size for n in os.listdir(final)),
            "bundles": sum(1 for r in repositories if r.get("bundle")), "expiredEvidence": len(collected["expired"]),
            "protectedFiles": len(secrets), "lfsObjects": len(lfs_files), "reused": False, **common}


def reap_recent_users(scope: str, ids: list, seconds: float) -> list[str]:
    """Finished jobs that are not among `ids` but declared a cwd or worktree inside `scope` and finished less than `seconds` ago
    (or have no finish time yet): a later job that worked in an earlier job's clone extends that clone's expiry. An interrupted
    job whose continuation chain has finished counts with its chain's last finish, so a clone it worked in does not expire early."""
    marks = ",".join("?" * len(REAP_TERMINAL))
    cutoff = now() - seconds
    with db() as conn:
        recent = rows(conn, f"SELECT * FROM jobs WHERE status IN ({marks}) AND (finished_at IS NULL OR finished_at > ?)", (*REAP_TERMINAL, cutoff))
        for job in rows(conn, "SELECT * FROM jobs WHERE status='interrupted'"):
            clock = reap_continued_interrupted(conn, job)
            if clock is not None and clock > cutoff:
                recent.append(job)
    return [j["id"] for j in recent if j["id"] not in ids and any(reap_inside(d, scope) for d in reap_declared_paths(j))]


def reap_clone_expired(ids: list, scope: str | None = None) -> bool:
    """Every job the clone is named for finished (done/failed/cancelled, or interrupted with a durable continuation) at least
    cloneExpiryDays ago, and (with `scope`, the clone's path) no other job that declared that folder as its cwd or worktree
    finished more recently than that."""
    days = load_policy().get("cloneExpiryDays", 3)
    if not ids or not isinstance(days, int) or isinstance(days, bool) or days <= 0:
        return False
    with db() as conn:
        found = [row(conn, "SELECT * FROM jobs WHERE id=?", (i,)) for i in ids]
        for job in found:
            if not job:
                return False
            clock = reap_job_finished_clock(conn, job)
            if not clock or now() - clock < days * 86400:
                return False
    return not (scope and reap_recent_users(scope, ids, days * 86400))


def reap_scratch_idle_budget(policy: dict | None = None) -> dict:
    """The budget one scratch folder's idle walk may spend: how many entries it may list and how many bytes it may see.
    A host may set either key in its policy. An entry budget that cannot be read is zero: the walk stops at its first
    entry and the folder is kept. A byte budget of zero is no byte bound at all (bytes bound no work); an unreadable one
    is the same, because a bound nobody can read is no bound."""
    policy = policy if policy is not None else load_policy()
    def whole(key: str, fallback: int, minimum: int) -> int:
        if key not in policy:
            return fallback
        value = policy[key]
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= minimum else 0
    return {"entries": whole("agentScratchIdleMaxEntries", AGENT_SCRATCH_IDLE_MAX_ENTRIES, 1),
            "bytes": whole("agentScratchIdleMaxBytes", AGENT_SCRATCH_IDLE_MAX_BYTES, 0)}


def reap_scratch_idle_proof(path: str, policy: dict | None = None) -> dict:
    """Whether nothing in the folder changed within evidenceExpiryDays, and what the walk that says so spent.

    The proof covers the whole tree: the walk is descriptor-relative (a symlink is never followed, a directory swapped
    for one is never entered) and it reads every entry's mtime, so a change anywhere in the tree is seen, however deep.
    The budget is per folder and honest: a walk that runs out of it is not idle, and the budget it spent is part of the
    answer, so the receipt says why the folder stayed. The walk's own failures keep a folder that would otherwise be
    idle: an entry it cannot read, a protected name it must not enter, and expiry turned off."""
    policy = policy if policy is not None else load_policy()
    budget = reap_scratch_idle_budget(policy)
    proof = {"idle": False, "reason": "", "entries": 0, "bytes": 0, "newest": None, "budget": budget, "seconds": 0.0}
    days = evidence_expiry_days(policy)
    if not days:
        proof["reason"] = "expiry-off"
        return proof
    cutoff = now() - days * 86400
    started, deadline = time.monotonic(), time.monotonic() + AGENT_SCRATCH_IDLE_SECONDS
    newest = [0.0]

    def visit(rel, name, info, level, device):
        if info is None:
            raise ReapRefused("protected-leaf")  # a protected name is named, never stat'ed, opened or entered
        proof["entries"] += 1
        if proof["entries"] > budget["entries"]:
            raise ReapRefused("budget-exceeded")
        if info.st_mtime > cutoff:
            raise ReapRefused("recent-change")
        if info.st_mtime > newest[0]:
            newest[0] = info.st_mtime
        if not stat.S_ISDIR(info.st_mode):
            proof["bytes"] += info.st_blocks * 512
            if budget["bytes"] and proof["bytes"] > budget["bytes"]:
                raise ReapRefused("budget-bytes-exceeded")
        return True  # enter a directory: the whole tree is covered, the budget is what bounds the walk

    try:
        root = os.lstat(path)
        if not stat.S_ISDIR(root.st_mode):
            raise ReapRefused("walk-root-not-a-directory")
        if root.st_mtime > cutoff:
            raise ReapRefused("recent-change")
        newest[0] = root.st_mtime
        reap_fd_walk(path, visit, max(1, budget["entries"] + 1), deadline=deadline,
                     incomplete="budget-exceeded", too_deep="too-deep")
    except ReapRefused as refusal:
        proof["reason"] = str(refusal)
    except OSError:
        proof["reason"] = "unreadable"
    proof["seconds"] = round(time.monotonic() - started, 3)
    if proof["reason"] == "budget-exceeded" and time.monotonic() > deadline:
        proof["reason"] = "budget-time-exceeded"
    if proof["reason"] and proof["reason"] not in ("recent-change", "protected-leaf", "budget-exceeded", "budget-time-exceeded",
                                                   "budget-bytes-exceeded", "too-deep"):
        proof["reason"] = "unreadable"  # a root that is not a folder, a folder that changed under the walk, a read error
    proof["newest"] = newest[0] or None
    if not proof["reason"]:
        proof["idle"], proof["reason"] = True, "idle"
    return proof


def reap_scratch_idle(path: str) -> bool:
    """Nothing in the folder (every entry below it, names and mtimes only, never followed) changed within
    evidenceExpiryDays. The walk's own budget, an unreadable entry, a protected name or expiry turned off is not idle."""
    return reap_scratch_idle_proof(path)["idle"]


def reap_item_idle_reason(item: dict) -> str:
    """Why an agent-scratch folder was kept, named by its own idle proof: what the walk saw and why it did not call it idle."""
    proof = item.get("idleProof")
    reason = proof.get("reason") if isinstance(proof, dict) else ""
    return f"idle-{reason}" if reason and reason != "idle" else "retention-preserved:unmarked"


def reap_release_evidence_superseded(name: str) -> bool:
    """True for a host-app version older than installed that is neither installed nor the rollback (both must be known), or a
    component version outside its newest RELEASE_EVIDENCE_KEEP. Anything else, and anything unreadable, is kept."""
    match = RELEASE_EVIDENCE_NAME.match(name)
    if not match:
        return False
    version = reap_version_tuple(match["version"])
    if not match["product"]:
        marks = reap_release_marks()
        if not marks["installed"] or not marks["rollback"]:
            return False
        if match["version"] in (marks["installed"], marks["rollback"]):
            return False
        return version < reap_version_tuple(marks["installed"])
    try:
        siblings = {reap_version_tuple(m["version"]) for e in os.scandir(reap_release_evidence_root())
                    if (m := RELEASE_EVIDENCE_NAME.match(e.name)) and m["product"] == match["product"] and e.is_dir(follow_symlinks=False)}
    except OSError:
        return False
    return version not in sorted(siblings)[-RELEASE_EVIDENCE_KEEP:]


def reap_release_evidence_items() -> list[dict]:
    """Every top-level folder of the release-evidence root as an item; symlinks and protected names are not items."""
    root = reap_release_evidence_root()
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except FileNotFoundError:
        return []
    items = []
    for entry in entries:
        if reap_protected(entry.name) or entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            continue
        item = {"path": reap_canon(entry.path), "root": reap_canon(root), "owner": RELEASE_EVIDENCE_OWNER, "jobs": [],
                "class": "release-evidence", "kind": "release-evidence", "ownership": "release-evidence-dir"}
        items.append({**item, "retention": reap_item_retention(item)})
    return items


def reap_agent_scratch_items() -> list[dict]:
    """Every top-level folder of the agent-scratch root as an item. Names only; symlinks and protected names are not items."""
    root = reap_agent_scratch_root()
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except FileNotFoundError:
        return []
    items = []
    for entry in entries:
        if reap_protected(entry.name) or entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            continue
        item = {"path": reap_canon(entry.path), "root": reap_canon(root), "owner": AGENT_SCRATCH_OWNER, "jobs": [],
                "class": "agent-scratch", "kind": "agent-scratch", "ownership": "agent-scratch-dir"}
        items.append({**item, "retention": reap_item_retention(item)})
    return items


def reap_cachedir_tagged(path: str) -> bool:
    """True when the folder's top level holds a valid CACHEDIR.TAG: a plain, single-link file of this user whose first 43
    bytes are the standard signature. Only those bytes of that one file are read; anything unexpected is False."""
    try:
        fd = os.open(path, REAP_ROOT_FLAGS)
        try:
            info = os.stat(CACHEDIR_TAG, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_size < len(CACHEDIR_SIGNATURE):
                return False
            tag = os.open(CACHEDIR_TAG, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            try:
                return os.read(tag, len(CACHEDIR_SIGNATURE)) == CACHEDIR_SIGNATURE
            finally:
                os.close(tag)
        finally:
            os.close(fd)
    except OSError:
        return False


# ---- an interrupted job whose work a durable continuation took over ---------------------------------------------------
# A job whose worker terminal vanished without a report stays `interrupted`: it never reported, so the ordinary terminal rules
# do not release its folders, and it kept them for good. A host that continues such a job records the continuation it queued
# (the same durable record it re-points the interrupted job's dependents with). With that record the old job holds nothing the
# continuation still needs, so its artifacts age exactly like a finished job's: the evidence window releases its scratch, the
# clone window releases its checkout. Every other proof (retention, protected leaves, git, other jobs' claims, a live worker
# terminal, producer locks) still runs. Without a provable interruption, without a continuation record, or with a record naming
# a continuation that does not exist, the job keeps everything, exactly as before.

def reap_iso_clock(stamp):
    """A zoned ISO-8601 clock as seconds since the epoch; a number unchanged; None when it is neither."""
    if isinstance(stamp, str):
        # UltraTerm interruption records use ISO timestamps. Require an explicit timezone;
        # never infer local time, parse numeric strings or substitute a creation/update clock.
        try:
            match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})", stamp)
            if not match:
                raise ValueError("not a zoned ISO timestamp")
            clock = time.strptime(match[1], "%Y-%m-%dT%H:%M:%S")
            zone = match[3]
            if clock.tm_sec > 59 or (zone != "Z" and (int(zone[1:3]) > 23 or int(zone[4:6]) > 59)):
                raise ValueError("invalid timestamp")
            # Gregorian ordinal arithmetic avoids local timezone/DST inference and adds
            # no new module dependency to the byte-for-byte embedded engine.
            year = clock.tm_year - 1
            days = 365 * year + year // 4 - year // 100 + year // 400 + clock.tm_yday - 1 - 719162
            offset = 0 if zone == "Z" else (int(zone[1:3]) * 60 + int(zone[4:6])) * 60 * (1 if zone[0] == "+" else -1)
            return days * 86400 + clock.tm_hour * 3600 + clock.tm_min * 60 + clock.tm_sec + float(match[2] or 0) - offset
        except (ValueError, OverflowError):
            return None
    return stamp


def reap_interruption_clock(conn, job: dict) -> float | None:
    """When the worker of an interrupted job vanished: the job's own interruption record, or the matching
    job.interrupted event. Never an unrelated finish clock; None when nothing proves the interruption."""
    if job.get("status") != "interrupted":
        return None
    meta = job_meta(job)
    interrupted = meta.get("interrupted")
    stamp = interrupted.get("at") if isinstance(interrupted, dict) else None
    if "interrupted" not in meta:
        event = row(conn, "SELECT ts FROM events WHERE subject=? AND kind='job.interrupted' ORDER BY id DESC LIMIT 1", (job["id"],))
        event_stamp, finished = event.get("ts") if event else None, job.get("finished_at")
        started = job.get("started_at")
        if started is not None and (not isinstance(started, (int, float)) or isinstance(started, bool)):
            return None  # a start clock that cannot be read proves nothing
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (event_stamp, finished)) \
                and abs(event_stamp - finished) < 1 and (started or 0) < event_stamp:
            stamp = event_stamp  # older hosts record interruption in the event log, not meta
    stamp = reap_iso_clock(stamp)
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool) or not 0 < stamp <= now():
        return None
    return stamp


REAP_SUCCESSOR_CACHE: dict = {}


def reap_state_key(conn) -> str:
    """The store this connection reads, so a cached read is never taken from another one."""
    try:
        return "|".join(str(entry[2]) for entry in conn.execute("PRAGMA database_list").fetchall() if entry[2])
    except Exception:
        return ""


def reap_successor_records(conn) -> list[dict] | None:
    """The scope handoff records, parsed once per short window: the key suffix is not relied on, the record names its job."""
    stamp, key = time.monotonic(), reap_state_key(conn)
    cached = REAP_SUCCESSOR_CACHE.get(("records", key))
    if cached is not None and REAP_SUCCESSOR_CACHE.get(("at", key), 0) > stamp - 30:
        return cached
    records = []
    for record in rows(conn, "SELECT key, value FROM meta WHERE key LIKE 'cleanup:successor:%'"):
        try:
            proof = json.loads(record["value"])
        except (ValueError, TypeError, KeyError):
            return None  # a record that cannot be read proves nothing
        if not isinstance(proof, dict) or not isinstance(proof.get("job"), str) or not proof["job"].strip():
            return None
        records.append(proof)
    REAP_SUCCESSOR_CACHE[("records", key)] = records
    REAP_SUCCESSOR_CACHE[("at", key)] = stamp
    return records


def reap_resume_records(conn) -> list[dict] | None:
    """The jobs that name a source in their own resume or redrive record, parsed once per short window."""
    stamp, key = time.monotonic(), reap_state_key(conn)
    cached = REAP_SUCCESSOR_CACHE.get(("resume", key))
    if cached is not None and REAP_SUCCESSOR_CACHE.get(("resumeAt", key), 0) > stamp - 30:
        return cached
    records = []
    for job in rows(conn, "SELECT id, meta FROM jobs"):
        meta = job_meta(job)
        for name in ("resume", "redrive", "automaticRecovery"):
            entry = meta.get(name)
            if isinstance(entry, dict) and isinstance(entry.get("from"), str) and entry["from"].strip():
                records.append({"job": job["id"], "from": entry["from"].strip()})
    REAP_SUCCESSOR_CACHE[("resume", key)] = records
    REAP_SUCCESSOR_CACHE[("resumeAt", key)] = stamp
    return records


def reap_continuation_ids(conn, job_id: str) -> list[str] | None:
    """The jobs this job's work continued in, or None when a record cannot be read.

    Three records: the continuation the host queued for it (an interrupted job's continuation, a failed job's redrive child),
    keyed by the job id; the successor that accepted its scope, which the host keys by the source's terminal id, so it is
    found by the job the record names; and the job that names this one in its own resume or redrive record, which survives a
    second redrive that overwrote the first record. A released scope does not end the chain: the successor is still followed,
    because the heir that released it may still be working."""
    ids = []
    record = row(conn, "SELECT value FROM meta WHERE key=?", ("interrupt-heir:" + job_id,))
    heir = record.get("value") if record else None
    if isinstance(heir, str) and heir.strip():
        ids.append(heir.strip())
    records = reap_successor_records(conn)
    if records is None:
        return None  # a record that cannot be read proves nothing
    for proof in records:
        if proof.get("job") != job_id:
            continue
        successor = proof.get("successor")
        if not isinstance(successor, str) or not successor.strip():
            return None
        ids.append(successor.strip())
    resumed = reap_resume_records(conn)
    if resumed is None:
        return None
    for record in resumed:
        if record.get("from") == job_id:
            ids.append(record["job"])
    return list(dict.fromkeys(ids))


def reap_chain_clock(conn, job: dict, depth: int = 0, seen: set | None = None) -> float | None:
    """The latest finish in a job's continuation chain, or None while any job in that chain is still live.

    Every job in the chain counts, whatever its status: the continuation the host queued for it and the successor that
    accepted its scope. A job that is queued, approved, running, blocked or parked is still live, so the work it holds is
    not finished and the source keeps everything. A missing job, a cycle, a chain deeper than the cap or a clock that
    cannot be proved keeps everything too."""
    if depth > 8:
        return None
    job_id = job.get("id")
    if not isinstance(job_id, str) or not job_id:
        return None
    seen = seen if seen is not None else set()
    if job_id in seen:
        return None  # a cycle proves nothing
    seen.add(job_id)
    successors = reap_continuation_ids(conn, job_id)
    if successors is None:
        return None
    if job.get("status") == "interrupted":
        base = reap_interruption_clock(conn, job)
        if base is None or not successors:
            return None  # an interrupted job without a continuation never finished
    elif job.get("status") in REAP_TERMINAL:
        base = job.get("finished_at")
        if not isinstance(base, (int, float)) or isinstance(base, bool) or base <= 0:
            return None
    else:
        return None  # a live job in the chain
    # The clock must belong to this job's own lifetime: after the job was created, and after it started when the host
    # recorded a start. A job with no recorded start is anchored on its creation.
    created = job.get("created_at")
    if not isinstance(created, (int, float)) or isinstance(created, bool) or base < created:
        return None
    started = job.get("started_at")
    if started is not None and (not isinstance(started, (int, float)) or isinstance(started, bool)):
        return None  # a start clock that cannot be read proves nothing
    if isinstance(started, (int, float)) and base < started:
        return None
    latest = base
    for successor_id in successors:
        successor = row(conn, "SELECT * FROM jobs WHERE id=?", (successor_id,))
        if not successor:
            return None
        successor_created = successor.get("created_at")
        if not isinstance(successor_created, (int, float)) or isinstance(successor_created, bool):
            return None
        if successor_created < created:
            return None  # the continuation was queued before this job existed: it cannot be this job's continuation
        if isinstance(started, (int, float)) and successor_created < started:
            return None
        later = reap_chain_clock(conn, successor, depth + 1, seen)
        if later is None:
            return None
        latest = max(latest, later)
    return latest


def reap_continued_interrupted(conn, job: dict) -> float | None:
    """The clock from which an interrupted job's artifacts may age, else None.

    The continuation record is the host's own: the id of the job it queued to continue the interrupted one. The whole
    continuation chain must have finished: a continuation that is still queued, waiting for approval or working may still
    need the source's folders, so the source keeps everything until that work is done. The clock is the latest finish in
    the chain (the source's own interruption, then every continuation's), so nothing ages before the last work stopped.

    A record that names a continuation that does not exist, or a chain that never ends, proves nothing: the job keeps
    everything, exactly as before."""
    if job.get("status") != "interrupted":
        return None
    return reap_chain_clock(conn, job)


def reap_job_finished_clock(conn, job: dict) -> float | None:
    """When a job's artifacts may start aging: its finish time, or the clock of an interrupted job whose whole continuation
    chain has finished. None while the job is still live (nothing may age)."""
    if job.get("status") in REAP_TERMINAL:
        return job.get("finished_at")
    return reap_continued_interrupted(conn, job)


def reap_scratch_expired(job_id: str | None) -> bool:
    """The job finished (done/failed/cancelled, or interrupted with a durable continuation) at least
    evidenceExpiryDays ago."""
    days = evidence_expiry_days(load_policy())
    if not job_id or not days:
        return False
    with db() as conn:
        job = row(conn, "SELECT * FROM jobs WHERE id=?", (job_id,))
        if not job:
            return False
        clock = reap_job_finished_clock(conn, job)
    return bool(clock) and now() - clock >= days * 86400


# ---- protected leaves: identities before and after, from lstat and listings only -------------------------------------

def reap_preserved_leaves(path: str, root: str) -> list[str]:
    """Protected names that sit directly in each directory from the item's parent up to its root. Names only, listed
    descriptor-relative: the root is opened with no symlink in any component, each directory down to the item's parent
    with O_NOFOLLOW and compared by device and inode with the lstat that chose it, so a directory swapped for a symlink
    (to a protected root, say) is never listed. A protected name on the way down is never opened. Any failure refuses."""
    parent = os.path.dirname(path)
    if not reap_inside(parent, root):
        raise ReapRefused("preserved-inventory-incomplete")
    steps = [] if parent == root else Path(parent).relative_to(root).parts
    if any(reap_protected(step) for step in steps):
        raise ReapRefused("preserved-inventory-incomplete")
    levels, fd = [], None
    try:
        fd = os.open(root, REAP_ROOT_FLAGS)
        for depth in range(len(steps) + 1):
            with os.scandir(fd) as entries:
                names = sorted(entry.name for entry in entries)
            levels.append(os.path.join(root, *steps[:depth]))
            levels.append([name for name in names if reap_protected(name)])
            if depth == len(steps):
                break
            info = os.stat(steps[depth], dir_fd=fd, follow_symlinks=False)
            child = os.open(steps[depth], REAP_DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
            opened = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ReapRefused("preserved-inventory-incomplete")
    except OSError:
        raise ReapRefused("preserved-inventory-incomplete")
    finally:
        if fd is not None:
            os.close(fd)
    found = []
    for directory, protected in zip(reversed(levels[0::2]), reversed(levels[1::2])):  # the item's parent first, the root last
        found.extend(os.path.join(directory, name) for name in protected)
    return found


def reap_identity(leaf: str) -> dict:
    """The identity of one protected leaf from ONE lstat of the leaf itself. A protected leaf is never entered: this never
    lists, opens or stats anything below it and never reads a byte, so a receipt carries no name, content or environment
    value from BELOW the leaf. It does carry the leaf's own name (`path`, for example `.env.production` or `.wrangler`). It proves only the leaf ROOT's metadata (type, device, inode, mode, link count, size, mtime, ctime).
    `rootMetaDigest` is the sha256 of the JSON list of the first eight REAP_IDENTITY_KEYS values, in that order, so a reader
    can recompute it. What cannot be measured without entering is recorded as unmeasured, never as 0: a directory's entry
    count and the bytes below it are null and `unverified` names what no check has seen. Adding, removing or renaming a direct
    child normally moves the directory's own mtime and ctime, but a rewrite of a file deeper down, or a same-size content
    change, leaves the root's metadata as it was and is NOT detected."""
    info = os.lstat(leaf)
    mode = info.st_mode
    kind = "dir" if stat.S_ISDIR(mode) else "link" if stat.S_ISLNK(mode) else "file" if stat.S_ISREG(mode) else "other"
    fields = [kind, info.st_dev, info.st_ino, mode, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
    return {"path": leaf, "kind": kind, "dev": info.st_dev, "ino": info.st_ino, "mode": mode, "nlink": info.st_nlink,
            "size": info.st_size, "mtimeNs": info.st_mtime_ns, "ctimeNs": info.st_ctime_ns,
            "scope": REAP_IDENTITY_SCOPE, "entries": None, "bytes": None,
            "unverified": "subtree-entries-and-contents" if kind == "dir" else "link-target" if kind == "link" else "file-contents",
            "rootMetaDigest": hashlib.sha256(json.dumps(fields).encode()).hexdigest()}


def reap_changed_identities(before: list[dict]) -> list[str]:
    changed = []
    for entry in before:
        try:
            after = reap_identity(entry["path"])
        except (OSError, ReapRefused):
            after = None
        if after is None or any(after[key] != entry[key] for key in REAP_IDENTITY_KEYS):
            changed.append(entry["path"])
    return changed


# ---- producer locks: a build queue's own per-project flocks (the host names its state folder: reap_queue_state) ----

def reap_queue_lock(project: str) -> Path:
    """The lock file a build queue takes for one project: <queue state>/projects/<name>-<sha256(path)[:16]>.lock."""
    absolute = str(Path(project).expanduser().resolve())
    digest = hashlib.sha256(absolute.encode("utf-8")).hexdigest()[:16]
    safe = "".join(c for c in (Path(absolute).name or "project") if c.isalnum() or c in "-_").rstrip("-").lower()[:32]
    return reap_queue_state() / "projects" / f"{safe}-{digest}.lock"


def reap_cargo_locks(target: str) -> list[Path]:
    """Bounded two-level names walk, never glob below a protected name, symlink or mount."""
    found, listed = [], 0
    if any(reap_protected(part) for part in Path(target).parts):
        raise ReapRefused("protected-cargo-root")
    device = os.lstat(target).st_dev
    def walk(fd, path, depth):
        nonlocal listed
        with os.scandir(fd) as entries:
            for entry in entries:
                listed += 1
                if listed > REAP_WALK_ENTRIES:
                    raise ReapRefused("cargo-lock-scan-incomplete")
                if reap_protected(entry.name) or reap_is_git(entry.name):
                    continue
                info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
                if info.st_dev != device:
                    raise ReapRefused("cargo-lock-crosses-device")
                childpath = path / entry.name
                if depth in (1, 2) and entry.name in (".cargo-lock", ".cargo-build-lock") and stat.S_ISREG(info.st_mode):
                    found.append(childpath)
                elif depth < 2 and stat.S_ISDIR(info.st_mode):
                    child = os.open(entry.name, REAP_DIR_FLAGS, dir_fd=fd)
                    try:
                        opened = os.fstat(child)
                        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                            raise ReapRefused("cargo-lock-identity-changed")
                        walk(child, childpath, depth + 1)
                    finally:
                        os.close(child)
    fd = os.open(target, REAP_ROOT_FLAGS)
    try:
        walk(fd, Path(target), 0)
    finally:
        os.close(fd)
    return found


@contextmanager
def reap_locks(paths: list[Path], create: bool):
    """Nonblocking exclusive flocks held until the block ends, on the SAME canonical lock files the producer takes
    (same state directory and key as reap_queue_lock). A busy or unobtainable lock preserves the item. A
    symlinked lock file is never followed. The producer's stale-lock cleanup unlinks a lock file it can lock, so a lock
    taken on a file that is no longer the one at its path excludes no writer: that preserves the item too. Without create
    (audits, dry runs) only lock files that already exist are probed, so nothing is written."""
    held = []
    try:
        for path in paths:
            try:
                if create:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o666)
                else:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            except FileNotFoundError:
                continue  # an absent lock file has no holder
            except OSError:
                raise ReapRefused(f"lock-unavailable:{path.name}")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                raise ReapRefused(f"lock-busy:{path.name}")
            try:
                taken, current = os.fstat(fd), os.lstat(path)
                stable = (taken.st_dev, taken.st_ino) == (current.st_dev, current.st_ino) and stat.S_ISREG(current.st_mode)
            except OSError:
                stable = False
            if not stable:
                os.close(fd)
                raise ReapRefused(f"lock-unstable:{path.name}")
            held.append(fd)
        yield len(held)
    finally:
        for fd in held:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


# ---- receipts: append-only, one JSON object per line, per item -------------------------------------------------

def reap_durable(fd: int) -> None:
    """F_FULLFSYNC asks the drive to flush its cache, which fsync alone does not on macOS; fsync where unsupported."""
    fullsync = getattr(fcntl, "F_FULLFSYNC", None)
    if fullsync is None:
        os.fsync(fd)
        return
    try:
        fcntl.fcntl(fd, fullsync)
    except OSError as error:
        if error.errno not in (errno.ENOTSUP, errno.ENOTTY, errno.EINVAL):
            raise
        os.fsync(fd)


def reap_receipt_lock(fd: int) -> None:
    """The receipt file's exclusive lock, with a bounded blocking wait. Two reaper runs (the tick and a manual reap
    of another job) append to the same day's file; the second one waits for the first record instead of failing, and only a
    wait longer than REAP_RECEIPT_LOCK_SECONDS is `receipt-unavailable:lock-timeout:<seconds>s`."""
    deadline = time.monotonic() + REAP_RECEIPT_LOCK_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as error:
            if error.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise
        if time.monotonic() >= deadline:
            raise ReapRefused(f"receipt-unavailable:lock-timeout:{REAP_RECEIPT_LOCK_SECONDS:g}s")
        time.sleep(0.02)


def reap_receipt(record: dict) -> None:
    """One JSON object per line in a single append, then flushed with the directory entry. Never truncates, never follows
    a symlinked directory or receipt file. Anything short of a complete, flushed line raises ReapRefused
    `receipt-unavailable:<cause>`: a plan that is not durable removes nothing."""
    owner = getattr(REAP_CLEANUP_OWNER, "held", None)
    line = (json.dumps({"v": 2, "ts": now(), "at": iso(), "pid": os.getpid(),
                        "cleanupOwnerRun": owner["run"] if owner else None, **record}, sort_keys=True, default=str) + "\n").encode()
    name = f"receipts-{time.strftime('%Y%m%d', time.localtime(now()))}.jsonl"
    try:
        directory = reap_receipt_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        dfd = os.open(directory, REAP_DIR_FLAGS)
    except OSError as error:
        raise ReapRefused("receipt-unavailable:" + reap_named(error))
    try:
        info = os.fstat(dfd)
        if info.st_uid != os.getuid():
            raise ReapRefused("receipt-unavailable:foreign-directory")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.fchmod(dfd, 0o700)
        fd = os.open(name, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
        try:
            # Serialize appenders so a previous ENOSPC fragment cannot swallow the next record.
            reap_receipt_lock(fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ReapRefused("receipt-unavailable:not-a-private-file")
            if stat.S_IMODE(info.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
            if info.st_size:
                os.lseek(fd, -1, os.SEEK_END)
                if os.read(fd, 1) != b"\n":
                    line = b"\n" + line
            written = os.write(fd, line)
            if written != len(line):
                # One record is one append; the remainder is never written separately, another writer may be between.
                try:
                    os.write(fd, b"\n")  # end the fragment so the next record starts on its own line
                except OSError:
                    pass
                raise ReapRefused(f"receipt-unavailable:short-write:{written}/{len(line)}")
            reap_durable(fd)
        finally:
            os.close(fd)
        reap_durable(dfd)
    except OSError as error:
        raise ReapRefused("receipt-unavailable:" + reap_named(error))
    finally:
        os.close(dfd)


# ---- ownership: which job an artifact belongs to ---------------------------------------------------------------

def reap_clone_roots() -> list[tuple[str, list[str]]]:
    """Job clones/worktrees by name in every clone base the host names (reap_clone_bases): <base>/*-jNNNN*, <base>/jNNNN-*,
    and in a base with a required prefix only names that start with it (<base>/.jNNNN*). Names only."""
    found = []
    for base, prefix in reap_clone_bases():
        try:
            entries = sorted(os.scandir(base), key=lambda e: e.name)
        except FileNotFoundError:
            continue
        except OSError as error:  # an unreadable root is not an empty one: nothing may be marked reaped on its word
            raise HostError(f"cannot list {base}: {error.__class__.__name__}")
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            ids = reap_job_ids(entry.name)
            if prefix and not entry.name.startswith(prefix):
                continue
            if ids:
                found.append((reap_canon(entry.path), ids))
    return found


def reap_clone_base_paths() -> list[str]:
    """The clone bases for boundary comparisons: each base's parent canonical, its own name taken as written, so a base that
    is itself a symlink never matches the canonical path of a clone found through it (such clones are kept)."""
    return [os.path.join(reap_canon(Path(base).parent), Path(base).name) for base, _ in reap_clone_bases()]


def reap_declared_paths(job: dict) -> list[str]:
    """Machine-recorded cwd, worktrees and path claims, never free text from a prompt or brief."""
    meta = job_meta(job)
    declared = [job.get("cwd")] + list(meta.get("worktrees") or []) + list(meta.get("paths") or [])
    return [reap_canon(Path(str(p)).expanduser()) for p in declared if isinstance(p, str) and p.strip().startswith(("/", "~"))]


_REAP_LINKED: dict = {}


def reap_linked_cached(root: str) -> bool:
    """reap_linked_worktree, remembered for 30 seconds so an audit over hundreds of jobs asks git once per path."""
    stamp = time.monotonic()
    hit = _REAP_LINKED.get(root)
    if hit is None or stamp - hit[0] > 30:
        try:
            hit = (stamp, reap_linked_worktree(root))
        except ReapRefused:
            hit = (stamp, False)
        _REAP_LINKED[root] = hit
    return hit[1]


def reap_job_roots(job: dict, clones: list) -> list[tuple[str, str]]:
    """(clone root, how ownership is known) for one job: its named clones, plus declared linked worktrees. A declared
    main checkout (many jobs run in one shared checkout) is not the job's to reap and is not listed at all."""
    roots = [(root, "name-pattern") for root, ids in clones if job["id"] in ids]
    bases = reap_clone_base_paths()
    for declared in reap_declared_paths(job):
        if os.path.isdir(declared) and not any(declared == r for r, _ in roots) \
                and os.path.dirname(declared) in bases and reap_linked_cached(declared):
            roots.append((declared, "declared-worktree"))
    return roots


def reap_repo_of(path: str, root: str) -> str:
    """The git repository an item belongs to: the nearest directory at or above it, up to the clone root, holding
    a .git entry. A clone root that is only a folder of several repositories (j0268-core/*) is handled per repository."""
    current = os.path.dirname(path)
    while reap_inside(current, root):
        if os.path.lexists(os.path.join(current, ".git")):
            return current
        if current == root:
            break
        current = os.path.dirname(current)
    return root


def reap_job_items(job: dict, clones: list, audit_dirs: bool = True) -> tuple[list[dict], list[str]]:
    """Everything a job left behind that the reaper could remove, as items. Names only. Whole clones come last."""
    items, notes, clone_items = [], [], []
    for root, how in reap_job_roots(job, clones):
        found, incomplete = reap_scan_clone(root)
        if incomplete:
            notes.append(f"scan incomplete: {root}")
        ids = reap_job_ids(os.path.basename(root)) if how == "name-pattern" else [job["id"]]
        for path, kind in found:
            item = {"path": path, "root": root, "repo": reap_repo_of(path, root), "owner": job["id"], "jobs": ids,
                    "class": "job-clone-regenerable", "kind": kind, "ownership": how}
            items.append({**item, "retention": reap_item_retention(item)})
        if how == "name-pattern" and not incomplete:  # the clone itself, after its outputs
            whole = {"path": root, "root": reap_canon(os.path.dirname(root)), "repo": root, "owner": job["id"], "jobs": ids,
                     "class": "job-clone-root", "kind": "clone", "ownership": how}
            retention = reap_item_retention(whole)
            if retention == REAP_DISPOSABLE:  # listed only once removable: before expiry, or marked kept, the clone is not an item
                clone_items.append({**whole, "retention": retention})
    cache = reap_cache_root() / job["id"]
    try:
        with os.scandir(cache) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                if not reap_protected(entry.name) and not entry.is_symlink() and entry.is_dir(follow_symlinks=False):
                    item = {"path": reap_canon(entry.path), "root": reap_canon(cache), "owner": job["id"], "jobs": [job["id"]],
                            "class": "job-cache-subdir", "kind": "cache-subdir", "ownership": "job-cache-dir"}
                    items.append({**item, "retention": reap_item_retention(item)})
    except FileNotFoundError:
        pass
    except OSError:
        notes.append(f"unreadable: {cache}")
    for item in items:
        if item["class"] == "job-cache-subdir" and os.path.basename(item["path"]) in REAP_OUTPUT_KINDS and reap_output_registry_present(item["path"]):
            targeted = reap_output_item(job["id"], item["path"])
            if targeted:
                item.update(targeted)
    return items + clone_items, notes


# ---- exact terminal-job outputs (never a source-root permission) ------------------------------------------------

def reap_output_registry_present(path: str) -> bool:
    """Cheap capability discovery only; this never grants authority or reads a record."""
    registry = reap_queue_state() / "output-producers" / (hashlib.sha256(path.encode("utf-8")).hexdigest() + ".json")
    try:
        info = os.lstat(registry)
        return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
    except OSError:
        return False


def reap_output_item(owner: str, path: str) -> dict | None:
    """Reconstruct the source/output association from host-owned paths, not a caller's manifest.
    Detached output is only <cache>/<job>/{target,node_modules}, beside one declared source checkout.
    Ambiguous declarations, source symlinks, main shared checkouts and release trees are not inferred."""
    if os.path.basename(path) not in REAP_OUTPUT_KINDS or reap_canon(path) != path:
        return None
    with db() as conn:
        job = row(conn, "SELECT * FROM jobs WHERE id=?", (owner,))
    if not job or job["status"] not in REAP_OUTPUT_TERMINAL:
        return None
    cache = reap_canon(reap_cache_root() / owner)
    sources = []
    for root, how in reap_job_roots(job, reap_clone_roots()):
        if reap_inside(path, root) and path != root:
            source = reap_repo_of(path, root)
            if os.path.lexists(os.path.join(source, ".git")):
                sources.append((source, root, root, how))
    if os.path.dirname(path) == cache:
        # Host path claims are machine records too; cwd is often null for managed workers.
        declared = reap_declared_paths(job) + reap_output_claims(job)
        for source in sorted(set(declared)):
            if os.path.dirname(source) == cache and source != path and os.path.basename(source) not in REAP_REGENERABLE \
                    and not os.path.islink(source) and os.path.lexists(os.path.join(source, ".git")):
                sources.append((source, source, cache, "detached-job-output"))
    sources = sorted(set(sources))
    if len(sources) != 1:
        return None
    source, source_root, root, how = sources[0]
    if how != "detached-job-output" and os.path.dirname(source_root) not in reap_clone_base_paths():
        return None  # a symlinked clone base cannot gain authority through this narrower route
    if any(reap_protected(p) for p in Path(source).parts):
        return None
    # Neither configured release data nor anything inside another derived output is a source checkout.
    if reap_inside_folded(source, reap_canon(reap_release_root())) \
            or any(reap_fold(p) in REAP_REGENERABLE for p in Path(source).relative_to(source_root).parts):
        return None
    item = {"path": path, "root": root, "source": source, "sourceRoot": source_root,
            "owner": owner, "jobs": [owner], "class": "job-derived-output", "kind": os.path.basename(path),
            "ownership": how}
    item["retention"] = reap_item_retention(item)
    return item


def reap_output_terminal_blocker(conn, job: dict, live_ids: set | None, idle: dict | None = None) -> str | None:
    """Fresh terminal proof without treating continuation/waiting on SOURCE as an output claim."""
    if job["status"] not in REAP_OUTPUT_TERMINAL:
        return "job-" + job["status"]
    stamp = job.get("finished_at")
    if job["status"] == "interrupted":
        # Explicit clocks supersede finished_at. Older hosts need a matching
        # interruption event, never an unrelated stale finish clock.
        stamp = reap_interruption_clock(conn, job)
    else:
        stamp = reap_iso_clock(stamp)
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool) or not 0 < stamp <= now() - REAP_SETTLE_SECONDS:
        return "output-terminal-time-unverified"
    if live_ids is None:
        return "terminal-state-unknown"
    terminal = job.get("terminal_id")
    if terminal:
        if terminal in live_ids and not reap_open_terminal_past_grace(job, idle):
            return "worker-terminal-still-open"
        record = row(conn, "SELECT status FROM cleanup_records WHERE terminal_id=?", (terminal,))
        if record and record["status"] != "closed":
            return "cleanup-" + record["status"]
        if any(j["status"] not in REAP_TERMINAL for j in
               rows(conn, "SELECT status FROM jobs WHERE terminal_id=? AND id!=?", (terminal, job["id"]))):
            return "terminal-shared-with-live-job"
    return None


def reap_output_claims(job: dict, outputs_only: bool = False) -> list[str]:
    """Explicit machine paths, including UltraTerm meta.paths, never prose.
    paths/output declarations claim both ancestors and descendants; cwd/worktrees only claim
    outputs they enter, so a source-only continuation does not itself own detached output.
    Malformed claims fail closed. Source ownership remains cwd/worktrees, not meta.paths.
    """
    meta = job_meta(job)
    if job.get("meta"):
        try:
            raw = json.loads(job["meta"]) if isinstance(job["meta"], str) else job["meta"]
            if not isinstance(raw, dict):
                raise ValueError("invalid job metadata")
        except (ValueError, TypeError):
            raise ReapRefused("output-declarations-unverified:" + job["id"])
    cwd = job.get("cwd")
    worktrees = meta.get("worktrees", [])
    if (cwd is not None and (not isinstance(cwd, str) or not cwd.startswith("/") or ".." in Path(cwd).parts)) \
            or not isinstance(worktrees, list) \
            or not all(isinstance(p, str) and p.startswith("/") and ".." not in Path(p).parts for p in worktrees):
        raise ReapRefused("output-declarations-unverified:" + job["id"])
    paths = [] if outputs_only else list(reap_declared_paths(job))
    for key in ("paths", "outputPaths", "outputs", "buildOutputs", "targetDir", "cargoTargetDir"):
        value = meta.get(key, [])
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list) or not all(isinstance(p, str) and p.startswith("/") and ".." not in Path(p).parts for p in values):
            raise ReapRefused("output-declarations-unverified:" + job["id"])
        paths.extend(reap_canon(p) for p in values)
    return sorted(set(paths))


def reap_output_authority(item: dict) -> dict:
    trusted = reap_output_item(item["owner"], item["path"])
    if not trusted or any(trusted.get(k) != item.get(k) for k in ("source", "sourceRoot", "root", "ownership", "jobs")):
        raise ReapRefused("output-source-ambiguous")
    with db() as conn:
        jobs = rows(conn, "SELECT * FROM jobs")
        owner = next((j for j in jobs if j["id"] == item["owner"]), None)
        if not owner:
            raise ReapRefused("owner-unknown")
        blocker = reap_output_terminal_blocker(conn, owner, reap_live_ids())
        if blocker:
            raise ReapRefused(blocker)
        releases = rows(conn, "SELECT value FROM meta WHERE key LIKE 'cleanup:released:%'")
    for record in releases:
        try:
            proof = json.loads(record["value"])
            kept = proof.get("keep", [])
            if not isinstance(kept, list) or not all(isinstance(p, str) and p.startswith("/") for p in kept):
                raise ValueError("invalid keep paths")
        except (ValueError, TypeError, AttributeError):
            raise ReapRefused("output-release-claims-unverified")
        for path in kept:
            if reap_inside_folded(item["path"], reap_canon(path)) or reap_inside_folded(reap_canon(path), item["path"]):
                raise ReapRefused("kept-by-release:" + path)
    inventory = []
    for job in jobs:
        claims = reap_output_claims(job)
        outputs = reap_output_claims(job, outputs_only=True)
        # Other interrupted jobs remain conservative live claimants; only this owner has a terminal proof.
        if job["id"] != item["owner"] and job["status"] not in REAP_TERMINAL:
            if any(reap_inside_folded(p, item["path"]) for p in claims) \
                    or any(reap_inside_folded(item["path"], p) for p in outputs):
                raise ReapRefused("output-claimed-by:" + job["id"])
        inventory.append((job["id"], job["status"], claims, outputs))
    ownership = globals().get("reap_output_ownership")
    if not callable(ownership):
        raise ReapRefused("output-producer-unverified:host-ownership-interface-required")
    producer = ownership(item["source"], item["path"])
    reap_output_producer_proof(item, producer, reap_output_projects(item["source"], item["path"], item["root"]))
    return {"producer": producer, "source": "host-exact-output", "owner": item["owner"], "status": owner["status"],
            "finishedAt": owner.get("finished_at"), "terminal": owner.get("terminal_id"), "claims": inventory}


def reap_output_projects(source: str, output: str, root: str) -> list[str]:
    projects = {source}
    current = os.path.dirname(output)
    while reap_inside(current, root):
        projects.add(current)
        if current == root:
            break
        current = os.path.dirname(current)
    return sorted(projects)


def reap_registered_output_ownership(source: str, output: str) -> dict:
    """Read a producer-written registry record; absence never permits adoption of an old output.
    The queue writes this while holding the same permanent project locks this caller holds.
    Same-user cooperative exclusion only: not protection against a malicious writer bypassing the queue.
    """
    root = reap_queue_state() / "output-producers"
    digest = hashlib.sha256(output.encode("utf-8")).hexdigest()
    path = root / (digest + ".json")
    try:
        fence = reap_fence(str(root), str(reap_queue_state()))
        queue_fd = reap_open_fence(fence)
        try:
            parent_fd = os.open(root.name, REAP_DIR_FLAGS, dir_fd=queue_fd)
        finally:
            os.close(queue_fd)
        try:
            reap_expect(parent_fd, fence[-1][1], fence[-1][2], "output-registry-directory-changed")
            if os.fstat(parent_fd).st_mode & 0o077:
                raise ReapRefused("output-producer-unverified:unsafe-registry-directory")
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > 65536:
                raise ReapRefused("output-producer-unverified:unsafe-registry")
            raw = os.read(fd, 65537)
        finally:
            os.close(fd)
        record = json.loads(raw)
    except (OSError, ValueError, UnicodeError) as error:
        raise ReapRefused("output-producer-unverified:registry:" + reap_named(error)) from error
    if not isinstance(record, dict) or record.get("source") != source or record.get("output") != output:
        raise ReapRefused("output-producer-unverified:registry-association")
    trusted = reap_output_item(record.get("owner"), output)
    if not trusted or trusted["source"] != source:
        raise ReapRefused("output-producer-unverified:registry-owner")
    projects = reap_output_projects(source, output, trusted["root"])
    lock_projects = record.get("lockProjects")
    if not isinstance(lock_projects, list) or not all(p in lock_projects for p in projects):
        raise ReapRefused("output-producer-unverified:registry-locks")
    for project in projects:
        try:
            info = os.lstat(reap_queue_lock(project))
        except OSError as error:
            raise ReapRefused("output-producer-unverified:lock-missing") from error
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ReapRefused("output-producer-unverified:lock-unsafe")
    for key, directory in (("sourceIdentity", source), ("outputIdentity", output)):
        identity = record.get(key)
        info = os.lstat(directory)
        if not isinstance(identity, dict) or not stat.S_ISDIR(info.st_mode) or any(
                identity.get(k) != v for k, v in (("dev", info.st_dev), ("ino", info.st_ino))):
            raise ReapRefused("output-producer-unverified:registry-identity:" + key)
    if record.get("state") != "finished":
        producer = record.get("producer")
        pid = producer.get("pid") if isinstance(producer, dict) else None
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ReapRefused("output-producer-unverified:registry-producer")
        table = reap_process_table()
        if any(str(number) == str(pid) for number, _ in table["commands"]):
            raise ReapRefused("output-producer-unverified:producer-still-active")
        # A dead producer's exact output is still checked under its locks, including Cargo locks.
        if record.get("state") != "active":
            raise ReapRefused("output-producer-unverified:registry-state")
    proof = {k: record.get(k) for k in ("protocol", "source", "output", "owner", "kind", "claimInventory")}
    proof["projects"] = projects
    reap_output_producer_proof(trusted, proof, projects)
    return proof


@contextmanager
def reap_output_producer_guard(source: str, output: str):
    """Cooperative producer protocol, NOT an OS sandbox or an all-process exclusion claim.

    Required host interface: reap_output_ownership(source, output) returns a FRESH record
    {protocol: 'trimreaper-project-locks-v1', source, output, owner, kind,
     projects: [canonical project paths], claimInventory: 'job-meta-paths-v1'}.
    It is called with ALL reap_output_projects locks already held. The host must verify the
    exact source/output ownership from its producer registry, including detached target dirs;
    reject unmanaged/unregistered producers; and implement mandatory shared reap_queue_lock
    keys for EVERY build/install route before source/output access, held through subprocess
    exit. Keep lock inodes permanent (no unlink/replacement), register output claims before
    work starts under the same locks, and forbid new producers/ownership changes until exit.
    Returning True, reconstructing a record from these arguments, or merely checking a job's
    terminal state does NOT implement this contract. Missing capability preserves the output.
    Core checks records and claims again before removal. Fresh process/handle probes remain
    required: unknown builds are blocked, not presumed cooperative. No safety against a
    malicious same-user writer bypassing the cooperative host is claimed.
    """
    ownership = globals().get("reap_output_ownership")
    if not callable(ownership):
        raise ReapRefused("output-producer-unverified:host-ownership-interface-required")
    proof = ownership(source, output)
    if not isinstance(proof, dict):
        raise ReapRefused("output-producer-unverified:ownership-record-required")
    yield proof


def reap_output_producer_proof(item: dict, proof, projects: list[str]) -> None:
    expected = {"protocol": "trimreaper-project-locks-v1", "source": item["source"],
                "output": item["path"], "owner": item["owner"], "kind": item["kind"],
                "projects": projects, "claimInventory": "job-meta-paths-v1"}
    if not isinstance(proof, dict) or any(proof.get(k) != v for k, v in expected.items()):
        raise ReapRefused("output-producer-unverified:ownership-record-mismatch")


def reap_output_control_agent(pid: str, command: str, table: dict, item: dict) -> bool:
    """An exact host-registered control command, never a generic node exemption.
    Host registrations are (absolute executable, absolute entrypoint) pairs identifying Pi/
    control agents, not build runners. lsof must independently show that executable as txt
    and a cwd outside both source and output. All source/output handles are checked first.
    """
    parts = command.split()
    if len(parts) < 2 or not all(p.startswith("/") for p in parts[:2]):
        return False
    known = globals().get("REAP_OUTPUT_CONTROL_AGENTS", ())
    if (parts[0], parts[1]) not in known:
        return False
    files = [(fd, reap_private_alias(name)) for number, _, fd, name in table["files"] if number == pid]
    cwd = [name for fd, name in files if fd == "cwd"]
    executable = any(fd == "txt" and reap_canon(name) == reap_canon(parts[0]) for fd, name in files)
    return executable and len(cwd) == 1 and not any(
        reap_inside_folded(reap_canon(cwd[0]), item[key]) for key in ("source", "path"))


def reap_item_holders(item: dict, table: dict | None) -> None:
    if item["class"] != "job-derived-output":
        reap_holders(item["path"], item["path"] if item["class"] == "job-clone-root" else item["root"], table)
        return
    if table is None:
        code, out = reap_probe(["lsof", "-n", "-P", "-Fp", "--", item["path"]], 30)
        if code not in (0, 1):
            raise ReapRefused("probe-failed:lsof:" + str(code))
        if out.strip():
            raise ReapRefused("in-use:target-open")
        table = reap_process_table()
    output, source = reap_fold(item["path"]), reap_fold(item["source"])
    for pid, command, fd, name in table["files"]:
        name = reap_fold(reap_private_alias(name))
        if reap_inside(name, output):
            raise ReapRefused("in-use:output-handle:" + pid)
        # Only source cwd is exempt. Non-cwd handles in the source still fail closed.
        if fd != "cwd" and reap_inside(name, source):
            raise ReapRefused("in-use:source-handle:" + pid)
    builders = re.compile(r"(?:^|[\s/])(?:cargo|rustc|rustup|npm|npx|node|pnpm|yarn|bun|make|cmake|ninja|maturin|webpack|vite)(?:\s|$)")
    for pid, command in table["commands"]:
        folded = reap_fold(command)
        # Unattributed builders are unsafe too: relative paths/environment can hide a detached target.
        if output in folded or source in folded or (builders.search(folded) and not reap_output_control_agent(pid, command, table, item)):
            raise ReapRefused("in-use:output-command:" + pid)


def reap_item(item: dict, mode: str = "delete", ctx: dict | None = None) -> dict:
    result = reap_item_strict(item, mode, ctx)
    if item.get("class") not in ("job-clone-regenerable", "job-cache-subdir") \
            or os.path.basename(str(item.get("path"))) not in REAP_OUTPUT_KINDS \
            or result["outcome"] != "refused" or result.get("stopped") or result.get("removed") or result.get("archive"):
        return result
    if not callable(globals().get("reap_output_producer_guard")) or not reap_output_registry_present(item["path"]):
        return result  # preserve strict diagnostics and avoid extra probes for legacy outputs
    if result.get("reason", "").startswith(("outside-", "path-not-canonical", "allowed-root-", "protected-", "not-a-real-", "foreign-owner")):
        return result  # a source-boundary refusal is never a scoped-output permission
    targeted = reap_output_item(item["owner"], item["path"])
    if targeted:
        output_result = reap_item_strict(targeted, mode, ctx)
        if output_result["outcome"] in ("deleted", "would-delete") or output_result.get("stopped"):
            return output_result
        result["outputRouteReason"] = output_result.get("reason")
    return result


def reap_output_sizes(items: list[dict]) -> dict:
    """Bounded, fully gated audit ranking. Unknown, blocked and incompletely sized outputs get no priority.
    Audit never authorizes removal: every proof is rerun with fresh process state by the deletion pass."""
    deadline, scores, ctx = time.monotonic() + REAP_OUTPUT_SCHEDULE_SECONDS, {}, {}
    for item in items[:REAP_OUTPUT_SCHEDULE_ITEMS]:
        if time.monotonic() >= deadline:
            break
        if os.path.basename(item["path"]) not in REAP_OUTPUT_KINDS or reap_retention(item) != REAP_DISPOSABLE \
                or not reap_output_registry_present(item["path"]):
            continue
        try:
            if "table" not in ctx:
                ctx["table"] = reap_process_table()
            ctx["sizeBudget"] = max(0, deadline - time.monotonic())
            result = reap_item(item, "audit", ctx)
        except (ReapRefused, HostError, OSError, ValueError, sqlite3.Error):
            continue
        if result["outcome"] == "would-delete" and not result["bytesIncomplete"]:
            scores[item["path"]] = result["bytes"] or 0
    return scores


def reap_output_order(items: list[dict]) -> list[dict]:
    scores = reap_output_sizes(items)
    return sorted(items, key=lambda i: (i["class"] == "job-clone-root", -scores.get(i["path"], -1)))


def reap_output_job_order(jobs: list[dict], clones: list) -> list[dict]:
    candidates, deadline = [], time.monotonic() + REAP_OUTPUT_SCHEDULE_SECONDS
    for job in jobs[:REAP_OUTPUT_SCHEDULE_ITEMS]:
        if len(candidates) >= REAP_OUTPUT_SCHEDULE_ITEMS or time.monotonic() >= deadline:
            break
        items, _ = reap_job_items(job, clones)
        candidates.extend(i for i in items if os.path.basename(i["path"]) in REAP_OUTPUT_KINDS)
    candidates = candidates[:REAP_OUTPUT_SCHEDULE_ITEMS]
    sizes = reap_output_sizes(candidates)
    scores = {}
    for item in candidates:
        if item["path"] in sizes:
            scores[item["owner"]] = max(scores.get(item["owner"], 0), sizes[item["path"]])
    return sorted(jobs, key=lambda j: -scores.get(j["id"], -1))


# ---- the single deletion path -----------------------------------------------------------------------------------

def reap_vet_root(root: str) -> None:
    """An allowed root (a parent of clones, the cache root, the releases root) is never the home directory, a
    system root, or anything enclosing or inside one. Comparisons fold case, so `/users` cannot slip past `/Users`."""
    if not os.path.isabs(root) or reap_canon(root) != root:
        raise ReapRefused("allowed-root-not-canonical")
    homes = [reap_canon(Path.home())]
    try:
        homes.append(reap_canon(pwd.getpwuid(os.getuid()).pw_dir))
    except KeyError:
        pass
    if root == "/" or any(reap_inside_folded(home, root) for home in homes):
        raise ReapRefused("allowed-root-too-broad")
    for system in REAP_SYSTEM_ROOTS + REAP_SYSTEM_PARENTS:
        if reap_inside_folded(system, root) or reap_inside_folded(reap_canon(system), root):
            raise ReapRefused("allowed-root-encloses-system-root")
    for system in REAP_SYSTEM_ROOTS:
        if reap_inside_folded(root, system) or reap_inside_folded(root, reap_canon(system)):
            raise ReapRefused("allowed-root-inside-system-root")
    try:
        reap_root = reap_canon(reap_home())  # the home the host reaps below (a test or second instance moves it)
    except HostError:
        raise ReapRefused("allowed-root-unverified:reap-home-unknown")
    if reap_inside_folded(reap_root, root):
        raise ReapRefused("allowed-root-too-broad")


def reap_boundary(item: dict) -> str:
    """Canonical-boundary gate. Returns the item's kind; refuses anything outside the root its class allows, anything
    below a protected name, and an allowed root that is too broad."""
    path, root = item["path"], item["root"]
    if reap_canon(path) != path or reap_canon(root) != root:
        raise ReapRefused("path-not-canonical")
    if not path.startswith(root + "/") or (item.get("repo") and (reap_canon(item["repo"]) != item["repo"] or not reap_inside(item["repo"], root) or not reap_inside(path, item["repo"]))):
        raise ReapRefused("outside-root")
    for part in Path(path).parts:
        if reap_protected(part):
            raise ReapRefused(f"protected-ancestor:{part}")
    rel = tuple(Path(path).relative_to(root).parts)
    cls, parent, owner = item["class"], reap_canon(Path(root).parent), item["owner"]
    if cls == "job-derived-output":
        trusted = reap_output_item(item["owner"], path)
        if trusted is None or any(item.get(k) != trusted.get(k) for k in
                                  ("root", "source", "sourceRoot", "kind", "ownership", "jobs")):
            raise ReapRefused("output-source-ambiguous")
        reap_vet_root(reap_canon(reap_cache_root()) if trusted["ownership"] == "detached-job-output"
                      else os.path.dirname(trusted["sourceRoot"]))
        kind = os.path.basename(path)
    elif cls == "job-clone-regenerable":
        if parent not in reap_clone_base_paths():
            raise ReapRefused("outside-allowed-roots")
        reap_vet_root(parent)  # the clone base this clone sits in
        name_ids = reap_job_ids(os.path.basename(root))
        if item.get("ownership") == "name-pattern":
            if owner not in name_ids or item.get("jobs") != name_ids:
                raise ReapRefused("owner-not-in-name")
        elif item.get("ownership") == "declared-worktree":
            if name_ids and owner not in name_ids:
                raise ReapRefused("owner-not-in-name")
            if not reap_linked_worktree(root):
                raise ReapRefused("declared-path-is-not-a-linked-worktree")
        else:
            raise ReapRefused("unknown-ownership")
        kind = reap_kind(rel)
    elif cls == "job-cache-subdir":
        if parent != reap_canon(reap_cache_root()) or os.path.basename(root) != owner or len(rel) != 1 or reap_protected(rel[0]):
            raise ReapRefused("outside-allowed-roots")
        reap_vet_root(parent)
        kind = "cache-subdir"
    elif cls == "job-clone-root":
        if root not in reap_clone_base_paths() or rel != (os.path.basename(path),) or item.get("ownership") != "name-pattern":
            raise ReapRefused("outside-allowed-roots")
        reap_vet_root(root)
        name_ids = reap_job_ids(os.path.basename(path))
        if owner not in name_ids or item.get("jobs") != name_ids:
            raise ReapRefused("owner-not-in-name")
        kind = "clone"
    elif cls == "job-archive":
        if parent != reap_canon(reap_archive_root()) or os.path.basename(root) != owner or len(rel) != 1 or reap_protected(rel[0]) or item.get("ownership") != "reaper-archive":
            raise ReapRefused("outside-allowed-roots")
        reap_vet_root(parent)
        kind = "archive"
    elif cls == "release-evidence":
        if root != reap_canon(reap_release_evidence_root()) or owner != RELEASE_EVIDENCE_OWNER or len(rel) != 1 or reap_protected(rel[0]):
            raise ReapRefused("outside-allowed-roots")
        reap_vet_root(root)
        kind = "release-evidence"
    elif cls == "agent-scratch":
        if root != reap_canon(reap_agent_scratch_root()) or owner != AGENT_SCRATCH_OWNER or len(rel) != 1 or reap_protected(rel[0]):
            raise ReapRefused("outside-allowed-roots")
        reap_vet_root(root)
        kind = "agent-scratch"
    else:  # release-target
        releases = reap_canon(reap_release_root())
        depth = len(Path(root).relative_to(releases).parts) if reap_inside(root, releases) and root != releases else 0
        if depth not in (1, 2) or rel not in (("target",), ("src-tauri", "target")) or owner != "release:" + Path(root).relative_to(releases).parts[0]:
            raise ReapRefused("outside-allowed-roots")
        reap_vet_root(releases)
        kind = "target"
    if kind is None or (item.get("kind") and item["kind"] != kind):
        raise ReapRefused("not-a-regenerable-output")
    return kind


def reap_fence(path: str, root: str) -> list[tuple[str, int, int]]:
    """The device and inode of the root and of every directory down to the item, which the removal re-opens and compares."""
    info = os.lstat(root)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ReapRefused("root-not-a-real-directory")
    fence, current = [(root, info.st_dev, info.st_ino)], root
    for part in Path(path).relative_to(root).parts:
        current = os.path.join(current, part)
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ReapRefused("not-a-real-directory")
        if info.st_dev != fence[0][1]:
            raise ReapRefused("crosses-filesystem")
        fence.append((part, info.st_dev, info.st_ino))
    if info.st_uid != os.getuid():
        raise ReapRefused("foreign-owner")
    return fence


def reap_authority(item: dict) -> dict:
    """Fresh job state for this item, from the host's own job records (never the item's word): every job named by the clone is
    finished, and no unfinished job claims the path (named in the directory, declared as its cwd/worktree, or waiting on
    the owner). Returns the evidence; the caller asks again later and refuses on any difference."""
    if item.get("class") == "job-derived-output":
        return reap_output_authority(item)
    with db() as conn:
        jobs = {j["id"]: j for j in rows(conn, "SELECT * FROM jobs")}
        REAP_SUCCESSOR_CACHE.clear()  # this pass decides on the records as they are now, not on a cached read
        # An interrupted job whose work a durable continuation took over is not a live claimer any more: the continuation holds
        # the work and declares its own paths. Without that record the job keeps claiming, as before.
        continued = {j["id"] for j in jobs.values() if reap_continued_interrupted(conn, j) is not None}
        # A continued job is finished work for its own windows, but every proof the job blocker makes (settle window, live
        # worker terminal, cleanup record, live sibling, awaited-by) still has to hold, freshly, on this very pass.
        live_ids = reap_live_ids() if continued else None
        continued_blocked = {j["id"]: reap_job_blocker(conn, j, live_ids) for j in jobs.values() if j["id"] in continued}
        successors = {}
        for record in rows(conn, "SELECT value FROM meta WHERE key LIKE 'cleanup:successor:%'"):
            try:
                proof = json.loads(record["value"])
            except ValueError:
                continue
            if proof.get("job") and proof.get("successor"):
                successors[proof["job"]] = proof["successor"]
        released = {}
        for r in rows(conn, "SELECT key, value FROM meta WHERE key LIKE 'cleanup:released:%'"):
            try:
                released[r["key"].split(":", 2)[2]] = json.loads(r["value"]) or {"by": "?"}
            except ValueError:
                released[r["key"].split(":", 2)[2]] = {"by": "?"}
    ids = list(dict.fromkeys(item.get("jobs") or []))
    # Scratch tasks and release versions are independent siblings, not claims on their collection root.
    # Repository output still uses its source root so a source consumer preserves its build output.
    independent = item["class"] in ("agent-scratch", "release-evidence", "job-cache-subdir")
    scope = item["path"] if independent or item["class"] == "job-clone-root" else item["root"]
    if item["class"] not in ("release-target", "agent-scratch", "release-evidence") and item["owner"] not in ids:
        raise ReapRefused("owner-not-recorded")
    for job_id in ids:
        job = jobs.get(job_id)
        if not job:
            raise ReapRefused(f"owner-unknown:{job_id}")
        if job["status"] not in REAP_TERMINAL:
            # A continued interrupted owner is finished work for its own windows, but every proof the job blocker makes
            # (settle window, live worker terminal, cleanup record, live sibling, awaited-by) still has to hold, freshly.
            # When it does not, the pressure path may still release the derived output, exactly as before.
            if job_id in continued and continued_blocked.get(job_id) is None:
                pass
            elif not reap_pressure_allows(item, job):
                raise ReapRefused(f"claimed-by:{job_id}({job['status']})")
        # A source whose scope moved to an accepted successor stays claimed while that successor is unfinished:
        # the successor inherits the source's worktrees and evidence.
        chain, seen = successors.get(job_id), {job_id}
        if released.get(job_id):
            chain = None  # the successor released this finished source's scope (scope-release, with evidence)
            for kept in released[job_id].get("keep") or []:
                if reap_inside(scope, reap_canon(Path(kept).expanduser())) or reap_inside(reap_canon(Path(kept).expanduser()), scope):
                    raise ReapRefused(f"kept-by-release:{kept}")
        while chain and chain not in seen:
            seen.add(chain)
            heir = jobs.get(chain)
            if heir and heir["status"] not in REAP_TERMINAL:
                raise ReapRefused(f"inherited-by:{chain}({heir['status']})")
            chain = successors.get(chain)
    if item["class"] == "job-clone-root":
        window = load_policy().get("cloneExpiryDays", 3)
        recent = reap_recent_users(scope, ids, window * 86400) if isinstance(window, int) and not isinstance(window, bool) and window > 0 else []
        if recent:
            raise ReapRefused(f"recently-used-by:{recent[0]}")  # a later job worked in this clone: it expires cloneExpiryDays after that job finished
    for job in jobs.values():
        if job["status"] in REAP_TERMINAL:
            continue
        continued_here = job["id"] in continued and continued_blocked.get(job["id"]) is None
        if not continued_here:
            if any(reap_inside(d, scope) or (independent and reap_inside(scope, d))
                   for d in reap_declared_paths(job)) and not reap_pressure_allows(item, job):
                raise ReapRefused(f"claimed-by:{job['id']}({job['status']})")
        wait = job_wait(job)
        if wait and wait["job"] in ids:
            raise ReapRefused(f"awaited-by:{job['id']}({job['status']})")
    return {"source": "host-jobs", "owner": item["owner"], "jobs": {job_id: jobs[job_id]["status"] for job_id in ids}, "claims": []}


def reap_ownership_gate(item: dict) -> list[str]:
    reap_authority(item)
    return ["owner-terminal", "no-other-claim"]


# ---- removal: descriptor-based, non-following, protected-name aware ------------------------------------------------------

def reap_expect(fd: int, dev: int, ino: int, reason: str) -> None:
    info = os.fstat(fd)
    if (info.st_dev, info.st_ino) != (dev, ino):
        raise ReapRefused(reason)


def reap_open_fence(fence: list[tuple[str, int, int]]) -> int:
    """The item's parent, opened from the root one component at a time with O_NOFOLLOW, each compared with the boundary
    check. A symlink, a replaced directory or a new mount anywhere in the chain refuses."""
    (root, dev, ino), label = fence[0], "root"
    try:
        fd = os.open(root, REAP_ROOT_FLAGS)
    except OSError:
        raise ReapRefused("fence-changed:root")
    try:
        reap_expect(fd, dev, ino, "fence-changed:root")
        for label, dev, ino in fence[1:-1]:
            child = os.open(label, REAP_DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
            reap_expect(fd, dev, ino, f"fence-changed:{label}")
    except OSError:
        os.close(fd)
        raise ReapRefused(f"fence-changed:{label}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def reap_empty_directory(fd: int, dev: int, depth: int, rel: str, tally: dict, allow_git: bool, templates: frozenset = frozenset(), base: str = "",
                         protected_ok: bool = False, expiring_evidence: bool = False) -> None:
    """Unlink everything below an open directory. A protected name (or `.git`, unless allowed) that appeared after the final
    scan stops the walk before it is entered or unlinked; so do another device, another owner, a swapped entry and
    REAP_MAX_DEPTH."""
    if depth > REAP_MAX_DEPTH:
        raise ReapRefused("removal-too-deep")
    info = os.fstat(fd)
    if info.st_uid != os.getuid():
        raise ReapRefused(f"foreign-owner:{rel or '.'}")
    if stat.S_IMODE(info.st_mode) & 0o700 != 0o700:
        os.fchmod(fd, stat.S_IMODE(info.st_mode) | 0o700)  # a read-only directory blocks unlinking its own entries
    names = os.listdir(fd)
    if expiring_evidence:
        if any(marked_evidence_keep(n) for n in names):
            raise ReapRefused("marked-evidence-kept-record-appeared:" + (rel or "."))
        markers = [n for n in names if reap_fold(n).startswith(REAP_MARKER_PREFIX)]
        if len(markers) > 1 or any(n not in (REAP_MARKER_PREFIX + "evidence", REAP_MARKER_PREFIX + REAP_DISPOSABLE) for n in markers):
            raise ReapRefused("marked-evidence-preserving-marker-appeared:" + (rel or "."))
        for marker in markers:
            mi = os.stat(marker, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(mi.st_mode) or mi.st_uid != os.getuid() or mi.st_nlink != 1:
                raise ReapRefused("marked-evidence-marker-changed")
    for name in names:
        sub = f"{rel}/{name}" if rel else name
        if reap_protected(name) and protected_ok:
            pass  # removed below like any other entry (whole clone, cloneProtectedCopies=remove)
        elif reap_protected(name):
            if base and os.path.join(base, sub) in templates:
                entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISREG(entry.st_mode) and entry.st_nlink == 1:
                    expected = templates.get(os.path.join(base, sub)) if isinstance(templates, dict) else None
                    if expected and reap_vendored_file(fd, name, True) != expected:
                        raise ReapRefused(f"vendored-file-changed:{sub}")
                    os.unlink(name, dir_fd=fd)  # a tracked template, or freshly hashed manifest entry
                    tally["removed"] += 1
                    continue
            raise ReapRefused(f"protected-leaf-appeared:{sub}")
        if reap_is_git(name) and not allow_git:
            raise ReapRefused(f"nested-git-appeared:{sub}")
        entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(entry.st_mode):
            if entry.st_dev != dev:
                raise ReapRefused(f"mountpoint:{sub}")
            try:
                child = os.open(name, REAP_DIR_FLAGS, dir_fd=fd)
            except OSError:
                raise ReapRefused(f"swapped:{sub}")
            try:
                reap_expect(child, entry.st_dev, entry.st_ino, f"swapped:{sub}")
                reap_empty_directory(child, dev, depth + 1, sub, tally, allow_git, templates, base, protected_ok, expiring_evidence)
            finally:
                os.close(child)
            again = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if (again.st_dev, again.st_ino) != (entry.st_dev, entry.st_ino):
                raise ReapRefused(f"swapped:{sub}")
            os.rmdir(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)  # a symlink is unlinked, never followed
        tally["removed"] += 1


def reap_remove_tree(fence: list[tuple[str, int, int]], tally: dict, allow_git: bool = False, templates: frozenset = frozenset(), base: str = "",
                     protected_ok: bool = False, expiring_evidence: bool = False) -> None:
    parent = reap_open_fence(fence)
    try:
        name, dev, ino = fence[-1]
        try:
            fd = os.open(name, REAP_DIR_FLAGS, dir_fd=parent)
        except OSError:
            raise ReapRefused("identity-changed")
        try:
            reap_expect(fd, dev, ino, "identity-changed")
            reap_empty_directory(fd, dev, 1, "", tally, allow_git, templates, base, protected_ok, expiring_evidence)
        finally:
            os.close(fd)
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (dev, ino):
            raise ReapRefused("identity-changed")
        os.rmdir(name, dir_fd=parent)
        tally["removed"] += 1
    finally:
        os.close(parent)


def reap_remove_and_verify(fence: list, path: str, before: list[dict], result: dict, allow_git: bool, templates: frozenset = frozenset(),
                           protected_ok: bool = False, expiring_evidence: bool = False) -> None:
    """Remove the item and, however the removal ends (done, stopped, OSError or anything else), re-take the preserved
    identities. A difference is `preserved-state-changed`, the removal's own cause stays with it, and the sweep stops."""
    checks, tally, cause = result["checks"], {"removed": 0}, "removal-interrupted"
    try:
        if templates or protected_ok or expiring_evidence:
            reap_remove_tree(fence, tally, allow_git, templates, path, protected_ok, expiring_evidence)
        else:
            reap_remove_tree(fence, tally, allow_git)
        cause = None
    except ReapRefused as stop:
        cause = f"removal-stopped:{stop}"
    except Exception as error:
        cause = "removal-error:" + reap_named(error)
    finally:
        changed = reap_changed_identities(before)
        result["removed"] = tally["removed"]
        if cause is None and os.path.lexists(path):
            cause = "residue-after-delete"
        if changed:
            result["outcome"] = "preserved-state-changed"
            result["reason"] = "changed:" + ",".join(changed[:8]) + (f";cause:{cause}" if cause else "")
            result["stopped"] = result["reason"]
        else:
            checks.append("preserved-root-metadata-unchanged")
            if cause:
                result["outcome"], result["reason"] = ("partial" if tally["removed"] else "refused"), cause
                if tally["removed"]:
                    result["stopped"] = f"partial:{cause}"  # a removal that started and did not finish stops the sweep
            else:
                result["outcome"] = "deleted"
                checks.append("readback-absent")


REAP_CLEANUP_OWNER = threading.local()
REAP_CLEANUP_JOURNAL_BYTES = 4 * 1024 * 1024


@contextmanager
def reap_cleanup_owner(scope: str, acknowledge: dict | None = None, inspect: bool = False, expected: str | None = None):
    """One deleting worker per artifact home, across jobs, releases, ticks and state directories.
    Keep the inode permanently: unlinking a busy lock would admit a second worker. The private journal in the
    locked file is the handoff note; an interrupted/missing final note refuses the next sweep for investigation.
    Producer locks, ownership and per-item receipts remain mandatory and independent of this exclusion."""
    root = Path(reap_canon(str(reap_home())))
    held = getattr(REAP_CLEANUP_OWNER, "held", None)
    if held and held["pid"] == os.getpid():
        if acknowledge:
            raise ReapRefused("cleanup-owner-busy:cannot-acknowledge-an-active-sweep")
        if expected is not None:
            raise ReapRefused("cleanup-owner-busy:cannot-compact-an-active-sweep")
        if held.get("stopped") and not inspect:
            raise ReapRefused("cleanup-handoff-stopped:" + held["stopped"])
        if held["root"] != str(root):
            raise ReapRefused("cleanup-owner-unavailable:artifact-home-changed-during-sweep")
        try:
            leased = os.fstat(held["_fd"])
            current = os.stat("disk-cleanup.lock", dir_fd=held["_dirfd"], follow_symlinks=False)
            last = json.loads(os.pread(held["_fd"], min(16384, leased.st_size), max(0, leased.st_size - 16384)).splitlines()[-1])
            if not isinstance(last, dict) or (leased.st_dev, leased.st_ino) != (current.st_dev, current.st_ino) or last.get("run") != held["run"] or last.get("phase") != "started":
                raise ValueError("stale lease")
            fcntl.flock(held["_fd"], fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            held["stopped"] = held.get("stopped") or "cleanup-owner-stale"
            REAP_CLEANUP_OWNER.held = None
            raise ReapRefused("cleanup-owner-stale:read-the-current-handoff-before-retrying")
        yield {"phase": "active", "expectedRun": held["run"], "handoff": {k: v for k, v in held.items() if not k.startswith("_")}} if inspect else held
        return
    fd, entered = None, False
    try:
        dfd = os.open(root, REAP_ROOT_FLAGS)
        try:
            for name in REAP_OWNER_DIRS:
                if not inspect:
                    try:
                        os.mkdir(name, 0o700, dir_fd=dfd)
                    except FileExistsError:
                        pass
                child = os.open(name, REAP_DIR_FLAGS, dir_fd=dfd)
                os.close(dfd)
                dfd = child
                info = os.fstat(dfd)
                if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022
                        or (name == "cleanup" and stat.S_IMODE(info.st_mode) != 0o700)):
                    raise ReapRefused("cleanup-owner-unavailable:unsafe-directory")
            flags = os.O_RDONLY if inspect else os.O_RDWR | os.O_APPEND | os.O_CREAT
            fd = os.open("disk-cleanup.lock", flags | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
                raise ReapRefused("cleanup-owner-unavailable:not-private-file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                    raise ReapRefused("cleanup-owner-busy:another-worker-is-reaping; read its handoff and receipts before retrying")
                raise
            current = os.stat("disk-cleanup.lock", dir_fd=dfd, follow_symlinks=False)
            if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise ReapRefused("cleanup-owner-unavailable:lock-replaced")
            info = os.fstat(fd)  # read size AFTER exclusion, never from a pre-lock snapshot
            os.lseek(fd, max(0, info.st_size - 16384), os.SEEK_SET)
            tail = os.read(fd, 16384)
            previous, phase = None, "none"
            if tail:
                phase = "fragment" if not tail.endswith(b"\n") else "invalid"
                try:
                    previous = json.loads(tail.splitlines()[-1]) if tail.endswith(b"\n") else None
                except (ValueError, IndexError):
                    pass
                if isinstance(previous, dict) and re.fullmatch(r"[0-9a-f]{12}", str(previous.get("run", ""))):
                    phase = previous.get("phase", "invalid")
                else:
                    previous = None
            history = []
            if not tail:
                with os.scandir(dfd) as entries:
                    if any(entry.name.startswith("owner-history-") and entry.name.endswith(".jsonl") for entry in entries):
                        meta = os.fstat(dfd)
                        history = [("presence-only", meta.st_dev, meta.st_ino, meta.st_mtime_ns, meta.st_ctime_ns)]
                        phase = "compaction-incomplete"  # no archive-count ceiling can disable recovery
            token = previous["run"] if previous else "journal-sha256:" + hashlib.sha256(tail + json.dumps(sorted(history)).encode()).hexdigest()
            if expected is not None and (token != expected or phase not in ("finished", "acknowledged")):
                raise ReapRefused("cleanup-compaction-stale-or-unsettled:read-current-owner-status")
            if inspect:
                yield {"phase": phase, "expectedRun": token, "handoff": previous, "bytes": info.st_size}
                return
            if acknowledge:
                if phase in ("none", "finished", "acknowledged"):
                    raise ReapRefused("cleanup-acknowledge-not-needed")
                if acknowledge["run"] != token:
                    raise ReapRefused("cleanup-acknowledge-stale:read-current-owner-status")
            elif phase not in ("none", "finished", "acknowledged"):
                raise ReapRefused("cleanup-handoff-incomplete:previous-worker; read reap-owner-status and acknowledge reviewed notes and receipts")
            owner = {"pid": os.getpid(), "root": str(root), "run": uuid.uuid4().hex[:12], "scope": scope,
                     "receipts": str(reap_receipt_dir()), "previousRun": token if tail or history else None}
            if acknowledge:
                owner.update(evidence=acknowledge["evidence"], evidenceSha256=acknowledge["evidenceSha256"],
                             previousPhase=phase)

            def note(phase, outcome):
                public = {k: v for k, v in owner.items() if not k.startswith("_") and k != "stopped"}
                summary = outcome[:2048]
                while True:
                    line = (json.dumps({**public, "phase": phase, "outcome": summary,
                                       "outcomeTruncated": summary != outcome, "at": iso()}, sort_keys=True) + "\n").encode()
                    if len(line) <= 8192:
                        break
                    if not summary:
                        raise ReapRefused("cleanup-handoff-unavailable:note-too-large")
                    summary = summary[:len(summary) // 2]  # bound ENCODED JSON bytes, including non-ASCII escaping
                try:
                    if tail and not tail.endswith(b"\n") and acknowledge:
                        if os.write(fd, b"\n") != 1:
                            raise ReapRefused("cleanup-handoff-unavailable:short-write")
                    if os.write(fd, line) != len(line):
                        raise ReapRefused("cleanup-handoff-unavailable:short-write")
                    reap_durable(fd)
                    reap_durable(dfd)
                except OSError as error:
                    raise ReapRefused("cleanup-handoff-unavailable:" + reap_named(error)) from error

            if acknowledge:
                note("acknowledged", "notes and receipts reviewed; unresolved paths preserved; fresh deletion gates still required")
                yield owner
                return
            note("started", "in-progress")  # durable before any artifact can be touched
            owner.update(_fd=fd, _dirfd=dfd, _note=note)
            outcome = "returned; inspect per-item receipts for deleted, retained and unresolved paths"
            try:
                REAP_CLEANUP_OWNER.held = owner  # inside the finally-protected region
                entered = True
                if scope != "compact" and info.st_size > REAP_CLEANUP_JOURNAL_BYTES:
                    reap_compact_owned(owner)
                yield owner
            except BaseException:
                outcome = "interrupted; investigate receipts and revalidate every remaining path"
                raise
            finally:
                REAP_CLEANUP_OWNER.held = None
                phase = "stopped" if owner.get("stopped") else "interrupted" if outcome.startswith("interrupted;") else "finished"
                note(phase, owner.get("stopped") or outcome)  # stopped work needs explicit receipt acknowledgment
        finally:
            os.close(dfd)
    except OSError as error:
        if entered:
            raise  # sweep errors are not lock-availability errors
        raise ReapRefused("cleanup-owner-unavailable:" + reap_named(error)) from error
    finally:
        if fd is not None:
            os.close(fd)  # releases flock; never unlink the shared lock


def reap_compact_owned(owner: dict) -> dict:
    """Archive the complete journal durably, then compact in-place without ever replacing its lock inode.
    A crash with an empty journal is detected by its retained archive and requires explicit acknowledgment."""
    if getattr(REAP_CLEANUP_OWNER, "held", None) is not owner or owner.get("stopped"):
        raise ReapRefused("cleanup-compaction-needs-active-owner")
    fd, dfd = owner["_fd"], owner["_dirfd"]
    taken, current = os.fstat(fd), os.stat("disk-cleanup.lock", dir_fd=dfd, follow_symlinks=False)
    if (taken.st_dev, taken.st_ino) != (current.st_dev, current.st_ino):
        raise ReapRefused("cleanup-compaction-lock-replaced")
    try:
        space = os.fstatvfs(dfd)  # measure the actual journal/archive filesystem, even on a separate mount
        available = space.f_bavail * space.f_frsize
    except OSError:
        owner["compactionSkipped"] = "space-unverified"
        return {"compacted": False, "reason": "space-unverified"}
    required = max(1, taken.st_size) * 2
    if available < required:
        owner["compactionSkipped"] = "insufficient-free-space"
        return {"compacted": False, "reason": "insufficient-free-space", "availableBytes": available, "requiredBytes": required}
    name = "owner-history-" + uuid.uuid4().hex + ".jsonl"
    archive = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
    owner.update(compactionArchiveAttempt=str(Path(owner["root"], *REAP_OWNER_DIRS) / name),
                 compactionArchiveBytes=0, compactionArchiveComplete=False)
    digest, count = hashlib.sha256(), 0
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        while chunk := os.read(fd, 65536):
            if os.write(archive, chunk) != len(chunk):
                raise ReapRefused("cleanup-compaction-archive-short-write")
            digest.update(chunk)
            count += len(chunk)
            owner["compactionArchiveBytes"] = count
        reap_durable(archive)
        reap_durable(dfd)  # archive bytes AND name durable before any truncation
        owner["compactionArchiveComplete"] = True
    except (OSError, ReapRefused) as error:
        try:
            owner["compactionArchiveBytes"] = os.fstat(archive).st_size
        except OSError:
            owner["compactionArchiveBytes"] = None
            owner["compactionArchiveBytesUnverified"] = True
        owner["stopped"] = owner.get("stopped") or "compaction-archive-failed:" + reap_named(error)
        raise  # the interrupted/stopped note names the partial archive; never leave unattributed debris
    finally:
        os.close(archive)
    owner.update(historyArchive=str(Path(owner["root"], *REAP_OWNER_DIRS) / name), historyArchiveSha256=digest.hexdigest())
    os.ftruncate(fd, 0)  # same locked inode; archives preserve every old note
    owner["_note"]("started", "journal compacted in place; retained history archive is the receipt for the ownership chain")
    return {"compacted": True, "archive": owner["historyArchive"], "archiveSha256": digest.hexdigest(), "archivedBytes": count}


REAP_SIZE_ESCALATE = 3   # a limit-bound sizing pass doubles the next pass's time and entry budget this many times (8x), persisted per folder


def reap_size_key(path: str) -> str:
    return "reap:size:" + hashlib.sha256(path.encode("utf-8", "surrogateescape")).hexdigest()[:24]


def reap_size_plan(path: str, seconds: float) -> tuple[float, int]:
    """Time and entry budget for sizing this folder before its removal. A folder whose earlier passes ended at a limit
    (`size-time-limit`, `size-entry-limit`) gets twice the budget each time, so a huge tree is sized over successive ticks instead
    of being refused for the same reason forever."""
    try:
        with db() as conn:
            stored = row(conn, "SELECT value FROM meta WHERE key=?", (reap_size_key(path),))
        passes = int(json.loads(stored["value"]).get("passes", 0)) if stored else 0
    except (HostError, OSError, ValueError, TypeError, sqlite3.Error):
        passes = 0
    factor = 2 ** min(max(passes, 0), REAP_SIZE_ESCALATE)
    return seconds * factor, REAP_WALK_ENTRIES * factor


def reap_size_note(path: str, sizing: dict) -> None:
    """Remember a limit-bound pass (for the next pass's budget); forget the folder once it sized completely."""
    try:
        with db() as conn:
            limited = not sizing["complete"] and str(sizing.get("reason")) in ("size-time-limit", "size-entry-limit")
            if limited:
                stored = row(conn, "SELECT value FROM meta WHERE key=?", (reap_size_key(path),))
                passes = int(json.loads(stored["value"]).get("passes", 0)) if stored else 0
                conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (reap_size_key(path), json.dumps(
                    {"passes": passes + 1, "reason": sizing["reason"], "at": now(), "path": path})))
            elif sizing["complete"]:
                conn.execute("DELETE FROM meta WHERE key=?", (reap_size_key(path),))
    except (HostError, OSError, ValueError, TypeError, sqlite3.Error):
        pass


def reap_worktree_forget(common: str, gitdir: str | None, removed: str) -> str:
    """After a linked worktree's folder is gone, the main repository forgets exactly that worktree: its entry
    <common>/worktrees/<name>, and only when the entry's gitdir pointer names the folder just removed and it is not locked.
    `git worktree prune` is not used: it drops every stale entry of the shared repository, including another agent's
    worktree on an unmounted volume. Returns the name of the check recorded."""
    try:
        entry = os.path.realpath(gitdir) if gitdir else ""
        if not entry or os.path.dirname(entry) != os.path.join(os.path.realpath(common), "worktrees"):
            return "worktree-entry-kept:not-an-entry"
        if stat.S_ISLNK(os.lstat(entry).st_mode) or not os.path.isdir(entry):
            return "worktree-entry-kept:not-a-directory"
        pointer = reap_read_pointer(os.path.join(entry, "gitdir"))
        if os.path.realpath(os.path.dirname(pointer)) != os.path.realpath(removed):
            return "worktree-entry-kept:points-elsewhere"
        if os.path.lexists(os.path.join(entry, "locked")):
            return "worktree-entry-kept:locked"
        shutil.rmtree(entry)
        return "worktree-entry-forgotten"
    except FileNotFoundError:
        return "worktree-entry-forgotten"
    except (OSError, UnicodeError) as error:
        return "worktree-entry-kept:" + reap_named(error)


def reap_item_strict(item: dict, mode: str = "delete", ctx: dict | None = None) -> dict:
    """Gate, then (in "delete" mode) remove, one directory. The only function that removes a host's artifacts (the reaper's own
    housekeeping and evidence-file expiry are listed in docs/SAFETY.md).
    mode: "delete" removes it; "dry-run" runs every fresh gate but removes and writes nothing; "audit" is a dry run
    that shares one process snapshot (ctx) and takes no locks it would have to create.

    Order, all of it under the producer locks once they are taken: manifest (owner, class, retention), canonical boundary
    and fence, retention re-derived, the host's own job claims, locks, git proofs, protected-leaf identities, size, the durable
    plan receipt, claims again, the fresh process and open-file scan, the identity of the item, the last protected scan,
    claims and retention again, the removal, the identities again, the result receipt. Nothing is cached between items
    in delete mode."""
    ctx = ctx if ctx is not None else {}
    apply = mode == "delete"
    result = {"path": item.get("path"), "owner": item.get("owner"), "class": item.get("class"), "kind": item.get("kind"),
              "ownership": item.get("ownership"), "retention": item.get("retention"), "bytes": None, "bytesIncomplete": False, "sizeReason": None, "archive": None, "checks": [], "preserved": [],
              "idle": None, "outcome": "refused", "reason": None}
    checks = result["checks"]
    cleanup_owner = getattr(REAP_CLEANUP_OWNER, "held", None)
    with ExitStack() as stack:
        try:
            if apply:
                cleanup_owner = stack.enter_context(reap_cleanup_owner("item"))
                checks.append("single-cleanup-worker")
            spec = REAP_CLASSES.get(item.get("class"))
            if not item.get("owner") or not spec or not item.get("path") or not item.get("root"):
                raise ReapRefused("retention-gate:owner-and-class-required")
            declared = item.get("retention")
            if not isinstance(declared, str) or not declared.strip():
                raise ReapRefused("retention-gate:retention-missing")
            if item.get("class") == "agent-scratch" and declared == "unmarked":
                # An unmarked folder is judged by its own idle proof, taken now. A folder that aged out since the listing is
                # not taken now: the next pass lists it as disposable and removes it, so a removal receipt always says so.
                proof = reap_scratch_idle_proof(item["path"])
                item["idleProof"], result["idle"] = proof, proof
                if proof["idle"]:
                    raise ReapRefused("retention-changed")
                raise ReapRefused(reap_item_idle_reason(item))
            if declared != REAP_DISPOSABLE:
                raise ReapRefused(f"retention-preserved:{declared[:40]}")
            checks.append("retention-owner-class")
            info = os.lstat(item["path"])
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ReapRefused("not-a-real-directory")
            if os.lstat(item["root"]).st_dev != info.st_dev:
                raise ReapRefused("crosses-filesystem")
            result["kind"] = reap_boundary(item)
            fence = reap_fence(item["path"], item["root"])
            checks.append("canonical-boundary")
            targeted = item["class"] == "job-derived-output"
            if targeted:
                source = item["source"]
                source_fence = reap_fence(source, item["sourceRoot"])
                projects = reap_output_projects(source, item["path"], item["root"])
                stack.enter_context(reap_locks([reap_queue_lock(p) for p in projects], create=apply))
                # The core guard implements the shared cooperative protocol; host ownership is
                # verified under these exact locks, not by a blanket all-writers=True assertion.
                guard = globals().get("reap_output_producer_guard")
                if not callable(guard):
                    raise ReapRefused("output-producer-unverified:host-guard-unavailable")
                proof = stack.enter_context(guard(source, item["path"]))
                reap_output_producer_proof(item, proof, projects)
                if result["kind"] == "target":
                    stack.enter_context(reap_locks(reap_cargo_locks(item["path"]), create=False))
                checks.extend(["exclusive-output-producer-guard", "producer-locks"])
            retention = reap_retention(item)  # established now, from the directory itself: the declared value is only a claim
            if item.get("class") == "agent-scratch":
                result["idle"] = item.get("idleProof")  # the proof this call just took, with the budget it spent
            if retention != REAP_DISPOSABLE:
                if item.get("class") == "agent-scratch" and retention == "unmarked":
                    raise ReapRefused(reap_item_idle_reason(item))
                raise ReapRefused(f"retention-preserved:{retention}")
            checks.append("retention-disposable")
            expiring_evidence = reap_marked_evidence_item(item)
            if expiring_evidence:
                checks.append("marked-evidence-expired-report-kept")
            evidence = reap_authority(item)
            checks.extend(["owner-terminal", "no-other-claim"])
            allow_git = item["class"] in ("job-cache-subdir", "agent-scratch", "job-clone-root", "release-evidence")
            whole_clone = item["class"] == "job-clone-root"
            if whole_clone:
                # the clone's own build-queue lock and every Cargo lock in it, held through the removal
                stack.enter_context(reap_locks([reap_queue_lock(item["path"])], create=apply))
                for target in ("target", "src-tauri/target"):
                    if os.path.isdir(os.path.join(item["path"], target)) and not os.path.islink(os.path.join(item["path"], target)):
                        stack.enter_context(reap_locks(reap_cargo_locks(os.path.join(item["path"], target)), create=False))
                checks.append("producer-locks")
            elif spec["lock"] and not targeted:
                projects, current = [], Path(item["path"]).parent
                while reap_inside(str(current), item["root"]):
                    projects.append(str(current))
                    current = current.parent
                locks = [reap_queue_lock(p) for p in projects]
                if item["class"] == "release-target":
                    locks.append(reap_queue_lock(str(Path(item["root"]).parent)))
                if result["kind"] == "target":
                    locks.extend(reap_cargo_locks(item["path"]))
                stack.enter_context(reap_locks(locks, create=apply))  # held through the removal and the result receipt
                checks.append("producer-locks")
            elif expiring_evidence:
                queue_locks, cargo_locks = reap_marked_evidence_locks(item["path"])
                stack.enter_context(reap_locks(queue_locks, create=apply))
                stack.enter_context(reap_locks(cargo_locks, create=False))
                checks.append("marked-evidence-producer-locks")
            elif item["class"] == "job-cache-subdir" and reap_cachedir_tagged(item["path"]):
                # a Cargo target dir kept in a job folder: its existing build locks are held through the removal too
                stack.enter_context(reap_locks(reap_cargo_locks(item["path"]), create=False))
                checks.append("cache-tag-locks")
            repos, proved, git_dirs, archive, templates, secrets = [], set(), [], None, frozenset(), []
            protected_ok = whole_clone and load_policy().get("cloneProtectedCopies", "remove") == "remove"
            try:
                templates = reap_tracked_templates(item["path"]) if whole_clone else frozenset()
                reap_protected_scan(item["path"], allow_git=allow_git, markers=True, repos=repos if allow_git else None, nested_disposable=whole_clone,
                                    templates=templates, protected_ok=protected_ok, secrets=secrets if protected_ok else None,
                                    expiring_evidence=expiring_evidence)
                if whole_clone:  # exactly one repository, the clone itself: a nested repository keeps the whole clone
                    if set(repos) != {item["path"]}:
                        raise ReapRefused("nested-git-in-clone")
                    needs = spec["git"] + (("protected-ok",) if protected_ok else ())
                    checks.extend(reap_git_proof(item["path"], item["path"], needs, ctx.setdefault("git", {}) if mode == "audit" else None))
                    proved.add(item["path"])
                    code, dirs = reap_git(item["path"], "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
                    gitdir, _, common = dirs.strip().partition("\n")
                    result["gitCommonDir"] = common.strip() if code == 0 and common.strip() and common.strip() != os.path.join(item["path"], ".git") else None
                    result["gitWorktreeDir"] = gitdir.strip() if result["gitCommonDir"] else None
                    held = [name for name in secrets if not any(part in REAP_ARCHIVE_SKIP for part in Path(name).parts[:-1])]  # not a package's .env.example in node_modules
                    if held and reap_archive_on() and reap_archive_eligible(item):
                        # secret copies and local state go with the clone under cloneProtectedCopies=remove: archived first (owner-only)
                        raise ReapRefused(f"protected-copies:{len(held)}:{held[0][:60]}")
                elif targeted:
                    if reap_inside(item["path"], item["source"]):
                        checks.extend(reap_git_proof(item["source"], item["path"], ("ignored", "untracked")))
                    else:
                        # The host guard proves the detached source/output association; Git cannot check-ignore
                        # outside the checkout. Source Git metadata must nevertheless be safe to interpret.
                        reap_git_gate(item["source"])
                        checks.append("detached-source-git-safe")
                elif spec["git"]:
                    checks.extend(reap_git_proof(item.get("repo") or item["root"], item["path"], reap_class_git_needs(item), ctx.setdefault("git", {}) if mode == "audit" else None))
                if allow_git and not whole_clone:  # EVERY .git the scan permits is proved, at any depth and under any name, or the item stays whole
                    for repo in sorted(set(repos)):
                        checks.extend(["nested-" + c for c in reap_git_proof(repo, repo, ("clean", "pushed", "all-refs"), ctx.setdefault("git", {}) if mode == "audit" else None)])
                        proved.add(repo)
                    checks.append("every-nested-git-proved")
            except ReapRefused as refusal:
                # cloneArchive=archive: work no remote holds (or a repository git cannot read) is saved first; otherwise the refusal stands
                archive = reap_archive_settles(item, str(refusal), reap_archive_eligible(item))
                repos, git_dirs = [], []
                reap_protected_scan(item["path"], allow_git=True, markers=True, repos=repos, git_dirs=git_dirs, nested_disposable=whole_clone,
                                    templates=templates, protected_ok=protected_ok, expiring_evidence=expiring_evidence)
                proved = set(repos)
                result["archive"] = {"reason": archive["reason"], "repositories": len(proved), "gitDirs": len(set(git_dirs))}
                checks.append("archive-settles-refusal")
            before = []
            if mode != "audit":
                before = [reap_identity(leaf) for leaf in reap_preserved_leaves(item["path"], item["root"])]
                result["preserved"] = before
                checks.append("preserved-root-metadata")
            seconds, entry_limit = reap_size_plan(item["path"], ctx.get("sizeBudget", 120)) if apply else (ctx.get("sizeBudget", 120), REAP_WALK_ENTRIES)
            sizing = reap_metadata_size(item["path"], seconds, entry_limit,
                                        templates=templates | reap_vendored_templates(item["path"]).keys(), protected_ok=protected_ok)
            if apply:
                reap_size_note(item["path"], sizing)
            result["bytes"] = sizing["bytes"]
            if not sizing["complete"]:
                result["bytesIncomplete"] = True
                result["sizeReason"] = sizing["reason"]
                raise ReapRefused(sizing["reason"])
            if apply:
                reap_receipt({"phase": "plan", "run": ctx.get("run"), **{k: result[k] for k in ("path", "owner", "class", "kind", "ownership", "retention", "bytes", "archive", "checks", "preserved", "idle")}})
                checks.append("plan-receipt-durable")
            if reap_authority(item) != evidence:
                raise ReapRefused("authority-changed")
            reap_item_holders(item, ctx.get("table") if mode == "audit" else None)  # fresh, with the locks still held
            checks.append("no-process-or-handle")
            made = None
            if archive is not None:  # saved and verified under the same locks, before the last look and the removal
                if apply:
                    made = reap_archive_create(item, archive, repos, git_dirs, ctx.get("run"))
                    result["archive"].update({k: made.get(k) for k in ("path", "bytes", "files", "inputBytes", "bundles", "protectedFiles", "lfsObjects", "manifestSha256", "reused", "superseded")})
                    if made.get("gitCommonDir"):
                        result["gitCommonDir"], result["gitWorktreeDir"] = made["gitCommonDir"], made.get("gitDir")
                    checks.append("archive-verified")
                else:
                    checks.append("archive-planned")
            last = os.lstat(item["path"])
            if (last.st_dev, last.st_ino) != (fence[-1][1], fence[-1][2]) or not stat.S_ISDIR(last.st_mode):
                raise ReapRefused("identity-changed")
            again, again_dirs = [], []
            if whole_clone:
                try:
                    current = reap_tracked_templates(item["path"])
                except ReapRefused:
                    if archive is None:
                        raise
                    current = templates  # git cannot be read at all: the archive path already saved the folder as plain files
                if current != templates:
                    raise ReapRefused("tracked-templates-changed")
            reap_protected_scan(item["path"], allow_git=allow_git, markers=True, repos=again if allow_git else None, nested_disposable=whole_clone,
                                templates=templates, protected_ok=protected_ok,
                                git_dirs=again_dirs if archive is not None else None, expiring_evidence=expiring_evidence)  # last look
            if set(again) != proved or (archive is not None and sorted(set(again_dirs)) != sorted(set(git_dirs))):
                raise ReapRefused("nested-git-changed")
            if made is not None:  # the archive took time: a process, a file, an ignored folder or a ref may have moved since
                reap_item_holders(item, None)  # fresh probes again, locks still held
                checks.append("no-process-or-handle-after-archive")
                units, collected = reap_archive_state(item, again, again_dirs)
                if reap_archive_fingerprint(units, collected) != made["fingerprint"]:
                    raise ReapRefused("archive-stale:folder-changed-after-archive")
                if collected["stamp"] != made["stamp"]:
                    raise ReapRefused("archive-stale:tree-changed-after-archive")
                checks.append("archive-still-current")
            checks.append("no-protected-leaf")
            retention = reap_retention(item)
            if item.get("class") == "agent-scratch":
                result["idle"] = item.get("idleProof")  # the last proof, before anything is removed
            if retention != REAP_DISPOSABLE:
                raise ReapRefused("retention-changed")
            if reap_authority(item) != evidence:
                raise ReapRefused("authority-changed")
            checks.append("authority-final")
            if targeted and reap_fence(item["source"], item["sourceRoot"]) != source_fence:
                raise ReapRefused("output-source-identity-changed")
            if not apply:
                result["outcome"] = "would-delete"
            else:
                reap_item_holders(item, None)  # once more, after the archive and its tree compare took their time
                checks.append("no-process-or-handle-final")
                if made is not None:
                    # Stamped BEFORE the first byte is removed: a crash or power loss part-way through leaves a stamped archive, which the
                    # superseding pass never deletes (an unstamped one next to a half-removed clone could be replaced by a smaller archive
                    # of the remainder, losing the files that were already gone). Its expiry counts from the removal's start.
                    had_stamp = reap_archive_sole_since(made["path"]) is not None  # an earlier incarnation's stamp is never ours to undo
                    since = reap_archive_mark_sole(made["path"], item["path"], ctx.get("run"))
                    result["archive"]["soleCopySince"] = since
                    checks.append("archive-sole-copy-recorded" if since else "archive-sole-copy-unrecorded")
                    if not since:
                        raise ReapRefused("archive-sole-copy-unrecorded")  # no stamp, no removal: the archive must be protected first
                try:
                    removal_templates = dict.fromkeys(templates)
                    removal_templates.update(reap_vendored_templates(item["path"]))
                    reap_remove_and_verify(fence, item["path"], before, result, allow_git, removal_templates, protected_ok, expiring_evidence)
                finally:
                    if made is not None and since and not had_stamp and not result.get("removed"):
                        # Nothing was removed (the removal refused before its first byte): the clone is still the primary copy and the archive's
                        # expiry clock must not have started. A removal that began keeps its stamp, whatever happened after.
                        try:
                            os.unlink(os.path.join(made["path"], "sole-copy-since.json"))
                        except OSError:
                            pass
                        result["archive"].pop("soleCopySince", None)
                if whole_clone and result["outcome"] == "deleted" and result.get("gitCommonDir"):
                    # the main repository forgets this removed linked worktree and no other entry (its branch, tags and stash stay)
                    checks.append(reap_worktree_forget(result["gitCommonDir"], result.get("gitWorktreeDir"), item["path"]))
        except ReapRefused as refusal:
            result["reason"] = str(refusal)
        except (OSError, ValueError, sqlite3.Error) as error:
            if "removed" in result:  # the removal ran and was verified: keep that outcome, add the cause
                result["reason"] = f"{result['reason']};error:{reap_named(error)}"
            else:
                result["outcome"], result["reason"] = "error", f"{error.__class__.__name__}:{getattr(error, 'errno', '')}"
        finally:
            if apply:  # still inside the lock block: the result is receipted before any lock is released
                try:
                    reap_receipt({"phase": "result", "run": ctx.get("run"), "outcome": result["outcome"], "reason": result["reason"],
                                  "removed": result.get("removed"), "bytesIncomplete": result["bytesIncomplete"], "sizeReason": result["sizeReason"], **{k: result[k] for k in ("path", "owner", "class", "kind", "ownership", "retention", "bytes", "archive", "checks", "preserved", "idle")}})
                except ReapRefused:
                    result["receiptWritten"] = False
                    result["stopped"] = result.get("stopped") or "result-receipt-unavailable"
                if cleanup_owner and cleanup_owner.get("stopped"):
                    result["stopped"] = result.get("stopped") or cleanup_owner["stopped"]
                if result.get("stopped"):
                    held = cleanup_owner or getattr(REAP_CLEANUP_OWNER, "held", None)
                    if held:
                        held["stopped"] = held.get("stopped") or result["stopped"]
    return result


# ---- job level: when a job's artifacts become reapable --------------------------------------------------------------

def reap_live_snapshot() -> tuple:
    """Live terminal ids and how long each has been idle, or (None, None) when the host cannot be asked (then no
    terminal-bound job is reapable). Both come from one snapshot so a job's terminal proof cannot mix two moments."""
    try:
        live = live_all()
    except (HostError, OSError):
        return None, None
    idle = {}
    for terminal in live:
        try:
            idle[terminal["id"]] = max(0.0, float(terminal.get("idleSeconds") or 0))
        except (TypeError, ValueError):
            idle.pop(terminal["id"], None)  # unknown idleness never counts as idle
    return {s["id"] for s in live}, idle


def reap_live_ids() -> set | None:
    """Ids of live terminals, or None when the terminal host cannot be asked (then no terminal-bound job is reapable)."""
    return reap_live_snapshot()[0]


def reap_idle_map(live: list) -> dict:
    """Seconds each live terminal has been idle, from a snapshot the caller already holds."""
    idle = {}
    for terminal in live:
        try:
            idle[terminal["id"]] = max(0.0, float(terminal.get("idleSeconds") or 0))
        except (TypeError, ValueError):
            continue
    return idle


def reap_open_terminal_grace_hours(policy: dict | None = None) -> float:
    """`reapOpenTerminalGraceHours`: how long a finished job's still-open worker terminal may stay idle before it stops
    holding that job's folders (0 keeps every folder of a job whose terminal is open)."""
    value = (policy or load_policy()).get("reapOpenTerminalGraceHours", 0)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0.0


def reap_open_terminal_past_grace(job: dict, idle: dict | None, policy: dict | None = None) -> bool:
    """The documented rule: once the job finished `reapOpenTerminalGraceHours` ago AND its worker terminal has been idle
    as long, that open terminal no longer holds the job's folders. The terminal itself is never closed or touched, and
    every other proof (a live process using the folder, a retention marker, git state, the item's own settled mtime)
    still runs. Unknown idleness or a resumed terminal keep the old wait."""
    terminal = job.get("terminal_id")
    hours = reap_open_terminal_grace_hours(policy)
    finished = job.get("finished_at")
    if not terminal or not hours or not idle or not isinstance(finished, (int, float)) or isinstance(finished, bool):
        return False
    grace = hours * 3600
    idle_for = idle.get(terminal)
    return (isinstance(idle_for, (int, float)) and not isinstance(idle_for, bool) and idle_for >= grace
            and 0 < finished <= now() - grace)


def reap_terminal_cleanup_state(conn, terminal: str) -> str:
    """Where a live worker terminal of a finished job stands on the supported cleanup path (the cleanup-status words): the record's
    status; `awaiting-verification` when every job on it is done and no verified record exists yet; otherwise it needs a
    cleanup-record from an operator or verifier before the guarded cleanup may close it."""
    record = row(conn, "SELECT status FROM cleanup_records WHERE terminal_id=?", (terminal,))
    if record:
        return record["status"]
    jobs = rows(conn, "SELECT status FROM jobs WHERE terminal_id=?", (terminal,))
    return "awaiting-verification" if jobs and all(j["status"] == "done" for j in jobs) else "needs-cleanup-record"


REAP_TERMINAL_OPEN_GRACE = 6 * 3600     # a finished job's live worker terminal this long after it finished is reported (and handed off), not just waited for
REAP_TERMINAL_OPEN_REPEAT = 86400       # ... and reported again this often while nothing about it changes


def reap_terminal_handoff(job: dict, state: str) -> dict:
    """What closes a finished job's still-open worker terminal, through the supported cleanup path only (the reaper never closes a
    terminal and never removes anything under a live terminal's cwd)."""
    terminal = job.get("terminal_id")
    if state == "verified":
        step = "verified: the guarded cleanup closes it after closeGraceSeconds, then this job's artifacts are reaped on the next tick"
    elif state == "awaiting-verification":
        step = "a verifier job checks it; or review the report and run `" + REAP_CLI + " cleanup-record` for this terminal"
    elif state in ("closing", "awaiting-final-reply", "unknown"):
        step = f"cleanup is {state}: see `{REAP_CLI} cleanup-status`; the reaper waits for it"
    else:
        step = f"review the worker, then `{REAP_CLI} cleanup-record` for this terminal (a failed/cancelled job has no verifier); the guarded cleanup then closes it"
    return {"job": job["id"], "status": job["status"], "terminal": terminal, "cleanup": state, "next": step}


def reap_terminal_handoff_tick(conn, live_ids: set, errors: list) -> int:
    """Finished jobs whose worker terminal is still live REAP_TERMINAL_OPEN_GRACE after they finished get one `job.reap_waiting_on_terminal`
    event (state and next step; repeated daily or when the cleanup state changes), so the wait is visible instead of silent."""
    sent = 0
    for job in rows(conn, "SELECT * FROM jobs WHERE status IN ('done','failed','cancelled') AND terminal_id IS NOT NULL ORDER BY finished_at"):
        terminal = job["terminal_id"]
        if terminal not in live_ids or not job["finished_at"] or now() - job["finished_at"] < REAP_TERMINAL_OPEN_GRACE:
            continue
        state = reap_terminal_cleanup_state(conn, terminal)
        key = "reap:open:" + job["id"]
        stored = row(conn, "SELECT value FROM meta WHERE key=?", (key,))
        try:
            last = json.loads(stored["value"]) if stored else {}
        except ValueError:
            last = {}
        if last.get("state") == state and now() - last.get("at", 0) < REAP_TERMINAL_OPEN_REPEAT:
            continue
        if sent >= 10:
            break
        log_event(conn, "job.reap_waiting_on_terminal", job["id"], "tick", reap_terminal_handoff(job, state))
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, json.dumps({"state": state, "at": now()})))
        sent += 1
    return sent


def reap_class_git_needs(item: dict) -> tuple:
    """The git proofs one item needs, from its class and its kind alone: never from the caller's word."""
    spec = REAP_CLASSES.get(item.get("class")) or {}
    if item.get("kind") in REAP_DERIVED_KINDS and os.path.basename(item.get("path") or "") == item.get("kind") \
            and spec.get("derivedGit"):
        return tuple(spec["derivedGit"])
    return tuple(spec.get("git") or ())


def reap_job_blocker(conn, job: dict, live_ids: set | None, idle: dict | None = None) -> str | None:
    """Why a job's artifacts may not be touched yet; None when the job is finished, settled and its worker is gone."""
    # An interrupted job whose work a durable continuation took over is finished work: the same windows apply, from the
    # interruption clock. Every other check below still runs for it.
    clock = reap_job_finished_clock(conn, job)
    continued = job["status"] not in REAP_TERMINAL and clock is not None
    if job["status"] not in REAP_TERMINAL and clock is None:
        return f"job-{job['status']}"
    if not clock:
        return "settling"
    terminal = job.get("terminal_id")
    closed = bool(terminal) and bool(row(conn, "SELECT 1 FROM cleanup_records WHERE terminal_id=? AND status='closed'", (terminal,)))
    # A continued interrupted job ages from its chain's last finish, which is not this terminal's close: it waits the
    # ordinary settle window, never the shorter one a closed terminal earns.
    settle = REAP_SETTLE_SECONDS if continued else (REAP_CLOSED_SETTLE_SECONDS if closed else REAP_SETTLE_SECONDS)
    if now() - clock < settle:
        return "settling"
    if terminal:
        if live_ids is None:
            return "terminal-state-unknown"
        if terminal in live_ids and not reap_open_terminal_past_grace(job, idle):
            return "worker-terminal-still-open:" + reap_terminal_cleanup_state(conn, terminal)
        record = row(conn, "SELECT status FROM cleanup_records WHERE terminal_id=?", (terminal,))
        if record and record["status"] in ("verified", "closing", "awaiting-final-reply", "unknown"):
            return f"cleanup-{record['status']}"
        for sibling in rows(conn, "SELECT id,status FROM jobs WHERE terminal_id=? AND id!=?", (terminal, job["id"])):
            if sibling["status"] not in REAP_TERMINAL:
                return f"terminal-shared-with:{sibling['id']}({sibling['status']})"
    for other in rows(conn, "SELECT id,status,meta FROM jobs"):
        if other["status"] not in REAP_TERMINAL:
            wait = job_wait(other)
            if wait and wait["job"] == job["id"]:
                return f"awaited-by:{other['id']}({other['status']})"
    return None


def reap_state(conn, job_id: str) -> dict | None:
    stored = row(conn, "SELECT value FROM meta WHERE key=?", ("reaper:" + job_id,))
    try:
        return json.loads(stored["value"]) if stored else None
    except ValueError:
        return None


def reap_state_wait(state: dict | None) -> bool:
    """True while the tick leaves a job alone: a pass that left nothing to do (until a clone still waiting for its expiry is due to
    be looked at again: `complete` alone never suppresses that), or the backoff after a pass that preserved something."""
    if not state:
        return False
    if state.get("complete"):
        if state.get("lingering") is False:
            return True
        return now() < state.get("revisitAt", state.get("at", 0) + REAP_REVISIT_SECONDS)  # a state older than `lingering` is looked at again once
    return now() - state.get("at", 0) < REAP_RETRY_SECONDS[min(max(state.get("attempts", 1), 1), len(REAP_RETRY_SECONDS)) - 1]


def reap_revisit(job: dict, clones: list) -> dict:
    """What a finished-clean state says about looking again: `lingering: False` when none of the job's clones is left, else the
    time to look: when cloneExpiryDays after the job finished make the clone removable, at the latest REAP_REVISIT_SECONDS."""
    if not any(os.path.lexists(root) for root, ids in clones if job["id"] in ids):
        return {"lingering": False}
    days, revisit = load_policy().get("cloneExpiryDays", 3), now() + REAP_REVISIT_SECONDS
    if isinstance(days, int) and not isinstance(days, bool) and days > 0 and job.get("finished_at"):
        revisit = min(revisit, max(job["finished_at"] + days * 86400 + 60, now() + 60))
    return {"lingering": True, "revisitAt": revisit}


def reap_state_put(conn, job_id: str, state: dict) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", ("reaper:" + job_id, json.dumps(state, sort_keys=True)))


def reap_summarize(results: list[dict]) -> dict:
    deleted = [r for r in results if r["outcome"] == "deleted"]
    kept = [r for r in results if r["outcome"] not in ("deleted", "would-delete")]
    archived = [r for r in deleted if r.get("archive") and r["archive"].get("path")]
    return {"deleted": len(deleted), "reclaimedBytes": sum(r["bytes"] or 0 for r in deleted), "preserved": len(kept),
            "archived": len(archived), "archiveBytes": sum(r["archive"].get("bytes") or 0 for r in archived),
            "reasons": sorted({r["reason"].split(":")[0] if r["reason"] else r["outcome"] for r in kept})}


def reap_job_lock(job_id: str) -> int:
    """The per-job single-runner lock file, opened below the receipt directory without following a symlink anywhere in it
    (the same private directory the receipts use). Raises ReapRefused `receipt-unavailable:<cause>`."""
    try:
        directory = reap_receipt_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        dfd = os.open(directory, REAP_DIR_FLAGS)
    except OSError as error:
        raise ReapRefused("receipt-unavailable:" + reap_named(error))
    try:
        if os.fstat(dfd).st_uid != os.getuid():
            raise ReapRefused("receipt-unavailable:foreign-directory")
        try:
            os.mkdir("locks", 0o700, dir_fd=dfd)
        except FileExistsError:
            pass
        lfd = os.open("locks", REAP_DIR_FLAGS, dir_fd=dfd)
        try:
            return os.open(f"{job_id}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=lfd)
        finally:
            os.close(lfd)
    except OSError as error:
        raise ReapRefused("receipt-unavailable:" + reap_named(error))
    finally:
        os.close(dfd)


def reap_sweep(items: list[dict], mode: str, ctx: dict) -> tuple[list[dict], str | None]:
    """Items one after another, each with its own locks and fresh checks. Preserved protected state that differs afterwards,
    a removal that started and did not finish cleanly, or a result receipt that could not be written stops the sweep at
    once: nothing after it is attempted and the cause is reported."""
    results = []
    cleanup_owner = getattr(REAP_CLEANUP_OWNER, "held", None)
    for item in items:
        result = reap_item(item, mode, ctx)
        results.append(result)
        if cleanup_owner and cleanup_owner.get("stopped"):
            result["stopped"] = result.get("stopped") or cleanup_owner["stopped"]
        if result.get("stopped"):
            held = getattr(REAP_CLEANUP_OWNER, "held", None)
            if held:
                held["stopped"] = result["stopped"]
            return results, f"{result['stopped']} ({os.path.basename(str(result.get('path')))})"
    return results, None


def reap_job(conn, job: dict, dry_run: bool, actor: str, clones: list | None = None, pressure: bool = False) -> dict:
    if dry_run:
        return reap_job_owned(conn, job, dry_run, actor, clones, pressure)
    with reap_cleanup_owner("job:" + job["id"]):
        return reap_job_owned(conn, job, dry_run, actor, clones, pressure)


def reap_job_owned(conn, job: dict, dry_run: bool, actor: str, clones: list | None = None, pressure: bool = False,
                   outputs_only: bool = False) -> dict:
    """Reap one finished job's artifacts through reap_item and record the outcome in the job's event log. `pressure` reaps only the
    derived build output of an idle unfinished job and records no reap state: its real reap still runs once it finishes."""
    clones = clones if clones is not None else reap_clone_roots()
    items, notes = reap_job_items(job, clones)
    if outputs_only:
        items = [targeted for i in items if (targeted := reap_output_item(job["id"], i["path"])) is not None]
    items = reap_output_order(items)
    if pressure:
        items = [i for i in items if i.get("class") == "job-clone-regenerable" and i.get("kind") in REAP_DERIVED_KINDS]
    out = {"job": job["id"], "status": job["status"], "dryRun": dry_run, "items": [], "notes": notes, "attempted": bool(items)}
    if not items:
        return {**out, "deleted": 0, "reclaimedBytes": 0, "preserved": 0, "dfDeltaBytes": 0, "reasons": []}
    fd = None
    if not dry_run:  # a dry run writes nothing, not even a lock file
        try:
            fd = reap_job_lock(job["id"])
        except ReapRefused as refusal:
            raise HostError(f"reap of {job['id']} refused before anything was touched: {refusal}", "the reaper's private state directory must be a real directory of this user")
    try:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return {**out, "deleted": 0, "reclaimedBytes": 0, "preserved": len(items), "dfDeltaBytes": 0, "reasons": ["reap-already-running"]}
        ctx = {"run": uuid.uuid4().hex[:12]}
        before = reap_free_bytes()
        REAP_PRESSURE.on = pressure and disk_level(load_policy(), disk_state()) != "ok"
        try:
            results, stopped = reap_sweep(items, "dry-run" if dry_run else "delete", ctx)
        finally:
            REAP_PRESSURE.on = False
        after = reap_free_bytes()
    finally:
        if fd is not None:
            os.close(fd)
    summary = reap_summarize(results)
    delta = (after - before) if before is not None and after is not None else None
    out.update(summary, items=results, dfDeltaBytes=delta, dfFreeBefore=before, dfFreeAfter=after, run=ctx["run"])
    if stopped:
        out["stopped"] = stopped
    if (pressure or outputs_only) and not dry_run:
        if summary["deleted"]:
            log_event(conn, "job.reaped_under_pressure", job["id"], actor, {"reclaimedBytes": summary["reclaimedBytes"], "dfDeltaBytes": delta,
                                                                          "deleted": summary["deleted"], "preserved": summary["preserved"], "run": ctx["run"]})
        return out
    if not dry_run:
        signature = sorted((r["path"], r["reason"]) for r in results if r["outcome"] not in ("deleted",))
        previous = reap_state(conn, job["id"]) or {}
        attempts = (previous.get("attempts") or 0) + 1
        complete = all(r["outcome"] == "deleted" for r in results) and len(results) == len(items) and not notes and not stopped
        reap_state_put(conn, job["id"], {"at": now(), "attempts": attempts, "complete": complete, "signature": hashlib.sha256(json.dumps(signature).encode()).hexdigest()[:16],
                                         "reclaimedBytes": (previous.get("reclaimedBytes") or 0) + summary["reclaimedBytes"],
                                         **(reap_revisit(job, clones) if complete else {})})
        brief = [{"path": r["path"], "class": r["class"], "bytes": r["bytes"], "outcome": r["outcome"], "reason": r["reason"],
                  **({"archive": r["archive"].get("path")} if r.get("archive") and r["archive"].get("path") else {})} for r in results][:40]
        kept = [r for r in results if r["outcome"] != "deleted"]
        if kept or notes:
            reap_note(conn, job["id"], {"state": "blocked", "at": iso(), "preserved": len(kept), "reasons": summary["reasons"],
                                         "words": f"{len(kept)} folder(s) kept: " + ", ".join(summary["reasons"]) + (f"; {'; '.join(notes)}" if notes else ""),
                                         "signature": hashlib.sha256(json.dumps(sorted((r["path"], r["reason"]) for r in kept)).encode()).hexdigest()[:16]})
        else:
            reap_note(conn, job["id"], {"state": "reaped", "at": iso(), "reclaimedBytes": summary["reclaimedBytes"], "signature": "reaped"})
        if stopped:
            log_event(conn, "job.reap_stopped", job["id"], actor, {"reason": stopped, "run": ctx["run"], "attempted": len(results), "items": brief})
        if summary["deleted"]:
            log_event(conn, "job.reaped", job["id"], actor, {"reclaimedBytes": summary["reclaimedBytes"], "dfDeltaBytes": delta, "deleted": summary["deleted"],
                                                             "preserved": summary["preserved"], "run": ctx["run"], "items": brief})
        elif previous.get("signature") != reap_state(conn, job["id"])["signature"]:
            log_event(conn, "job.reap_blocked", job["id"], actor, {"preserved": summary["preserved"], "reasons": summary["reasons"], "run": ctx["run"], "items": brief})
    return out


def reap_one(conn, job: dict, live_ids: set | None, clones: list, errors: list, live_idle: dict | None = None) -> dict | None:
    """One finished job through the guarded reaper if it is due; the outcome, or None when it is not due or nothing ran."""
    state = reap_state(conn, job["id"])
    if reap_state_wait(state):
        return None
    outputs_only = bool(reap_job_blocker(conn, job, live_ids, live_idle))
    if outputs_only and (reap_output_terminal_blocker(conn, job, live_ids, live_idle) or not any(
            reap_output_registry_present(i["path"]) for i in reap_job_items(job, clones)[0])):
        return None
    try:
        if outputs_only:
            outcome = reap_job_owned(conn, job, False, "tick", clones, outputs_only=True)
        else:
            outcome = reap_job(conn, job, False, "tick", clones)
    except (HostError, OSError, ValueError, sqlite3.Error, ReapRefused) as error:
        errors.append({"reap": job["id"], "error": str(error)})
        held = getattr(REAP_CLEANUP_OWNER, "held", None)
        if held:
            held["stopped"] = held.get("stopped") or "job-error:" + reap_named(error)
        reap_state_put(conn, job["id"], {**(state or {}), "at": now(), "attempts": ((state or {}).get("attempts") or 0) + 1,
                                        "complete": False, "lastError": str(error)})
        return None
    if outcome.get("stopped"):
        errors.append({"reap": job["id"], "error": "sweep stopped: " + str(outcome["stopped"])})
    if not outcome["attempted"]:
        if not outcome["notes"]:  # nothing found and nothing unreadable: there is nothing left to look for
            reap_state_put(conn, job["id"], {"at": now(), "attempts": 0, "complete": True, "reclaimedBytes": 0, **reap_revisit(job, clones)})
        else:  # notes-only discovery failures must back off, not grow an owner journal every tick
            reap_state_put(conn, job["id"], {**(state or {}), "at": now(), "attempts": ((state or {}).get("attempts") or 0) + 1,
                                            "complete": False, "lastError": "; ".join(outcome["notes"])[:400]})
        return None
    return outcome


def reap_closed_job(conn, policy: dict, job_id: str | None, actions: list, errors: list) -> None:
    """The job-close hook: the moment the guarded cleanup has closed a finished job's worker, that job (only) goes through the
    same guarded reaper the tick uses. A blocked check is recorded on the job row by reap_job; nothing is forced."""
    if not job_id or not policy.get("autoReap", False):
        return
    job = row(conn, "SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job or job["status"] not in REAP_TERMINAL:
        return
    try:
        reap_home()
    except HostError:
        return  # state moved without a reaper home (tests, a second instance): reaping is off, silently
    try:
        live_ids, live_idle = reap_live_snapshot()
        outcome = reap_one(conn, job, live_ids, reap_clone_roots(), errors, live_idle)
    except (HostError, OSError, sqlite3.Error) as error:
        errors.append({"reap": job_id, "error": str(error)})
        return
    if outcome and outcome["deleted"]:
        actions.append({"did": "reaped", "job": job_id, "items": outcome["deleted"], "bytes": outcome["reclaimedBytes"], "dfDeltaBytes": outcome["dfDeltaBytes"], "at": "close"})


# ---- evidence expiry: bulky files of long-finished jobs -----------------------------------------------------------------
# A finished job's screenshots, recordings, traces and logs in <cache root>/J-NNNN expire `evidenceExpiryDays`
# after the job finished. Reports, handoffs and JSON stay (only the suffixes below are candidates). A folder holding any
# retention marker (`.ut-retention-*`, the disposable one included: the reaper owns those), a `.git` entry, a Git
# directory layout, a protected name or the name `protected-archive` is skipped whole, at any depth; a marker in the job
# folder itself keeps the whole job. Symlinks are never followed, devices never crossed, hard-linked files are kept.
# Each file's name, bytes and sha256 go durably to <job>/evidence-expired.jsonl before it is unlinked.
EVIDENCE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic", ".tif", ".tiff", ".bmp",
                     ".mp4", ".mov", ".webm", ".m4v", ".har", ".trace", ".log")
EVIDENCE_KEEP_DIRS = ("protected-archive",)
# Build outputs and environments are not evidence: never walked (the reaper owns marked ones).
EVIDENCE_SKIP_DIRS = REAP_REGENERABLE + (".venv", "venv", "site-packages")
EVIDENCE_SKIP_MARKERS = ("cachedir.tag", "pyvenv.cfg")   # a folder tagged as a tool cache or a Python environment
EVIDENCE_MANIFEST = "evidence-expired.jsonl"
EVIDENCE_SCAN_ENTRIES = 100000      # entries one job folder's walk may list before it reports itself incomplete
EVIDENCE_MAX_DEPTH = 16
EVIDENCE_BATCH = 200                # files planned (manifest flushed once) before their unlinks
EVIDENCE_JOB_SECONDS = 60           # wall-clock budget of one job's pass; the rest waits for the next pass
EVIDENCE_TICK_JOBS = 3              # jobs one tick may expire files for
EVIDENCE_TICK_SCANS = 12            # job folders one tick may walk
EVIDENCE_RECHECK_SECONDS = 7 * 86400
EVIDENCE_RETRY_SECONDS = 3600


def evidence_expiry_days(policy: dict) -> int:
    value = policy.get("evidenceExpiryDays", 14)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def evidence_name(name: str) -> bool:
    return reap_fold(name).endswith(EVIDENCE_SUFFIXES) and not reap_fold(name).startswith(REAP_MARKER_PREFIX)


def evidence_walk(root: str, cutoff: float, deadline: float, marked: bool = False,
                  marked_cutoff: float | None = None) -> tuple[list[dict], list[str]]:
    """Expirable files below one job folder, from names and lstat only (no file is opened). Returns (files, kept) where
    kept names each folder skipped whole and why. Raises ReapRefused on a changed identity, an exhausted budget or an
    unreadable folder: an incomplete walk expires nothing."""
    top = os.lstat(root)
    if not stat.S_ISDIR(top.st_mode):
        raise ReapRefused("evidence-root-not-a-directory")
    files, kept, listed, uid = [], [], 0, os.getuid()
    def walk(fd, rel, level, active_cutoff):
        nonlocal listed
        with os.scandir(fd) as entries:
            names = sorted(entry.name for entry in entries)
        listed += len(names)
        if listed > EVIDENCE_SCAN_ENTRIES or time.monotonic() > deadline:
            raise ReapRefused("evidence-scan-incomplete")
        folded = {reap_fold(n) for n in names}
        if any(f.startswith(REAP_MARKER_PREFIX) for f in folded):
            # Only the separately verified, report-backed five-day rule can pass an evidence marker.
            current = os.path.join(root, rel)
            if not marked or reap_cache_retention(current) != "evidence":
                kept.append(f"{rel or '.'}:retention-marker")
                return
            active_cutoff = marked_cutoff if marked_cutoff is not None else cutoff
        if any(reap_is_git(n) or reap_protected(n) for n in names) or ("objects" in folded and folded & {"head", "refs", "packed-refs", "reftable"}):
            kept.append(f"{rel or '.'}:git-or-protected")
            return
        if folded & set(EVIDENCE_SKIP_MARKERS):
            kept.append(f"{rel or '.'}:tool-cache")
            return
        for name in names:
            sub = f"{rel}/{name}" if rel else name
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if info.st_dev != top.st_dev:
                    kept.append(f"{sub}:mountpoint")
                elif reap_fold(name) in EVIDENCE_KEEP_DIRS or marked_evidence_keep(name):
                    kept.append(f"{sub}:kept-folder")
                elif reap_fold(name) in EVIDENCE_SKIP_DIRS:
                    kept.append(f"{sub}:build-output")
                elif level + 1 > EVIDENCE_MAX_DEPTH:
                    kept.append(f"{sub}:too-deep")
                else:
                    child = os.open(name, REAP_DIR_FLAGS, dir_fd=fd)
                    try:
                        opened = os.fstat(child)
                        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                            raise ReapRefused("walk-identity-changed")
                        walk(child, sub, level + 1, active_cutoff)
                    finally:
                        os.close(child)
            elif (stat.S_ISREG(info.st_mode) and evidence_name(name) and not marked_evidence_keep(name) and info.st_uid == uid and info.st_nlink == 1
                  and info.st_mtime <= active_cutoff):
                files.append({"rel": sub, "dev": info.st_dev, "ino": info.st_ino, "bytes": info.st_size, "mtimeNs": info.st_mtime_ns})
    fd = os.open(root, REAP_ROOT_FLAGS)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (top.st_dev, top.st_ino):
            raise ReapRefused("walk-identity-changed")
        walk(fd, "", 0, cutoff)
    except OSError as error:
        raise ReapRefused("evidence-scan-unreadable:" + reap_named(error))
    finally:
        os.close(fd)
    return files, kept


def evidence_same(info, entry: dict) -> bool:
    return (stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
            and (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) == (entry["dev"], entry["ino"], entry["bytes"], entry["mtimeNs"]))


def evidence_parent(root_fd: int, rel: str, marked: bool | None = None) -> int:
    """Open each ancestor with O_NOFOLLOW and recheck retention/protected names when expiring files."""
    fd = os.dup(root_fd)
    try:
        for part in (*Path(rel).parts[:-1], None):
            if marked is not None:
                names = os.listdir(fd)
                markers = [n for n in names if reap_fold(n).startswith(REAP_MARKER_PREFIX)]
                if markers:
                    if not marked or markers != [REAP_MARKER_PREFIX + "evidence"]:
                        raise ReapRefused("evidence-retention-changed")
                    mi = os.stat(markers[0], dir_fd=fd, follow_symlinks=False)
                    if not stat.S_ISREG(mi.st_mode) or mi.st_uid != os.getuid() or mi.st_nlink != 1:
                        raise ReapRefused("evidence-marker-changed")
                if any(reap_protected(n) or reap_is_git(n) for n in names):
                    raise ReapRefused("evidence-protected-name-appeared")
            if part is not None:
                child = os.open(part, REAP_DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def evidence_stop(job_id: str, out: dict) -> None:
    held = getattr(REAP_CLEANUP_OWNER, "held", None)
    if held:
        held["stopped"] = held.get("stopped") or f"evidence-interrupted:{job_id}:after-{out['expired']}-files"


def evidence_expire_job(conn, job: dict, days: int, dry_run: bool, actor: str) -> dict:
    """A real run holds the single cleanup owner (reap_cleanup_owner), like every other deleting sweep; a dry run takes no lock."""
    if dry_run:
        return evidence_expire_job_owned(conn, job, days, dry_run, actor)
    with reap_cleanup_owner("evidence:" + job["id"]):
        return evidence_expire_job_owned(conn, job, days, dry_run, actor)


def marked_evidence_expire_job(conn, job: dict, dry_run: bool) -> dict:
    """Existing guarded folder path, limited to expired marked job-cache children. Keeps the job root and records."""
    items = [i for i in reap_job_items(job, [])[0] if reap_marked_evidence_item(i)]
    out = {"days": marked_evidence_expiry_days(load_policy()), "items": [], "dfDeltaBytes": 0}
    if not items:
        return {**out, **reap_summarize([])}
    with ExitStack() as stack:
        if not dry_run:
            stack.enter_context(reap_cleanup_owner("marked-evidence:" + job["id"]))
            fd = reap_job_lock(job["id"])
            stack.callback(os.close, fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ReapRefused("reap-already-running")
        before = reap_free_bytes()
        results, stopped = reap_sweep(items, "dry-run" if dry_run else "delete", {"run": uuid.uuid4().hex[:12]})
        after = reap_free_bytes()
    return {**out, **reap_summarize(results), "items": results, "stopped": stopped,
            "dfDeltaBytes": after - before if after is not None and before is not None else None}


def evidence_expire_job_owned(conn, job: dict, days: int, dry_run: bool, actor: str) -> dict:
    """Expire one finished job's bulky evidence. The job must be finished `days` ago and pass reap_job_blocker (the
    caller checks) and reap_authority (checked here and again before every batch); no process may hold a cwd,
    handle or command line inside the job folder. Dry-run lists and writes nothing."""
    cutoff = now() - days * 86400
    out = {"job": job["id"], "dryRun": dry_run, "days": days, "files": 0, "bytes": 0, "expired": 0, "expiredBytes": 0,
           "kept": [], "changed": [], "reason": None}
    if job.get("status") not in REAP_TERMINAL:
        clock = reap_job_finished_clock(conn, job)
        if clock is None:
            return {**out, "reason": "job-not-terminal"}
    else:
        clock = job.get("finished_at")
    if not clock or (clock > cutoff and not marked_evidence_job(job["id"])):
        return {**out, "reason": f"finished-less-than-{days}-days-ago"}
    cache_root = reap_canon(reap_cache_root())
    reap_vet_root(cache_root)
    root = reap_canon(Path(cache_root) / job["id"])
    if os.path.dirname(root) != cache_root or os.path.basename(root) != job["id"]:
        raise ReapRefused("outside-allowed-roots")
    try:
        info = os.lstat(root)
    except FileNotFoundError:
        return {**out, "reason": "no-job-folder"}
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        return {**out, "reason": "job-folder-not-a-real-directory"}
    item = {"class": "job-cache-subdir", "owner": job["id"], "jobs": [job["id"]], "root": root, "path": root}
    authority = reap_authority(item)
    out["markedEvidence"] = marked_evidence_expire_job(conn, job, dry_run)
    if out["markedEvidence"].get("stopped"):
        return {**out, "stopped": out["markedEvidence"]["stopped"]}
    deadline = time.monotonic() + EVIDENCE_JOB_SECONDS
    try:
        marked = marked_evidence_job(job["id"])
        files, kept = evidence_walk(root, cutoff if days else float("-inf"), deadline, marked=marked,
                                   marked_cutoff=now() - marked_evidence_expiry_days(load_policy()) * 86400) if days or marked else ([], [])
    except ReapRefused as refusal:
        if str(refusal) != "evidence-scan-incomplete":
            raise
        return {**out, "reason": "scan-incomplete"}  # too large or too slow to list: nothing expires, the tick retries daily
    out.update(files=len(files), bytes=sum(f["bytes"] for f in files), kept=kept[:20])
    if dry_run or not files:
        return out
    lock = reap_job_lock(job["id"])  # the same per-job lock as reap_job: never beside a reap of this job
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {**out, "reason": "reap-already-running"}
        run = uuid.uuid4().hex[:12]
        out["run"] = run
        reap_holders(root, root)
        reap_receipt({"phase": "evidence-plan", "run": run, "owner": job["id"], "path": root, "files": len(files), "bytes": out["bytes"], "days": days})
        try:
            root_fd = os.open(root, REAP_ROOT_FLAGS)
        except BaseException:
            evidence_stop(job["id"], out)
            raise
        try:
            if (os.fstat(root_fd).st_dev, os.fstat(root_fd).st_ino) != (info.st_dev, info.st_ino):
                raise ReapRefused("identity-changed")
            manifest = os.open(EVIDENCE_MANIFEST, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=root_fd)
            try:
                mi = os.fstat(manifest)
                if not stat.S_ISREG(mi.st_mode) or mi.st_uid != os.getuid() or mi.st_nlink != 1:
                    raise ReapRefused("manifest-not-a-private-file")
                for start in range(0, len(files), EVIDENCE_BATCH):
                    if time.monotonic() > deadline:
                        out["stopped"] = "job-budget"
                        break
                    if reap_authority(item) != authority:
                        out["stopped"] = "authority-changed"
                        break
                    if marked and not marked_evidence_job(job["id"]):
                        raise ReapRefused("marked-evidence-report-or-age-changed")
                    planned = []
                    for entry in files[start:start + EVIDENCE_BATCH]:
                        try:
                            pfd = evidence_parent(root_fd, entry["rel"], marked=marked)
                        except FileNotFoundError:
                            out["changed"].append(entry["rel"])
                            continue
                        try:
                            name = Path(entry["rel"]).name
                            ffd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=pfd)
                            try:
                                if not evidence_same(os.fstat(ffd), entry):
                                    out["changed"].append(entry["rel"])
                                    continue
                                digest = hashlib.sha256()
                                while chunk := os.read(ffd, 1 << 20):
                                    digest.update(chunk)
                            finally:
                                os.close(ffd)
                        except FileNotFoundError:
                            out["changed"].append(entry["rel"])
                            continue
                        finally:
                            os.close(pfd)
                        line = json.dumps({"v": 1, "at": iso(), "run": run, "name": entry["rel"], "bytes": entry["bytes"],
                                           "sha256": digest.hexdigest(), "mtime": iso(entry["mtimeNs"] / 1e9)}, sort_keys=True) + "\n"
                        if os.write(manifest, line.encode()) != len(line.encode()):
                            raise ReapRefused("manifest-short-write")
                        planned.append(entry)
                    reap_durable(manifest)  # every name and hash is on disk before any of its files goes
                    for entry in planned:
                        try:
                            pfd = evidence_parent(root_fd, entry["rel"], marked=marked)
                        except FileNotFoundError:
                            out["changed"].append(entry["rel"])
                            continue
                        try:
                            name = Path(entry["rel"]).name
                            if not evidence_same(os.stat(name, dir_fd=pfd, follow_symlinks=False), entry):
                                out["changed"].append(entry["rel"])
                                continue
                            os.unlink(name, dir_fd=pfd)
                        except FileNotFoundError:
                            out["changed"].append(entry["rel"])
                            continue
                        finally:
                            os.close(pfd)
                        out["expired"] += 1
                        out["expiredBytes"] += entry["bytes"]
                if out["expired"]:
                    reap_durable(root_fd)
            finally:
                os.close(manifest)
        except BaseException:
            evidence_stop(job["id"], out)  # a pass that failed after its plan is a stopped handoff, as in reap_sweep
            raise
        finally:
            os.close(root_fd)
            try:
                reap_receipt({"phase": "evidence-result", "run": run, "owner": job["id"], "path": root, "expired": out["expired"],
                              "bytes": out["expiredBytes"], "changed": out["changed"][:20], "stopped": out.get("stopped")})
            except ReapRefused:
                out["stopped"] = "result-receipt-unavailable"
                evidence_stop(job["id"], out)
    finally:
        os.close(lock)
    out["changed"] = out["changed"][:20]
    if out["expired"]:
        log_event(conn, "job.evidence_expired", job["id"], actor, {"files": out["expired"], "bytes": out["expiredBytes"], "days": days, "run": run,
                                                                    "manifest": EVIDENCE_MANIFEST, "stopped": out.get("stopped")})
    return out


def evidence_expiry_tick(conn, policy: dict, live: list, actions: list, errors: list) -> None:
    """Rides on autoReap: finished jobs older than evidenceExpiryDays whose worker is gone, oldest first, a few per tick.
    A job folder is walked again at most weekly once it came out complete, hourly while it did not."""
    days = evidence_expiry_days(policy)
    enabled = [d for d in (days, marked_evidence_expiry_days(policy)) if d]
    if not policy.get("autoReap", False) or not enabled:
        return
    scan_days = min(enabled)
    try:
        reap_home()
    except HostError:
        return  # state moved without a reaper home (tests, a second instance): reaping is off, silently
    live_ids, live_idle, due = {s["id"] for s in live}, reap_idle_map(live), []
    for job in rows(conn, "SELECT * FROM jobs WHERE status IN ('done','failed','cancelled') AND finished_at IS NOT NULL AND finished_at<=? ORDER BY finished_at",
                    (now() - scan_days * 86400,)):
        if len(due) >= EVIDENCE_TICK_SCANS:
            break
        key = "evidence-expiry:" + job["id"]
        stored = row(conn, "SELECT value FROM meta WHERE key=?", (key,))
        try:
            saved = json.loads(stored["value"]) if stored else {}
            next_at = float(saved.get("next", 0)) if saved.get("expiryRule") == [days, marked_evidence_expiry_days(policy)] else 0.0
        except (ValueError, AttributeError, TypeError):
            next_at = 0.0
        if now() < next_at or reap_job_blocker(conn, job, live_ids, live_idle):
            continue
        if not os.path.isdir(reap_cache_root() / job["id"]):  # nothing to expire: no lease, no journal line
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                         (key, json.dumps({"at": now(), "next": now() + EVIDENCE_RECHECK_SECONDS, "expired": 0, "reason": "no-job-folder",
                                          "expiryRule": [days, marked_evidence_expiry_days(policy)]}, sort_keys=True)))
            continue
        due.append(job)
    if not due:
        return
    try:
        with reap_cleanup_owner("evidence-tick"):
            evidence_expiry_owned(conn, days, due, actions, errors)
    except (ReapRefused, HostError, OSError, ValueError, sqlite3.Error) as error:
        errors.append({"evidence-expiry": "cleanup-owner", "error": str(error)})


def evidence_expiry_owned(conn, days: int, due: list, actions: list, errors: list) -> None:
    started, worked = time.monotonic(), 0
    for job in due:
        if worked >= EVIDENCE_TICK_JOBS or time.monotonic() - started > REAP_TICK_SECONDS:
            break
        key = "evidence-expiry:" + job["id"]
        wait = EVIDENCE_RETRY_SECONDS
        try:
            out = evidence_expire_job(conn, job, days, False, "tick")
            if out.get("reason") == "scan-incomplete":
                wait = 86400
            elif not out.get("stopped") and not out.get("changed") and out.get("reason") != "reap-already-running":
                # Marked evidence gets an hourly retry, including while it ages through its separate five-day window.
                wait = EVIDENCE_RETRY_SECONDS if any("retention-marker" in k for k in out.get("kept", [])) else EVIDENCE_RECHECK_SECONDS
        except (HostError, OSError, ValueError, sqlite3.Error, ReapRefused) as error:
            errors.append({"evidence-expiry": job["id"], "error": str(error)})
            out = None
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                     (key, json.dumps({"at": now(), "next": now() + wait, "expired": out["expired"] if out else 0,
                                       "reason": out.get("reason") if out else "error",
                                       "expiryRule": [days, marked_evidence_expiry_days(load_policy())]}, sort_keys=True)))
        marked = (out or {}).get("markedEvidence") or {}
        if out and (out["expired"] or marked.get("deleted")):
            worked += 1
            actions.append({"did": "expired-evidence", "job": job["id"], "files": out["expired"], "bytes": out["expiredBytes"],
                            "markedDirectories": marked.get("deleted", 0), "markedBytes": marked.get("reclaimedBytes", 0),
                            "markedDfDeltaBytes": marked.get("dfDeltaBytes", 0)})
        held = getattr(REAP_CLEANUP_OWNER, "held", None)
        if held and held.get("stopped"):
            return  # no next job until this handoff is acknowledged (reap-owner-status, reap-acknowledge)


def reap_note(conn, job_id: str, note: dict | None) -> None:
    """The reaper's last word on a job lives on the job row (meta.reap), so the host's job view can show why a clone stayed."""
    job = row(conn, "SELECT meta FROM jobs WHERE id=?", (job_id,))
    meta = job_meta(job)
    if note is None and "reap" not in meta:
        return
    if note is None:
        meta.pop("reap")
    elif meta.get("reap", {}).get("signature") == note.get("signature") and meta["reap"].get("state") == note.get("state"):
        return
    else:
        meta["reap"] = note
    conn.execute("UPDATE jobs SET meta=? WHERE id=?", (json.dumps(meta, sort_keys=True) if meta else None, job_id))


def scope_release_tick(conn, actions: list) -> None:
    """The inherited scope of a finished source is released automatically once the last job of its accepted chain is done, failed
    or cancelled (no live heir): the record carries the evidence "successor terminal". A live heir keeps the scope, as does any
    live job that declares a path (the reaper still refuses those); a manual scope-release stays for edge cases and for `keep`."""
    successors = {}
    for record in rows(conn, "SELECT value FROM meta WHERE key LIKE 'cleanup:successor:%'"):
        try:
            proof = json.loads(record["value"])
        except ValueError:
            continue
        if proof.get("job") and proof.get("successor"):
            successors[proof["job"]] = proof["successor"]
    for source, first in sorted(successors.items()):
        job = row(conn, "SELECT status FROM jobs WHERE id=?", (source,))
        if not job or job["status"] not in REAP_TERMINAL or row(conn, "SELECT 1 FROM meta WHERE key=?", ("cleanup:released:" + source,)):
            continue
        chain, seen, last, live = first, {source}, None, False
        while chain and chain not in seen:
            seen.add(chain)
            heir = row(conn, "SELECT id,status FROM jobs WHERE id=?", (chain,))
            if heir and heir["status"] not in REAP_TERMINAL:
                live = True
                break
            last = heir["id"] if heir else last
            chain = successors.get(chain)
        if live or not last:
            continue
        evidence = f"successor terminal: {last} is {row(conn, 'SELECT status FROM jobs WHERE id=?', (last,))['status']}"
        with txn(conn):
            conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES(?,?)", ("cleanup:released:" + source,
                         json.dumps({"by": "tick", "evidence": evidence, "at": now(), "keep": [], "automatic": True}, sort_keys=True)))
            log_event(conn, "cleanup.scope_released", source, "tick", {"by": last, "evidence": evidence, "automatic": True})
        actions.append({"did": "released-scope", "job": source, "evidence": evidence})


def reap_tick(conn, policy: dict, live: list, actions: list, errors: list) -> None:
    if not policy.get("autoReap", False):
        return
    try:
        reap_home()
    except HostError:
        return
    try:
        scope_release_tick(conn, actions)
    except (HostError, OSError, ValueError, sqlite3.Error) as error:
        errors.append({"scope-release": str(error)})
    try:
        reap_agent_scratch_tick(conn, actions, errors)
    except (ReapRefused, HostError, OSError, ValueError, sqlite3.Error) as error:
        errors.append({"reap": "agent-scratch", "error": str(error)})
    try:
        reap_archive_clean_partials()  # every tick, whatever job is due: a crashed archive's staging folder older than a day
    except (OSError, ValueError) as error:
        errors.append({"reap": "archive-partials", "error": str(error)})
    try:
        live_ids, live_idle = {s["id"] for s in live}, reap_idle_map(live)
        try:
            reap_terminal_handoff_tick(conn, live_ids, errors)
        except (HostError, OSError, ValueError, sqlite3.Error) as error:
            errors.append({"reap": "terminal-handoff", "error": str(error)})
        due = []
        for job in rows(conn, "SELECT * FROM jobs WHERE status IN ('done','failed','cancelled','interrupted') ORDER BY finished_at"):
            if reap_state_wait(reap_state(conn, job["id"])):
                continue
            if not reap_job_blocker(conn, job, live_ids, live_idle) or not reap_output_terminal_blocker(conn, job, live_ids, live_idle):
                due.append(job)
        if not due:
            idle = rows(conn, "SELECT * FROM jobs WHERE status IN ('parked','blocked','interrupted')")
            if not idle or (disk_level(policy, disk_state()) == "ok" and not any(reap_idle_expired(j, policy) for j in idle)):
                return  # truly idle ticks create no journal and need no handoff
        try:
            clones = reap_clone_roots()  # discovery failure must not take a lease or grow the owner journal
        except (HostError, OSError, ValueError, sqlite3.Error) as error:
            errors.append({"reap": str(error)})  # preserve the existing discovery-error contract
            return
        with reap_cleanup_owner("tick"):
            reap_tick_owned(conn, policy, live, actions, errors, due, clones)
    except (ReapRefused, HostError, OSError, ValueError, sqlite3.Error) as error:
        errors.append({"reap": "cleanup-owner", "error": str(error)})


REAP_PRESSURE_RECHECK_SECONDS = 1800


def reap_pressure_pass(conn, policy: dict, live_ids: set, started: float, actions: list, errors: list, clones: list) -> None:
    """Derived output under disk pressure or after parkedReapHours of quiet, within the tick's time budget.
    Jobs oldest first; each job is looked at again at most every half hour. Clone, cache and evidence remain untouched."""
    pressure = disk_level(policy, disk_state()) != "ok"
    for job in rows(conn, "SELECT * FROM jobs WHERE status IN (%s) ORDER BY created_at" % ",".join("?" * len(REAP_PRESSURE_IDLE)), REAP_PRESSURE_IDLE):
        if not pressure and not reap_idle_expired(job, policy):
            continue
        if time.monotonic() - started > REAP_TICK_SECONDS:
            return
        key = "reap:pressure:" + job["id"]
        last = row(conn, "SELECT value FROM meta WHERE key=?", (key,))
        if last and now() - float(last["value"]) < REAP_PRESSURE_RECHECK_SECONDS:
            continue
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(now())))
        try:
            outcome = reap_job_owned(conn, job, False, "tick", clones, pressure=True)
        except (HostError, OSError, ValueError, sqlite3.Error, ReapRefused) as error:
            errors.append({"reapPressure": job["id"], "error": str(error)})
            continue
        if outcome.get("deleted"):
            actions.append({"did": "reaped-under-pressure", "job": job["id"], "items": outcome["deleted"], "bytes": outcome["reclaimedBytes"],
                            "dfDeltaBytes": outcome["dfDeltaBytes"]})
        if outcome.get("stopped"):
            held = getattr(REAP_CLEANUP_OWNER, "held", None)
            if held:
                held["stopped"] = outcome["stopped"]
            return


def reap_tick_owned(conn, policy: dict, live: list, actions: list, errors: list, due: list, clones: list) -> None:
    """Job-close hook: reap the artifacts of jobs that are finished, settled and whose worker is gone."""
    if not policy.get("autoReap", False):
        return
    try:
        reap_home()
    except HostError:
        return  # state moved without a reaper home (tests, a second instance): reaping is off, silently
    live_ids, started, attempted, reaped = {s["id"] for s in live}, time.monotonic(), 0, False
    live_idle = reap_idle_map(live)
    cleanup_owner = getattr(REAP_CLEANUP_OWNER, "held", None)
    for job in reap_output_job_order(due, clones):
        if attempted >= REAP_TICK_JOBS or time.monotonic() - started > REAP_TICK_SECONDS:
            break
        outcome = reap_one(conn, job, live_ids, clones, errors, live_idle)
        held = cleanup_owner or getattr(REAP_CLEANUP_OWNER, "held", None)
        if held and held.get("stopped") and not outcome:
            return
        if not outcome or not outcome["attempted"]:
            continue
        attempted += 1
        if outcome["deleted"]:
            reaped = True
            actions.append({"did": "reaped", "job": job["id"], "items": outcome["deleted"], "bytes": outcome["reclaimedBytes"], "dfDeltaBytes": outcome["dfDeltaBytes"]})
        if outcome.get("stopped"):
            held = getattr(REAP_CLEANUP_OWNER, "held", None)
            if held:
                held["stopped"] = outcome["stopped"]
            return  # no next job or release may run until this handoff is acknowledged
    reap_pressure_pass(conn, policy, live_ids, started, actions, errors, clones)
    # Superseded release candidates are pruned on a job close too, at most hourly.
    last = row(conn, "SELECT value FROM meta WHERE key='reaper:releases'")
    if reaped and (not last or now() - float(last["value"]) > 3600):
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('reaper:releases',?)", (str(now()),))
        try:
            pruned = reap_releases(False)
        except (HostError, OSError, ValueError, sqlite3.Error, ReapRefused) as error:
            errors.append({"reap": "releases", "error": str(error)})
            held = getattr(REAP_CLEANUP_OWNER, "held", None)
            if held:
                held["stopped"] = held.get("stopped") or "releases-error:" + reap_named(error)
            return
        if pruned["deleted"]:
            log_event(conn, "release.reaped", None, "tick", {"reclaimedBytes": pruned["reclaimedBytes"], "deleted": pruned["deleted"], "installed": pruned["installed"], "rollback": pruned["rollback"]})
            actions.append({"did": "reaped-releases", "items": pruned["deleted"], "bytes": pruned["reclaimedBytes"]})


# ---- superseded release candidates ----------------------------------------------------------------------------------

def reap_app_version(app: Path) -> str | None:
    try:
        with open(app / "Contents" / "Info.plist", "rb") as stream:
            version = plistlib.load(stream).get("CFBundleShortVersionString")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return version if isinstance(version, str) and re.match(r"^\d+\.\d+\.\d+", version) else None


def reap_version_tuple(text: str) -> tuple | None:
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(g) for g in match.groups()) if match else None


def reap_release_candidates() -> dict:
    """Candidate directories and what to do with each. Refuses everything when installed or rollback is unknown;
    keeps the installed version, the rollback, anything newer than installed and anything it cannot read as a version."""
    marks, releases, candidates = reap_release_marks(), reap_release_root(), []
    unknown = [name for name in ("installed", "rollback") if not marks[name]]
    try:
        entries = sorted(os.scandir(releases), key=lambda e: e.name)
    except OSError:
        entries = []
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            continue
        base = reap_version_tuple(entry.name)
        if unknown:
            keep = f"{'/'.join(unknown)} version unknown"
        elif base is None:
            keep = "not a version name"
        elif entry.name.split("-")[0].lstrip("v") in (marks["installed"], marks["rollback"]):
            keep = "installed" if entry.name.split("-")[0].lstrip("v") == marks["installed"] else "rollback"
        elif base >= reap_version_tuple(marks["installed"]):
            keep = "not older than installed"
        else:
            keep = None
        candidates.append({"name": entry.name, "path": reap_canon(entry.path), "keep": keep})
    return {**marks, "candidates": candidates}


def reap_release_items(candidate: dict) -> list[dict]:
    """target dirs of one superseded candidate: <candidate>/target, <candidate>/<checkout>/target and <checkout>/src-tauri/target."""
    roots = [candidate["path"]]
    try:
        roots += [reap_canon(e.path) for e in sorted(os.scandir(candidate["path"]), key=lambda e: e.name)
                  if e.is_dir(follow_symlinks=False) and not e.is_symlink() and not reap_protected(e.name)]
    except OSError:
        pass
    items = []
    for root in roots:
        for target in (os.path.join(root, "target"), os.path.join(root, "src-tauri", "target")):
          if os.path.isdir(target) and not os.path.islink(target):
            items.append({"path": reap_canon(target), "root": root, "owner": "release:" + candidate["name"],
                          "jobs": reap_job_ids(candidate["name"], os.path.basename(root)), "class": "release-target", "kind": "target",
                          "ownership": "superseded-release", "retention": reap_cache_retention(target)})
    return items


def reap_agent_scratch(dry_run: bool) -> dict:
    """Idle folders of the agent-scratch root through the same guarded per-item path (reap --agentScratch, and the tick)."""
    mode, ctx = ("dry-run" if dry_run else "delete"), {"run": uuid.uuid4().hex[:12]}
    if mode != "delete":
        done, stopped = reap_sweep(reap_agent_scratch_items(), mode, ctx)
    else:
        with reap_cleanup_owner("agent-scratch"):
            done, stopped = reap_sweep(reap_agent_scratch_items(), mode, ctx)
    out = {"items": done, **({"stopped": stopped} if stopped else {})}
    out.update(reap_summarize(done))
    return out


def reap_release_evidence(dry_run: bool) -> dict:
    """Superseded release evidence through the same guarded per-item path (reap --releaseEvidence, and the tick)."""
    mode, ctx = ("dry-run" if dry_run else "delete"), {"run": uuid.uuid4().hex[:12]}
    items = [i for i in reap_release_evidence_items() if i["retention"] == REAP_DISPOSABLE]  # kept versions are not items
    if mode != "delete":
        done, stopped = reap_sweep(items, mode, ctx)
    else:
        with reap_cleanup_owner("release-evidence"):
            done, stopped = reap_sweep(items, mode, ctx)
    out = {"items": done, **({"stopped": stopped} if stopped else {})}
    out.update(reap_summarize(done))
    return out


def reap_archive_stamp_legacy() -> int:
    """An archive written before `sole-copy-since.json` existed, whose clone is already gone: its removal time is unknown, so it is
    recorded now (marked `observed`) and the archive expires archiveExpiryDays after this pass, never earlier."""
    stamped = 0
    for item in reap_archive_items():
        if reap_archive_sole_since(item["path"]) is None:
            clone = reap_archive_clone_of(item["path"])
            if clone is not None and not os.path.lexists(clone) and reap_archive_mark_sole(item["path"], clone, None, observed=True):
                stamped += 1
    return stamped


def reap_archive_expiry(dry_run: bool) -> dict:
    """The reaper's own archives past archiveExpiryDays, through the same guarded per-item path (reap --archives, and the tick)."""
    mode, ctx = ("dry-run" if dry_run else "delete"), {"run": uuid.uuid4().hex[:12]}
    if not dry_run:
        reap_archive_stamp_legacy()
    items = [i for i in reap_archive_items() if i["retention"] == REAP_DISPOSABLE]  # younger archives are not items to remove
    if mode != "delete":
        done, stopped = reap_sweep(items, mode, ctx)
    else:
        with reap_cleanup_owner("archive-expiry"):
            done, stopped = reap_sweep(items, mode, ctx)
    out = {"items": done, **({"stopped": stopped} if stopped else {})}
    out.update(reap_summarize(done))
    return out


def reap_agent_scratch_tick(conn, actions: list, errors: list) -> None:
    """At most hourly: idle agent scratch goes. A folder still in use or recently changed is simply kept for a later pass."""
    if conn is None:
        return  # no state to pace the pass on (callers that drive the job reaper alone)
    last = row(conn, "SELECT value FROM meta WHERE key='reaper:agent-scratch'")
    if last and now() - float(last["value"]) < AGENT_SCRATCH_TICK_SECONDS:
        return
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('reaper:agent-scratch',?)", (str(now()),))
    if any(i["retention"] == REAP_DISPOSABLE for i in reap_release_evidence_items()):
        pruned = reap_release_evidence(False)
        if pruned.get("stopped"):
            errors.append({"reap": "release-evidence", "error": "sweep stopped: " + str(pruned["stopped"])})
            return
        if pruned["deleted"]:
            log_event(conn, "release-evidence.reaped", None, "tick", {"reclaimedBytes": pruned["reclaimedBytes"], "deleted": pruned["deleted"]})
            actions.append({"did": "reaped-release-evidence", "items": pruned["deleted"], "bytes": pruned["reclaimedBytes"]})
    reap_archive_stamp_legacy()
    if any(i["retention"] == REAP_DISPOSABLE for i in reap_archive_items()):
        expired = reap_archive_expiry(False)
        if expired.get("stopped"):
            errors.append({"reap": "archive-expiry", "error": "sweep stopped: " + str(expired["stopped"])})
            return
        if expired["deleted"]:
            log_event(conn, "archive.expired", None, "tick", {"reclaimedBytes": expired["reclaimedBytes"], "deleted": expired["deleted"]})
            actions.append({"did": "expired-archives", "items": expired["deleted"], "bytes": expired["reclaimedBytes"]})
    if not any(i["retention"] == REAP_DISPOSABLE for i in reap_agent_scratch_items()):
        return  # idle ticks take no cleanup lease and grow no journal
    out = reap_agent_scratch(False)
    if out.get("stopped"):
        errors.append({"reap": "agent-scratch", "error": "sweep stopped: " + str(out["stopped"])})
    if out["deleted"]:
        log_event(conn, "agent-scratch.reaped", None, "tick", {"reclaimedBytes": out["reclaimedBytes"], "deleted": out["deleted"]})
        actions.append({"did": "reaped-agent-scratch", "items": out["deleted"], "bytes": out["reclaimedBytes"]})


def reap_releases(dry_run: bool, ctx: dict | None = None, mode: str | None = None) -> dict:
    if (mode or ("dry-run" if dry_run else "delete")) != "delete":
        return reap_releases_owned(dry_run, ctx, mode)
    with reap_cleanup_owner("releases"):
        return reap_releases_owned(dry_run, ctx, mode)


def reap_releases_owned(dry_run: bool, ctx: dict | None = None, mode: str | None = None) -> dict:
    plan = reap_release_candidates()
    ctx = ctx if ctx is not None else {"run": uuid.uuid4().hex[:12]}
    out = {"installed": plan["installed"], "rollback": plan["rollback"], "candidates": [], "items": []}
    for candidate in plan["candidates"]:
        entry = {"name": candidate["name"], "keep": candidate["keep"]}
        out["candidates"].append(entry)
        if candidate["keep"]:
            continue
        done, stopped = reap_sweep(reap_release_items(candidate), mode or ("dry-run" if dry_run else "delete"), ctx)
        out["items"].extend(done)
        if stopped:
            out["stopped"] = stopped
            break
    out.update(reap_summarize(out["items"]))
    return out


# ---- tools -----------------------------------------------------------------------------------------------------------

def tool_reap_owner_status(params: dict) -> dict:
    try:
        with reap_cleanup_owner("inspect", inspect=True) as state:
            return state
    except ReapRefused as error:
        if isinstance(error.__cause__, FileNotFoundError):
            return {"phase": "none", "expectedRun": None, "handoff": None, "bytes": 0}
        raise HostError(str(error), "preserve the lock; another cleanup worker or unsafe state prevents inspection") from error


def tool_reap_owner_compact(params: dict) -> dict:
    try:
        with reap_cleanup_owner("compact", expected=require(params, "run")) as owner:
            return reap_compact_owned(owner)
    except (ReapRefused, OSError) as error:
        raise HostError(str(error), "read reap-owner-status; compact only a settled handoff, never remove or replace the lock") from error


def tool_reap_acknowledge(params: dict) -> dict:
    token, raw = require(params, "run"), require(params, "evidence")
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts or any(reap_protected(p) for p in path.parts):
        raise HostError("cleanup acknowledgment needs an absolute private handoff report, not protected state")
    fd = None
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise HostError("cleanup acknowledgment report must not be a symlink")
        resolved = reap_canon(str(path))
        if any(reap_protected(p) for p in Path(resolved).parts):
            raise HostError("cleanup acknowledgment report resolves to protected state")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW_ANY"):
            flags = (flags & ~os.O_NOFOLLOW) | os.O_NOFOLLOW_ANY
        fd = os.open(resolved, flags)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600 or not 0 < info.st_size <= 65536:
            raise HostError("cleanup acknowledgment report must be owned, regular, single-linked, 0600 and at most 64 KiB")
        data = os.read(fd, 65537)
        text = data.decode("utf-8")
        required = (f"Cleanup owner run: {token}", "Receipts reviewed: yes", "Unresolved paths preserved: yes",
                    "Deleted paths:", "Retained paths:", "Receipt references:", "Active consumers:", "Next safe step:")
        if len(data) != info.st_size or b"\0" in data or any(line not in text.splitlines() for line in required[:3]) or any(not any(entry.startswith(line) and entry[len(line):].strip() for entry in text.splitlines()) for line in required[3:]):
            raise HostError("cleanup acknowledgment report is incomplete: use the documented handoff checklist and exact run token")
        evidence = {"run": token, "evidence": str(path), "evidenceSha256": hashlib.sha256(data).hexdigest()}
        with reap_cleanup_owner("acknowledge:" + actor_of(params), acknowledge=evidence) as owner:
            return {"acknowledged": True, "run": token, "acknowledgmentRun": owner["run"], "evidenceSha256": evidence["evidenceSha256"]}
    except (ReapRefused, OSError, UnicodeError) as error:
        raise HostError(str(error), "read reap-owner-status; review notes and receipts, preserve unresolved paths, and never erase the lock") from error
    finally:
        if fd is not None:
            os.close(fd)


def tool_reap(params: dict) -> dict:
    try:
        return tool_reap_owned(params)
    except ReapRefused as error:
        raise HostError(str(error), "preserve artifacts; read the cleanup owner's handoff and receipts, then coordinate exclusive ownership") from error


def tool_reap_owned(params: dict) -> dict:
    """`reap --job J-NNNN [--dry-run]`: the explicit run of the same gates; `reap --releases` prunes superseded candidates."""
    if params.get("agentScratch") or params.get("releaseEvidence") or params.get("archives"):
        if params.get("job") or params.get("releases") or sum(1 for k in ("agentScratch", "releaseEvidence", "archives") if params.get(k)) > 1:
            raise HostError("reap takes one of --job, --releases, --agentScratch, --releaseEvidence or --archives")
        if params.get("archives"):
            return reap_archive_expiry(bool(params.get("dryRun")))
        if params.get("releaseEvidence"):
            return reap_release_evidence(bool(params.get("dryRun")))
        return reap_agent_scratch(bool(params.get("dryRun")))
    if params.get("releases"):
        if params.get("job"):
            raise HostError("reap takes --job or --releases, not both")
        out = reap_releases(bool(params.get("dryRun")))
        if not params.get("dryRun") and out["deleted"]:
            with db() as conn:
                log_event(conn, "release.reaped", None, actor_of(params), {"reclaimedBytes": out["reclaimedBytes"], "deleted": out["deleted"], "installed": out["installed"], "rollback": out["rollback"]})
        return out
    job_id = require(params, "job")
    live_ids, live_idle = reap_live_snapshot()
    with db() as conn:
        job = row(conn, "SELECT * FROM jobs WHERE id=?", (job_id,))
        if not job:
            raise HostError(f"no job {job_id}")
        blocker = reap_job_blocker(conn, job, live_ids, live_idle)
        outputs_only = bool(blocker) and not reap_output_terminal_blocker(conn, job, live_ids, live_idle) and any(
            reap_output_registry_present(i["path"]) for i in reap_job_items(job, reap_clone_roots())[0])
        if blocker and (not outputs_only or params.get("expireEvidence")):
            raise HostError(f"{job_id} is not reapable: {blocker}", "only finished jobs whose worker is gone are reaped; run reap-status for the state of every job")
        if params.get("expireEvidence"):
            days = evidence_expiry_days(load_policy())
            if not days and not marked_evidence_expiry_days(load_policy()):
                raise HostError("evidence expiry is off (both evidenceExpiryDays and markedEvidenceExpiryDays=0)",
                                f"{REAP_CLI} policy-set markedEvidenceExpiryDays=5 turns marked expiry on")
            try:
                return evidence_expire_job(conn, job, days, bool(params.get("dryRun")), actor_of(params))
            except ReapRefused as refusal:
                raise HostError(f"evidence expiry of {job_id} refused, nothing was removed after the refusal: {refusal}")
            except OSError as error:
                raise HostError(f"evidence expiry of {job_id} stopped: {reap_named(error)}",
                                    "the cleanup owner recorded a stopped handoff; read reap-owner-status and the manifest before acknowledging") from error
        if outputs_only:
            return reap_job_owned(conn, job, bool(params.get("dryRun")), actor_of(params), outputs_only=True)
        return reap_job(conn, job, bool(params.get("dryRun")), actor_of(params))


def reap_audit_item(item: dict, blocker: str | None, ctx: dict, deadline: float) -> dict:
    base = {"path": item["path"], "class": item["class"], "kind": item["kind"]}
    try:
        info = os.lstat(item["path"])
    except OSError:
        return {**base, "state": "gone", "bytes": 0, "blocking": None}
    ctx["sizeBudget"] = 1.0 if time.monotonic() > deadline else max(1.0, min(60.0, deadline - time.monotonic()))
    size_reason = None
    idle_pressure = False
    if blocker and os.path.basename(item["path"]) in REAP_OUTPUT_KINDS and reap_output_registry_present(item["path"]):
        with db() as conn:
            job = row(conn, "SELECT * FROM jobs WHERE id=?", (item["owner"],))
            if job and not reap_output_terminal_blocker(conn, job, *reap_live_snapshot()):
                blocker = None  # exact-output authority still runs; source roots never enter this branch
    if blocker in ("job-parked", "job-blocked", "job-interrupted") and item.get("class") == "job-clone-regenerable" \
            and item.get("kind") in REAP_DERIVED_KINDS:
        with db() as conn:
            job = row(conn, "SELECT * FROM jobs WHERE id=?", (item["owner"],))
        idle_pressure = disk_level(load_policy(), disk_state()) != "ok"
        if job and (idle_pressure or reap_idle_expired(job, load_policy())):
            blocker = None  # every fresh authority/terminal/process proof still runs below
    if blocker:
        entry = {"state": "blocked", "blocking": blocker}
        done = None
    elif time.monotonic() > deadline:
        entry = {"state": "blocked", "blocking": "audit-budget-exhausted"}
        done = None
    else:
        previous_pressure = getattr(REAP_PRESSURE, "on", False)
        REAP_PRESSURE.on = idle_pressure
        try:
            done = reap_item(item, "audit", ctx)
        finally:
            REAP_PRESSURE.on = previous_pressure
        entry = {"state": "reapable" if done["outcome"] == "would-delete" else "blocked", "blocking": done["reason"]}
        if done.get("idle"):
            entry["idle"] = done["idle"]  # the folder's own idle proof: its reason and the budget the walk spent
        if done.get("archive"):
            entry["archive"] = str(done["archive"].get("reason", "")).split(":")[0]  # reapable once archived first
    if done is not None and done["bytes"] is not None:
        size, complete = done["bytes"], not done.get("bytesIncomplete")
        size_reason = done.get("sizeReason")
    else:
        sizing = reap_metadata_size(item["path"], max(0, min(60, deadline - time.monotonic())))
        size, complete, size_reason = sizing["bytes"], sizing["complete"], sizing["reason"]
    entry["bytes"] = size
    if size is None or not complete:
        entry["sizeIncomplete"] = True
        entry["sizeReason"] = size_reason or "size-unknown"
    return {**base, **entry, "mtime": iso(info.st_mtime), "dev": info.st_dev, "ino": info.st_ino}


# Every reason an item is kept, as an action the operator (or a later tick) can take. reap-status prints the line for each reason
# it reports, with the bytes behind it; an unlisted reason still prints, with a pointer to the item's own text.
REAP_REASON_ACTIONS = {
    "job-parked": "unfinished job (parked): clone/cache/evidence kept; derived output goes under pressure or after parkedReapHours without a report",
    "job-approved": "job approved and waiting to run: kept until it finishes",
    "job-running": "job running: kept until it finishes",
    "job-queued": "job queued: kept until it finishes",
    "job-blocked": "job blocked: clone/cache/evidence kept; derived output goes under pressure or after parkedReapHours without a report",
    "job-interrupted": "job interrupted: clone/cache/evidence kept; derived output goes only under disk pressure",
    "settling": "job finished moments ago: reaped after the settle window",
    "worker-terminal-still-open": "worker terminal still live: never removed under a live terminal; close it through cleanup-status / cleanup-record",
    "terminal-state-unknown": "the terminal host did not answer: retried next tick",
    "kept-by-release": "the installed release or its rollback was built from this folder: kept",
    "retention-preserved": "carries a retention class (evidence/customer/release-runtime-rollback) or an unknown marker: kept",
    "idle-recent-change": "something in the folder changed inside the evidence window: kept until it is quiet for that long",
    "idle-budget-exceeded": "the folder holds more entries than the idle walk may list: kept; raise agentScratchIdleMaxEntries or the receipt names what the walk spent",
    "idle-budget-time-exceeded": "the idle walk ran out of its time budget on this folder: kept; the receipt names the seconds it spent",
    "idle-budget-bytes-exceeded": "the folder is bigger than the byte budget the idle walk may see: kept; raise agentScratchIdleMaxBytes or the receipt names what the walk spent",
    "idle-unreadable": "an entry of the folder could not be read: kept; nothing is removed on a guess",
    "idle-protected-leaf": "the folder holds a protected name (.env*, .dev.vars*, .wrangler, .vercel): kept",
    "idle-too-deep": "the folder is deeper than the walk may go: kept; nothing is removed on a guess",
    "idle-expiry-off": "evidence expiry is off: every agent-scratch folder is kept",
    "retention-ambiguous": "conflicting retention markers: kept; leave exactly one marker",
    "dirty": "uncommitted changes: archived under <archive root>/<job>/ then removed once the job is past cloneExpiryDays",
    "unpushed": "commits no remote holds: bundled (git bundle, verified) then removed once past cloneExpiryDays",
    "stash-present": "a stash: bundled then removed once past cloneExpiryDays",
    "ignored-unknown": "ignored data that is not regenerable: archived as files then removed once past cloneExpiryDays",
    "unproved-git-dir": "a Git directory without a .git entry: archived whole then removed once past cloneExpiryDays",
    "git-error": "git could not be read: retried with backoff, then archived as plain files and removed",
    "git-unsafe-index": "git index cannot be trusted: retried, then archived as plain files and removed",
    "nested-git-in-clone": "more than one repository in the clone: all archived, then removed once past cloneExpiryDays",
    "protected-copies": "holds .env*/.dev.vars*/.wrangler/.vercel: archived owner-only (protected.tar.gz), never printed, then removed once past cloneExpiryDays",
    "archive-stale": "the folder, a process or a ref changed after its archive was made: kept; archived again on the next pass",
    "archive-failed": "the archive could not be completed (named after the colon): kept until it can be",
    "archive-verify-failed": "the archive did not verify: kept; nothing was removed",
    "linked-worktrees": "a linked worktree of this repository may still exist (or was moved): kept until it is reaped, pruned or provably gone",
    "recently-used-by": "a later job worked in this clone: it expires cloneExpiryDays after that job finished",
    "archive-pending": "inside cloneExpiryDays: nothing is archived or removed before the job has been finished that long",
    "archive-too-large": "archive would exceed the size cap: kept; move or delete the data by hand",
    "archive-no-space": "not enough free disk to archive safely: kept until space is freed",
    "protected-leaf": "holds .env*/.dev.vars*/.wrangler/.vercel: never copied or removed by a cache/regenerable item; kept",
    "in-use": "a process has it open or as its cwd: kept until it exits",
    "not-git-ignored": "build output that git does not ignore: kept",
    "tracked-files": "tracked files inside: kept",
    "size-time-limit": "sizing hit its time limit this pass: the reaper's own pass doubles the budget each attempt (up to 8x) until it completes",
    "size-entry-limit": "too many entries to size this pass: the reaper's own pass doubles the entry budget each attempt (up to 8x) until it completes",
    "audit-pending": "not audited yet this cycle: reap-status resumes at its cursor on the next call and tick",
    "audit-path-unreadable": "the path could not be read again after the cached audit: kept; the next pass audits it anew",
    "audit-path-unknown": "the path could not be proved again after the cached audit: kept; the next pass audits it anew",
    "audit-path-changed": "the path is a different object than the cached audit saw: kept; the next pass audits it anew",
    "audit-path-symlink": "the path is a link now, not the audited folder: kept; the next pass audits it anew",
}


def reap_reason_action(reason: str) -> str:
    head = reason.split(":")[0].split(";")[0]
    return REAP_REASON_ACTIONS.get(head) or "kept: see the item's blocking text in reap-status --items"


def reap_bucket(categories: dict, reasons: dict, entry: dict, reason_bytes: dict | None = None) -> None:
    bucket = categories.setdefault(entry["class"], {"items": 0, "bytes": 0, "reapable": 0, "reapableBytes": 0, "blocked": 0, "sizeUnknown": 0})
    bucket["items"] += 1
    bucket["bytes"] += entry["bytes"] or 0
    bucket["sizeUnknown"] += 1 if entry.get("sizeIncomplete") else 0
    if entry["state"] == "reapable":
        bucket["reapable"] += 1
        bucket["reapableBytes"] += entry["bytes"] or 0
    else:
        bucket["blocked"] += 1
        key = (entry["blocking"] or "unknown").split(":")[0].split(";")[0]
        reasons[key] = reasons.get(key, 0) + 1
        if reason_bytes is not None:
            reason_bytes[key] = reason_bytes.get(key, 0) + (entry["bytes"] or 0)


REAP_AUDIT_CACHE_SECONDS = 86400       # a job's audit from an earlier pass is shown (marked cached) this long when the budget ends before it
REAP_AUDIT_JOB_SECONDS = 60            # least time a started job's items get, whatever is left of the budget (the next call resumes after it)
REAP_AUDIT_CURSOR = "reap:audit-cursor"
REAP_AUDIT_PERSIST = threading.local()   # set by the tick's sizing pass: that pass (and only it) writes the cursor and the per-job cache


def reap_audit_cursor(conn) -> dict:
    stored = row(conn, "SELECT value FROM meta WHERE key=?", (REAP_AUDIT_CURSOR,))
    try:
        found = json.loads(stored["value"]) if stored else {}
    except ValueError:
        found = {}
    return found if isinstance(found, dict) else {}


def reap_audit_remember(conn, job: dict, blocker: str | None, entries: list) -> None:
    """The job's audit, compact, for the passes whose budget ends before they reach it (shown as cached, never as a block)."""
    keep = ("path", "class", "kind", "state", "blocking", "bytes", "sizeIncomplete", "sizeReason", "archive", "mtime", "dev", "ino")
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", ("reap:audit:" + job["id"], json.dumps(
        {"at": now(), "status": job["status"], "blocking": blocker, "entries": [{k: e[k] for k in keep if k in e} for e in entries]})))


def reap_audit_cached(conn, job: dict) -> dict | None:
    stored = row(conn, "SELECT value FROM meta WHERE key=?", ("reap:audit:" + job["id"],))
    try:
        found = json.loads(stored["value"]) if stored else None
    except ValueError:
        return None
    if not isinstance(found, dict) or found.get("status") != job["status"] or now() - found.get("at", 0) > REAP_AUDIT_CACHE_SECONDS:
        return None
    return found


def reap_audit_revalidate(entry: dict) -> dict | None:
    """Metadata-only proof that a cached entry still names the same object. A path that is gone is dropped: it counts for
    nothing, neither as reapable nor as bytes. A path that cannot be read, is a link now, or is a different object than
    the one the cached audit saw is kept but never reapable and never sized again: a cached audit is a hint, never an
    authority, and the revalidation reads no content and enters no folder."""
    path = entry.get("path")
    if not path:
        return None
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except PermissionError:
        return {**entry, "state": "blocked", "blocking": "audit-path-unreadable", "bytes": 0,
                "sizeIncomplete": True, "sizeReason": "path-unreadable"}
    except OSError:
        return {**entry, "state": "blocked", "blocking": "audit-path-unknown", "bytes": 0,
                "sizeIncomplete": True, "sizeReason": "path-unknown"}
    if stat.S_ISLNK(info.st_mode):
        return {**entry, "state": "blocked", "blocking": "audit-path-symlink", "bytes": 0,
                "sizeIncomplete": True, "sizeReason": "path-symlink"}
    if any(entry.get(key) is None for key in ("dev", "ino", "mtime")):
        return {**entry, "state": "blocked", "blocking": "audit-path-unknown", "bytes": 0,
                "sizeIncomplete": True, "sizeReason": "path-unknown"}
    same_object = entry["dev"] == info.st_dev and entry["ino"] == info.st_ino
    same_time = entry["mtime"] == iso(info.st_mtime)
    if not (same_object and same_time):
        return {**entry, "state": "blocked", "blocking": "audit-path-changed", "bytes": 0,
                "sizeIncomplete": True, "sizeReason": "path-changed"}
    return entry


def reap_audit(only: str | None, limit: int, include_items: bool, budget: int, persist: bool = False, after: str | None = None) -> dict:
    """Metadata-only audit of unreaped artifacts. Deletes nothing, removes nothing, takes no lock it would have to create. It is
    incremental: jobs are audited from a cursor until the budget ends; the jobs after that show their audit from an earlier pass
    (`cached`) or are counted as `pending`, never as blocked, and the next call resumes at the cursor. The explicit reap-status
    writes nothing: it reads the persisted cursor and cache and returns `summary.audit.cursorAfter` (pass it back as `after` to
    resume). The tick's sizing pass (`persist`) writes the cursor (meta `reap:audit-cursor`) and each job's compact audit (meta
    `reap:audit:<job>`). A single `only` job is always audited fresh."""
    persist = persist or bool(getattr(REAP_AUDIT_PERSIST, "on", False))
    started = time.monotonic()
    deadline = started + budget
    job_deadline = started + budget * 0.8  # the rest of the budget is for the releases and agent scratch
    live_ids, live_idle = reap_live_snapshot()
    clones = reap_clone_roots()
    ctx: dict = {"run": "audit"}
    try:
        ctx["table"] = reap_process_table()
    except ReapRefused as refusal:
        ctx["table_error"] = str(refusal)
    listed, categories, reasons, reason_bytes, incomplete, seen = [], {}, {}, {}, [], set()
    pending, cached_jobs, last_done, cycle_done, handoff, fresh = [], 0, None, True, [], 0
    gone_paths, unknown_paths = 0, 0
    with db() as conn:
        jobs = rows(conn, "SELECT * FROM jobs WHERE id=?", (only,)) if only else rows(conn, "SELECT * FROM jobs ORDER BY id")
        known = {j["id"] for j in rows(conn, "SELECT id FROM jobs")}
        cursor = {} if only else reap_audit_cursor(conn)
        if after is None and persist:
            after = cursor.get("after") if isinstance(cursor.get("after"), str) else None
        if after:  # resume where the last pass stopped, then wrap round
            jobs = [j for j in jobs if j["id"] > after] + [j for j in jobs if j["id"] <= after]
        for job in jobs:
            entries, blocker, note = None, None, None
            if not only and fresh and time.monotonic() > job_deadline:  # at least one job per call, so a tiny budget still advances the cursor
                cycle_done = False
                cached = reap_audit_cached(conn, job)
                if cached is None:
                    pending.append(job["id"])
                    continue
                blocker, entries, note = cached.get("blocking"), [], cached
                for entry in [e for e in cached["entries"] if e["path"] not in seen]:
                    kept = reap_audit_revalidate(entry)
                    if kept is None:
                        gone_paths += 1
                    else:
                        if kept.get("state") != entry.get("state"):
                            unknown_paths += 1
                        entries.append(kept)
                cached_jobs += 1
            else:
                fresh += 1
                items, notes = reap_job_items(job, clones)
                incomplete.extend(notes)
                items = [i for i in items if i["path"] not in seen]  # a clone named for two jobs is listed once
                seen.update(i["path"] for i in items)
                if not items:
                    last_done = job["id"]
                    if persist:
                        reap_audit_remember(conn, job, None, [])
                    continue
                blocker = reap_job_blocker(conn, job, live_ids, live_idle)
                if not blocker and "table" not in ctx:
                    blocker = ctx.get("table_error") or "process-table-unavailable"
                item_deadline = max(job_deadline, time.monotonic() + REAP_AUDIT_JOB_SECONDS)  # a job that was started is finished: its items are never starved
                entries = [e for e in (reap_audit_item(item, blocker, ctx, item_deadline) for item in items) if e["state"] != "gone"]
                if persist and not only:
                    reap_audit_remember(conn, job, blocker, entries)
                last_done = job["id"]
            if note is not None:
                seen.update(e["path"] for e in entries)
                entries = [{**e, "cached": True} for e in entries]
            if not entries:
                continue
            for e in entries:
                reap_bucket(categories, reasons, e, reason_bytes)
            if (blocker or "").startswith("worker-terminal-still-open:"):
                handoff.append({**reap_terminal_handoff(job, blocker.split(":", 1)[1]), "bytes": sum(e["bytes"] or 0 for e in entries)})
            listed.append({"job": job["id"], "status": job["status"], "title": whole_title(job["title"]), "blocking": blocker, "items": len(entries),
                           "bytes": sum(e["bytes"] or 0 for e in entries), "cached": note is not None or None,
                           "detail": entries if include_items else None})
        if persist and not only:
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (REAP_AUDIT_CURSOR, json.dumps(
                {"after": None if cycle_done else last_done or after, "at": now(), "complete": cycle_done,
                 "completedAt": now() if cycle_done else cursor.get("completedAt"), "pending": len(pending)})))
    unowned_cache, unowned_clones = [], []
    if not only:
        try:
            unowned_cache = [e.name for e in os.scandir(reap_cache_root()) if re.match(r"^J-\d{4,5}$", e.name) and e.name not in known and e.is_dir(follow_symlinks=False)]
        except OSError:
            pass
        unowned_clones = [root for root, ids in clones if not any(i in known for i in ids)]
    listed.sort(key=lambda j: j["bytes"], reverse=True)
    releases = reap_release_candidates()
    release_rows = []
    for candidate in releases["candidates"]:
        entry = {"name": candidate["name"], "keep": candidate["keep"], "items": []}
        if not candidate["keep"]:
            for item in reap_release_items(candidate):
                audited = reap_audit_item(item, None, ctx, deadline)
                entry["items"].append(audited)
                reap_bucket(categories, reasons, audited, reason_bytes)
        release_rows.append(entry)
    scratch_rows = []
    if not only:
        for item in reap_agent_scratch_items():
            audited = reap_audit_item(item, None, ctx, deadline)
            scratch_rows.append(audited)
            reap_bucket(categories, reasons, audited, reason_bytes)
    if pending or cached_jobs:
        incomplete.append(f"audit budget of {budget}s reached: {len(pending)} job(s) not audited yet (pending, not blocked), {cached_jobs} shown from an earlier pass; "
                          "the next reap-status call and the tick resume at the persisted cursor")
    if gone_paths or unknown_paths:
        incomplete.append(f"cached audit: {gone_paths} path(s) are gone and {unknown_paths} path(s) could not be proved again "
                          "(unreadable, replaced or a link now): none of them is counted as reapable")
    return {"metadataOnly": True, "deleted": 0, "at": iso(), "elapsedSeconds": round(time.monotonic() - started, 1), "dfFreeBytes": reap_free_bytes(),
            "summary": {"categories": categories, "unreapedBytes": sum(c["bytes"] for c in categories.values()),
                        "reapableBytes": sum(c["reapableBytes"] for c in categories.values()),
                        "blockedByReason": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
                        "blockedBytesByReason": dict(sorted(reason_bytes.items(), key=lambda kv: -kv[1])),
                        "reasonActions": {k: reap_reason_action(k) for k in reasons},
                        "terminalHandoff": sorted(handoff, key=lambda h: -h["bytes"])[:40],
                        "audit": {"complete": cycle_done, "pendingJobs": len(pending), "pendingJobIds": pending[:20], "cachedJobs": cached_jobs,
                                  "cursorAfter": None if cycle_done else last_done or after},
                        "jobsWithArtifacts": len(listed), "unownedCacheDirs": len(unowned_cache), "unownedClones": len(unowned_clones)},
            "jobs": listed[:limit], "jobsOmitted": max(0, len(listed) - limit),
            "releases": {"installed": releases["installed"], "rollback": releases["rollback"], "candidates": release_rows},
            "agentScratch": scratch_rows if include_items else len(scratch_rows),
            "incomplete": incomplete}


def tool_reap_status(params: dict) -> dict:
    after = params.get("after")
    if after is not None and not re.fullmatch(r"J-\d{4,5}", str(after)):
        raise HostError("after must be a job id such as J-0123 (the summary.audit.cursorAfter of the previous call)")
    return reap_audit(params.get("job"), max(1, int(params.get("limit", 40))), bool(params.get("items")), max(1, int(params.get("budgetSeconds", REAP_AUDIT_SECONDS))),
                      after=after)


def reap_quick_summary(conn) -> dict:
    """cleanup-status addition: how many finished jobs still hold artifacts. Names only, no sizing; reap-status has bytes."""
    clones, jobs, items = reap_clone_roots(), 0, 0
    for job in rows(conn, "SELECT * FROM jobs WHERE status IN ('done','failed','cancelled')"):
        found, _ = reap_job_items(job, clones)
        jobs += 1 if found else 0
        items += len(found)
    return {"finishedJobsWithArtifacts": jobs, "artifactDirs": items, "autoReap": load_policy().get("autoReap", False),
            "note": f"sizes and blocking reasons: {REAP_CLI} reap-status"}
# <<< trimreaper core <<<
