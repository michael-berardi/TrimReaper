"""Overflow to an external volume: move cold folders there, leave a symlink behind.

Regenerable output inside a candidate (node_modules, .next, .open-next, .astro, .turbo, __pycache__, a Cargo target's debug,
incremental and release/{deps,build}) is deleted first through the cleaner's guarded removal and never copied. Nothing else is
deleted before the copy is proved. A folder moves only when it is idle (nothing changed inside for
`quietMinutes` or its job finished), no process has it open, and the volume is mounted with room to spare. The original path becomes a symlink
into the overflow root, so every tool keeps working; `restore` brings a folder back. Every step writes a receipt first.

There is no fixed age. A folder is judged by whether anything is using it:
  - a folder of a finished job (done, failed, cancelled) moves as soon as no process has it open;
  - a folder of a job that is still live never moves, and a moved one is copied back once its job is live again;
  - any other folder moves when no process has it open and nothing in it changed for `quietMinutes`.
Whatever its job says, a folder in use for work stays, judged from the folder itself and not from a marker someone must
remember to leave:
  - a git repository or worktree inside it with uncommitted changes (`git-dirty`), or whose checked-out branch has commits
    its upstream lacks or has no upstream on a remote (`git-unpushed`); a clean, fully pushed one may move;
  - a file inside it that was read within the quiet time (`read-within-Nmin`), where the volume records access times;
    where it does not, only changes count and the receipt says so (`reads`);
  - release artifacts: a folder named like a release (`releaseNamePattern`) or holding a `releaseMarkers` entry (`wt`)
    that holds a non-empty `releaseArtifacts` child (`out`, `dist`, `release`, `unz`): neither it nor, with keepFolder,
    any of its children moves (`release-artifacts`).
The biggest qualifying folders go first, and the pass stops once the disk is back under `cleanToPercent`
(`--all` ignores that goal and moves everything that qualifies).

Configuration (config.json, key `offload`):
    {"root": "/Volumes/Disk/Overflow", "minFreeGB": 20, "quietMinutes": 60,
     "jobStatusCommand": ["/path/to/tool", "list-jobs"],
     "sources": [{"path": "~/.cache/jobs", "children": true, "jobs": true}]}
`children: true` moves each direct child separately; otherwise the source folder itself moves. `keepFolder: true` (with
`children`) keeps every child a real directory holding real files and moves only that child's subfolders (and none of them while a sibling in that child changed within the quiet time or a process
has its working directory or a file open under it: `parent-active` in `kept`), so a tool that
refuses symlinks (a job store reading `J-0042/coordination.jsonl`) still works; links made by an earlier version are split
or copied back the same way. `jobs: true` reads a job
number (J-0042) from each child's name and asks `jobStatusCommand`, which prints JSON: {"J-0042": "done"} or
{"jobs": [{"id": "J-0042", "status": "done"}]}.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import time
import uuid
from pathlib import Path

from . import clean, core, host

DAY = 86400
WALK_ENTRIES = 400_000
WALK_SECONDS = 20
JOB_NAME = re.compile(r"[Jj]-?(\d{4,5})")
FINISHED = {"done", "failed", "cancelled"}
LIVE = {"queued", "approved", "running", "blocked"}  # parked and interrupted jobs are idle: the quiet rule decides
SETTLE_SECONDS = 300  # a finished job's folder must still have been quiet this long (late writes, a closing process)
README = ("This folder is TrimReaper's overflow: cold folders moved off the Mac's internal disk.\n"
          "Each folder keeps its home-relative path below here, and the original location is a symlink to it.\n"
          "Bring one back with `trimreaper offload restore <original path>`.\n")


RELEASE_ARTIFACTS = ["out", "dist", "release", "unz"]  # children holding built release output awaiting publication
RELEASE_MARKERS = ["wt"]  # a sibling that marks a folder as a release workspace (its worktree)
RELEASE_NAME = r"(?i)^(rel|release)([-_.\d]|$)|^v\d+\.\d+"  # folder names that read as a release
MAX_REPOS = 50  # more git repositories than this inside one folder cannot all be proved idle in a pass: it stays
SETTING_KEYS = {"root", "minFreeGB", "quietMinutes", "jobStatusCommand", "sources", "releaseArtifacts", "releaseMarkers", "releaseNamePattern"}


def settings() -> dict | None:
    cfg = host.config().get("offload")
    if not cfg:
        return None
    return {"root": host.expand(cfg["root"]), "minFreeGB": cfg.get("minFreeGB", 20), "quietMinutes": cfg.get("quietMinutes", 60),
            "jobStatusCommand": cfg.get("jobStatusCommand"), "sources": cfg.get("sources", []),
            "release": {"artifacts": [n.lower() for n in cfg.get("releaseArtifacts", RELEASE_ARTIFACTS)],
                        "markers": [n.lower() for n in cfg.get("releaseMarkers", RELEASE_MARKERS)],
                        "pattern": re.compile(cfg.get("releaseNamePattern", RELEASE_NAME))}}


def job_statuses(cfg: dict) -> dict:
    """{"J-0042": "done"} from the configured command; raises HostError naming the cause when it fails."""
    command = cfg.get("jobStatusCommand")
    if not command:
        return {}
    argv = [str(host.expand(part)) if part.startswith("~/") else part for part in command]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        raise host.HostError(f"jobStatusCommand failed to run: {error}")
    if result.returncode:
        raise host.HostError(f"jobStatusCommand exited {result.returncode}: {result.stderr.strip()[:200]}")
    try:
        data = json.loads(result.stdout)
    except ValueError as error:
        raise host.HostError(f"jobStatusCommand did not print JSON: {error}")
    rows = data.get("jobs") if isinstance(data, dict) and isinstance(data.get("jobs"), list) else None
    if rows is not None:
        data = {row["id"]: row["status"] for row in rows if isinstance(row, dict) and "id" in row and "status" in row}
    if not isinstance(data, dict) or not data:
        raise host.HostError("jobStatusCommand printed no jobs; refusing to guess which are finished")
    return {str(k).upper(): str(v) for k, v in data.items()}


def job_of(name: str) -> str | None:
    match = JOB_NAME.search(name)
    return f"J-{match.group(1)}" if match else None


def check(cfg: dict, path: Path) -> None:
    """Config shape, named failures."""
    if not isinstance(cfg, dict) or set(cfg) - SETTING_KEYS:
        raise host.HostError(f"{path}: offload takes root, minFreeGB, quietMinutes, jobStatusCommand, sources, releaseArtifacts, "
                             "releaseMarkers and releaseNamePattern only")
    for key in ("releaseArtifacts", "releaseMarkers"):
        names = cfg.get(key, [])
        if not isinstance(names, list) or not all(isinstance(n, str) and n and "/" not in n for n in names):
            raise host.HostError(f"{path}: offload.{key} must be a list of folder names")
    pattern = cfg.get("releaseNamePattern", RELEASE_NAME)
    try:
        re.compile(pattern)
    except (re.error, TypeError) as error:
        raise host.HostError(f"{path}: offload.releaseNamePattern must be a regular expression ({error})")
    command = cfg.get("jobStatusCommand")
    if command is not None and (not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command)):
        raise host.HostError(f"{path}: offload.jobStatusCommand must be a command as a list of strings")
    minutes = cfg.get("quietMinutes", 60)
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes < 0:
        raise host.HostError(f"{path}: offload.quietMinutes must be a number of minutes, not {json.dumps(minutes)}")
    if not isinstance(cfg.get("root"), str):
        raise host.HostError(f"{path}: offload.root must be a path")
    host.path_form(cfg["root"])
    free = cfg.get("minFreeGB", 20)
    if isinstance(free, bool) or not isinstance(free, (int, float)) or free < 0:
        raise host.HostError(f"{path}: offload.minFreeGB must be a number of at least 0")
    sources = cfg.get("sources", [])
    if not isinstance(sources, list):
        raise host.HostError(f"{path}: offload.sources must be a list")
    for source in sources:
        if not isinstance(source, dict) or set(source) - {"path", "quietMinutes", "children", "minMB", "jobs", "keepFolder"} or not isinstance(source.get("path"), str):
            raise host.HostError(f"{path}: offload source {json.dumps(source)} needs a path and may have quietMinutes, children, minMB, jobs, keepFolder")
        host.path_form(source["path"])
        if source.get("keepFolder") and not source.get("children"):
            raise host.HostError(f"{path}: offload source keepFolder needs children")
        if source.get("jobs") and not command:
            raise host.HostError(f"{path}: a source with jobs needs offload.jobStatusCommand")
        minutes = source.get("quietMinutes", 0)
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes < 0:
            raise host.HostError(f"{path}: offload source quietMinutes must be a number of minutes, not {json.dumps(minutes)}")


def mount_of(path: Path) -> Path:
    path = Path(os.path.realpath(path))
    while not os.path.ismount(path) and path != path.parent:
        path = path.parent
    return path


def volume_ready(root: Path) -> str | None:
    """None when the overflow volume is mounted, separate from the home disk, and writable; else the cause."""
    probe = root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    mount = mount_of(probe)
    if mount == Path("/"):
        return f"the volume under {root} is not mounted (plug in the drive)"
    if os.stat(mount).st_dev == os.stat(host.reap_home()).st_dev:
        return f"{root} is on the same disk as the home folder"
    if not os.access(probe, os.W_OK):
        return f"{probe} is not writable"
    return None


def tree_stats(path: Path) -> dict:
    """Entries, bytes, newest change (mtime/ctime), newest read (atime of a regular file outside `.git` metadata, which
    this tool's own git probes read) and the git repositories (folders holding a `.git` entry) of a tree; never follows
    links. `complete` False when a bound hit."""
    start, entries, size, newest, read = time.time(), 0, 0, 0.0, 0.0
    stack, complete, repos = [(str(path), False)], True, []
    while stack:
        current, metadata = stack.pop()
        try:
            info = os.lstat(current)
            newest = max(newest, changed_entry(info))
            entries += 1
            if stat.S_ISREG(info.st_mode):
                size += info.st_blocks * 512
                if not metadata:
                    read = max(read, info.st_atime)
            elif stat.S_ISDIR(info.st_mode):
                with os.scandir(current) as it:
                    for entry in it:
                        git = core.reap_is_git(entry.name)
                        if git and not metadata:
                            repos.append(current)
                        stack.append((entry.path, metadata or git))
        except OSError:
            complete = False
        if entries > WALK_ENTRIES or time.time() - start > WALK_SECONDS:
            complete = False
            break
    return {"entries": entries, "bytes": size, "newest": newest, "read": read, "repos": repos, "complete": complete}


def changed_entry(info: os.stat_result) -> float:
    return max(info.st_mtime, info.st_ctime)


def recent_change(path: str, cutoff: float, scan: dict | None = None, reads: bool = False) -> tuple[float | None, bool]:
    """(newest change time, complete) of a tree, stopping at the first change after `cutoff`; never follows links.
    With `reads`, a read (atime) of a regular file outside `.git` metadata after `cutoff` counts too and comes back
    negative (-time), so the caller can name it. `scan` carries one clock and one entry count shared by every tree of a
    kept folder; a bound hit is incomplete."""
    scan = scan if scan is not None else {}
    scan.setdefault("start", time.time())
    scan.setdefault("entries", 0)
    stack = [(path, False)]
    while stack:
        current, metadata = stack.pop()
        try:
            info = os.lstat(current)
            if changed_entry(info) > cutoff:
                return changed_entry(info), True
            if reads and not metadata and stat.S_ISREG(info.st_mode) and info.st_atime > cutoff:
                return -info.st_atime, True
            scan["entries"] += 1
            if stat.S_ISDIR(info.st_mode):
                with os.scandir(current) as it:
                    stack.extend((entry.path, metadata or core.reap_is_git(entry.name)) for entry in it)
        except OSError:
            return None, False
        if scan["entries"] > WALK_ENTRIES or time.time() - scan["start"] > WALK_SECONDS:
            return None, False
    return None, True


_MOUNTS: dict = {}


def mount_table() -> list[tuple[str, set]]:
    """(mount point, options) of every mounted volume, from `mount`; empty when it cannot be read."""
    try:
        result = subprocess.run(["mount"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in result.stdout.splitlines() if result.returncode == 0 else []:
        match = re.match(r"^.+? on (.+?) (?:type \S+ )?\(([^()]*)\)$", line)
        if match:
            rows.append((match.group(1), {o.strip().lower() for o in match.group(2).split(",")}))
    return rows


def reads_recorded(path: Path) -> str | None:
    """None when the volume holding `path` records file access times, else why not. Read from the mount options of the
    volume with the same device (`noatime` turns them off; macOS APFS otherwise records the first read after each write)."""
    try:
        device = os.lstat(path).st_dev
    except OSError as error:
        return f"unknown ({error.__class__.__name__})"
    if device not in _MOUNTS:
        verdict = "unknown (no mount entry for the volume)"
        for point, options in mount_table():
            try:
                if os.stat(point).st_dev != device:
                    continue
            except OSError:
                continue
            verdict = "the volume is mounted noatime" if "noatime" in options else None
            break
        _MOUNTS[device] = verdict
    return _MOUNTS[device]


def git_dir_gone(repo: str) -> str | None:
    """The missing git dir a worktree's `.git` file names (its repository was deleted), else None. Such a folder has no history left to
    lose or to prove: its files are all there is, and moving them keeps them whole."""
    dot_git = os.path.join(repo, ".git")
    if not os.path.isfile(dot_git) or os.path.islink(dot_git):
        return None
    target = clean.clean_pointer_target(dot_git)
    return target if target and not os.path.isfile(os.path.join(target, "HEAD")) else None


def git_in_use(repo: str, gone_ok: bool = False) -> str | None:
    """None when a repository or worktree is clean with its checked-out branch fully on its remote upstream; else
    `git-dirty:...` (uncommitted changes, or cleanliness that cannot be proved) or `git-unpushed:...`. Uses the
    cleaner's fenced git probes (core.reap_git*), which never take optional locks or rewrite the index. With `gone_ok` (a finished
    job's worktree) a git dir that no longer exists is not work in progress."""
    if gone_ok and git_dir_gone(repo):
        return None
    try:
        with core.reap_git_session(repo):
            code, out = core.reap_git_clean_closed(repo, protected_ok=True)
            if code:
                return f"git-dirty:unproven:status-exit-{code}"
            if out.strip():
                return "git-dirty:" + ",".join(sorted({line[:2].strip() or "?" for line in out.splitlines()}))
            code, branch = core.reap_git(repo, "symbolic-ref", "-q", "HEAD")
            if code == 0:
                name = branch.strip().removeprefix("refs/heads/")
                code, upstream = core.reap_git(repo, "rev-parse", "--symbolic-full-name", f"{name}@{{upstream}}")
                upstream = upstream.strip()
                if code != 0 or not upstream.startswith("refs/remotes/"):
                    return f"git-unpushed:no-upstream:{name}"
                code, ahead = core.reap_git(repo, "rev-list", "--count", "HEAD", "--not", upstream)
                if code != 0 or not ahead.strip().isdigit():
                    return f"git-unpushed:unproven:rev-list-exit-{code}"
                if int(ahead):
                    return f"git-unpushed:{int(ahead)}:{name}"
            code, ahead = core.reap_git(repo, "rev-list", "--count", "HEAD", "--not", "--remotes")
            if code != 0 or not ahead.strip().isdigit():
                return f"git-unpushed:unproven:rev-list-exit-{code}"
            if int(ahead):
                return f"git-unpushed:{int(ahead)}:detached"
    except core.ReapRefused as refused:
        return f"git-dirty:unproven:{refused}"
    return None


def git_refusal(path: Path, stats: dict, gone_ok: bool = False) -> str | None:
    """The first repository at or under `path` that is work in progress, named with its place in the folder."""
    repos = stats.get("repos")
    if repos is None:  # stats from elsewhere: look for repositories now
        repos = [str(path)] if os.path.lexists(path / ".git") else []
    if len(repos) > MAX_REPOS:
        return f"git-dirty:unproven:{len(repos)}-repositories"
    for repo in sorted(repos):
        why = git_in_use(repo, gone_ok)
        if why:
            place = os.path.relpath(repo, path)
            return why if place == "." else f"{why} ({place})"
    return None


def release_refusal(folder: Path, rules: dict | None) -> str | None:
    """`release-artifacts:<names>` when `folder` reads as a release workspace (its name matches the pattern, or it holds a
    marker such as `wt`) and holds a non-empty artifact child (`out`, `dist`, `release`, `unz`) or one already moved."""
    if not rules or not rules["artifacts"]:
        return None
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    folded = {name.lower(): name for name in names}
    held = []
    for key in rules["artifacts"]:
        child = folder / folded[key] if key in folded else None
        if child is None:
            continue
        try:
            if child.is_symlink():
                held.append(folded[key])
            elif child.is_dir():
                with os.scandir(child) as it:
                    if any(True for _ in it):
                        held.append(folded[key])
        except OSError:
            held.append(folded[key])
    if held and (rules["pattern"].search(folder.name) or any(m in folded for m in rules["markers"])):
        return "release-artifacts:" + ",".join(sorted(held))
    return None


def parent_active(item: dict, quiet: float, table: dict, now: float, shared: dict | None = None) -> str | None:
    """With keepFolder the kept folder is used as a whole: a cold subfolder stays while a sibling changed within
    `quiet` or a process has its cwd or a file open anywhere under the kept folder, including inside a sibling an earlier
    pass moved to the overflow volume (now a link, whose process paths are the real ones there). `shared` holds one scan
    budget and the sibling results for every item of the folder; None scans afresh."""
    parent = item.get("parent")
    if parent is None:
        return None
    shared = item["siblings"] if shared is None else shared
    found, scan, peers = shared.setdefault("found", {}), shared.setdefault("scan", {}), shared.get("peers", {})
    try:
        names = sorted(os.listdir(parent))
    except OSError as error:
        return f"parent-active: unreadable {error.__class__.__name__}"
    linked, reads = [], item.get("reads") == "recorded" and item.get("readQuiet", True)
    for name in names:
        if name == item["path"].name or name.startswith(".offload-"):
            continue
        if os.path.islink(parent / name):
            linked.append(name)
            continue
        if name not in found:
            seen = peers.get(name, {}).get("stats")
            if seen and seen["complete"] and seen["newest"] <= now - quiet and (not reads or seen.get("read", 0) <= now - quiet):
                found[name] = (None, True)  # the pass already walked this sibling as a candidate
            else:
                found[name] = recent_change(str(parent / name), max(now - quiet, shared.get("regen", {}).get(name, peers.get(name, {}).get("regenDone", 0))), scan, reads)
        newest, complete = found[name]
        if newest is not None and newest < 0:
            return f"parent-active: {name} read {max(0, int((now + newest) // 60))} min ago"
        if newest is not None:
            return f"parent-active: {name} changed {max(0, int((now - newest) // 60))} min ago"
        if not complete:
            return f"parent-active: {name} scan incomplete"
    watched = [str(parent)]
    root = item.get("root")
    for name in linked:
        try:
            target = os.path.realpath(parent / name, strict=True)
        except OSError as error:
            return f"parent-active: {name} link cannot be resolved ({error.__class__.__name__})"
        if root is not None and core.reap_inside(target, os.path.realpath(root)):
            watched.append(target)
    try:
        for place in watched:
            core.reap_holders(place, place, table)
    except core.ReapRefused as refused:
        return f"parent-active: {refused}"
    return None


def candidates(cfg: dict) -> list[dict]:
    home = host.reap_home()
    items = []
    for source in cfg["sources"]:
        base = host.expand(source["path"])
        if base.is_symlink() or not base.is_dir():
            continue
        quiet = max(source.get("quietMinutes", 0), cfg["quietMinutes"]) * 60
        common = {"quiet": quiet, "root": cfg["root"], "jobs": bool(source.get("jobs")), "minBytes": int(source.get("minMB", 0)) * 1024 * 1024,
                  "release": cfg.get("release")}
        if not source.get("children"):
            items.append({"path": base, "owner": base.name, **common})
        elif not source.get("keepFolder"):
            items += [{"path": p, "owner": p.name, **common} for p in sorted(base.iterdir())]
        else:
            for folder in sorted(base.iterdir()):
                if folder.is_symlink():
                    split_link(folder, cfg)
                if folder.is_symlink() or not folder.is_dir():
                    continue
                heal_file_links(folder, cfg)
                shared = {"peers": {}}
                made = [{"path": p, "owner": folder.name, "parent": folder, "siblings": shared, **common}
                        for p in sorted(folder.iterdir()) if p.is_dir() and not p.is_symlink()]
                shared["peers"].update({m["path"].name: m for m in made})
                items += made
    out = []
    for item in items:
        try:
            item["rel"] = item["path"].relative_to(home)
        except ValueError:
            continue
        out.append(item)
    return out


def heal_file_links(folder: Path, cfg: dict) -> None:
    """A link to a single file inside a kept folder (made by an earlier version) is replaced by the file itself."""
    root = str(cfg["root"])
    for entry in sorted(folder.iterdir()):
        if entry.is_symlink() and os.readlink(entry).startswith(root) and os.path.isfile(entry):
            target = Path(os.readlink(entry))
            partial = folder / f".offload-{entry.name}.restoring"
            shutil.copy2(target, partial)
            if partial.stat().st_size != target.stat().st_size:
                partial.unlink()
                raise host.HostError(f"copy of {target} does not match; the link was left in place")
            entry.unlink()
            os.rename(partial, entry)
            target.unlink()
            core.reap_receipt({"kind": "offload-file-restored", "path": str(entry), "from": str(target)})


def split_link(folder: Path, cfg: dict) -> None:
    """A whole-folder link from an earlier version becomes a real folder holding one link per entry; nothing is copied."""
    dest = Path(os.readlink(folder))
    if not str(dest).startswith(str(cfg["root"])) or not dest.is_dir():
        return
    holding = folder.parent / f".offload-{folder.name}.splitting"
    holding.mkdir()
    try:
        for entry in sorted(dest.iterdir()):
            if entry.is_dir() and not entry.is_symlink():
                os.symlink(entry, holding / entry.name)
            else:
                shutil.copy2(entry, holding / entry.name)
        folder.unlink()
        os.rename(holding, folder)
    except OSError:
        shutil.rmtree(holding, ignore_errors=True)
        raise
    core.reap_receipt({"kind": "offload-split", "path": str(folder), "from": str(dest)})


def fixture_refusal(path: Path) -> str | None:
    """A fixture folder is delete-or-keep, never moved: test scaffolding the reaper expires, and a moved copy only hid it from
    the fixture tools (J-4302: 14.7 GB of model weights moved to the overflow disk, then `path-not-canonical`)."""
    if path.name.lower() == "fixtures" or path.name.startswith("ut-fixture-"):
        return "fixture-never-moved"
    try:
        for child in [path, *sorted(c for c in path.iterdir() if c.is_dir() and not c.is_symlink())[:2000]]:  # the folder and its children
            if (child / "fixtures").is_dir() and not (child / "fixtures").is_symlink():
                return "holds-fixtures-never-moved"
    except FileNotFoundError:
        return None
    except OSError:
        return "fixtures-unreadable"
    return None


def refusal(item: dict, stats: dict, table: dict, now: float, statuses: dict) -> str | None:
    path = item["path"]
    if path.is_symlink():
        return "already-offloaded" if os.path.realpath(path).startswith(str(item["dest"].parent.parent)) else "symlink"
    if not path.is_dir() or path.name.startswith(".offload-"):
        return "not-a-folder"
    if core.reap_protected(path.name):
        return "protected"
    kept = fixture_refusal(path)
    if kept:
        return kept
    for folder in ([item["parent"]] if item.get("parent") is not None else []) + [path]:
        held = release_refusal(folder, item.get("release"))
        if held:
            return held
    if not stats["complete"]:
        return "walk-incomplete"
    job = job_of(item["owner"]) if item["jobs"] else None
    state = statuses.get(job) if job else None
    if job and state is None:
        return f"job-unknown:{job}"
    if state in LIVE:
        return f"job-live:{job}:{state}"
    quiet = SETTLE_SECONDS if state in FINISHED else item["quiet"]
    item["readQuiet"] = not job  # a job's folder is judged by changes and open handles: TrimReaper's and the job tool's own scans reset atime
    if now - stats["newest"] < quiet:
        return f"changed-within-{int(quiet // 60)}min"
    if item.get("reads") == "recorded" and item.get("readQuiet", True) and now - stats.get("read", 0) < quiet:
        return f"read-within-{int(quiet // 60)}min"
    if stats["bytes"] < item["minBytes"]:
        return "below-minimum-size"
    in_use = git_refusal(path, stats, state in FINISHED)
    if in_use:
        return in_use
    item["settled"] = quiet
    active = parent_active(item, quiet, table, now)
    if active:
        return active
    try:
        core.reap_holders(str(path), str(path), table)
    except core.ReapRefused as refused:
        return str(refused)
    return None


def copy_tree(source: Path, partial: Path) -> None:
    if partial.exists():
        shutil.rmtree(partial)
    partial.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["ditto", str(source), str(partial)], capture_output=True, text=True)
    if result.returncode:
        raise host.HostError(f"copy of {source} failed: {result.stderr.strip()[:300]}")


