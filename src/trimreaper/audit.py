"""`trimreaper audit`: bounded, metadata-only, protected-leaf-pruned top-down disk hog diagnostic.

Reads filesystem metadata only (paths, ownership, types, allocated blocks `st_blocks * 512`), never opens file
contents, never follows symlinks, and never enters protected leaves (`.wrangler`, `.vercel`, `.env*`, `.dev.vars*`).
Accounts for writable volume occupancy, macOS APFS purgeable space, local snapshots, swap, sparse-vs-allocated VM
images, TCC/root-opaque directories, and explains how TrimReaper's rules and configuration cover each top consumer.
"""
from __future__ import annotations

import os
import plistlib
import pwd
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import clean, core, host, offload

DEFAULT_MIN_MB = 256
DEFAULT_BUDGET_SECONDS = 30
DEFAULT_TOP = 25
PER_ROOT_ENTRY_LIMIT = 400_000
MAX_DEPTH = 64


def _tilde(path: str, home: str) -> str:
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def _bounded_size(path: str, deadline: float, entry_limit: int = PER_ROOT_ENTRY_LIMIT) -> dict:
    """Allocated-blocks size walk (st_blocks * 512) that never follows links, never crosses devices, and never enters
    or stats protected leaves (`.wrangler`, `.vercel`, `.env*`, `.dev.vars*`). Records EPERM / opaque dirs explicitly."""
    out = {
        "bytes": 0,
        "complete": True,
        "reason": None,
        "entries": 0,
        "opaque": [],
        "prunedProtected": 0,
    }
    try:
        root = os.lstat(path)
    except PermissionError:
        out["complete"] = False
        out["reason"] = "permission-denied"
        out["opaque"].append(path)
        return out
    except OSError as error:
        out["complete"] = False
        out["reason"] = core.reap_named(error)
        return out
    if stat.S_ISLNK(root.st_mode):
        return out
    if not stat.S_ISDIR(root.st_mode):
        out["bytes"] = root.st_blocks * 512
        out["entries"] = 1
        return out
    device = root.st_dev
    seen_inodes: set[tuple[int, int]] = set()
    stack: list[tuple[str, int]] = [(path, 0)]
    while stack:
        if time.monotonic() >= deadline:
            out["complete"] = False
            out["reason"] = "time-budget"
            break
        folder, depth = stack.pop()
        if depth > MAX_DEPTH:
            out["complete"] = False
            out["reason"] = out["reason"] or "depth-limit"
            continue
        try:
            with os.scandir(folder) as scan:
                for entry in scan:
                    out["entries"] += 1
                    if out["entries"] > entry_limit:
                        out["complete"] = False
                        out["reason"] = "entry-limit"
                        return out
                    if out["entries"] % 512 == 0 and time.monotonic() >= deadline:
                        out["complete"] = False
                        out["reason"] = "time-budget"
                        return out
                    if core.reap_protected(entry.name):
                        out["prunedProtected"] += 1
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except PermissionError:
                        out["complete"] = False
                        out["reason"] = out["reason"] or "permission-denied"
                        if len(out["opaque"]) < 30:
                            out["opaque"].append(entry.path)
                        continue
                    except OSError:
                        continue
                    if info.st_dev != device or stat.S_ISLNK(info.st_mode):
                        continue
                    key = (info.st_dev, info.st_ino)
                    if key not in seen_inodes:
                        seen_inodes.add(key)
                        out["bytes"] += info.st_blocks * 512
                    if stat.S_ISDIR(info.st_mode):
                        stack.append((entry.path, depth + 1))
        except PermissionError:
            out["complete"] = False
            out["reason"] = out["reason"] or "permission-denied"
            if len(out["opaque"]) < 30:
                out["opaque"].append(folder)
        except OSError:
            out["complete"] = False
            out["reason"] = out["reason"] or "unreadable"
    return out


def _is_real_home(home: str) -> bool:
    """True when `home` is this user's real OS home directory (false in throwaway test sandboxes)."""
    try:
        real_home = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        return False
    return core.reap_canon(home) == core.reap_canon(real_home)


def _snapshots(real_home: bool) -> dict:
    """Local APFS snapshots (macOS only; read-only)."""
    if sys.platform != "darwin" or not real_home:
        return {"count": 0, "snapshots": []}
    code, out = clean.clean_run(["tmutil", "listlocalsnapshots", "/"], 15)
    if code != 0:
        return {"count": 0, "snapshots": [], "error": f"tmutil-exit-{code}"}
    snaps = [line.strip() for line in out.splitlines() if line.strip() and not line.lower().startswith("snapshots for ")]
    return {"count": len(snaps), "snapshots": snaps[:30]}


