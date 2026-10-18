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
import os
import shutil
import subprocess
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
CLEAN_AUTO_SECONDS = 600       # stampede guard only: automatic passes are event-driven (see clean_auto_observe)
CLEAN_AUTO_KEY = "clean:auto"
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
CLEAN_EXTRA_NAMES = ("rust-build-output", "project-dependencies", "agent-leftovers", "browser-clones", "playwright")
CLEAN_RUST_BUSY = ("cargo", "rustc", "rust-analyzer", "clippy", "clippy-driver", "cargo-clippy")
CLEAN_NAMES = tuple(t["name"] for t in CLEAN_TOOLS) + tuple(f["name"] for f in CLEAN_FOLDERS) + (CLEAN_WORKTREES,) + CLEAN_EXTRA_NAMES


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
    high, low = policy.get("cleanAtPercent", 85), policy.get("cleanToPercent", 80)
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


def clean_newest_change(path: str, gitdir: str | None) -> float:
    """The newest mtime among the folder's top-level entries and its HEAD, index and HEAD reflog. The folder's own mtime and the
    regenerable output folders are left out: removing one output (or a clean pass removing them) must not make the project look
    freshly worked on."""
    stamps = []
    with os.scandir(path) as entries:
        for entry in entries:
            if not reap_protected(entry.name) and entry.name not in CLEAN_PROJECT_OUTPUTS and entry.name != "target":
                stamps.append(entry.stat(follow_symlinks=False).st_mtime)
    for name in ("HEAD", "index", os.path.join("logs", "HEAD")):
        try:
            stamps.append(os.lstat(os.path.join(gitdir, name)).st_mtime) if gitdir else None
        except OSError:
            pass
    return max(stamps) if stamps else os.lstat(path).st_mtime


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


def clean_job_claims(path: str) -> None:
    """No unfinished job declares the folder, a folder inside it, or a folder around it (job add --cwd/--worktree)."""
    with db() as conn:
        for job in rows(conn, "SELECT * FROM jobs WHERE status NOT IN ('done','failed','cancelled')"):
            for declared in reap_declared_paths(job):
                if reap_inside(declared, path) or reap_inside(path, declared):
                    raise ReapRefused(f"declared-by-unfinished-job:{job['id']}")


def clean_worktree_gates(entry: dict, idle_days: int, table: dict | None) -> None:
    """Every reason to keep a worktree, cheapest first, the process probe last; raises ReapRefused naming the first.
    `table` None probes fresh."""
    path = entry["path"]
    if "locked" in entry["git"]:
        raise ReapRefused("worktree-locked")
    if reap_job_ids(*Path(path).parts):
        raise ReapRefused("job-folder:reaped-with-its-job")
    if any(reap_protected(part) for part in Path(path).parts):
        raise ReapRefused("protected-ancestor")
    reap_vet_root(os.path.dirname(path))  # the removal fences from the parent: never the home folder or a system folder
    retention = reap_cache_retention(path)
    if retention not in ("unmarked", REAP_DISPOSABLE):
        raise ReapRefused("retention:" + retention)
    idle = (now() - clean_newest_change(path, entry["gitdir"])) / 86400
    if idle < idle_days:
        raise ReapRefused(f"recent:{idle:.1f}d-of-{idle_days}d")
    clean_job_claims(path)
    repos = []
    reap_protected_scan(path, allow_git=True, markers=True, repos=repos, nested_disposable=True)
    if {reap_canon(r) for r in repos} != {path}:
        raise ReapRefused("nested-repository")
    with reap_git_session(path):
        clean_linked_identity(entry)
        code, out = reap_git_clean_closed(path)  # index flags that hide edits (skip-worktree, assume-unchanged) refuse
        if code != 0:
            raise ReapRefused(f"git-error:status:{code}")
        if out.strip():
            raise ReapRefused("dirty")
        reap_git_ignored_known(path)
        clean_commits_saved(path, "HEAD")
    reap_holders(path, path, table)


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
CLEAN_TEMP_SYSTEM = re.compile(r"(?i)^(?:\.|com\.apple\.|TemporaryItems$|.*\.(?:sock|socket|pid|lock)$|launchd|tmux-|ssh-|powerlog|"
                               r"CoreSimulator|xcrun_db|KSOutOfProcessFetcher)")


