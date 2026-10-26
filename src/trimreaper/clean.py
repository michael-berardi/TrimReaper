"""`trimreaper clean`: free space outside agent jobs, on any machine, for any user.

Three things, each with its own rule:

* Tool caches go through the tool's own cleaner (`npm cache clean --force`, `uv cache prune`, `brew cleanup` ...), and only
  while that tool is not running. Cargo's registry and Xcode's DerivedData have no cleaner: each of their folders goes
  through the engine's guarded removal instead.
* Agent worktrees: linked git worktrees left by Claude Code, Codex, Cursor, Conductor and the like. One goes when it is idle,
  unlocked, clean, holds nothing git ignores but build output, holds no protected name, no process uses it, and every commit
  its HEAD ever pointed at is still on a branch, tag or remote. It goes through the guarded removal; its branch stays.
* Stale build/session debris and superseded browser builds use guarded removal; models and disk images are report-only.

Only names, sizes and times are read, never file contents. Space freed is the volume's free space after minus before.
Everything between the `trimreaper clean` markers can be vendored byte for byte after the engine, like the engine itself
(docs/EMBEDDING.md): it reads the engine's names and the host's names as globals, and every name it defines starts with
`clean_` or `CLEAN_`.
"""
from __future__ import annotations

import json
import re
import os
import shutil
import subprocess
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .core import *  # noqa: F401,F403  (the body reads the engine's and host's names as globals, as it does when vendored)
from .core import ReapRefused
from .host import reap_worktree_homes

# The host names the clean pass reads besides core.HOST_NAMES (checked by tests/test_embedding.py).
CLEAN_HOST_NAMES = ("reap_worktree_homes",)

# >>> trimreaper clean >>>
CLEAN_TOOL_SECONDS = 900       # one native cleaner; longer is a named timeout
CLEAN_SIZE_SECONDS = 20        # one size walk; longer is reported as incomplete
CLEAN_AUTO_SECONDS = 600       # stampede guard: retry sustained pressure at most once per ten minutes
CLEAN_AUTO_QUIET_SECONDS = 3600  # ... and after a pass that removed nothing, once per hour: the windows it waits for are hours long
CLEAN_AUTO_RUNNING_SECONDS = 1800  # a climbing pass can take minutes: no second pass starts while one stamped itself running this recently
CLEAN_AUTO_KEY = "clean:auto"
CLEAN_STEADY_KEY = "clean:steady"
CLEAN_TOOL_ENV = {"HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_ENV_HINTS": "1", "HOMEBREW_NO_AUTOREMOVE": "1"}  # cleanup never uninstalls a formula
# Tools run from the home folder, so a project's own config (.npmrc, a pinned yarn) never changes which cache they clean.


def clean_home(*rel: str) -> str:
    return str(Path(reap_home(), *rel))


def clean_run(argv: list[str], timeout: float) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                              timeout=timeout, env={**os.environ, **CLEAN_TOOL_ENV}, cwd=clean_home())
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 127, ""
    return done.returncode, done.stdout


def clean_env_dir(variable: str, default: str, *tail: str):
    def where():
        base = os.environ.get(variable)
        return [os.path.join(os.path.expanduser(base) if base else clean_home(default), *tail)]
    return where


def clean_asked(*argv: str):
    """The cache folder as the tool itself reports it."""
    def where():
        code, out = clean_run(list(argv), 15)
        line = out.strip().splitlines()[-1].strip() if code == 0 and out.strip() else ""
        return [line] if os.path.isabs(line) else []
    return where


def clean_yarn_classic() -> str | None:
    code, out = clean_run(["yarn", "--version"], 15)
    return None if code == 0 and out.strip().startswith("1.") else "yarn-2-or-later-keeps-its-cache-per-project"


def clean_simctl() -> str | None:
    return None if clean_run(["xcrun", "--find", "simctl"], 15)[0] == 0 else "xcode-missing"  # after clean_installed("xcrun")


def clean_docker_up() -> str | None:
    """Docker is running here: its endpoint is a local unix socket (never DOCKER_HOST or a context naming another machine)."""
    host = os.environ.get("DOCKER_HOST")
    if host is None:
        code, out = clean_run(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], 15)
        host = out.strip() if code == 0 else ""
    if not host.startswith("unix://"):
        return "docker-endpoint-not-local:" + (host.split("://")[0] or "unknown")
    return None if clean_run(["docker", "info", "--format", "{{.ServerVersion}}"], 15)[0] == 0 else "docker-not-running"


def clean_docker_ready() -> str | None:
    reason = clean_docker_up()
    if reason:
        return reason
    code, out = clean_run(["ps", "-axww", "-o", "pid=,command="], 30)
    if code != 0 or not out.strip():
        return "probe-failed:docker-build-processes"
    for line in out.splitlines():
        tokens = line.split()[1:]
        for index, token in enumerate(tokens):
            program = os.path.basename(token).lower()
            tail = tokens[index + 1:]
            if program in ("docker-buildx", "buildkitd", "buildctl") or (program == "docker" and
                    any(word in ("build", "buildx") for word in tail)):
                return "in-use:docker-build"
    return None


def clean_colima_trim() -> tuple[int, str] | None:
    host = os.environ.get("DOCKER_HOST")
    if host is None:
        code, out = clean_run(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], 15)
        host = out.strip() if code == 0 else ""
    if host.startswith("unix://") and ".colima" in Path(host[7:]).parts:
        return clean_run(["colima", "ssh", "--", "sudo", "fstrim", "-av"], CLEAN_TOOL_SECONDS)
    return None


def clean_installed(program: str) -> str | None:
    """Why `program` cannot run, or None. On macOS, /usr/bin/pip3, xcrun and the like are stubs that open an "install the
    developer tools" window when the Command Line Tools are missing; such a stub counts as missing."""
    found = shutil.which(program)
    if not found:
        return "tool-missing"
    if found.startswith("/usr/bin/") and os.uname().sysname == "Darwin" and clean_run(["/usr/bin/xcode-select", "-p"], 15)[0] != 0:
        return "tool-missing:command-line-tools-not-installed"
    return None


def clean_cargo_roots() -> list[str]:
    base = os.environ.get("CARGO_HOME")
    base = os.path.expanduser(base) if base else clean_home(".cargo")
    return [os.path.join(base, "registry", "cache"), os.path.join(base, "registry", "src")]


# A tool cache is cleaned by its own tool. `busy`: process names that keep it as it is. `deep` runs only when asked for.
CLEAN_TOOLS = (
    {"name": "npm", "argv": ["npm", "cache", "clean", "--force"], "busy": ("npm",), "where": clean_env_dir("npm_config_cache", ".npm", "_cacache")},
    {"name": "pnpm", "argv": ["pnpm", "store", "prune"], "busy": ("pnpm",), "where": clean_asked("pnpm", "store", "path")},
    {"name": "yarn", "argv": ["yarn", "cache", "clean"], "busy": ("yarn",), "where": clean_asked("yarn", "cache", "dir"), "ready": clean_yarn_classic},
    {"name": "bun", "argv": ["bun", "pm", "cache", "rm"], "busy": ("bun",), "where": clean_env_dir("BUN_INSTALL_CACHE_DIR", ".bun/install/cache")},
    {"name": "uv", "argv": ["uv", "cache", "prune"], "busy": ("uv",), "where": clean_asked("uv", "cache", "dir")},
    {"name": "pip", "argv": ["pip3", "cache", "purge"], "busy": ("pip", "pip3"), "where": clean_asked("pip3", "cache", "dir")},
    {"name": "go", "argv": ["go", "clean", "-cache", "-testcache", "-fuzzcache"], "busy": ("go",), "where": clean_asked("go", "env", "GOCACHE")},
    {"name": "homebrew", "argv": ["brew", "cleanup", "--prune=all"], "busy": ("brew",), "where": clean_asked("brew", "--cache")},
    {"name": "simulators", "deep": True, "argv": ["xcrun", "simctl", "delete", "unavailable"], "busy": ("simulator",), "ready": clean_simctl},
    {"name": "docker-build-cache", "argv": ["docker", "builder", "prune", "-af"], "busy": (), "ready": clean_docker_ready},
    {"name": "go-modules", "deep": True, "argv": ["go", "clean", "-modcache"], "busy": ("go", "gopls"), "where": clean_asked("go", "env", "GOMODCACHE")},
    {"name": "docker-images", "deep": True, "argv": ["docker", "image", "prune", "-af"], "busy": (), "ready": clean_docker_ready},
)
# Caches with no cleaner of their own: each folder directly inside a root goes through the guarded removal.
CLEAN_FOLDERS = (
    {"name": "cargo-registry", "busy": ("cargo", "rustc", "rust-analyzer", "clippy-driver"), "roots": clean_cargo_roots},
    {"name": "xcode-derived-data", "busy": ("xcode", "xcodebuild", "swift-frontend", "sourcekit-lsp"),
     "roots": lambda: [clean_home("Library", "Developer", "Xcode", "DerivedData")]},
)
CLEAN_WORKTREES = "worktrees"
CLEAN_EXTRA_NAMES = ("rust-build-output", "project-dependencies", "agent-leftovers", "browser-clones", "playwright", "finished-jobs")
CLEAN_RUST_BUSY = ("cargo", "rustc", "rust-analyzer", "clippy", "clippy-driver", "cargo-clippy")
CLEAN_NAMES = tuple(t["name"] for t in CLEAN_TOOLS) + tuple(f["name"] for f in CLEAN_FOLDERS) + (CLEAN_WORKTREES,) + CLEAN_EXTRA_NAMES

# ---- the ladder -----------------------------------------------------------------------------------------------------------------
# When the routine rules cannot reach the goal, the pass climbs these rungs one at a time and stops at the first that does. A rung
# widens one kind of window and never relaxes a proof that protects work or secrets: a starting or running job's folder, a folder a
# process uses, a protected name (.env*, .dev.vars*, .wrangler, .vercel), a release artifact, a primary clone's own source and
# a folder holding git state nobody proved saved all stay, whatever the rung. Only the last rung touches unsaved work, and only
# after a verified archive of it (bundles for commits, a hashed tar for files).
CLEAN_RUNGS = ("routine", "idle-repositories", "worktrees-and-finished-jobs", "pinned-folders", "unmarked-retention", "archive-then-remove")
CLEAN_STEADY = "steady"
CLEAN_STEADY_NAMES = ("rust-build-output", "project-dependencies", "finished-jobs")   # the quiet pass takes nothing else
CLEAN_POLICY = {"cleanEscalate": True, "cleanEscalateRepoHours": 2, "cleanEscalateWorktreeHours": 24, "cleanEscalateJobHours": 24,
                "cleanEscalatePinnedHours": 24, "cleanSteadyHours": 6, "cleanSteadyRepoDays": 3, "cleanSteadyJobHours": 48,
                "worktreeIdleDays": 7, "projectIdleHours": 48}   # the fallbacks of a host whose policy predates a key
CLEAN_OUTPUT_PROOF = ("clean", "pushed", "ignored", "untracked")
CLEAN_IGNORED_PROOF = ("ignored", "untracked")      # regenerable output needs git to ignore it and track none of it, not a clean tree
CLEAN_REFUSED_SECONDS = 86400    # a removal the filesystem refused is not retried for this long, unless the folder changed
CLEAN_REFUSED_PREFIX = "clean:refused:"
CLEAN_REFUSED_CAUSES = ("removal-error:", "removal-stopped:filesystem", "removal-stopped:foreign", "removal-stopped:mountpoint",
                        "filesystem-protected", "foreign-owner", "not-owned-by-user", "mountpoint", "protected-leaf-appeared", "nested-git-appeared")
CLEAN_JOB_REGENERABLE = re.compile(r"(?i)^(?:fixtures?|candidates?|deploy.*|release-?candidates?|.*-candidates?|candidate-.*|.*-fixtures?|fixtures?-.*|"
                                   r"checkouts?|wt|worktrees?|.*-wt|builds?|dist|out|outputs?|native-runs|runs|tmp|temp)$")
CLEAN_JOB_SECONDS = 120          # wall clock one rung's job sweep may spend, dry run or not
CLEAN_RELEASE_SUFFIXES = (".dmg", ".pkg", ".app", ".zip", ".tgz", ".tar.gz", ".tar.xz", ".xz", ".deb", ".rpm", ".msi", ".exe", ".appimage",
                          ".ipa", ".apk", ".whl", ".crate", ".vsix", ".nupkg")
CLEAN_WIDE_OUTPUTS = {"dist": ("package.json",), "build": ("package.json",)}   # from the first rung up: ignored, untracked, never a release


def clean_setting(policy: dict, key: str):
    value = policy.get(key, CLEAN_POLICY[key])
    return CLEAN_POLICY[key] if value is None else value