def _schedule_state(real_home: bool) -> dict:
    """Whether TrimReaper's own launchd schedule plist is installed and loaded (macOS read-only check)."""
    if sys.platform != "darwin" or not real_home:
        return {"supported": False, "installed": False, "loaded": False}
    label = "io.github.trimreaper.tick"
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    installed = plist.is_file()
    loaded = False
    every_min = None
    if installed:
        try:
            parsed = plistlib.loads(plist.read_bytes())
            if isinstance(parsed, dict) and isinstance(parsed.get("StartInterval"), int):
                every_min = parsed["StartInterval"] // 60
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    try:
        done = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                              capture_output=True, text=True, errors="replace", timeout=10)
        loaded = done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        loaded = False
    return {"supported": True, "installed": installed, "loaded": loaded, "everyMinutes": every_min}


def _classify_hog(path: str, home: str, cfg: dict, policy: dict, sched: dict) -> tuple[str, str]:
    """Generic, provider-neutral explanation of whether TrimReaper covers a directory and why space remains there."""
    canon = core.reap_canon(path)
    home_canon = core.reap_canon(home)
    rel = os.path.relpath(canon, home_canon) if core.reap_inside(canon, home_canon) else ""
    parts = Path(rel).parts if rel and rel != "." else ()

    # Configured offload sources
    off_cfg = cfg.get("offload")
    if isinstance(off_cfg, dict):
        for source in off_cfg.get("sources") or ():
            src_path = source.get("path")
            if not isinstance(src_path, str):
                continue
            try:
                src_canon = core.reap_canon(host.expand(src_path))
            except (host.HostError, OSError):
                continue
            if canon == src_canon or core.reap_inside(canon, src_canon):
                if not sched.get("loaded") and sched.get("supported"):
                    return "offload-configured", "listed in offload.sources, but trimreaper schedule is not loaded (run `trimreaper offload run` or install schedule)"
                return "offload-configured", "covered by offload.sources; kept while recently active, dirty/unpushed, below minMB, or above disk goal"

    # Configured compressLogsRoots
    for root in policy.get("compressLogsRoots") or ():
        try:
            c_root = core.reap_canon(clean.clean_expand(root))
        except OSError:
            continue
        if canon == c_root or core.reap_inside(canon, c_root):
            if not sched.get("loaded") and sched.get("supported"):
                return "compress-logs", "configured in compressLogsRoots, but trimreaper schedule is not loaded"
            return "compress-logs", "covered by compress-logs (lossless compression of idle files)"

    # Configured scratch / cache / clone roots
    scratch = cfg.get("scratchRoot")
    if scratch:
        try:
            s_canon = core.reap_canon(host.expand(scratch))
            if canon == s_canon or core.reap_inside(canon, s_canon):
                return "covered:agent-scratch", "managed by scratchRoot / rust-build-output / tagged-caches; kept while recently modified, holding protected leaves, or unsaved git work"
        except (host.HostError, OSError):
            pass

    for coord in policy.get("coordinatorRoots") or ():
        try:
            c_canon = core.reap_canon(clean.clean_expand(coord))
            if canon == c_canon or core.reap_inside(canon, c_canon):
                if not policy.get("coordinatorJobListCommand"):
                    return "covered:coordinator-outputs", "covered by coordinator-outputs; set policy.coordinatorJobListCommand for fast batch job lookup"
                return "covered:coordinator-outputs", "covered by coordinator-outputs; kept for live/unknown jobs or non-regenerable data"
        except OSError:
            pass

    if parts:
        first = parts[0]
        if first in ("Models", ".ollama", ".lmstudio") or (len(parts) >= 2 and parts[:2] == (".cache", "huggingface")):
            return "report-only:model", "local AI models are never auto-deleted; remove or offload explicitly if unused"
        if first == ".colima":
            return "report-only:vm-disk", "Colima VM disk (check sparse vs allocated); use `trimreaper clean --trim-vm` when stopped"
        if first in (".cargo", ".npm", ".bun", ".pnpm-store"):
            if not sched.get("loaded") and sched.get("supported"):
                return "covered:tool-cache", "covered by `trimreaper clean`, but schedule is not loaded (or tool was active)"
            return "covered:tool-cache", "covered by `trimreaper clean` when tool is idle"
        if len(parts) >= 2 and parts[:2] in ((".cache", "uv"), (".cache", "pip"), (".cache", "go-build"),
                                              (".cache", "puppeteer"), (".cache", "Cypress"), (".cache", "electron"),
                                              (".cache", "ruff"), (".cache", "mypy"),
                                              (".cache", "node-gyp"), (".cache", "typescript"), (".cache", "cargo-xwin")):
            return "covered:tool-cache", "covered by `trimreaper clean` when idle and above size threshold"
        if len(parts) >= 3 and parts[:3] in (("Library", "Caches", "org.swift.swiftpm"), ("Library", "Caches", "ms-playwright"),
                                              ("Library", "Caches", "Homebrew"), ("Library", "Caches", "cargo-xwin"),
                                              ("Library", "Developer", "Xcode"), ("Library", "Developer", "CoreSimulator")):
            return "covered:macos-cache", "covered by `trimreaper clean` (cache/DerivedData/unavailable simulators only; Archives & Devices stay)"
        if first in clean.CLEAN_CODE_HOMES and first != ".cache":
            return "covered:project-outputs", "source code stays; regenerable build outputs (node_modules, .next, .build, .cloudflare, target/debug) clean when idle"
        if first == ".cache":
            return "unconfigured:cache", "unrecognized ~/.cache subfolder; not auto-deleted without CACHEDIR.TAG or offload.sources config"
        if first in (".local", ".rustup", "Downloads", "Documents", "Desktop"):
            return "non-disposable:user-or-runtime", "application/runtime/user data; never auto-deleted (consider offload.sources if cold)"
        if first == "Library":
            return "non-disposable:library", "macOS Library data (app support, toolchains, caches); non-cache subdirs are opt-in only"
    if core.reap_inside(canon, "/Applications"):
        return "non-disposable:applications", "installed macOS applications (opt-in only)"
    if core.reap_inside(canon, "/opt/homebrew"):
        return "non-disposable:homebrew", "installed Homebrew packages (only cache/old versions cleaned by `brew cleanup`)"
    if any(core.reap_inside(canon, root) for root in ("/private/var/folders", "/var/folders", "/private/tmp", "/tmp")):
        return "covered:temp-and-darwin-caches", "recognized temp & Darwin cache dirs clean via `trimreaper clean` once idle and unheld"
    return "unclassified", "outside built-in disposable rules; inspect before deleting or adding to offload.sources"