def signature(path: Path) -> list:
    """Relative path, type and size of every entry; two trees with equal signatures hold the same names and sizes."""
    rows = []
    for current, dirs, files in os.walk(path, followlinks=False):
        dirs.sort()
        for name in sorted(files + [d for d in dirs if os.path.islink(os.path.join(current, d))]):
            full = os.path.join(current, name)
            info = os.lstat(full)
            rows.append((os.path.relpath(full, path), stat.S_IFMT(info.st_mode), info.st_size if stat.S_ISREG(info.st_mode) else 0))
    return rows


def move_one(item: dict, dry_run: bool) -> dict:
    path, dest = item["path"], item["dest"]
    if dry_run:
        return {"path": str(path), "to": str(dest), "bytes": item["bytes"], "moved": False, "reads": item.get("reads")}
    partial = dest.parent / f".offload-{dest.name}.partial"
    core.reap_receipt({"kind": "offload-plan", "path": str(path), "to": str(dest), "bytes": item["bytes"], "reads": item.get("reads")})
    copy_tree(path, partial)
    if signature(path) != signature(partial):
        shutil.rmtree(partial, ignore_errors=True)
        raise host.HostError(f"copy of {path} does not match the original; nothing was removed")
    if tree_stats(path)["newest"] > item["stats"]["newest"]:
        shutil.rmtree(partial, ignore_errors=True)
        raise host.HostError(f"{path} changed during the copy; nothing was removed")
    table = core.reap_process_table()
    # The copy can take minutes: a process may have opened the folder since `refusal` checked it, whether or not our own
    # regeneration touched it. Never swap in that case.
    try:
        core.reap_holders(str(path), str(path), table)
    except core.ReapRefused:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    active = parent_active(item, item.get("settled", item["quiet"]), table, time.time(),
                         {"regen": {n: p["regenDone"] for n, p in item.get("siblings", {}).get("peers", {}).items() if p.get("regenDone")}})
    if active:  # the copy can take minutes: look at the kept folder again before the original is swapped
        shutil.rmtree(partial, ignore_errors=True)
        raise host.HostError(f"{path} was not moved, the kept folder became active during the copy ({active})")
    if dest.exists() and not dest.name.endswith(".partial"):
        # A home symlink can be replaced by a real folder of the same name. That folder is the only copy left in home;
        # the overflow folder is the older copy. Deleting it here would destroy the only surviving copy.
        shutil.rmtree(partial, ignore_errors=True)
        raise host.HostError(f"offload-destination-exists:{dest}: refusing to delete an existing overflow folder; nothing was moved")
    if dest.exists():
        shutil.rmtree(dest)
    os.rename(partial, dest)
    holding = path.parent / f".offload-{path.name}.old"
    os.rename(path, holding)
    os.symlink(dest, path)
    shutil.rmtree(holding)
    core.reap_receipt({"kind": "offload-done", "path": str(path), "to": str(dest), "bytes": item["bytes"], "reads": item.get("reads")})
    return {"path": str(path), "to": str(dest), "bytes": item["bytes"], "moved": True, "reads": item.get("reads")}