def clean_temp_roots() -> list[tuple[str, int]]:
    roots = []
    shared = os.path.realpath("/tmp")
    if os.path.isdir(shared):
        roots.append((shared, CLEAN_TEMP_IDLE_HOURS["shared"]))
    if os.environ.get("TMPDIR") and os.path.realpath(os.environ["TMPDIR"]) != shared:
        roots.append((os.path.realpath(os.environ["TMPDIR"]), CLEAN_TEMP_IDLE_HOURS["user"]))
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


def clean_stale_tree(path: str, hours: int = 6) -> None:
    """Every entry must be `hours` idle; bounded metadata-only walk, never follow links."""
    cutoff, count = now() - hours * 3600, 0
    stack = [(path, 0)]
    while stack:
        folder, depth = stack.pop()
        if depth > REAP_MAX_DEPTH:
            raise ReapRefused("idle-scan-incomplete")
        info = os.lstat(folder)
        if info.st_uid != os.getuid():
            raise ReapRefused("not-owned-by-user")
        if info.st_mtime > cutoff:
            raise ReapRefused(f"recent:less-than-{hours}h")
        with os.scandir(folder) as entries:
            for entry in entries:
                count += 1
                if count > REAP_WALK_ENTRIES:
                    raise ReapRefused("idle-scan-incomplete")
                info = entry.stat(follow_symlinks=False)
                if info.st_uid != os.getuid():
                    raise ReapRefused("not-owned-by-user")
                if info.st_mtime > cutoff:
                    raise ReapRefused(f"recent:less-than-{hours}h")
                if entry.is_dir(follow_symlinks=False):
                    stack.append((entry.path, depth + 1))


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


def clean_project_outputs(errors: list) -> list[tuple[str, str]]:
    """Every regenerable dependency or output folder inside a repository in the code homes, with the repository it belongs to."""
    found = []
    for path, repo in clean_repo_walk(errors, frozenset(CLEAN_PROJECT_OUTPUTS), "project-dependencies"):
        manifests = CLEAN_PROJECT_OUTPUTS[os.path.basename(path)]
        if not manifests or any(os.path.isfile(os.path.join(os.path.dirname(path), m)) for m in manifests):
            found.append((path, repo))
    return found


def clean_repo_walk(errors: list, wanted: frozenset, label: str) -> list[tuple[str, str]]:
    """Folders named in `wanted` inside repositories, without entering them or any generated tree."""
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
    clean_folder_gates(path)
    reap_git_proof(repo, path, ("clean", "pushed", "ignored", "untracked"))
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


def clean_project_gates(entry: dict, table: dict | None) -> None:
    """Dependency output additionally waits for the existing project idle policy."""
    if "idleHours" not in entry:
        raise ReapRefused("project-idle-policy-unproved")
    limit = clean_repository_output_gates(entry, table)
    repo = entry["repo"]
    dot_git = os.path.join(repo, ".git")
    gitdir = clean_pointer_target(dot_git) if os.path.isfile(dot_git) else dot_git
    idle = (now() - clean_newest_change(repo, gitdir)) / 3600
    if idle < limit:
        raise ReapRefused(f"recent:{idle:.0f}h-of-{limit}h")


def clean_extra_gates(entry: dict, table: dict | None = None) -> None:
    path, kind = entry["path"], entry["name"]
    if kind == "project-dependencies":
        clean_project_gates(entry, table)
    elif kind != "rust-build-output":
        clean_folder_gates(path)
    if kind == "project-dependencies":
        pass
    elif kind == "rust-build-output":
        clean_repository_output_gates(entry, table)
        busy = sorted(set(CLEAN_RUST_BUSY) & clean_running())
        if busy:
            raise ReapRefused("in-use:" + ",".join(busy))
        clean_folder_gates(entry["target"])
    elif kind == "agent-leftovers":
        clean_stale_tree(path, entry.get("idleHours", 6))
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