def _candidate_targets(home: str) -> list[tuple[str, str]]:
    """Bounded set of well-known top-level and developer roots to measure (never an unbounded whole-home walk)."""
    targets: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add_children(parent: str, group: str, include_hidden: bool = True) -> None:
        if not parent or not os.path.isdir(parent) or os.path.islink(parent):
            return
        try:
            with os.scandir(parent) as scan:
                for entry in scan:
                    if not include_hidden and entry.name.startswith("."):
                        continue
                    if core.reap_protected(entry.name) or entry.is_symlink():
                        continue
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if entry.path not in seen:
                        seen.add(entry.path)
                        targets.append((entry.path, group))
        except OSError:
            pass

    def add_dir(path: str | None, group: str) -> None:
        if not path or path in seen or not os.path.isdir(path) or os.path.islink(path):
            return
        seen.add(path)
        targets.append((path, group))

    # Home direct children, expanding .cache and Library/Caches one level deeper for actionable attribution
    cache_dir = os.path.join(home, ".cache")
    lib_dir = os.path.join(home, "Library")
    lib_caches = os.path.join(lib_dir, "Caches")
    lib_app_support = os.path.join(lib_dir, "Application Support")
    lib_dev = os.path.join(lib_dir, "Developer")

    try:
        with os.scandir(home) as scan:
            for entry in scan:
                if core.reap_protected(entry.name) or entry.is_symlink():
                    continue
                if entry.name in (".Trash", ".cache", "Library"):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False) and entry.path not in seen:
                        seen.add(entry.path)
                        targets.append((entry.path, "home"))
                except OSError:
                    continue
    except OSError:
        pass

    add_children(cache_dir, "cache")
    add_children(lib_caches, "library-caches")
    add_children(lib_app_support, "library-app-support")
    add_children(lib_dev, "library-developer")
    add_dir(os.path.join(lib_dir, "org.swift.swiftpm"), "library")

    # Per-user Darwin cache/temp and shared temp (only when auditing the real user home, never test sandboxes)
    real_home = _is_real_home(home)
    if real_home:
        darwin_c = clean.clean_darwin_cache_dir()
        add_dir(darwin_c, "darwin-cache")
        tmp_env = os.environ.get("TMPDIR")
        if tmp_env:
            add_dir(os.path.realpath(tmp_env), "user-temp")
        add_dir(os.path.realpath("/tmp"), "shared-temp")

    # System developer roots on macOS (measured only when auditing the real home)
    if sys.platform == "darwin" and real_home:
        for sys_root in ("/Applications", "/opt/homebrew", "/Library/Developer"):
            add_dir(sys_root, "system")

    return targets