def clean_profile(policy: dict, rung: int | str) -> dict:
    """What one rung of the ladder (or the steady pass) lets the gates accept. Windows an operator turned off (0) stay off."""
    days, hours = clean_setting(policy, "worktreeIdleDays"), clean_setting(policy, "projectIdleHours")
    profile = {"rung": rung, "name": rung if isinstance(rung, str) else CLEAN_RUNGS[rung], "worktreeHours": days * 24, "projectHours": hours,
               "temp": dict(CLEAN_TEMP_IDLE_HOURS), "outputProof": CLEAN_OUTPUT_PROOF, "wide": False, "archive": False, "job": None, "tools": True}
    if rung == CLEAN_STEADY:
        steady = clean_setting(policy, "cleanSteadyRepoDays") * 24
        profile.update(projectHours=max(hours, steady) if hours else 0, tools=False,
                       job={"hours": clean_setting(policy, "cleanSteadyJobHours"), "filter": "all", "pinned": False, "clones": True, "keepUnsaved": False})
        return profile
    if rung >= 1:
        profile.update(projectHours=min(hours, clean_setting(policy, "cleanEscalateRepoHours")) if hours else 0,
                       temp={"shared": CLEAN_TEMP_ESCALATED_HOURS, "user": CLEAN_TEMP_ESCALATED_HOURS}, outputProof=CLEAN_IGNORED_PROOF,
                       wide=True, tools=False)
    if rung >= 2:
        profile["worktreeHours"] = min(profile["worktreeHours"], clean_setting(policy, "cleanEscalateWorktreeHours")) if days else 0
        profile["job"] = {"hours": clean_setting(policy, "cleanEscalateJobHours"), "filter": "regenerable", "pinned": False, "clones": True,
                          "keepUnsaved": True}
    if rung >= 3:
        profile["job"]["pinned"] = clean_setting(policy, "cleanEscalatePinnedHours")
    if rung >= 4:
        profile["job"]["filter"] = "all"
    if rung >= 5:
        profile["job"]["keepUnsaved"] = False
        profile["archive"] = True
    return profile


def clean_ladder(policy: dict, escalate: bool | None) -> list:
    on = clean_setting(policy, "cleanEscalate") if escalate is None else escalate
    return list(range(len(CLEAN_RUNGS))) if on else [0]


def clean_tilde(path: str) -> str:
    for base in {str(reap_home()), str(Path.home())}:
        if reap_inside(path, base):
            return "~" + path[len(base):]
    return path


def clean_free() -> int | None:
    disk = clean_disk()
    return disk["freeBytes"] if disk else None


def clean_os_refusal(error: OSError) -> str:
    """A candidate failure never aborts independent candidates or authorizes a retry."""
    return ("missing:" if isinstance(error, FileNotFoundError) else "unreadable:") + reap_named(error)


def clean_scan(path: str, label: str, errors: list) -> list:
    try:
        with os.scandir(path) as scan:
            return list(scan)
    except OSError as error:
        errors.append({"clean": label, "path": clean_tilde(str(path)), "error": clean_os_refusal(error)})
        return []


def clean_directory(entry, label: str, errors: list) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError as error:
        errors.append({"clean": label, "path": clean_tilde(entry.path), "error": clean_os_refusal(error)})
        return False


def clean_goals(policy: dict) -> tuple[float, float]:
    high, low = policy.get("cleanAtPercent", 85), policy.get("cleanToPercent", 80)  # a host's missing keys read as the engine's historical goals
    if high == 0 and not isinstance(high, bool):
        high = 85  # automatic off is not a refusal of an explicitly requested manual clean
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (high, low)) or not 0 < low < high <= 100:
        raise HostError("clean goals must satisfy 0 < cleanToPercent < cleanAtPercent <= 100")
    return high, low


def clean_disk() -> dict | None:
    try:
        return disk_state()
    except (OSError, HostError):
        return None