def repo_above(path: str) -> tuple[str | None, bool]:
    """The repository enclosing `path`, searched up to the home folder (never only to the candidate folder: a source whose children sit
    inside a repository is still that repository's work), and whether its `.git` file names a git dir that is gone."""
    home = core.reap_canon(host.reap_home())
    current = os.path.dirname(path)
    while core.reap_inside(current, home) and current != home:
        if os.path.lexists(os.path.join(current, ".git")):
            return current, bool(git_dir_gone(current))
        current = os.path.dirname(current)
    return None, False


def regen_first(items: list, statuses: dict, dry_run: bool, now: float, only: str | None) -> dict:  # items: folders `refusal` already passed
    """Regenerable output inside each candidate folder (node_modules, .next, .open-next, .astro, .turbo, __pycache__, a Cargo target's debug,
    incremental and release deps/build) is deleted, never copied: it goes through the cleaner's guarded removal with its gates (protected
    leaves never entered, git ignoring it, no process holding it, quiet), and only what remains is moved. A folder of a live or unknown job
    is not touched. A dry run lists what would go."""
    out = {"deleted": [], "deletedBytes": 0, "kept": [], "errors": [], "touched": set()}
    table, run_id = None, uuid.uuid4().hex[:12]
    for item in items:
        path = item["path"]
        if only and str(path) != str(Path(only)):
            continue
        if path.is_symlink() or not path.is_dir() or path.name.startswith(".offload-") or core.reap_protected(path.name):
            continue
        job = job_of(item["owner"]) if item["jobs"] else None
        state = statuses.get(job) if job else None
        if job and (state is None or state in LIVE):
            continue
        errors: list = []
        found = clean.clean_regen_find(str(path), errors)
        out["errors"] += [f"{path}: {e.get('error')}" for e in errors]
        if not found:
            continue
        window = (SETTLE_SECONDS if state in FINISHED else item["quiet"]) / 3600
        for target, kind in found:
            entry = {"name": "offload-regenerable", "kind": "debris", "path": target, "root": os.path.dirname(target), "paths": [target]}
            repo, broken = repo_above(target)
            if broken:  # nothing can prove what git tracks there: the folder moves whole instead
                out["kept"].append({"path": target, "why": "git-dir-gone:cannot-prove-untracked"})
                continue
            def gates(fresh: bool):
                nonlocal table
                if not fresh and table is None:
                    table = core.reap_process_table()
                clean.clean_regen_gates(target, kind, None if fresh else table, repo)
                clean.clean_stale_tree(target, window, fresh=True)
            try:
                gates(False)
                size = core.reap_metadata_size(target, 60)
                entry["bytes"] = size["bytes"] or 0
                if dry_run:
                    out["deleted"].append({"path": target, "bytes": entry["bytes"], "kind": kind, "deleted": False})
                    out["deletedBytes"] += entry["bytes"]
                    continue
                with core.reap_cleanup_owner("clean"):
                    clean.clean_remove_folder(entry, run_id, entry["root"], False, lambda: gates(True))
                if entry.get("outcome") != "removed":
                    out["kept"].append({"path": target, "why": str(entry.get("reason") or entry.get("outcome"))})
                    continue
                out["touched"].add(path)
                out["deleted"].append({"path": target, "bytes": entry["bytes"], "kind": kind, "deleted": True})
                out["deletedBytes"] += entry["bytes"]
            except (core.ReapRefused, OSError, host.HostError) as refused:
                out["kept"].append({"path": target, "why": str(refused) if not isinstance(refused, OSError) else clean.clean_os_refusal(refused)})
    return out