def clean_extra_entries(only: set, errors: list, idle_hours: int = 48) -> list[dict]:
    entries = []
    def add(name, path, root, **extra):
        entries.append({"name": name, "kind": "debris", "path": path, "root": root, "paths": [path], "tier": "safe", **extra})
    if "rust-build-output" in only:
        for target, repo in clean_rust_targets(errors):
            debug = os.path.join(target, "debug")
            if os.path.isdir(debug) and not os.path.islink(debug):
                add("rust-build-output", debug, target, target=target, repo=repo)
            for profile in clean_scan(target, "rust-build-output", errors):
                path = os.path.join(profile.path, "incremental")
                if profile.name != "debug" and clean_directory(profile, "rust-build-output", errors) and not reap_protected(profile.name) \
                        and os.path.isdir(path) and not os.path.islink(path):
                    add("rust-build-output", path, target, target=target, repo=repo)
    if "project-dependencies" in only and idle_hours:
        for path, repo in clean_project_outputs(errors):
            add("project-dependencies", path, os.path.dirname(path), repo=repo, idleHours=idle_hours)
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


# ---- the plan -------------------------------------------------------------------------------------------------------

def clean_plan(only: set, deep: bool, errors: list, skipped: list, days: int, hours: int = 48) -> list[dict]:
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
                    clean_worktree_gates(entry, days, table)
            except ReapRefused as refusal:
                entry["keep"] = str(refusal)
            except OSError as error:
                entry["keep"] = clean_os_refusal(error)
            entries.append(entry)
    entries.extend(clean_extra_entries(only, errors, hours))
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
        clean_remove_folder(entry, run_id, os.path.dirname(path), True, lambda: clean_worktree_gates(entry, days, None))
        return
    def last_look():
        busy = sorted(set(next(f for f in CLEAN_FOLDERS if f["name"] == entry["name"])["busy"]) & clean_running())
        if busy:
            raise ReapRefused("in-use:" + ",".join(busy))
        clean_folder_gates(path)
        reap_holders(path, path)
    clean_remove_folder(entry, run_id, entry["root"], False, last_look)


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
    out = {k: v for k, v in entry.items() if k not in ("spec", "git", "gitdir", "paths", "root", "repo")}
    for key in ("path",):
        if out.get(key):
            out[key] = clean_tilde(out[key])
    if entry.get("repo"):
        out["repo"] = clean_tilde(entry["repo"])
    return out


