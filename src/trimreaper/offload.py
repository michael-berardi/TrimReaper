"""Overflow to an external volume: move cold folders there, leave a symlink behind.

Nothing is deleted before the copy is proved. A folder moves only when it is idle (nothing changed inside for
`idleDays`), no process has it open, and the volume is mounted with room to spare. The original path becomes a symlink
into the overflow root, so every tool keeps working; `restore` brings a folder back. Every step writes a receipt first.

Configuration (config.json, key `offload`):
    {"root": "/Volumes/Disk/Overflow", "minFreeGB": 20,
     "sources": [{"path": "~/.cache/jobs", "idleDays": 3, "children": true}]}
`children: true` moves each direct child separately; otherwise the source folder itself moves.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

from . import core, host

DAY = 86400
WALK_ENTRIES = 400_000
WALK_SECONDS = 20
README = ("This folder is TrimReaper's overflow: cold folders moved off the Mac's internal disk.\n"
          "Each folder keeps its home-relative path below here, and the original location is a symlink to it.\n"
          "Bring one back with `trimreaper offload restore <original path>`.\n")


def settings() -> dict | None:
    cfg = host.config().get("offload")
    if not cfg:
        return None
    return {"root": host.expand(cfg["root"]), "minFreeGB": cfg.get("minFreeGB", 20), "sources": cfg.get("sources", [])}


def check(cfg: dict, path: Path) -> None:
    """Config shape, named failures."""
    if not isinstance(cfg, dict) or set(cfg) - {"root", "minFreeGB", "sources"}:
        raise host.HostError(f"{path}: offload takes root, minFreeGB and sources only")
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
        if not isinstance(source, dict) or set(source) - {"path", "idleDays", "children", "minMB"} or not isinstance(source.get("path"), str):
            raise host.HostError(f"{path}: offload source {json.dumps(source)} needs a path and may have idleDays, children, minMB")
        host.path_form(source["path"])
        days = source.get("idleDays", 3)
        if isinstance(days, bool) or not isinstance(days, (int, float)) or days < 0:
            raise host.HostError(f"{path}: offload source idleDays must be a number of days, not {json.dumps(days)}")


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
    """Entries, bytes and newest change (mtime/ctime) of a tree; never follows links. `complete` False when a bound hit."""
    start, entries, size, newest = time.time(), 0, 0, 0.0
    stack, complete = [str(path)], True
    while stack:
        current = stack.pop()
        try:
            info = os.lstat(current)
            newest = max(newest, info.st_mtime, info.st_ctime)
            entries += 1
            if stat.S_ISREG(info.st_mode):
                size += info.st_blocks * 512
            elif stat.S_ISDIR(info.st_mode):
                with os.scandir(current) as it:
                    stack.extend(entry.path for entry in it)
        except OSError:
            complete = False
        if entries > WALK_ENTRIES or time.time() - start > WALK_SECONDS:
            complete = False
            break
    return {"entries": entries, "bytes": size, "newest": newest, "complete": complete}


def candidates(cfg: dict) -> list[dict]:
    items = []
    home = host.reap_home()
    for source in cfg["sources"]:
        base = host.expand(source["path"])
        if base.is_symlink() or not base.is_dir():
            continue
        targets = [p for p in sorted(base.iterdir())] if source.get("children") else [base]
        for target in targets:
            items.append({"path": target, "idleDays": source.get("idleDays", 3), "minBytes": int(source.get("minMB", 0)) * 1024 * 1024})
    out = []
    for item in items:
        try:
            item["rel"] = item["path"].relative_to(home)
        except ValueError:
            continue
        out.append(item)
    return out


def refusal(item: dict, stats: dict, table: dict, now: float) -> str | None:
    path = item["path"]
    if path.is_symlink():
        return "already-offloaded" if os.path.realpath(path).startswith(str(item["dest"].parent.parent)) else "symlink"
    if not path.is_dir() or path.name.startswith(".offload-"):
        return "not-a-folder"
    if core.reap_protected(path.name):
        return "protected"
    if not stats["complete"]:
        return "walk-incomplete"
    if now - stats["newest"] < item["idleDays"] * DAY:
        return f"changed-within-{item['idleDays']}d"
    if stats["bytes"] < item["minBytes"]:
        return "below-minimum-size"
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
        return {"path": str(path), "to": str(dest), "bytes": item["bytes"], "moved": False}
    partial = dest.parent / f".offload-{dest.name}.partial"
    core.reap_receipt({"kind": "offload-plan", "path": str(path), "to": str(dest), "bytes": item["bytes"]})
    copy_tree(path, partial)
    if signature(path) != signature(partial):
        shutil.rmtree(partial, ignore_errors=True)
        raise host.HostError(f"copy of {path} does not match the original; nothing was removed")
    if tree_stats(path)["newest"] > item["stats"]["newest"]:
        shutil.rmtree(partial, ignore_errors=True)
        raise host.HostError(f"{path} changed during the copy; nothing was removed")
    if dest.exists():
        shutil.rmtree(dest)
    os.rename(partial, dest)
    holding = path.parent / f".offload-{path.name}.old"
    os.rename(path, holding)
    os.symlink(dest, path)
    shutil.rmtree(holding)
    core.reap_receipt({"kind": "offload-done", "path": str(path), "to": str(dest), "bytes": item["bytes"]})
    return {"path": str(path), "to": str(dest), "bytes": item["bytes"], "moved": True}


def offload_pass(dry_run: bool = False, limit_bytes: int | None = None, only: str | None = None) -> dict:
    cfg = settings()
    if not cfg:
        return {"skipped": "offload is not configured (config key `offload`)", "moved": [], "kept": [], "errors": []}
    root = cfg["root"]
    cause = volume_ready(root)
    if cause:
        return {"skipped": cause, "moved": [], "kept": [], "errors": []}
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
        item["stats"] = tree_stats(item["path"]) if item["path"].is_dir() and not item["path"].is_symlink() else {"complete": True, "newest": now, "bytes": 0, "entries": 0}
        item["bytes"] = item["stats"]["bytes"]
        sized.append(item)
    for item in sorted(sized, key=lambda i: -i["bytes"]):
        why = refusal(item, item["stats"], table, now)
        if why:
            if why != "already-offloaded":
                kept.append({"path": str(item["path"]), "why": why, "bytes": item["bytes"]})
            continue
        if free - planned_bytes - item["bytes"] < floor:
            kept.append({"path": str(item["path"]), "why": "overflow-volume-would-drop-below-minFreeGB", "bytes": item["bytes"]})
            continue
        if limit_bytes is not None and planned_bytes >= limit_bytes:
            kept.append({"path": str(item["path"]), "why": "pass-limit-reached", "bytes": item["bytes"]})
            continue
        try:
            moved.append(move_one(item, dry_run))
            planned_bytes += item["bytes"]
        except (host.HostError, core.ReapRefused, OSError) as error:
            errors.append(f"{item['path']}: {error}")
    return {"root": str(root), "dryRun": dry_run, "movedBytes": planned_bytes, "moved": moved, "kept": kept[:60],
            "keptCount": len(kept), "errors": errors, "disk": host.disk_state()}


def status() -> dict:
    cfg = settings()
    if not cfg:
        return {"configured": False}
    root = cfg["root"]
    cause = volume_ready(root)
    links = []
    for source in cfg["sources"]:
        base = host.expand(source["path"])
        if not base.is_dir() and not base.is_symlink():
            continue
        for path in ([base] if base.is_symlink() else sorted(base.iterdir())):
            if path.is_symlink() and str(os.readlink(path)).startswith(str(root)):
                links.append({"path": str(path), "to": os.readlink(path), "present": os.path.exists(path)})
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