def offload_pass(dry_run: bool = False, limit_bytes: int | None = None, only: str | None = None, everything: bool = False) -> dict:
    cfg = settings()
    if not cfg:
        return {"skipped": "offload is not configured (config key `offload`)", "moved": [], "kept": [], "errors": []}
    root = cfg["root"]
    cause = volume_ready(root)
    if cause:
        return {"skipped": cause, "moved": [], "kept": [], "errors": []}
    statuses = job_statuses(cfg)
    returned = bring_back_live(cfg, statuses, dry_run)
    orphans = prune_orphans(cfg, statuses, dry_run) if not only else {"pruned": [], "prunedBytes": 0, "kept": [], "keptCount": 0, "errors": []}
    free = shutil.disk_usage(mount_of(root)).free
    floor = int(cfg["minFreeGB"] * 1024 ** 3)
    table = core.reap_process_table()
    now = time.time()
    moved, kept, errors, planned_bytes = [], [], [], 0
    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)
        readme = root / "README.txt"
        if not readme.exists():
            readme.write_text(README)
    items = candidates(cfg)
    for item in items:
        item["dest"] = root / "home" / item["rel"]
    sized = []
    for item in items:
        if only and str(item["path"]) != str(Path(only)):
            continue
        real = item["path"].is_dir() and not item["path"].is_symlink()
        item["stats"] = tree_stats(item["path"]) if real else {"complete": True, "newest": now, "bytes": 0, "entries": 0}
        item["bytes"] = item["stats"]["bytes"]
        off = reads_recorded(item["path"]) if real else None
        item["reads"] = "recorded" if off is None else f"not-recorded: {off}; only changes count as use"
        sized.append(item)
    judged = {item["path"]: refusal(item, item["stats"], table, now, statuses) for item in sized}
    # Plan first: the folders that will really move (size, goal and limit rules applied to the sizes before any deletion).
    plan, planned = [], 0
    for item in sorted(sized, key=lambda i: -i["bytes"]):
        if judged[item["path"]]:
            continue
        if free - planned - item["bytes"] < floor:
            judged[item["path"]] = "overflow-volume-would-drop-below-minFreeGB"
        elif not everything and goal_met(planned):
            judged[item["path"]] = "disk-goal-met"
        elif limit_bytes is not None and planned >= limit_bytes:
            judged[item["path"]] = "pass-limit-reached"
        else:
            plan.append(item)
            planned += item["bytes"]
    regen = regen_first(plan, statuses, dry_run, now, only)
    for item in plan:  # after our own deletions: the quiet verdict stands, the size is what is left, and our own writes are not "activity"
        if not dry_run and item["path"] in regen["touched"] and item["path"].is_dir():
            item["stats"] = tree_stats(item["path"])
            item["bytes"] = item["stats"]["bytes"]
            item["regenDone"] = time.time()
            if item["bytes"] < item["minBytes"]:
                judged[item["path"]] = "below-minimum-size"
    for item in sorted(sized, key=lambda i: -i["bytes"]):
        why = judged[item["path"]]
        if why:
            if why != "already-offloaded":
                entry = {"path": str(item["path"]), "why": why, "bytes": item["bytes"]}
                if item["reads"] != "recorded":
                    entry["reads"] = item["reads"]  # the quiet judgement could not see reads on this volume
                kept.append(entry)
            continue
        try:
            moved.append(move_one(item, dry_run))
            planned_bytes += item["bytes"]
        except (host.HostError, core.ReapRefused, OSError) as error:
            errors.append(f"{item['path']}: {error}")
    errors = orphans["errors"] + regen["errors"] + errors
    return {"root": str(root), "dryRun": dry_run, "movedBytes": planned_bytes, "moved": moved,
            "regenerable": {"dryRun": dry_run, "count": len(regen["deleted"]), "bytes": regen["deletedBytes"], "deleted": regen["deleted"][:200],
                            "kept": regen["kept"][:60], "keptCount": len(regen["kept"])}, "returned": returned, "kept": kept[:60],
            "keptCount": len(kept), "errors": errors, "disk": host.disk_state(),
            "orphans": {k: orphans[k] for k in ("pruned", "prunedBytes", "kept", "keptCount")}}