def run_audit(min_mb: int = DEFAULT_MIN_MB, budget_seconds: int = DEFAULT_BUDGET_SECONDS, top: int = DEFAULT_TOP) -> dict:
    """Bounded, read-only top-down disk audit with APFS purgeable space, snapshots, VM images, and per-hog coverage."""
    started = time.monotonic()
    budget = max(5, min(int(budget_seconds), 300))
    deadline = started + budget
    min_bytes = max(0, int(min_mb)) * 1024 * 1024
    top_n = max(1, min(int(top), 200))

    home = str(host.reap_home())
    real_home = _is_real_home(home)
    cfg = host.config()
    policy = host.load_policy()
    disk = host.disk_state()
    volume = disk["path"] if disk and disk.get("path") else "/System/Volumes/Data"
    purgeable = clean.clean_apfs_purgeable(volume) if real_home else None
    snapshots = _snapshots(real_home)
    swap = clean.clean_swap_inventory() if real_home else None
    sched = _schedule_state(real_home)
    off_status = offload.status()
    vm_disks = clean.clean_vm_inventory()

    # Include Docker.raw sparse vs allocated if present
    docker_raw = os.path.join(home, "Library", "Containers", "com.docker.docker", "Data", "vms", "0", "data", "Docker.raw")
    try:
        d_info = os.lstat(docker_raw)
        vm_disks.append({
            "kind": "disk-image",
            "name": "docker",
            "path": _tilde(docker_raw, home),
            "bytes": d_info.st_blocks * 512,
            "logicalBytes": d_info.st_size,
            "sizeComplete": True,
        })
    except OSError:
        pass
    for vm in vm_disks:
        if vm.get("path"):
            vm["path"] = _tilde(str(vm["path"]), home)

    targets = _candidate_targets(home)
    per_root_slice = max(1.0, min(8.0, (budget * 0.85) / max(1, len(targets) / 4)))

    def measure(item: tuple[str, str]) -> dict:
        path, group = item
        root_deadline = min(deadline, time.monotonic() + per_root_slice)
        res = _bounded_size(path, root_deadline)
        return {"path": path, "group": group, **res}

    measured = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for row in pool.map(measure, targets):
            measured.append(row)

    total_measured = sum(r["bytes"] for r in measured)
    incomplete_roots = [
        {"path": _tilde(r["path"], home), "reason": r["reason"], "bytesLowerBound": r["bytes"]}
        for r in measured if not r["complete"]
    ]
    opaque_paths = []
    pruned_protected = 0
    for r in measured:
        pruned_protected += r["prunedProtected"]
        for op in r["opaque"]:
            if len(opaque_paths) < 50:
                opaque_paths.append(_tilde(op, home))

    hogs = []
    for r in sorted(measured, key=lambda x: -x["bytes"]):
        if r["bytes"] < min_bytes:
            continue
        coverage, why = _classify_hog(r["path"], home, cfg, policy, sched)
        hogs.append({
            "path": _tilde(r["path"], home),
            "group": r["group"],
            "bytes": r["bytes"],
            "complete": r["complete"],
            "coverage": coverage,
            "why": why,
            **({"reason": r["reason"]} if r["reason"] else {}),
        })
        if len(hogs) >= top_n:
            break

    used_bytes = disk.get("usedBytes") if disk else None
    purgeable_bytes = purgeable.get("purgeableBytes") if purgeable else None
    unaccounted = None
    if used_bytes is not None:
        unaccounted = max(0, used_bytes - total_measured - (purgeable_bytes or 0))

    return {
        "disk": disk,
        "purgeable": purgeable,
        "snapshots": snapshots,
        "swap": swap,
        "schedule": sched,
        "offload": {
            "configured": off_status.get("configured", False),
            "ready": off_status.get("ready"),
            "cause": off_status.get("cause"),
            "offloaded": off_status.get("offloaded", 0),
            "danglingCount": len(off_status.get("dangling") or ()),
            "volumeFreeGB": off_status.get("volumeFreeGB"),
        },
        "vmDisks": vm_disks,
        "accounting": {
            "volumeUsedBytes": used_bytes,
            "measuredBytes": total_measured,
            "purgeableBytes": purgeable_bytes,
            "unmeasuredOrOpaqueBytes": unaccounted,
            "prunedProtectedLeaves": pruned_protected,
            "incompleteRootCount": len(incomplete_roots),
            "note": "unmeasuredOrOpaqueBytes covers macOS TCC/root-opaque folders, system volumes/MobileAssets, and protected leaves never entered",
        },
        "topHogs": hogs,
        "incompleteRoots": incomplete_roots[:30],
        "opaquePaths": opaque_paths,
        "elapsedSeconds": round(time.monotonic() - started, 2),
    }