def clean_occupancy(disk: dict | None, low: float) -> dict:
    """Never infer exact capacity from the host's rounded df percentage."""
    used, free = (disk.get("usedBytes"), disk.get("freeBytes")) if disk else (None, None)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v < float("inf") for v in (used, free)) or used + free <= 0:
        return {"usedPercent": None, "remainingBytes": None, "goalMet": None}
    total = used + free
    excess = max(0, used * 100 - total * low)
    return {"usedPercent": used * 100 / total, "remainingBytes": int(-(-excess // 100)), "goalMet": excess == 0}


def clean_running() -> set:
    """Names of running programs: the first words of each command line that are not flags, as program names, so a script run
    by an interpreter counts as itself."""
    code, out = clean_run(["ps", "-axww", "-o", "pid=,command="], 30)
    if code != 0 or not out.strip():
        raise ReapRefused(f"probe-failed:ps:{code}")
    names, me = set(), str(os.getpid())
    for line in out.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid == me:
            continue
        for token in command.split()[:4]:  # `bash .../brew.sh`, `ruby -W1 .../brew.rb`, `node .../npm-cli.js`, `python -m pip`
            if token.startswith("-"):
                continue
            name = os.path.basename(token).lower()
            for suffix in (".js", ".cjs", ".mjs", ".py", ".sh", ".rb"):
                name = name.removesuffix(suffix)
            names.add(name.removesuffix("-cli"))
    return names


# ---- agent worktrees ------------------------------------------------------------------------------------------------

def clean_worktree_homes() -> list[str]:
    """Where agent tools keep worktrees outside the repository (worktrees inside it are found through `git worktree list`)."""
    configured = reap_worktree_homes()
    if configured is not None:
        return [str(path) for path in configured]
    codex = os.environ.get("CODEX_HOME")
    return [os.path.join(os.path.expanduser(codex), "worktrees") if codex else clean_home(".codex", "worktrees"),
            clean_home(".cursor", "worktrees"), clean_home("conductor", "workspaces"), clean_home(".superset", "worktrees"),
            clean_home(".claude-squad", "worktrees"), os.path.join(os.path.realpath(os.environ.get("TMPDIR") or "/tmp"), "vibe-kanban", "worktrees")]


def clean_pointer_target(dot_git: str) -> str | None:
    """The git dir a worktree's `.git` file names (`gitdir: <path>`), or None."""
    try:
        text = reap_read_pointer(dot_git)
    except (OSError, UnicodeError):
        return None
    if not text.startswith("gitdir:"):
        return None
    target = text[len("gitdir:"):].strip()
    return os.path.realpath(target if os.path.isabs(target) else os.path.join(os.path.dirname(dot_git), target))


def clean_main_repos(errors: list) -> list[str]:
    """Main checkouts: folders in the clone bases with a `.git` folder, and the repository of every worktree in an agent home."""
    found = set()
    for base, prefix in reap_clone_bases():
        try:
            entries = list(os.scandir(base))
        except FileNotFoundError:
            continue
        except OSError as error:
            errors.append({"clean": CLEAN_WORKTREES, "error": f"{clean_tilde(str(base))}: {reap_named(error)}"})
            continue
        for entry in entries:
            dot_git = os.path.join(entry.path, ".git")
            if (not prefix or entry.name.startswith(prefix)) and clean_directory(entry, CLEAN_WORKTREES, errors) \
                    and os.path.isdir(dot_git) and not os.path.islink(dot_git):
                found.add(reap_canon(entry.path))
    for top in clean_worktree_homes():
        stack = [(top, 0)]
        while stack:
            folder, depth = stack.pop()
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            dot_git = None
            for entry in entries:
                if entry.name == ".git":
                    try:
                        if entry.is_file(follow_symlinks=False):
                            dot_git = entry
                    except OSError as error:
                        errors.append({"clean": CLEAN_WORKTREES, "path": clean_tilde(entry.path), "error": clean_os_refusal(error)})
            if dot_git is not None:
                gitdir = clean_pointer_target(dot_git.path)
                common = os.path.dirname(os.path.dirname(gitdir)) if gitdir else ""
                if gitdir and os.path.basename(os.path.dirname(gitdir)) == "worktrees" and os.path.basename(common) == ".git":
                    found.add(reap_canon(os.path.dirname(common)))
                continue
            if depth < 3:
                stack.extend((e.path, depth + 1) for e in entries if clean_directory(e, CLEAN_WORKTREES, errors) and not reap_protected(e.name))
    return sorted(found)


def clean_linked_worktrees(repo: str) -> list[dict]:
    """`git worktree list --porcelain -z`, without the main checkout."""
    code, out = reap_git(repo, "worktree", "list", "--porcelain", "-z")
    if code != 0:
        raise ReapRefused(f"git-error:worktree-list:{code}")
    records, current = [], {}
    for field in out.split("\0"):
        if not field:
            if current:
                records.append(current)
            current = {}
            continue
        key, _, value = field.partition(" ")
        current[key] = value
    if current:
        records.append(current)
    return [r for r in records[1:] if "worktree" in r and "bare" not in r]


def clean_worktree_gitdir(repo: str, path: str) -> str | None:
    """The worktree's entry in its main repository (`.git/worktrees/<name>`), from its own pointer or, when the folder is
    gone, from the entry whose `gitdir` names it."""
    gitdir = clean_pointer_target(os.path.join(path, ".git")) if os.path.lexists(path) else None
    if gitdir:
        return gitdir
    base = os.path.join(repo, ".git", "worktrees")
    try:
        names = os.listdir(base)
    except OSError:
        return None
    for name in names:
        try:
            pointer = reap_read_pointer(os.path.join(base, name, "gitdir"))
        except (OSError, UnicodeError):
            continue
        if os.path.realpath(os.path.dirname(pointer)) == os.path.realpath(path):
            return os.path.join(base, name)
    return None


CLEAN_IDLE_WALK_ENTRIES = 50000   # entries one idle walk may stat
CLEAN_IDLE_WALK_SECONDS = 5.0     # wall clock one idle walk may spend
CLEAN_IDLE_SKIP = {".git", "target", "dist", "build", ".venv", "venv", "__pycache__", ".cache", ".pytest_cache", ".ruff_cache",
                   ".mypy_cache", "coverage", "gen"}
CLEAN_REFLOG_LINE = re.compile(r"^[0-9a-f]{40,64} [0-9a-f]{40,64} [^\t]*> (\d{1,12}) [+-]\d{4}(?:\t.*)?$")
CLEAN_IDLE_CACHE: dict = {}       # (path, gitdir) -> (computed-at monotonic, newest, reason); one pass reuses a walk


class CLEAN_IDLE_UNPROVED(Exception):
    """The idle walk could not prove when the folder last changed (over budget, unreadable, another device)."""


def clean_reflog_last(path: str) -> float | None:
    """The time written in the reflog's last complete entry, not the file's mtime: `git gc`, `reflog expire` and pack
    maintenance rewrite every worktree's reflog file, which made all of a repository's worktrees look worked on at once.
    Only a whole, well-formed last line with an integer timestamp counts; anything else is None (no evidence either way)."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 65536))
            data = handle.read()
    except OSError:
        return None
    if not data.endswith(b"\n"):
        return None  # a partial final write
    lines = data[:-1].split(b"\n")
    if size > 65536 and len(lines) < 2:
        return None  # the last line is longer than the window: never parse a cut-off line
    match = CLEAN_REFLOG_LINE.match(lines[-1].decode("utf-8", "replace"))
    if not match:
        return None
    stamp = int(match.group(1))
    return float(stamp) if 0 < stamp <= now() + 86400 else None


def clean_newest_change(path: str, gitdir: str | None, fresh: bool = False) -> float:
    """When someone last worked here: the newest of the HEAD file, the HEAD reflog's last entry and the later of mtime and
    ctime of every working-tree file and folder (ctime: a copy that preserves mtimes still counts as a change).

    Never the git index: every `git status` (an IDE, an agent, an audit, this reaper's own gates) rewrites it, so reading it
    made a month-old worktree look touched today and it was kept forever (2026-10-24: 117 abandoned worktrees read as
    `recent:0.0d`). Regenerable output folders, caches and protected names are skipped, links are never followed and the
    walk never leaves the folder's device. Raises CLEAN_IDLE_UNPROVED when the walk exceeds its entry or time budget, meets an
    unreadable entry or another device: nothing is proved idle then. A planning pass computes each folder once; `fresh` (the
    removal's own recheck) always walks again, so a change made after planning is never hidden by a cached answer."""
    key = (path, gitdir)
    cached = None if fresh else CLEAN_IDLE_CACHE.get(key)
    if cached and time.monotonic() - cached[0] < 300:
        if cached[2]:
            raise CLEAN_IDLE_UNPROVED(cached[2])
        return cached[1]
    try:
        newest = clean_newest_change_walk(path, gitdir)
    except CLEAN_IDLE_UNPROVED as unproved:
        CLEAN_IDLE_CACHE[key] = (time.monotonic(), None, str(unproved))
        raise
    CLEAN_IDLE_CACHE[key] = (time.monotonic(), newest, None)
    return newest


def clean_newest_change_walk(path: str, gitdir: str | None) -> float:
    stamps = []
    if gitdir:
        try:
            stamps.append(os.lstat(os.path.join(gitdir, "HEAD")).st_mtime)
        except OSError:
            pass
        last = clean_reflog_last(os.path.join(gitdir, "logs", "HEAD"))
        if last is not None:
            stamps.append(last)
    try:
        root = os.lstat(path)
    except OSError:
        raise CLEAN_IDLE_UNPROVED("idle-unreadable")
    deadline = time.monotonic() + CLEAN_IDLE_WALK_SECONDS
    seen, stack = 0, [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if reap_protected(entry.name) or entry.name in CLEAN_PROJECT_OUTPUTS or entry.name in CLEAN_IDLE_SKIP:
                        continue
                    seen += 1
                    if seen > CLEAN_IDLE_WALK_ENTRIES:
                        raise CLEAN_IDLE_UNPROVED(f"idle-budget-exceeded:{CLEAN_IDLE_WALK_ENTRIES}-entries")
                    if not seen % 256 and time.monotonic() > deadline:
                        raise CLEAN_IDLE_UNPROVED(f"idle-budget-time-exceeded:{CLEAN_IDLE_WALK_SECONDS:g}s")
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        raise CLEAN_IDLE_UNPROVED("idle-unreadable")
                    if info.st_dev != root.st_dev:
                        raise CLEAN_IDLE_UNPROVED("idle-mountpoint")
                    stamps.append(reap_scratch_changed(info))  # the later of mtime and ctime, as the scratch idle proof
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
        except OSError:
            raise CLEAN_IDLE_UNPROVED("idle-unreadable")
    return max(stamps) if stamps else root.st_mtime


def clean_idle_days(path: str, gitdir: str | None, fresh: bool = False) -> float:
    """Days since the folder last changed; ReapRefused naming why when that cannot be proved."""
    try:
        return (now() - clean_newest_change(path, gitdir, fresh)) / 86400
    except CLEAN_IDLE_UNPROVED as unproved:
        raise ReapRefused(str(unproved))


def clean_linked_identity(entry: dict) -> None:
    """The folder is still exactly the linked worktree its repository records: `.git` is a file naming
    `<repo>/.git/worktrees/<n>`, that entry's `gitdir` names this folder back, and git agrees its git dir is not the common
    one. A full repository (or anything else) now standing at an old worktree's path is never treated as a worktree."""
    path, gitdir = entry["path"], entry["gitdir"]
    common = os.path.join(entry["repo"], ".git")
    dot_git = os.path.join(path, ".git")
    try:
        is_file = os.path.isfile(dot_git) and not os.path.islink(dot_git)
        back = reap_read_pointer(os.path.join(gitdir, "gitdir")) if gitdir else ""
    except (OSError, UnicodeError):
        raise ReapRefused("not-a-linked-worktree:unreadable-pointer")
    if not is_file or not gitdir or clean_pointer_target(dot_git) != gitdir \
            or os.path.dirname(gitdir) != os.path.join(reap_canon(common), "worktrees") \
            or os.path.realpath(os.path.dirname(os.path.join(gitdir, back))) != path:  # a relative pointer is relative to the entry
        raise ReapRefused("not-a-linked-worktree")
    code, out = reap_git(path, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
    lines = out.split()
    if code != 0 or len(lines) != 2 or reap_canon(lines[0]) != gitdir or reap_canon(lines[1]) != reap_canon(common):
        raise ReapRefused("not-a-linked-worktree:git-disagrees")
    if os.path.lexists(os.path.join(gitdir, "locked")):
        raise ReapRefused("worktree-locked")


def clean_job_reaper_owns(path: str) -> bool:
    """The job reaper will find this folder: it is a top-level entry of a clone base (with that base's required prefix), the
    only place reap_clone_roots looks. A job-numbered folder anywhere else (`<repo>/.worktrees/J-0042`, `/tmp/j0042-base`, a
    scratch folder) belongs to no job pass, so deferring it to one kept it forever (2026-10-24: 60 such worktrees)."""
    parent, name = reap_canon(os.path.dirname(path)), os.path.basename(path)
    for base, prefix in reap_clone_bases():
        if reap_canon(str(base)) == parent and (not prefix or name.startswith(prefix)):
            return True
    return False


def clean_jobs_finished(ids: list, path: str) -> bool:
    """Every job the folder's own name gives finished cloneExpiryDays ago (the job reaper's clone rule, including later jobs
    that worked in it), and the job reaper's own blocker clears each one (settled, worker terminal gone, no live job sharing
    its terminal, nobody awaiting it), and no successor of it is still live. Read without raising: anything unreadable or
    unknown keeps the full idle window."""
    try:
        if not reap_clone_expired(ids, path):
            return False
        live_ids = reap_live_ids()
        if live_ids is None:
            return False
        with db() as conn:
            for job_id in ids:
                job = row(conn, "SELECT * FROM jobs WHERE id=?", (job_id,))
                if not job or reap_job_blocker(conn, job, live_ids) is not None:
                    return False
                if reap_chain_clock(conn, job) is None:  # a live (or unprovable) continuation or accepted successor
                    return False
        return True
    except Exception:
        return False


def clean_job_claims(path: str) -> None:
    """No unfinished job declares the folder, a folder inside it, or a folder around it (job add --cwd/--worktree)."""
    with db() as conn:
        for job in rows(conn, "SELECT * FROM jobs WHERE status NOT IN ('done','failed','cancelled')"):
            for declared in reap_declared_paths(job):
                if reap_inside(declared, path) or reap_inside(path, declared):
                    raise ReapRefused(f"declared-by-unfinished-job:{job['id']}")


def clean_worktree_gates(entry: dict, idle_days: float, table: dict | None, archive: bool = False) -> None:
    """Every reason to keep a worktree, cheapest first, the process probe last; raises ReapRefused naming the first.
    `table` None probes fresh. With `archive` (the ladder's last rung) work no remote holds (uncommitted files, commits only this
    worktree has, ignored data of unknown kind) does not keep the worktree: it is listed in `entry["unsaved"]` for a verified
    archive to save before anything is removed. Every other gate holds."""
    path = entry["path"]
    entry.pop("unsaved", None)
    if "locked" in entry["git"]:
        raise ReapRefused("worktree-locked")
    job_ids = reap_job_ids(*Path(path).parts)
    if job_ids and clean_job_reaper_owns(path):
        raise ReapRefused("job-folder:reaped-with-its-job")
    if any(reap_protected(part) for part in Path(path).parts):
        raise ReapRefused("protected-ancestor")
    reap_vet_root(os.path.dirname(path))  # the removal fences from the parent: never the home folder or a system folder
    retention = reap_cache_retention(path)
    if retention not in ("unmarked", REAP_DISPOSABLE):
        raise ReapRefused("retention:" + retention)
    # A worktree whose OWN folder name gives jobs that all finished cloneExpiryDays ago, settled with no open worker, no live
    # successor and nobody awaiting them, is finished work: it waits CLEAN_PROJECT_JOB_IDLE_HOURS of quiet instead of
    # worktreeIdleDays (every other gate below still holds). Ancestor names never shorten a wait; an unknown or live job keeps
    # the full window.
    idle = clean_idle_days(path, entry["gitdir"], fresh=table is None)  # table None: the removal's fresh recheck
    limit = idle_days
    own_ids = reap_job_ids(os.path.basename(path))
    if own_ids and clean_jobs_finished(own_ids, path):
        limit = min(idle_days, CLEAN_PROJECT_JOB_IDLE_HOURS / 24)
    if idle < limit:
        raise ReapRefused(f"recent:{idle:.1f}d-of-{limit:g}d")
    clean_job_claims(path)
    unsaved = []
    def unsaved_work(refusal: ReapRefused, kinds: tuple) -> None:
        if not archive or not str(refusal).startswith(kinds):
            raise refusal
        unsaved.append(str(refusal))
    repos, git_dirs = [], [] if archive else None  # archive mode: a git directory with no `.git` entry is saved whole, not a reason to keep
    reap_protected_scan(path, allow_git=True, markers=True, repos=repos, git_dirs=git_dirs, nested_disposable=True)
    if {reap_canon(r) for r in repos} != {path}:
        raise ReapRefused("nested-repository")
    if git_dirs:
        unsaved_work(ReapRefused("unproved-git-dir:" + sorted(set(git_dirs))[0]), ("unproved-git-dir:",))
    with reap_git_session(path):
        clean_linked_identity(entry)
        code, out = reap_git_clean_closed(path)  # index flags that hide edits (skip-worktree, assume-unchanged) refuse
        if code != 0:
            raise ReapRefused(f"git-error:status:{code}")
        if out.strip():
            unsaved_work(ReapRefused("dirty"), ("dirty",))
        try:
            reap_git_ignored_known(path)
        except ReapRefused as refusal:
            unsaved_work(refusal, ("ignored-unknown:",))
        try:
            clean_commits_saved(path, "HEAD")
        except ReapRefused as refusal:
            unsaved_work(refusal, ("unsaved-commits",))
    reap_holders(path, path, table)
    if unsaved:
        entry["unsaved"] = unsaved


def clean_commits_saved(repo: str, head: str) -> None:
    """Every commit `head` (a worktree's HEAD) points at or ever pointed at is on a branch, tag or remote, so nothing is lost
    when the worktree and its own HEAD reflog go. Its branch is never removed."""
    code, out = reap_git(repo, "rev-parse", "--verify", "--quiet", head + "^{commit}")
    if code != 0:
        raise ReapRefused(f"git-error:head:{code}")
    tips = [out.strip()]
    code, out = reap_git(repo, "reflog", "show", "--format=%H", head, "--")
    if code != 0:
        raise ReapRefused(f"git-error:reflog:{code}")  # an unreadable reflog may hold the only reference to a commit
    tips = list(dict.fromkeys(tips + out.split()))
    if len(tips) > REAP_ARCHIVE_REFLOG_TIPS:
        raise ReapRefused(f"reflog-too-long:{len(tips)}")
    code, out = reap_git(repo, "rev-list", "--max-count=1", *tips, "--not", "--branches", "--tags", "--remotes")
    if code != 0:
        raise ReapRefused(f"git-error:rev-list:{code}")
    if out.strip():
        raise ReapRefused("unsaved-commits:only-this-worktree-holds-them")


def clean_entry_gates(entry: dict) -> None:
    """A worktree whose folder is gone: the repository forgets it only when, like the engine's rule, the entry is
    REAP_WORKTREE_STALE_DAYS old, unlocked, its folder's parent is there (not an unmounted volume), and its commits are saved."""
    gitdir = entry["gitdir"]
    if os.path.lexists(entry["path"]):
        raise ReapRefused("folder-present")
    if "locked" in entry["git"] or not gitdir or os.path.lexists(os.path.join(gitdir, "locked")):
        raise ReapRefused("worktree-locked" if gitdir else "worktree-entry-unknown")
    if not os.path.isdir(os.path.dirname(entry["path"])):
        raise ReapRefused("folder-missing:parent-absent-maybe-unmounted")
    age = (now() - os.lstat(os.path.join(gitdir, "gitdir")).st_mtime) / 86400
    if age < REAP_WORKTREE_STALE_DAYS:
        raise ReapRefused(f"folder-missing:entry-{age:.0f}d-of-{REAP_WORKTREE_STALE_DAYS}d")
    clean_commits_saved(entry["repo"], f"worktrees/{os.path.basename(gitdir)}/HEAD")


def clean_folder_gates(path: str) -> None:
    """A cache folder with no cleaner of its own: no retention but `disposable`, no protected name, no nested repository."""
    # Check components before reading marker names: never traverse a protected or symlink ancestor.
    absolute = Path(os.path.abspath(path))
    for folder in reversed((absolute, *absolute.parents)):
        if reap_protected(folder.name):
            raise ReapRefused("protected-ancestor")
        if not stat.S_ISDIR(os.lstat(folder).st_mode):
            raise ReapRefused("non-directory-ancestor")
    home = reap_canon(reap_home())
    for folder in (absolute, *absolute.parents):
        retention = reap_cache_retention(str(folder))
        if retention not in ("unmarked", REAP_DISPOSABLE):
            raise ReapRefused("retention:" + retention)
        if str(folder) == home:
            break
    reap_protected_scan(path, markers=True)


def clean_listed(repo: str):
    try:
        return clean_linked_worktrees(repo)
    except ReapRefused as refusal:
        return refusal
    except OSError as error:
        return ReapRefused(clean_os_refusal(error))


def clean_worktree_entries(errors: list, skipped: list) -> list[dict]:
    entries, repos = [], clean_main_repos(errors)
    with ThreadPoolExecutor(max_workers=8) as pool:  # git-bound: one repository per worker
        found = list(pool.map(clean_listed, repos))
    for repo, records in zip(repos, found):
        if isinstance(records, ReapRefused):
            skipped.append({"repo": clean_tilde(repo), "reason": str(records)})
            continue
        for record in records:
            entry = {"name": CLEAN_WORKTREES, "kind": "worktree", "path": record["worktree"], "repo": repo, "git": record,
                     "branch": record.get("branch", "").removeprefix("refs/heads/") or None}
            try:
                path = entry["path"] = reap_canon(record["worktree"])
                entry["gitdir"] = clean_worktree_gitdir(repo, path)
                if not os.path.lexists(path):
                    entry["kind"] = "worktree-entry"  # only the repository's stale registration remains
            except OSError as error:
                entry["keep"] = clean_os_refusal(error)
            entries.append(entry)
    return entries


# ---- regenerable output and stale sessions ---------------------------------------------------------------------------

def clean_leftover_roots() -> list[tuple[str, bool]]:
    roots = [(f"/private/tmp/claude-{os.getuid()}", True)]
    if os.environ.get("TMPDIR"):
        roots.append((os.path.realpath(os.environ["TMPDIR"]), False))
    return roots


# Temp folders any tool may leave behind: a user's own folder in the shared temp folder after a day without a change, one in the per-user
# temp folder after three days. Names the system or a running app manages there are never candidates.
CLEAN_TEMP_IDLE_HOURS = {"shared": 24, "user": 72}
CLEAN_TEMP_ESCALATED_HOURS = 6   # from the first rung up
CLEAN_TEMP_SYSTEM = re.compile(r"(?i)^(?:\.|com\.apple\.|TemporaryItems$|.*\.(?:sock|socket|pid|lock)$|launchd|tmux-|ssh-|powerlog|"
                               r"CoreSimulator|xcrun_db|KSOutOfProcessFetcher)")


def clean_temp_roots(hours: dict | None = None) -> list[tuple[str, int]]:
    hours = hours or CLEAN_TEMP_IDLE_HOURS
    roots = []
    shared = os.path.realpath("/tmp")
    if os.path.isdir(shared):
        roots.append((shared, hours["shared"]))
    if os.environ.get("TMPDIR") and os.path.realpath(os.environ["TMPDIR"]) != shared:
        roots.append((os.path.realpath(os.environ["TMPDIR"]), hours["user"]))
    return roots


def clean_clone_roots() -> list[str]:
    """macOS code-sign clones: a browser copies its own app bundle into the per-user `X` folder at launch and removes the copy when it
    quits; a browser that is killed (automation, a crash) leaves its copy behind, one per launch, and an updated browser's old copies
    hold the old version's blocks. Every `<bundle id>.code_sign_clone` folder there."""
    tmp = os.environ.get("TMPDIR")
    if not tmp or os.uname().sysname != "Darwin":
        return []
    base = os.path.join(os.path.dirname(os.path.realpath(tmp).rstrip("/")), "X")
    try:
        return sorted(e.path for e in os.scandir(base) if e.name.endswith(".code_sign_clone") and e.is_dir(follow_symlinks=False))
    except OSError:
        return []


CLEAN_MEMO = threading.local()   # what one pass learned about folders that its next rung would learn again (data: None outside a pass)


def clean_memo_get(key):
    data = getattr(CLEAN_MEMO, "data", None)
    return data.get(key) if data is not None else None


def clean_memo_put(key, value) -> None:
    data = getattr(CLEAN_MEMO, "data", None)
    if data is not None:
        data[key] = value


def clean_once(key, check, fresh: bool) -> None:
    """Run `check()` (which raises ReapRefused to keep a folder) once per pass for a rung-independent proof, and replay its verdict on the
    next rung. The removal's own last look (`fresh`) always runs it again."""
    verdict = None if fresh else clean_memo_get(key)
    if verdict is None:
        try:
            check()
            verdict = ""
        except ReapRefused as refusal:
            verdict = str(refusal)
        if not fresh:
            clean_memo_put(key, verdict)
    if verdict:
        raise ReapRefused(verdict)


def clean_tree_walk(path: str, floor: float) -> tuple:
    """Walk a tree once, metadata only, never following a link: ("refused", reason) for what no window changes (another owner, too many
    entries or levels), ("recent", newest) at the first entry changed within `floor` hours, else ("idle", newest, acl) with the newest change
    of the whole tree and the first entry whose ACL denies delete (None when no entry does)."""
    cutoff, count, newest = now() - floor * 3600, 0, 0.0
    stack = [(path, 0)]
    while stack:
        folder, depth = stack.pop()
        if depth > REAP_MAX_DEPTH:
            return ("refused", "idle-scan-incomplete")
        info = os.lstat(folder)
        if info.st_uid != os.getuid():
            return ("refused", "not-owned-by-user")
        if info.st_mtime > cutoff:
            return ("recent", info.st_mtime)
        newest = max(newest, info.st_mtime)
        with os.scandir(folder) as entries:
            for entry in entries:
                count += 1
                if count > REAP_WALK_ENTRIES:
                    return ("refused", "idle-scan-incomplete")
                info = entry.stat(follow_symlinks=False)
                if info.st_uid != os.getuid():
                    return ("refused", "not-owned-by-user")
                if info.st_mtime > cutoff:
                    return ("recent", info.st_mtime)
                newest = max(newest, info.st_mtime)
                if entry.is_dir(follow_symlinks=False):
                    stack.append((entry.path, depth + 1))
    return ("idle", newest, reap_acl_denies(path))


def clean_stale_tree(path: str, hours: int = 6, fresh: bool = True) -> None:
    """Every entry must be `hours` idle; bounded metadata-only walk, never follow links. A tree holding an entry whose ACL denies
    delete is kept and named (`filesystem-protected-acl:<entry>`): it cannot be removed, and trying every pass only fills the receipts.
    A planning pass walks each tree once however many rungs it climbs (`fresh`, the removal's own last look, always walks again)."""
    key = ("tree", path)
    known = None if fresh else clean_memo_get(key)
    if known is not None and (known[1][0] != "recent" or hours >= known[0]):
        found = known[1]  # a walk that stopped at a change newer than `known[0]` hours answers every window at least that long
    else:
        found = clean_tree_walk(path, hours)
        if not fresh:
            clean_memo_put(key, [hours, found])
    if found[0] == "refused":
        raise ReapRefused(found[1])
    if found[0] == "recent" or found[1] > now() - hours * 3600:
        raise ReapRefused(f"recent:less-than-{hours}h")
    if found[2]:
        raise ReapRefused("filesystem-protected-acl:" + found[2][:80])


def clean_browser_builds(root: str, errors: list | None = None) -> list[tuple[str, str, int]]:
    found = []
    errors = errors if errors is not None else []
    if not os.path.isdir(root) or os.path.islink(root):
        return found
    for entry in clean_scan(root, "playwright", errors):
        match = re.fullmatch(r"([a-zA-Z][a-zA-Z0-9_-]*)-([0-9]+)", entry.name)
        if match and clean_directory(entry, "playwright", errors):
            found.append((entry.path, match[1], int(match[2])))
    return found


# Folders that never hold a Rust crate's target: dependency stores, virtual environments, generated web output and tool caches.
# (A walk of the dev and .cache homes listed 2.3 million entries, mostly these, and gave up at the 500,000-entry budget.)
CLEAN_RUST_SCAN_SKIP = frozenset({".git", "node_modules", "Library", ".cache", ".local", ".Trash", "dist", "build", "vendor", ".venv", "venv", "__pycache__",
                            "site-packages", ".next", ".astro", ".turbo", ".wrangler", ".svelte-kit", ".vercel", ".output", "coverage", ".pnpm-store",
                            "Pods", "DerivedData", ".gradle", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "screenshots"})
CLEAN_RUST_SCAN_TOOL_CACHES = frozenset({"uv", "pip", "npm", "pnpm", "yarn", "bun", "cargo", "go-build", "huggingface", "codex-runtimes", "ms-playwright",
                                   "puppeteer", "node-gyp", "typescript", "prisma", "turbo", "pre-commit", "deno", "electron", "selenium"})
CLEAN_RUST_SCAN_REPO_SEARCH_DEPTH = 4
# Names only (one scandir per folder, no stat of files): about 10 s per million entries. Its own budget, larger than the engine's
# deletion-proof walk (REAP_WALK_ENTRIES), because finding a target deletes nothing; each target is still proved before removal.
CLEAN_RUST_SCAN_ENTRIES = 3_000_000


def clean_rust_targets(errors: list) -> list[tuple[str, str]]:
    """Find targets in repositories, including nested crates, without entering generated trees."""
    return [(path, repo) for path, repo in clean_repo_walk(errors, frozenset({"target"}), "rust-build-output")]


# Dependency stores and generated output a project recreates with one install or build. Each is removed only when git ignores it,
# its manifest is beside it, and the project is idle (clean_project_gates). `dist` and `build` are never here: some projects commit them.
CLEAN_PROJECT_OUTPUTS = {"node_modules": ("package.json",), ".next": ("package.json",), ".astro": ("package.json",), ".turbo": ("package.json",),
                         ".svelte-kit": ("package.json",), ".nuxt": ("package.json",), ".parcel-cache": ("package.json",), ".vite": ("package.json",),
                         ".venv": ("pyproject.toml", "requirements.txt", "requirements-dev.txt", "uv.lock", "Pipfile", "setup.py"),
                         ".pytest_cache": (), ".mypy_cache": (), ".ruff_cache": ()}


CLEAN_PROJECT_JOB_IDLE_HOURS = 6
CLEAN_CODE_HOMES = ("dev", "Developer", "src", "code", "projects", "repos", "work", ".cache")


def clean_project_outputs(errors: list, wide: bool = False) -> list[tuple[str, str]]:
    """Every regenerable dependency or output folder inside a repository in the code homes, with the repository it belongs to.
    `wide` (the ladder's rungs) adds `dist` and `build`, which a project may commit: they go only when git ignores and tracks none
    of them, a manifest sits beside them and they hold no release artifact."""
    outputs = {**CLEAN_PROJECT_OUTPUTS, **(CLEAN_WIDE_OUTPUTS if wide else {})}
    found = []
    for path, repo in clean_repo_walk(errors, frozenset(outputs), "project-dependencies"):
        manifests = outputs[os.path.basename(path)]
        if not manifests or any(os.path.isfile(os.path.join(os.path.dirname(path), m)) for m in manifests):
            found.append((path, repo))
    return found


def clean_release_like(path: str) -> str | None:
    """The first release artifact (an installer, a disk image, a package, an archive) a folder holds within three levels, else None.
    A scan that cannot finish names itself: `dist` and `build` hold releases for some projects, so they stay unless it is shown
    they do not."""
    count, stack = 0, [(path, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            with os.scandir(folder) as scan:
                for entry in scan:
                    count += 1
                    if count > 20000:
                        return "scan-incomplete"
                    if entry.name.lower().endswith(CLEAN_RELEASE_SUFFIXES):
                        return entry.name[:80]
                    if depth < 3 and entry.is_dir(follow_symlinks=False):
                        stack.append((entry.path, depth + 1))
        except OSError:
            return "unreadable"
    return None


def clean_repo_walk(errors: list, wanted: frozenset, label: str) -> list[tuple[str, str]]:
    """Folders named in `wanted` inside repositories, without entering them or any generated tree. One pass discovers them once,
    however many rungs it climbs; a folder that is gone by the next rung drops out."""
    key = ("walk", tuple(sorted(wanted)))
    known = clean_memo_get(key)
    if known is not None:
        return [(path, repo) for path, repo in known if os.path.lexists(path)]
    found = clean_repo_walk_scan(errors, wanted, label)
    clean_memo_put(key, found)
    return found


def clean_repo_walk_scan(errors: list, wanted: frozenset, label: str) -> list[tuple[str, str]]:
    # The usual code and build homes, never the whole home: a home-wide walk ran out of its entry budget on a real machine
    # (discovery-incomplete) and found nothing. Clone bases and worktree homes add the host's own layout.
    # (Temp-folder build roots are the agent-leftovers target's job: the temp folder holds unreadable system folders.)
    homes = [clean_home(name) for name in CLEAN_CODE_HOMES]
    roots = {*homes, *(str(base) for base, _ in reap_clone_bases()), *clean_worktree_homes()}
    # Linked worktrees may live outside the home and configured clone bases.
    roots.update(entry["path"] for entry in clean_worktree_entries(errors, []) if entry["kind"] == "worktree")
    found, seen, count = set(), set(), 0
    # Breadth-first, so shallow repositories are found before any budget is spent deep inside one tree.
    stack, head = [(root, False, 0, "") for root in sorted(roots)], 0
    while head < len(stack):
        folder, in_repo, depth, repo = stack[head]
        head += 1
        if folder in seen or os.path.islink(folder) or not os.path.isdir(folder):
            continue
        seen.add(folder)
        if depth > REAP_MAX_DEPTH or any(reap_protected(part) for part in Path(folder).parts):
            continue
        try:
            with os.scandir(folder) as scan:
                entries = list(scan)
            count += len(entries)
            if count > CLEAN_RUST_SCAN_ENTRIES:
                errors.append({"clean": label, "error": f"discovery-incomplete: more than {CLEAN_RUST_SCAN_ENTRIES} entries"})
                break
            if any(e.name == ".git" and not e.is_symlink() for e in entries):
                in_repo, repo = True, folder
            for entry in entries:
                if not clean_directory(entry, label, errors) or reap_protected(entry.name):
                    continue
                if entry.name in wanted:
                    if in_repo:
                        found.add((entry.path, repo))
                    continue
                if entry.name == "target":
                    continue
                if entry.name in CLEAN_RUST_SCAN_SKIP or (folder in homes and entry.name in CLEAN_RUST_SCAN_TOOL_CACHES):
                    continue
                # Outside a repository only the first few levels are searched for repositories: a target lives in a repository.
                if in_repo or depth < CLEAN_RUST_SCAN_REPO_SEARCH_DEPTH:
                    stack.append((entry.path, in_repo, depth + 1, repo))
        except OSError as error:
            errors.append({"clean": label, "error": clean_tilde(folder) + ":" + reap_named(error)})
    return sorted(found)


def clean_rust_lock_paths(target: str, repo: str) -> list[Path]:
    # Include root locks as well as Cargo's profile/triple locks; the engine scan is bounded.
    return [reap_queue_lock(repo), Path(target, ".cargo-lock"), Path(target, ".cargo-build-lock"), *reap_cargo_locks(target)]


def clean_repository_output_gates(entry: dict, table: dict | None) -> float:
    """Shared repository, ownership and process proof for regenerable project output; return its idle limit."""
    path, repo = entry["path"], entry["repo"]
    if path == repo or not reap_inside(path, repo):
        raise ReapRefused("outside-repository")
    proof = tuple(entry.get("proof") or CLEAN_OUTPUT_PROOF)
    def static():  # nothing here depends on how long the project has been quiet, so one pass proves it once
        clean_folder_gates(path)
        if os.path.basename(path) in CLEAN_WIDE_OUTPUTS:
            found = clean_release_like(path)
            if found:
                raise ReapRefused("release-artifact:" + found)
        reap_git_proof(repo, path, proof)
    clean_once(("output", path, proof), static, table is None)
    # A copy made for a job (site-j0042, .worktrees/J-0042): kept while that job is unfinished, and once it is finished its output
    # needs only CLEAN_PROJECT_JOB_IDLE_HOURS of quiet. Every other project needs the operator's idleHours.
    # Only a job with a live worker (starting or running) protects regenerable output; a parked, blocked or queued job's project still
    # waits the full idleHours. A collection-root cwd names no project; explicit worktree declarations still protect it.
    limit = entry.get("idleHours", 0)
    explicit_named = [f"J-{int(n):04d}" for n in re.findall(r"(?:^|/)J-(\d{3,5})(?=/|$)", repo)]
    named = list(dict.fromkeys(reap_job_ids(*Path(repo).parts) + explicit_named))
    # Ancestor job names protect live work, but do not shorten the existing idle window.
    idle_named = list(dict.fromkeys(reap_job_ids(*Path(repo).parts[-2:]) + explicit_named))
    homes = {reap_canon(h) for h in [clean_home(), *(clean_home(n) for n in CLEAN_CODE_HOMES)] if os.path.isdir(h)}
    with db() as conn:
        unfinished = rows(conn, "SELECT * FROM jobs WHERE status NOT IN ('done','failed','cancelled')")
        named_states = {r["status"] for r in rows(conn, "SELECT status FROM jobs WHERE id IN (%s)" % ",".join("?" * len(idle_named)), tuple(idle_named))} if idle_named else set()
    for job in unfinished:
        live = job["status"] in ("starting", "running")
        if job["id"] in named:
            if live:
                raise ReapRefused(f"in-use-by-running-job:{job['id']}")
            continue
        # Only a collection-root cwd is nonspecific. Explicit worktrees remain concrete claims,
        # even when they happen to name a collection root too.
        claim = dict(job)
        if claim.get("cwd") and reap_canon(Path(claim["cwd"]).expanduser()) in homes:
            claim["cwd"] = None
        for declared in reap_declared_paths(claim):
            if live and (reap_inside(declared, repo) or reap_inside(repo, declared)):
                raise ReapRefused(f"in-use-by-running-job:{job['id']}")
    if named_states and not named_states - {"done", "failed", "cancelled"}:
        limit = min(limit, CLEAN_PROJECT_JOB_IDLE_HOURS)
    reap_holders(repo, repo, table)
    return limit


def clean_repo_idle_hours(repo: str, fresh: bool) -> float:
    dot_git = os.path.join(repo, ".git")
    gitdir = clean_pointer_target(dot_git) if os.path.isfile(dot_git) else dot_git
    return clean_idle_days(repo, gitdir, fresh=fresh) * 24


def clean_project_gates(entry: dict, table: dict | None) -> None:
    """Dependency output additionally waits for the existing project idle policy."""
    if "idleHours" not in entry:
        raise ReapRefused("project-idle-policy-unproved")
    limit = clean_repository_output_gates(entry, table)
    idle = clean_repo_idle_hours(entry["repo"], table is None)
    if idle < limit:
        raise ReapRefused(f"recent:{idle:.0f}h-of-{limit}h")


def clean_extra_gates(entry: dict, table: dict | None = None) -> None:
    path, kind = entry["path"], entry["name"]
    if kind == "project-dependencies":
        clean_project_gates(entry, table)
    elif kind != "rust-build-output":
        clean_once(("folder", path), lambda: clean_folder_gates(path), table is None)
    if kind == "project-dependencies":
        pass
    elif kind == "rust-build-output":
        clean_repository_output_gates(entry, table)
        if "clean" not in (entry.get("proof") or CLEAN_OUTPUT_PROOF) and entry.get("idleHours"):  # a dirty or stashed repo: only an idle one
            idle = clean_repo_idle_hours(entry["repo"], table is None)
            if idle < entry["idleHours"]:
                raise ReapRefused(f"recent:{idle:.0f}h-of-{entry['idleHours']}h")
        busy = sorted(set(CLEAN_RUST_BUSY) & clean_running())
        if busy:
            raise ReapRefused("in-use:" + ",".join(busy))
        clean_once(("folder", entry["target"]), lambda: clean_folder_gates(entry["target"]), table is None)
    elif kind == "agent-leftovers":
        clean_stale_tree(path, entry.get("idleHours", 6), fresh=table is None)
    elif kind == "browser-clones":
        info = os.lstat(path)
        if info.st_uid != os.getuid():
            raise ReapRefused("not-owned-by-user")
        if now() - info.st_mtime < 3600:
            raise ReapRefused("recent:less-than-1h")
    else:
        builds = clean_browser_builds(entry["root"])
        if not any(browser == entry["browser"] and build > entry["build"] for _, browser, build in builds):
            raise ReapRefused("newest-browser-build")
    reap_holders(entry.get("target", path), entry.get("target", path), table)


def clean_extra_entries(only: set, errors: list, idle_hours: int = 48, rung: dict | None = None) -> list[dict]:
    """`rung` is a ladder profile (clean_profile); without one the routine rules apply."""
    entries = []
    proof = tuple(rung["outputProof"]) if rung else CLEAN_OUTPUT_PROOF
    wide = bool(rung and rung["wide"])
    temp_hours = rung["temp"]["user"] if rung and rung["rung"] != 0 and rung["name"] != CLEAN_STEADY else None
    def add(name, path, root, **extra):
        entries.append({"name": name, "kind": "debris", "path": path, "root": root, "paths": [path], "tier": "safe", **extra})
    if "rust-build-output" in only:
        relaxed = {"proof": proof, "idleHours": idle_hours} if proof != CLEAN_OUTPUT_PROOF else {}
        for target, repo in clean_rust_targets(errors):
            debug = os.path.join(target, "debug")
            if os.path.isdir(debug) and not os.path.islink(debug):
                add("rust-build-output", debug, target, target=target, repo=repo, **relaxed)
            for profile in clean_scan(target, "rust-build-output", errors):
                path = os.path.join(profile.path, "incremental")
                if profile.name != "debug" and clean_directory(profile, "rust-build-output", errors) and not reap_protected(profile.name) \
                        and os.path.isdir(path) and not os.path.islink(path):
                    add("rust-build-output", path, target, target=target, repo=repo, **relaxed)
    if "project-dependencies" in only and idle_hours:
        for path, repo in clean_project_outputs(errors, wide):
            add("project-dependencies", path, os.path.dirname(path), repo=repo, idleHours=idle_hours, proof=proof)
    if "agent-leftovers" in only:
        for root, sessions in clean_leftover_roots():
            try:
                if not os.path.isdir(root) or os.path.islink(root) or os.lstat(root).st_uid != os.getuid():
                    continue
            except OSError as error:
                errors.append({"clean": "agent-leftovers", "path": clean_tilde(root), "error": clean_os_refusal(error)})
                continue
            for child in clean_scan(root, "agent-leftovers", errors):
                matches = sessions or bool(re.fullmatch(r"(?:.+-verify-.+|ultraterm-release\..+|ultraterm-publish-check\..+|.*-rust-target.*)", child.name))
                if matches and clean_directory(child, "agent-leftovers", errors) and not reap_protected(child.name):
                    add("agent-leftovers", child.path, root)
        seen = {e["path"] for e in entries}
        sessions_root = os.path.realpath(f"/private/tmp/claude-{os.getuid()}")
        for root, hours in clean_temp_roots():
            hours = min(hours, temp_hours) if temp_hours else hours
            for child in clean_scan(root, "agent-leftovers", errors):
                if child.path in seen or os.path.realpath(child.path) == sessions_root or CLEAN_TEMP_SYSTEM.match(child.name) \
                        or reap_protected(child.name) or not clean_directory(child, "agent-leftovers", errors):
                    continue
                try:
                    if child.stat(follow_symlinks=False).st_uid != os.getuid():
                        continue
                except OSError as error:
                    errors.append({"clean": "agent-leftovers", "path": clean_tilde(child.path), "error": clean_os_refusal(error)})
                    continue
                add("agent-leftovers", child.path, root, idleHours=hours)
    if "browser-clones" in only:
        for root in clean_clone_roots():
            for clone in clean_scan(root, "browser-clones", errors):
                if clone.name.startswith("code_sign_clone.") and clean_directory(clone, "browser-clones", errors):
                    add("browser-clones", clone.path, root)
    if "playwright" in only:
        root = clean_home("Library", "Caches", "ms-playwright")
        for path, browser, build in clean_browser_builds(root, errors):
            add("playwright", path, root, browser=browser, build=build)
    table = reap_process_table() if entries else None
    for entry in entries:
        try:
            with ExitStack() as stack:
                if entry["name"] in ("rust-build-output", "project-dependencies"):
                    locks = clean_rust_lock_paths(entry["target"], entry["repo"]) if entry["name"] == "rust-build-output" else [reap_queue_lock(entry["repo"])]
                    stack.enter_context(reap_locks(locks, create=False))
                clean_extra_gates(entry, table)
        except (ReapRefused, OSError) as error:
            entry["keep"] = str(error) if isinstance(error, ReapRefused) else clean_os_refusal(error)
    return entries


# ---- refusals the filesystem makes -------------------------------------------------------------------------------------------------
# A folder whose removal the filesystem refuses (an ACL that denies delete, immutable flags, another owner) is not retried every pass:
# the refusal is remembered for CLEAN_REFUSED_SECONDS, or until the folder is a different object, and the folder is listed as kept with
# the cause. Before this, 60 such temp folders were attempted, receipted and logged as a bare "refused" error on every tick.

def clean_refusal_key(path: str) -> str:
    return CLEAN_REFUSED_PREFIX + hashlib.sha256(path.encode("utf-8", "surrogateescape")).hexdigest()[:24]


def clean_identity(path: str) -> list | None:
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return [info.st_dev, info.st_ino, info.st_mtime_ns]


def clean_apply_refusals(entries: list[dict]) -> None:
    """Keep every candidate whose removal the filesystem refused recently, naming the cause. Memory that cannot be read changes nothing."""
    try:
        with db() as conn:
            stored = conn.execute("SELECT key, value FROM meta WHERE key LIKE ?", (CLEAN_REFUSED_PREFIX + "%",)).fetchall()
    except (sqlite3.Error, OSError, HostError):
        return
    known = {}
    for key, value in stored:
        try:
            known[key] = json.loads(value)
        except ValueError:
            continue
    for entry in entries:
        value = known.get(clean_refusal_key(entry["path"])) if entry.get("path") and "keep" not in entry else None
        if isinstance(value, dict) and now() - float(value.get("at") or 0) < CLEAN_REFUSED_SECONDS and value.get("identity") == clean_identity(entry["path"]):
            entry["keep"] = "refused-before:" + str(value.get("reason"))[:160]


def clean_remember_refusal(entry: dict, reason: str) -> None:
    if not entry.get("path") or not reason.startswith(CLEAN_REFUSED_CAUSES):
        return
    try:
        with db() as conn:
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (clean_refusal_key(entry["path"]), json.dumps(
                {"at": now(), "reason": reason[:200], "identity": clean_identity(entry["path"]), "path": entry["path"]})))
    except (sqlite3.Error, OSError, HostError):
        pass


def clean_forget_refusals() -> None:
    """Drop memory older than CLEAN_REFUSED_SECONDS, so the table does not grow with every folder that ever refused."""
    try:
        with db() as conn:
            for key, value in conn.execute("SELECT key, value FROM meta WHERE key LIKE ?", (CLEAN_REFUSED_PREFIX + "%",)).fetchall():
                try:
                    at = float(json.loads(value).get("at") or 0)
                except (ValueError, AttributeError, TypeError):
                    at = 0
                if now() - at >= CLEAN_REFUSED_SECONDS:
                    conn.execute("DELETE FROM meta WHERE key=?", (key,))
    except (sqlite3.Error, OSError, HostError):
        pass


# ---- the plan -------------------------------------------------------------------------------------------------------

def clean_plan(only: set, deep: bool, errors: list, skipped: list, days: float, hours: int = 48, rung: dict | None = None) -> list[dict]:
    """The candidates of one rung (`rung`: a clean_profile; none means the routine rules with `days` and `hours`)."""
    names = clean_running()
    def probe_tool(spec):
        entry = {"name": spec["name"], "kind": "tool-cache", "command": " ".join(spec["argv"]), "spec": spec,
                 "paths": [], "tier": "deep" if spec.get("deep") else "safe"}
        busy = sorted(set(spec["busy"]) & names)
        missing = clean_installed(spec["argv"][0])
        if spec.get("deep") and not deep:
            entry["keep"] = "deep-only:add---deep"
        elif missing:
            entry["keep"] = missing
        elif busy:
            entry["keep"] = "in-use:" + ",".join(busy)
        elif spec.get("ready"):
            not_ready = spec["ready"]()
            if not_ready:
                entry["keep"] = not_ready
        if not missing and spec.get("where"):
            entry["paths"] = [p for p in spec["where"]() if os.path.isdir(p)]
        return entry
    def tool_entry(spec):
        try:
            return probe_tool(spec)
        except OSError as error:
            return {"name": spec["name"], "kind": "tool-cache", "paths": [], "tier": "deep" if spec.get("deep") else "safe",
                    "keep": clean_os_refusal(error)}
    with ThreadPoolExecutor(max_workers=8) as pool:  # each probe runs the tool once (`uv cache dir` ...)
        entries = list(pool.map(tool_entry, [spec for spec in CLEAN_TOOLS if spec["name"] in only]))
    for spec in CLEAN_FOLDERS:
        if spec["name"] not in only:
            continue
        busy = sorted(set(spec["busy"]) & names)
        for root in spec["roots"]():
            if not os.path.isdir(root) or os.path.islink(root):
                continue
            try:
                root = reap_canon(root)
            except OSError as error:
                errors.append({"clean": spec["name"], "path": clean_tilde(root), "error": clean_os_refusal(error)})
                continue
            for child in sorted(clean_scan(root, spec["name"], errors), key=lambda c: c.name):
                entry = {"name": spec["name"], "kind": "folder-cache", "path": child.path, "root": root, "paths": [child.path], "tier": "safe"}
                try:
                    if not child.is_dir(follow_symlinks=False):
                        continue
                    if busy:
                        raise ReapRefused("in-use:" + ",".join(busy))
                    clean_folder_gates(child.path)
                except ReapRefused as refusal:
                    entry["keep"] = str(refusal)
                except OSError as error:
                    entry["keep"] = clean_os_refusal(error)
                entries.append(entry)
    if CLEAN_WORKTREES in only:
        table = None
        for entry in clean_worktree_entries(errors, skipped):
            entry["tier"], entry["paths"] = "safe", [entry["path"]] if entry["kind"] == "worktree" else []
            entry["idleDays"], entry["archiveMode"] = days, bool(rung and rung["archive"])
            try:
                if "keep" in entry:
                    entries.append(entry)
                    continue
                if not days:
                    raise ReapRefused("off:worktreeIdleDays=0")
                if entry["kind"] == "worktree-entry":
                    clean_entry_gates(entry)
                else:
                    table = table or reap_process_table()  # one snapshot for the plan; removal probes fresh
                    clean_worktree_gates(entry, days, table, entry["archiveMode"])
            except ReapRefused as refusal:
                entry["keep"] = str(refusal)
            except OSError as error:
                entry["keep"] = clean_os_refusal(error)
            entries.append(entry)
    entries.extend(clean_extra_entries(only, errors, hours, rung))
    clean_apply_refusals(entries)
    return entries


def clean_size_all(entries: list[dict]) -> None:
    jobs = [(entry, path) for entry in entries for path in entry["paths"]]
    def size_one(job):
        try:
            return reap_metadata_size(job[1], CLEAN_SIZE_SECONDS)
        except OSError as error:
            return {"bytes": None, "complete": False, "reason": clean_os_refusal(error)}
    with ThreadPoolExecutor(max_workers=16) as pool:
        sized = list(pool.map(size_one, jobs))
    for entry in entries:
        entry["bytes"], entry["sizeComplete"] = 0, True
    for (entry, _), size in zip(jobs, sized):
        entry["bytes"] += size["bytes"] or 0
        entry["sizeComplete"] = entry["sizeComplete"] and size["complete"]
        if not size["complete"]:
            reason = size.get("reason") or "size-incomplete"
            if reason.startswith("size-error:"):
                reason = ("missing:" if "FileNotFoundError" in reason else "unreadable:") + reason
            entry.setdefault("keep", reason)


# ---- removal --------------------------------------------------------------------------------------------------------

def clean_receipt(phase: str, entry: dict, run_id: str, **extra) -> None:
    reap_receipt({"phase": phase, "run": run_id, "class": "clean-" + entry["kind"], "name": entry["name"],
                       "path": entry.get("path"), "command": entry.get("command"), **extra})


def clean_run_tool(entry: dict, run_id: str) -> None:
    busy = sorted(set(entry["spec"]["busy"]) & clean_running())  # fresh, right before it runs
    if busy:
        entry["keep"] = "in-use:" + ",".join(busy)
        return
    if entry["name"].startswith("docker-"):
        reason = clean_docker_ready()
        if reason:
            entry["keep"] = reason
            return
    clean_receipt("plan", entry, run_id)
    before = clean_free()
    try:
        done = subprocess.run(entry["spec"]["argv"], stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                              timeout=CLEAN_TOOL_SECONDS, env={**os.environ, **CLEAN_TOOL_ENV}, cwd=clean_home())
        last = (done.stderr.strip() or done.stdout.strip()).splitlines()[-1:] or [""]
        outcome = "cleaned" if done.returncode == 0 else f"failed:exit-{done.returncode}:{last[0][:200]}"
    except subprocess.TimeoutExpired:
        outcome = f"failed:timeout-{CLEAN_TOOL_SECONDS}s"
    except OSError as error:
        outcome = "failed:" + reap_named(error)
    if entry["name"] == "docker-build-cache" and outcome == "cleaned":
        trim = clean_colima_trim()
        if trim is not None:
            entry["colimaTrim"] = "trimmed" if trim[0] == 0 else f"failed:exit-{trim[0]}"
    after = clean_free()
    entry["outcome"], entry["dfDeltaBytes"] = outcome, (after - before) if None not in (before, after) else None
    clean_receipt("result", entry, run_id, outcome=outcome, dfDeltaBytes=entry["dfDeltaBytes"], colimaTrim=entry.get("colimaTrim"))


def clean_remove_folder(entry: dict, run_id: str, root: str, allow_git: bool, last_look) -> None:
    """The engine's guarded removal of one folder below `root`: fence, identities of the protected names beside it, a durable
    plan, the last look, the descriptor-relative removal, and a result receipt. Raises ReapRefused before the plan."""
    path = entry["path"]
    reap_vet_root(root)
    if path == root or not reap_inside(path, root):
        raise ReapRefused("outside-root")
    fence = reap_fence(path, root)
    before = [reap_identity(leaf) for leaf in reap_preserved_leaves(path, root)]
    clean_receipt("plan", entry, run_id, bytes=entry.get("bytes"))
    result = {"checks": []}
    free_before = clean_free()
    try:
        last_look()
    except (ReapRefused, OSError) as refusal:
        reason = str(refusal) if isinstance(refusal, ReapRefused) else clean_os_refusal(refusal)
        clean_receipt("result", entry, run_id, outcome="refused", reason=reason)
        raise ReapRefused(reason)
    reap_remove_and_verify(fence, path, before, result, allow_git)
    if result["outcome"] == "deleted" and entry["kind"] == "worktree":
        result["checks"].append(reap_worktree_forget(os.path.join(entry["repo"], ".git"), entry["gitdir"], path))
    after = clean_free()
    entry["outcome"] = result["outcome"] if result["outcome"] != "deleted" else "removed"
    entry["dfDeltaBytes"] = (after - free_before) if None not in (after, free_before) else None
    entry["removedCount"] = result.get("removed")
    if result.get("reason"):
        entry["reason"] = result["reason"]
    if result.get("stopped"):
        entry["stopped"] = result["stopped"]
    clean_receipt("result", entry, run_id, outcome=result["outcome"], reason=result.get("reason"), removed=result.get("removed"),
            checks=result["checks"], dfDeltaBytes=entry["dfDeltaBytes"])


def clean_act(entry: dict, run_id: str, days: int) -> None:
    if entry["kind"] == "tool-cache":
        return clean_run_tool(entry, run_id)
    if entry["kind"] == "debris":
        with ExitStack() as stack:
            if entry["name"] in ("rust-build-output", "project-dependencies"):
                held = stack.enter_context(reap_locks([reap_queue_lock(entry["repo"])], create=True))
                if held != 1:
                    raise ReapRefused("lock-unavailable:project-queue")
                if entry["name"] == "rust-build-output":
                    # The canonical queue flock is already held; take only Cargo's existing locks here.
                    stack.enter_context(reap_locks(clean_rust_lock_paths(entry["target"], entry["repo"])[1:], create=False))
            clean_remove_folder(entry, run_id, entry["root"], False, lambda: clean_extra_gates(entry))
        return
    if entry["kind"] == "worktree-entry":
        clean_entry_gates(entry)  # fresh
        clean_receipt("plan", entry, run_id)
        entry["outcome"] = reap_worktree_forget(os.path.join(entry["repo"], ".git"), entry["gitdir"], entry["path"])
        clean_receipt("result", entry, run_id, outcome=entry["outcome"])
        return
    path = entry["path"]
    if entry["kind"] == "worktree":
        idle, archive = entry.get("idleDays", days), entry.get("archiveMode", False)
        if not entry.get("unsaved"):
            clean_remove_folder(entry, run_id, os.path.dirname(path), True, lambda: clean_worktree_gates(entry, idle, None, archive))
        else:
            clean_archive_then_remove(entry, run_id, idle)
        return
    def last_look():
        busy = sorted(set(next(f for f in CLEAN_FOLDERS if f["name"] == entry["name"])["busy"]) & clean_running())
        if busy:
            raise ReapRefused("in-use:" + ",".join(busy))
        clean_folder_gates(path)
        reap_holders(path, path)
    clean_remove_folder(entry, run_id, entry["root"], False, last_look)


# ---- archive, then remove (the ladder's last rung) ------------------------------------------------------------------------------------

CLEAN_ARCHIVE_OWNER = "clean-worktrees"   # <archiveRoot>/clean-worktrees/<folder>-<time>-<run>/ ; these archives never expire by themselves


def clean_worktree_last_look(entry: dict, idle: float, archive: bool) -> None:
    """The gates again, right before the removal. Unsaved work that appeared since planning is never removed unarchived."""
    clean_worktree_gates(entry, idle, None, archive)
    if entry.get("unsaved") and not entry.get("archived"):
        raise ReapRefused("unsaved-work-appeared:" + entry["unsaved"][0][:80])


def clean_archive_item(entry: dict) -> dict:
    return {"path": entry["path"], "root": os.path.dirname(entry["path"]), "repo": entry["path"], "owner": CLEAN_ARCHIVE_OWNER,
            "jobs": [CLEAN_ARCHIVE_OWNER], "class": "job-clone-root", "kind": "clone", "ownership": "clean-worktree"}


def clean_archive_then_remove(entry: dict, run_id: str, idle: float) -> None:
    """A worktree holding work no remote has: commits go in a verified git bundle, files in a hashed tar (the engine's archive: verified
    in an empty repository and by reading the tar back), the archive is stamped as the only copy before the first byte goes, and the
    folder is compared with the archive once more before and during removal. Any difference, any archive failure and any protected
    name keeps the worktree. The branch stays either way."""
    path = entry["path"]
    clean_worktree_gates(entry, idle, None, True)  # fresh: still removable apart from the work the archive will save
    if not entry.get("unsaved"):
        entry["archived"] = False
        return clean_remove_folder(entry, run_id, os.path.dirname(path), True, lambda: clean_worktree_last_look(entry, idle, True))
    item = clean_archive_item(entry)
    repos, git_dirs = [], []
    reap_protected_scan(path, allow_git=True, markers=True, repos=repos, git_dirs=git_dirs)  # a protected name keeps the worktree whole
    if {reap_canon(r) for r in repos} != {path}:
        raise ReapRefused("nested-repository")
    clean_receipt("plan", entry, run_id, archive=entry["unsaved"])
    made = reap_archive_create(item, {"reason": entry["unsaved"][0]}, repos, git_dirs, run_id)
    entry["archive"] = {k: made.get(k) for k in ("path", "bytes", "files", "inputBytes", "bundles", "manifestSha256", "reused")}
    entry["archived"] = True
    clean_receipt("archive", entry, run_id, archive=entry["archive"])
    def last_look():
        clean_worktree_last_look(entry, idle, True)
        again, again_dirs = [], []
        reap_protected_scan(path, allow_git=True, markers=True, repos=again, git_dirs=again_dirs)
        units, collected = reap_archive_state(item, again, again_dirs)
        if reap_archive_fingerprint(units, collected) != made["fingerprint"] or collected["stamp"] != made["stamp"]:
            raise ReapRefused("archive-stale:folder-changed-after-archive")
        had_stamp = reap_archive_sole_since(made["path"]) is not None
        if not reap_archive_mark_sole(made["path"], path, run_id):
            raise ReapRefused("archive-sole-copy-unrecorded")  # no stamp, no removal
        entry["stamped"] = not had_stamp
    try:
        clean_remove_folder(entry, run_id, os.path.dirname(path), True, last_look)
    finally:
        if entry.get("stamped") and not entry.get("removedCount"):
            try:
                os.unlink(os.path.join(made["path"], "sole-copy-since.json"))  # nothing was removed: the worktree is still the primary copy
            except OSError:
                pass


# ---- report-only inventory ------------------------------------------------------------------------------------------

def clean_inventory() -> list[dict]:
    """Read-only size report; older browser builds have a separate cleanup target, models and disk images do not."""
    found = []
    def children(kind: str, root: str | None, note: str, rename=lambda n: n, depth: int = 1):
        if not root or not os.path.isdir(root) or os.path.islink(root):
            return
        level = [root]
        for _ in range(depth):
            level = [e.path for folder in level for e in clean_scan(folder, kind, []) if clean_directory(e, kind, []) and not e.name.startswith(".")]
        for path in sorted(level):
            found.append({"kind": kind, "name": rename(os.path.relpath(path, root)), "path": path, "note": note})
    hub = os.environ.get("HF_HUB_CACHE") or (os.path.join(os.environ["HF_HOME"], "hub") if os.environ.get("HF_HOME") else None) \
        or os.path.join(os.environ.get("XDG_CACHE_HOME") or clean_home(".cache"), "huggingface", "hub")
    redownload = "removing it means downloading it again"
    children("model", hub, redownload, lambda n: n.split("--", 1)[-1].replace("--", "/"))
    children("model", clean_home(".lmstudio", "models"), redownload, depth=2)
    ollama = os.environ.get("OLLAMA_MODELS") or clean_home(".ollama", "models")
    if os.path.isdir(ollama):
        found.append({"kind": "model", "name": "ollama", "path": ollama, "note": "`ollama rm <model>` removes one"})
    browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or (clean_home("Library", "Caches", "ms-playwright") if os.uname().sysname == "Darwin"
                                                              else clean_home(".cache", "ms-playwright"))
    children("browser", browsers, "a project pinned to this Playwright version downloads it again")
    raw = clean_home("Library", "Containers", "com.docker.docker", "Data", "vms", "0", "data", "Docker.raw")
    try:
        info = os.lstat(raw)
        found.append({"kind": "disk-image", "name": "docker", "path": raw, "bytes": info.st_blocks * 512, "logicalBytes": info.st_size,
                      "sizeComplete": True, "note": "`docker system prune` frees space inside it; the file is sparse, so bytes is what it occupies"})
    except OSError:
        pass
    if shutil.which("tmutil"):
        code, out = clean_run(["tmutil", "listlocalsnapshots", "/"], 30)
        count = sum(1 for line in out.splitlines() if line.startswith("com.apple")) if code == 0 else 0
        if count:
            found.append({"kind": "snapshots", "name": "local snapshots", "count": count, "bytes": None,
                          "note": "macOS frees them when space runs low; `sudo tmutil deletelocalsnapshots /` removes them now"})
    for entry in found:
        entry.setdefault("paths", [entry["path"]] if "bytes" not in entry and entry.get("path") else [])
    clean_size_all([e for e in found if e["paths"]])
    for entry in found:
        entry.pop("paths", None)
        if entry.get("path"):
            entry["path"] = clean_tilde(entry["path"])
    return sorted(found, key=lambda e: -(e.get("bytes") or 0))


# ---- entry points ---------------------------------------------------------------------------------------------------

def clean_shown(entry: dict) -> dict:
    out = {k: v for k, v in entry.items() if k not in ("spec", "git", "gitdir", "paths", "sizePaths", "root", "repo")}
    for key in ("path",):
        if out.get(key):
            out[key] = clean_tilde(out[key])
    if entry.get("repo"):
        out["repo"] = clean_tilde(entry["repo"])
    return out


def clean_job_sweep(rung: dict, dry_run: bool, run_id: str, goal_met=None) -> dict:
    """One rung's pass over finished jobs' folders, through the job reaper's own guarded removal (`reap_sweep`: every gate, lock, handle
    probe, archive and receipt of a tick) but without the tick's two-jobs-per-run cap, and with the rung's shorter windows: a job
    counts once it finished `hours` ago; unmarked folders age out after that long (`evidenceExpiryDays`); with `clones`, whole clones
    too (`cloneExpiryDays`, dirty or unpushed work archived and verified first); with `pinned`, a folder another job lists as an input
    or waits on is released once that job finished or was interrupted that many hours ago. A window the operator set to 0 stays off.
    Jobs are read from the host's job store; a job the host calls live, a running worker and every claim keep their folders."""
    spec = rung["job"]
    days = max(1, -(-int(spec["hours"]) // 24))
    windows = {"evidenceExpiryDays": days, **({"cloneExpiryDays": days} if spec["clones"] else {})}
    out = {"jobs": 0, "items": 0, "removedBytes": 0, "candidateBytes": 0, "kept": {}, "errors": [], "incomplete": None, "stopped": None}
    started = time.monotonic()
    def kept(reason: str, items: int, size: int) -> None:
        found = out["kept"].setdefault(reason.split(":")[0].split(";")[0], {"items": 0, "bytes": 0})
        found["items"] += items
        found["bytes"] += size
    def keep(item: dict) -> bool:
        if spec["filter"] == "all":
            return True
        if item["class"] == "job-cache-subdir":
            return bool(CLEAN_JOB_REGENERABLE.match(os.path.basename(item["path"])))
        return item["class"] in ("job-clone-regenerable", "job-clone-root")  # a clone is listed only once its (shortened) window passed
    try:
        clones = reap_clone_roots()
        live_ids, live_idle = reap_live_snapshot()
        table = reap_process_table() if dry_run else None
    except (HostError, OSError, ValueError, sqlite3.Error, ReapRefused) as error:
        out["incomplete"] = "job-sweep-unavailable:" + reap_named(error)
        return out
    ctx = {"run": run_id, **({"table": table, "git": {}, "sizeBudget": 20} if dry_run else {})}  # a removal sizes with the engine's own budget
    with db() as conn:
        jobs = rows(conn, "SELECT * FROM jobs WHERE status IN ('done','failed','cancelled','interrupted') ORDER BY finished_at")
    limit = CLEAN_JOB_SECONDS // 3 if dry_run else CLEAN_JOB_SECONDS   # a dry run only estimates
    for job in jobs:
        if time.monotonic() - started > limit:
            out["incomplete"] = f"job-sweep-time-limit:{limit}s"
            break
        if goal_met is not None and goal_met():
            break
        lock = None
        try:
            with reap_escalated(windows, spec["pinned"] or None, spec["keepUnsaved"]), db() as conn:
                clock = reap_job_finished_clock(conn, job)
                if not clock or now() - clock < spec["hours"] * 3600:
                    continue
                blocker = reap_job_blocker(conn, job, live_ids, live_idle)
                if blocker:  # sized from the audit the tick last made (an estimate, never a walk): this is what the job holds back
                    cached = reap_audit_cached(conn, job)
                    kept(blocker, 1, sum(e.get("bytes") or 0 for e in (cached or {}).get("entries", []) if e.get("state") != "gone"))
                    continue
                items, notes = reap_job_items(job, clones)
                items = reap_output_order([i for i in items if keep(i)])
                if not items:
                    continue
                if not dry_run:
                    lock = reap_job_lock(job["id"])
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        kept("reap-already-running", len(items), 0)
                        continue
                before = clean_free()
                results, stopped = reap_sweep(items, "audit" if dry_run else "delete", ctx)
                after = clean_free()
                out["jobs"] += 1
                out["items"] += len(results)
                for result in results:
                    size = result["bytes"] or 0
                    if result["outcome"] == "deleted":
                        out["removedBytes"] += size
                    elif result["outcome"] == "would-delete":
                        out["candidateBytes"] += size
                    else:
                        kept(result["reason"] or result["outcome"], 1, size)
                if not dry_run and any(r["outcome"] == "deleted" for r in results):
                    log_event(conn, "job.reaped_under_pressure", job["id"], "clean", {
                        "reclaimedBytes": sum(r["bytes"] or 0 for r in results if r["outcome"] == "deleted"), "deleted": sum(1 for r in results if r["outcome"] == "deleted"),
                        "dfDeltaBytes": (after - before) if None not in (before, after) else None, "run": run_id, "rung": rung["name"]})
                if stopped:
                    out["stopped"] = stopped
                    break
        except (HostError, OSError, ValueError, sqlite3.Error, ReapRefused) as error:
            out["errors"].append({"clean": "finished-jobs", "job": job["id"], "error": str(error)[:200]})
        finally:
            if lock is not None:
                os.close(lock)
    return out


def clean_blockers(entries: list[dict], jobs: dict | None = None) -> list[dict]:
    """What keeps the most bytes, by reason: the top ten, so a pass that frees nothing says why. `jobs` is the job sweep's own view
    ({reason: {"items", "bytes"}}): a job it could not touch counts once, with the bytes its last audit sized."""
    totals: dict = {}
    for entry in entries:
        if "keep" in entry:
            head = entry["keep"].split(":")[0].split(";")[0]
            found = totals.setdefault(head, {"reason": head, "items": 0, "bytes": 0})
            found["items"] += 1
            found["bytes"] += entry.get("bytes") or 0
    for head, held in (jobs or {}).items():
        found = totals.setdefault("job:" + head, {"reason": "job:" + head, "items": 0, "bytes": 0})
        found["items"] += held["items"]
        found["bytes"] += held["bytes"]
    return sorted(totals.values(), key=lambda b: (-b["bytes"], -b["items"]))[:10]


CLEAN_DONE = ("cleaned", "removed", "worktree-entry-forgotten")


def clean_entry_key(entry: dict) -> str:
    return entry.get("path") or (entry["name"] + ":" + str(entry.get("command")))


def clean_pass(dry_run: bool, deep: bool = False, only: list | None = None, report: bool = True, policy: dict | None = None,
               escalate: bool | None = None, steady: bool = False) -> dict:
    CLEAN_MEMO.data = {}
    try:
        return clean_pass_run(dry_run, deep, only, report, policy, escalate, steady)
    finally:
        CLEAN_MEMO.data = None


def clean_pass_run(dry_run: bool, deep: bool, only: list | None, report: bool, policy: dict | None, escalate: bool | None, steady: bool) -> dict:
    """Free space until the measured low watermark is reached. The routine rules run first, largest candidate first; when they cannot
    reach the goal the pass climbs the ladder (CLEAN_RUNGS), re-planning with each rung's wider windows, and stops at the first rung that
    reaches it. A dry run plans every rung and removes nothing. `steady` is the quiet pass below the high watermark: it ignores the goal
    and removes only what is long idle (CLEAN_STEADY_NAMES). The result says what each rung found and freed, what was really removed,
    the net change of free space and the folders that keep the most bytes, by reason."""
    if only is not None and not only:
        raise HostError("no clean targets named", "omit the target list for all of them: " + ", ".join(CLEAN_NAMES))
    unknown = sorted(set(only or ()) - set(CLEAN_NAMES))
    if unknown:
        raise HostError(f"unknown clean targets: {', '.join(unknown)}", "targets: " + ", ".join(CLEAN_NAMES))
    chosen, errors, skipped = set(only or CLEAN_NAMES), [], []
    policy = policy if policy is not None else load_policy()
    high, low = clean_goals(policy)
    disk = clean_disk()
    initial = clean_occupancy(disk, low)
    rungs = [CLEAN_STEADY] if steady else clean_ladder(policy, escalate)
    tools = {t["name"] for t in CLEAN_TOOLS} | {f["name"] for f in CLEAN_FOLDERS}
    run_id = uuid.uuid4().hex[:12]
    merged: dict = {}      # every entry of every rung, the latest verdict per folder
    reports, attempted, jobs_kept, job_bytes, stopped = [], {}, {}, 0, None
    candidate_keys, removed_total, current, last_entries, sizes = set(), 0, initial, [], {}
    for position, rung_id in enumerate(rungs):
        if position and not steady and (current["goalMet"] is not False or stopped):
            break  # the goal is reached (or cannot be measured: then nothing is escalated on a guess): the rest of the ladder is not planned
        rung = clean_profile(policy, rung_id)
        names = chosen if rung["tools"] else {n for n in chosen if n not in tools}
        if steady:
            names &= set(CLEAN_STEADY_NAMES)
        days = rung["worktreeHours"] / 24
        entries = clean_plan(names, deep, errors, skipped, days, rung["projectHours"], rung)
        for entry in entries:
            prior = attempted.get(clean_entry_key(entry))
            if prior is not None and "keep" not in entry:
                entry["keep"] = prior.get("keep") or "attempted-earlier-this-pass"
            entry["sizePaths"] = list(entry.get("paths") or [])
            if (not dry_run and "keep" in entry) or str(entry.get("keep")).startswith("job-folder"):
                entry["paths"] = []  # never size job folders, or retained candidates during an actual removal
        to_size = []
        for entry in entries:  # a folder sized for an earlier rung is not walked again
            known = sizes.get(clean_entry_key(entry))
            if known and entry["paths"]:
                entry["paths"], (entry["bytes"], entry["sizeComplete"]) = [], known
                if not known[1]:
                    entry.setdefault("keep", "size-incomplete")
            elif entry["paths"]:
                to_size.append(entry)
            else:
                entry["bytes"], entry["sizeComplete"] = known or (0, True)
        clean_size_all(to_size)
        sizes.update({clean_entry_key(e): (e["bytes"], e["sizeComplete"]) for e in to_size})
        entries.sort(key=lambda e: ("keep" in e, -e["bytes"]))
        found = [e for e in entries if "keep" not in e and clean_entry_key(e) not in candidate_keys]
        report_rung = {"rung": CLEAN_STEADY if steady else position, "name": rung["name"], "candidates": len(found),
                       "candidateBytes": sum(e["bytes"] for e in found), "done": 0, "removedBytes": 0, "freedBytes": None, "goalMet": None}
        candidate_keys.update(clean_entry_key(e) for e in found)
        for entry in entries:
            key = clean_entry_key(entry)
            if key not in merged or merged[key].get("outcome") not in CLEAN_DONE:
                merged[key] = entry
        last_entries = entries
        free_before = clean_free()
        sweeps = rung["job"] is not None and "finished-jobs" in chosen
        act = not dry_run and (steady or (initial["goalMet"] is not True and current["goalMet"] is not True))
        if act and (sweeps or any("keep" not in e for e in entries)):
            with reap_cleanup_owner("clean"):
                if not steady:
                    current = clean_occupancy(clean_disk(), low)
                for entry in entries:
                    if (not steady and current["goalMet"] is True) or "keep" in entry:
                        continue
                    attempted[clean_entry_key(entry)] = entry
                    try:
                        clean_act(entry, run_id, days)
                    except ReapRefused as refusal:
                        entry["keep"] = str(refusal)
                        clean_remember_refusal(entry, entry["keep"])
                    except OSError as error:
                        entry["keep"] = clean_os_refusal(error)
                    if entry.get("outcome") == "refused" and entry.get("reason"):
                        entry["keep"] = entry["reason"]  # the filesystem or a last look said no: kept, with the cause, not an error
                        clean_remember_refusal(entry, entry["keep"])
                    if entry.get("outcome") in CLEAN_DONE:
                        report_rung["done"] += 1
                        report_rung["removedBytes"] += entry.get("bytes") or 0
                    if entry.get("stopped"):
                        stopped = entry["stopped"]
                        held = getattr(REAP_CLEANUP_OWNER, "held", None)
                        if held:
                            held["stopped"] = stopped
                        break
                    if not steady:
                        current = clean_occupancy(clean_disk(), low)
                if sweeps and not stopped and (steady or current["goalMet"] is not True):
                    swept = clean_job_sweep(rung, False, run_id, None if steady else (lambda: clean_occupancy(clean_disk(), low)["goalMet"] is True))
                    report_rung["done"] += swept["jobs"]
                    report_rung["removedBytes"] += swept["removedBytes"]
                    report_rung["jobs"] = {k: swept[k] for k in ("jobs", "items", "removedBytes", "incomplete")}
                    errors.extend(swept["errors"])
                    jobs_kept = swept["kept"]
                    stopped = swept["stopped"] or stopped
                    if not steady:
                        current = clean_occupancy(clean_disk(), low)
        elif dry_run and sweeps:
            swept = clean_job_sweep(rung, True, run_id)
            report_rung["jobs"] = {k: swept[k] for k in ("jobs", "items", "candidateBytes", "incomplete")}
            report_rung["candidateBytes"] += max(0, swept["candidateBytes"] - job_bytes)  # the sweeps are cumulative: this rung adds only the difference
            job_bytes = max(job_bytes, swept["candidateBytes"])
            errors.extend(swept["errors"])
            jobs_kept = swept["kept"]
        free_after = clean_free()
        report_rung["freedBytes"] = None if dry_run or None in (free_before, free_after) else free_after - free_before
        removed_total += report_rung["removedBytes"]
        report_rung["goalMet"] = None if steady else (initial["goalMet"] if dry_run else clean_occupancy(clean_disk(), low)["goalMet"])
        reports.append(report_rung)
    if not dry_run and not steady and clean_occupancy(clean_disk(), low)["goalMet"] is not True:
        sized = [e for e in last_entries if "keep" in e and not e["paths"] and e.get("sizePaths") and not str(e["keep"]).startswith("job-folder")]
        for entry in sized:
            entry["paths"] = entry["sizePaths"]
        clean_size_all(sized)  # the blockers by bytes: only when the goal is still unmet, and only for the last rung's view
        for entry in sized:
            entry["paths"] = []
    if not dry_run:
        clean_forget_refusals()
    all_entries = sorted(merged.values(), key=lambda e: ("keep" in e, -(e.get("bytes") or 0)))
    after = clean_disk()
    final = clean_occupancy(after, low)
    done = [e for e in all_entries if e.get("outcome") in CLEAN_DONE]
    failed = [e for e in all_entries if e.get("outcome") and e not in done and "keep" not in e]
    candidates = [e for e in all_entries if "keep" not in e]
    freed = None if dry_run or not (disk and after) else after["freeBytes"] - disk["freeBytes"]
    out = {"dryRun": dry_run, "deep": deep, "mode": CLEAN_STEADY if steady else ("ladder" if len(rungs) > 1 else "routine"), "disk": disk,
           "cleanAtPercent": high, "cleanToPercent": low,
           "initialOccupancy": initial, "finalOccupancy": final,
           "remainingBytes": final["remainingBytes"], "goalMet": final["goalMet"],
           "summary": {"candidates": len(candidates), "candidateBytes": sum(e["bytes"] for e in candidates) + job_bytes,
                       "kept": len(all_entries) - len(candidates), "done": len(done), "failed": len(failed),
                       "keptReasons": dict(sorted(Counter(e["keep"].split(":")[0] for e in all_entries if "keep" in e).items())),
                       "removedBytes": None if dry_run else removed_total, "freedBytes": freed,
                       "freedNote": ("removed more than the volume gained: other writers used the space meanwhile" if freed is not None and removed_total > 0 and freed < removed_total * 0.5
                                     else "nothing was removed; free space fell because other programs wrote" if freed is not None and freed < 0 and not removed_total else None)},
           "rungs": reports, "topBlockers": clean_blockers(all_entries, jobs_kept),
           "items": [clean_shown(e) for e in all_entries], "skippedRepos": skipped,
           "errors": errors + [{"clean": e["name"], "path": clean_tilde(e.get("path") or ""), "error": e["outcome"], "reason": e.get("reason")} for e in failed]}
    if not dry_run:
        out["diskAfter"] = after
    if stopped:
        out["stopped"] = stopped
    if report:
        out["report"] = clean_inventory()
    return out


CLEAN_PASS = clean_pass  # the pass itself, whatever a host later binds the name clean_pass to (UltraTerm wraps it to watch pressure passes)


def clean_auto_state(conn) -> dict:
    """The last automatic pass: {"at", "freeBytes"}; a legacy bare timestamp reads as {"at"}. Empty when armed (no pass pending)."""
    stored = row(conn, "SELECT value FROM meta WHERE key=?", (CLEAN_AUTO_KEY,))
    if not stored:
        return {}
    try:
        value = json.loads(stored["value"])
    except ValueError:
        return {}
    if isinstance(value, (int, float)):
        return {"at": float(value)}
    return value if isinstance(value, dict) else {}


def clean_auto_observe(conn, policy: dict, disk: dict | None, event=None) -> str | None:
    """Start at high and retry sustained pressure until low, bounded by the stampede cooldown.

    `event` is an opaque release-generation token. A changed release can retry unresolved work too.
    A pre-pass stamp prevents unchanged or failed ticks from spinning inside the cooldown.
    """
    if policy.get("cleanAtPercent") == 0:
        return None
    high, low = clean_goals(policy)
    occupancy = clean_occupancy(disk, low)
    percent = occupancy["usedPercent"]
    if percent is None:
        return None
    state = clean_auto_state(conn)
    if percent <= low:
        if state:
            conn.execute("DELETE FROM meta WHERE key=?", (CLEAN_AUTO_KEY,))
        return None
    event = policy.get("cleanEventToken") if event is None else event
    # A pass that removed nothing found nothing to wait out in ten minutes: retrying every tick only refills the log. A changed release
    # event (a job finished) is the exception, and so is new pressure below.
    if state.get("running") and now() - float(state.get("at") or 0) < CLEAN_AUTO_RUNNING_SECONDS:
        return None
    previous = state.get("freeBytes")
    floor = float(policy.get("cleanFloorGB", 10)) * 1024 ** 3
    growth = float(policy.get("cleanGrowthGB", 5)) * 1024 ** 3
    urgent = previous is not None and disk["freeBytes"] < previous and (disk["freeBytes"] < floor or previous - disk["freeBytes"] >= growth)
    quiet = state.get("removedBytes") == 0 and not urgent and not (event is not None and event != state.get("event"))
    if state and now() - float(state.get("at") or 0) < (CLEAN_AUTO_QUIET_SECONDS if quiet else CLEAN_AUTO_SECONDS):
        return None
    if not state:
        return "crossed" if percent >= high else None
    if state.get("goalMet") is not True and event is not None and event != state.get("event"):
        return "release"
    if percent < high:
        return "unfinished" if state.get("goalMet") is not True else None
    if previous is None:
        return "pressure"
    if disk["freeBytes"] < floor and disk["freeBytes"] < previous:
        return "floor"
    if disk["freeBytes"] < previous and previous - disk["freeBytes"] >= growth:
        return "growth"
    return "pressure"


def clean_auto(conn, policy: dict, actions: list, errors: list, event=None) -> None:
    """One bounded pass, with a pre-pass stamp and post-pass measured goal state: at the high watermark the ladder (until the goal is
    measured), below it the steady pass when one is due."""
    disk = clean_disk()
    reason = clean_auto_observe(conn, policy, disk, event)
    if not reason:
        return clean_steady_tick(conn, policy, actions, errors)
    event = policy.get("cleanEventToken") if event is None else event
    def save(measurement, goal_met, removed=None, running=False):
        state = {"at": now(), "freeBytes": measurement.get("freeBytes") if measurement else None,
                 "goalMet": goal_met, "event": event, "removedBytes": removed, **({"running": True} if running else {})}
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (CLEAN_AUTO_KEY, json.dumps(state)))
    save(disk, False, running=True)
    try:
        out = clean_pass(False, report=False, policy=policy)
    except (HostError, ReapRefused, OSError) as error:
        errors.append({"clean": "auto", "error": str(error)})
        return
    after = out.get("diskAfter")
    freed = after["freeBytes"] - disk["freeBytes"] if after and disk else None
    removed = out["summary"].get("removedBytes")  # None: the pass did not say, which is not "nothing" (no quiet wait)
    save(after, out["goalMet"], removed)
    actions.append({"did": "cleaned-at-disk-pressure", "reason": reason, "usedPercent": clean_occupancy(disk, policy.get("cleanToPercent", 80))["usedPercent"],
                    "atPercent": policy.get("cleanAtPercent", 85), "toPercent": policy.get("cleanToPercent", 80),
                    "goalMet": out["goalMet"], "remainingBytes": out["remainingBytes"],
                    "done": out["summary"]["done"], "freedBytes": freed, "removedBytes": removed or 0,
                    "rungs": [{k: r[k] for k in ("name", "done", "removedBytes", "goalMet")} for r in out.get("rungs", [])],
                    "topBlockers": out.get("topBlockers", [])[:3]})
    errors.extend(out["errors"])
    if out.get("stopped"):
        errors.append({"clean": "auto", "stopped": out["stopped"]})


def clean_steady_due(conn, policy: dict) -> bool:
    """A steady pass is due: automatic cleaning and cleanSteadyHours are on and that long has passed since the last one. Starts
    nothing; a host that runs the pass in a child process asks this to decide whether to start one."""
    hours = policy.get("cleanSteadyHours")  # a host whose policy has no such key has not opted in: no surprise removals from a partial policy
    if not isinstance(hours, int) or isinstance(hours, bool) or hours <= 0 or policy.get("cleanAtPercent") == 0:
        return False
    stored = row(conn, "SELECT value FROM meta WHERE key=?", (CLEAN_STEADY_KEY,))
    try:
        last = float(stored["value"]) if stored else 0.0
    except (ValueError, TypeError, KeyError):
        last = 0.0
    return now() - last >= hours * 3600


def clean_steady_tick(conn, policy: dict, actions: list, errors: list) -> None:
    """Below the high watermark a quiet pass still runs every cleanSteadyHours: regenerable output of repositories idle for
    cleanSteadyRepoDays and the artifacts of jobs finished cleanSteadyJobHours ago, so the disk never has to reach the watermark to be kept
    tidy. Nothing else (no tool cache, no young worktree) is touched. Off when automatic cleaning is."""
    if not clean_steady_due(conn, policy):
        return
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (CLEAN_STEADY_KEY, str(now())))  # a failed pass is not retried every tick
    before = clean_disk()
    try:
        out = CLEAN_PASS(False, report=False, policy=policy, steady=True)
    except (HostError, ReapRefused, OSError) as error:
        errors.append({"clean": "steady", "error": str(error)})
        return
    after = out.get("diskAfter")
    if out["summary"].get("removedBytes") or out["summary"]["done"]:
        actions.append({"did": "cleaned-steady", "done": out["summary"]["done"], "removedBytes": out["summary"].get("removedBytes") or 0,
                        "freedBytes": after["freeBytes"] - before["freeBytes"] if after and before else None})
    errors.extend(out["errors"])
    if out.get("stopped"):
        errors.append({"clean": "steady", "stopped": out["stopped"]})
# <<< trimreaper clean <<<