def clean_pass(dry_run: bool, deep: bool = False, only: list | None = None, report: bool = True, policy: dict | None = None) -> dict:
    """A bounded candidate snapshot, largest first; stop on measured (not estimated) low-water occupancy."""
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
    days = policy.get("worktreeIdleDays", 7)
    hours = policy.get("projectIdleHours", 48)
    entries = clean_plan(chosen, deep, errors, skipped, days, hours)
    for entry in entries:  # never size job folders, or retained candidates during an actual removal
        if (not dry_run and "keep" in entry) or str(entry.get("keep")).startswith("job-folder"):
            entry["paths"] = []
    clean_size_all(entries)
    entries.sort(key=lambda e: ("keep" in e, -e["bytes"]))
    stopped = None
    if not dry_run and initial["goalMet"] is not True and any("keep" not in e for e in entries):
        run_id = uuid.uuid4().hex[:12]
        with reap_cleanup_owner("clean"):
            current = clean_occupancy(clean_disk(), low)
            for entry in entries:
                if current["goalMet"] is True:
                    break
                if "keep" in entry:
                    continue
                try:
                    clean_act(entry, run_id, days)
                except ReapRefused as refusal:
                    entry["keep"] = str(refusal)
                except OSError as error:
                    entry["keep"] = clean_os_refusal(error)
                if entry.get("stopped"):
                    stopped = entry["stopped"]
                    held = getattr(REAP_CLEANUP_OWNER, "held", None)
                    if held:
                        held["stopped"] = stopped
                    break
                current = clean_occupancy(clean_disk(), low)
    after = clean_disk()
    final = clean_occupancy(after, low)
    done = [e for e in entries if e.get("outcome") in ("cleaned", "removed", "worktree-entry-forgotten")]
    failed = [e for e in entries if e.get("outcome") and e not in done]
    candidates = [e for e in entries if "keep" not in e]
    out = {"dryRun": dry_run, "deep": deep, "disk": disk,
           "cleanAtPercent": high, "cleanToPercent": low,
           "initialOccupancy": initial, "finalOccupancy": final,
           "remainingBytes": final["remainingBytes"], "goalMet": final["goalMet"],
           "summary": {"candidates": len(candidates), "candidateBytes": sum(e["bytes"] for e in candidates),
                       "kept": len(entries) - len(candidates), "done": len(done), "failed": len(failed),
                       "keptReasons": dict(sorted(Counter(e["keep"].split(":")[0] for e in entries if "keep" in e).items())),
                       "freedBytes": None if dry_run or not (disk and after) else after["freeBytes"] - disk["freeBytes"]},
           "items": [clean_shown(e) for e in sorted(entries, key=lambda e: ("keep" in e, -e["bytes"]))], "skippedRepos": skipped,
           "errors": errors + [{"clean": e["name"], "path": clean_tilde(e.get("path") or ""), "error": e["outcome"]} for e in failed]}
    if not dry_run:
        out["diskAfter"] = after
    if stopped:
        out["stopped"] = stopped
    if report:
        out["report"] = clean_inventory()
    return out


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
    """Hysteresis and new host events, never a periodic retry. `event` is an opaque release-generation token.

    Only below low re-arms a high crossing. Unresolved work can retry on a *changed* job/artifact release token;
    a token is saved before starting, so unchanged ticks (and failed passes) cannot spin. The cooldown delays
    new pressure/events but never creates one. Hosts may supply the token as policy.cleanEventToken instead.
    """
    if policy.get("cleanAtPercent") == 0:
        return None
    high, low = clean_goals(policy)
    occupancy = clean_occupancy(disk, low)
    percent = occupancy["usedPercent"]
    if percent is None:
        return None
    state = clean_auto_state(conn)
    if percent < low:
        if state:
            conn.execute("DELETE FROM meta WHERE key=?", (CLEAN_AUTO_KEY,))
        return None
    if state and now() - float(state.get("at") or 0) < CLEAN_AUTO_SECONDS:
        return None
    if not state:
        return "crossed" if percent >= high else None
    event = policy.get("cleanEventToken") if event is None else event
    if state.get("goalMet") is not True and event is not None and event != state.get("event"):
        return "release"
    if percent < high:
        return None
    previous = state.get("freeBytes")
    if previous is None:
        return None
    floor = float(policy.get("cleanFloorGB", 10)) * 1024 ** 3
    if disk["freeBytes"] < floor and disk["freeBytes"] < previous:
        return "floor"
    growth = float(policy.get("cleanGrowthGB", 5)) * 1024 ** 3
    if disk["freeBytes"] < previous and previous - disk["freeBytes"] >= growth:
        return "growth"
    return None


def clean_auto(conn, policy: dict, actions: list, errors: list, event=None) -> None:
    """One event-driven safe pass, with a pre-pass stamp and post-pass measured goal state."""
    disk = clean_disk()
    reason = clean_auto_observe(conn, policy, disk, event)
    if not reason:
        return
    event = policy.get("cleanEventToken") if event is None else event
    def save(measurement, goal_met):
        state = {"at": now(), "freeBytes": measurement.get("freeBytes") if measurement else None,
                 "goalMet": goal_met, "event": event}
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (CLEAN_AUTO_KEY, json.dumps(state)))
    save(disk, False)
    try:
        out = clean_pass(False, report=False, policy=policy)
    except (HostError, ReapRefused, OSError) as error:
        errors.append({"clean": "auto", "error": str(error)})
        return
    after = out.get("diskAfter")
    freed = after["freeBytes"] - disk["freeBytes"] if after else None
    save(after, out["goalMet"])
    actions.append({"did": "cleaned-at-disk-pressure", "reason": reason, "usedPercent": clean_occupancy(disk, policy.get("cleanToPercent", 80))["usedPercent"],
                    "atPercent": policy.get("cleanAtPercent", 85), "toPercent": policy.get("cleanToPercent", 80),
                    "goalMet": out["goalMet"], "remainingBytes": out["remainingBytes"],
                    "done": out["summary"]["done"], "freedBytes": freed})
    errors.extend(out["errors"])
    if out.get("stopped"):
        errors.append({"clean": "auto", "stopped": out["stopped"]})
# <<< trimreaper clean <<<