def mirrors_of(cfg: dict):
    """(mirror folder on the overflow volume, the home path it was moved from, the owner folder that must hold it or None, source) for
    every moved folder the configured sources can have left there, at the depth that source moves."""
    root, home = cfg["root"] / "home", host.reap_home()
    for source in cfg["sources"]:
        base = host.expand(source["path"])
        try:
            mirror = root / base.relative_to(home)
        except ValueError:
            continue
        if not mirror.is_dir() or mirror.is_symlink():
            continue
        if not source.get("children"):
            yield mirror, base, base.parent, source
            continue
        for child in sorted(mirror.iterdir()):
            if child.is_symlink() or not child.is_dir() or child.name.startswith(".offload-"):
                continue
            if source.get("keepFolder"):
                for inner in sorted(child.iterdir()):
                    if not inner.is_symlink() and inner.is_dir() and not inner.name.startswith(".offload-"):
                        yield inner, base / child.name / inner.name, base / child.name, source
            else:
                yield child, base / child.name, None, source  # the moved child was its own owner: its link going is its expiry


def kept_by_marker(path: Path) -> str | None:
    """The first preserving retention marker (`.ut-retention-*` other than disposable) in a tree, `walk-incomplete` when the
    bounded walk could not see all of it, else None. Names only; nothing is opened."""
    start, entries, stack = time.time(), 0, [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    entries += 1
                    if entry.name.startswith(core.REAP_MARKER_PREFIX) and entry.name != core.REAP_MARKER_PREFIX + "disposable":
                        return entry.name
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
        except OSError:
            return "walk-incomplete"
        if entries > WALK_ENTRIES or time.time() - start > WALK_SECONDS:
            return "walk-incomplete"
    return None


def prune_orphans(cfg: dict, statuses: dict, dry_run: bool) -> dict:
    """Moved folders whose home owner is gone (the reaper expired the scratch or job folder that held the link) are orphans: no
    path in home reaches them and no rule would ever look at them again, so the overflow volume only grew. Such a folder goes
    unless it holds a preserving retention marker, its job is live, or a process holds it. A folder whose owner is still there
    but whose link is missing is kept and reported: that is not the reaper's doing."""
    pruned, kept, errors, freed = [], [], [], 0
    table = None
    for mirror, original, owner, source in mirrors_of(cfg):
        if os.path.lexists(original):
            continue  # still linked (or restored): not an orphan
        if owner is not None and owner.exists():
            kept.append({"path": str(mirror), "why": "owner-present-link-missing"})
            continue
        job = job_of(owner.name if owner is not None and source.get("keepFolder") else original.name) if source.get("jobs") else None
        if job and statuses.get(job) in LIVE:
            kept.append({"path": str(mirror), "why": f"job-live:{job}"})
            continue
        marker = kept_by_marker(mirror)
        if marker:
            kept.append({"path": str(mirror), "why": f"retention:{marker}"})
            continue
        stats = tree_stats(mirror)
        try:
            table = table or core.reap_process_table()
            core.reap_holders(str(mirror), str(mirror), table)
        except core.ReapRefused as refused:
            kept.append({"path": str(mirror), "why": str(refused)})
            continue
        if dry_run:
            pruned.append({"path": str(mirror), "from": str(original), "bytes": stats["bytes"], "pruned": False})
            continue
        holding = mirror.parent / f".offload-{mirror.name}.pruning"
        try:
            core.reap_receipt({"kind": "offload-prune-plan", "path": str(mirror), "from": str(original), "bytes": stats["bytes"]})
            os.rename(mirror, holding)
            shutil.rmtree(holding)
            core.reap_receipt({"kind": "offload-prune-done", "path": str(mirror), "from": str(original), "bytes": stats["bytes"]})
        except (OSError, core.ReapRefused) as error:
            errors.append(f"{mirror}: {error}")
            continue
        freed += stats["bytes"]
        pruned.append({"path": str(mirror), "from": str(original), "bytes": stats["bytes"], "pruned": True})
    return {"pruned": pruned, "prunedBytes": freed, "kept": kept[:60], "keptCount": len(kept), "errors": errors}


def goal_met(planned_bytes: int) -> bool:
    """True once the home disk is at or under cleanToPercent, counting what this pass already planned to move."""
    disk = host.disk_state()
    if not disk or not disk.get("usedBytes") or disk.get("freeBytes") is None:
        return False
    goal = host.load_policy()["cleanToPercent"]
    return (disk["usedBytes"] - planned_bytes) / (disk["usedBytes"] + disk["freeBytes"]) * 100 <= goal


def links_of(cfg: dict):
    """(owner name, link path) for every offloaded link under the sources, whole-folder or per-entry."""
    root = str(cfg["root"])
    for source in cfg["sources"]:
        base = host.expand(source["path"])
        if base.is_symlink():
            if os.readlink(base).startswith(root):
                yield base.name, base, source
            continue
        if not base.is_dir():
            continue
        for path in sorted(base.iterdir()):
            if path.is_symlink():
                if os.readlink(path).startswith(root):
                    yield path.name, path, source
            elif source.get("children") and source.get("keepFolder") and path.is_dir():
                for inner in sorted(path.iterdir()):
                    if inner.is_symlink() and os.readlink(inner).startswith(root):
                        yield path.name, inner, source


def bring_back_live(cfg: dict, statuses: dict, dry_run: bool) -> list:
    """A moved folder whose job is live again returns to the internal disk before its work needs the speed."""
    back = []
    for owner, path, source in links_of(cfg):
        job = job_of(owner) if source.get("jobs") else None
        if not job or statuses.get(job) not in LIVE:
            continue
        if not dry_run:
            try:
                restore(str(path))
            except (host.HostError, core.ReapRefused, OSError) as error:
                back.append({"path": str(path), "error": str(error)})
                continue
        back.append({"path": str(path), "job": job, "status": statuses[job]})
    return back


def status() -> dict:
    cfg = settings()
    if not cfg:
        return {"configured": False}
    root = cfg["root"]
    cause = volume_ready(root)
    links = [{"path": str(path), "to": os.readlink(path), "present": os.path.exists(path)} for _, path, _ in links_of(cfg)]
    out = {"configured": True, "root": str(root), "ready": cause is None, "cause": cause, "offloaded": len(links),
           "dangling": [l["path"] for l in links if not l["present"]]}
    if cause is None:
        usage = shutil.disk_usage(mount_of(root))
        out["volumeFreeGB"] = round(usage.free / 1024 ** 3, 1)
    return out


def restore(path: str) -> dict:
    target = Path(path).expanduser()
    if not target.is_symlink():
        raise host.HostError(f"{target} is not an offloaded link")
    dest = Path(os.readlink(target))
    cfg = settings()
    if not cfg or not str(dest).startswith(str(cfg["root"])):
        raise host.HostError(f"{target} does not point into the overflow root")
    if not dest.is_dir():
        raise host.HostError(f"{dest} is missing; is the drive mounted?")
    partial = target.parent / f".offload-{target.name}.restoring"
    core.reap_receipt({"kind": "offload-restore-plan", "path": str(target), "from": str(dest)})
    copy_tree(dest, partial)
    if signature(dest) != signature(partial):
        shutil.rmtree(partial, ignore_errors=True)
        raise host.HostError("restored copy does not match; the link was left in place")
    target.unlink()
    os.rename(partial, target)
    shutil.rmtree(dest)
    core.reap_receipt({"kind": "offload-restore-done", "path": str(target), "from": str(dest)})
    return {"restored": str(target)}
