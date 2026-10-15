"""trimreaper: the command line. Every command prints one JSON document; a refusal exits 2 with its named cause."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

from . import __version__, core, host

BEGIN, END = "# >>> trimreaper core >>>\n", "# <<< trimreaper core <<<\n"


def core_body() -> str:
    """The engine exactly as an embedding host vendors it: the text between the markers of core.py."""
    text = Path(core.__file__).read_text(encoding="utf-8")
    start, end = text.index(BEGIN) + len(BEGIN), text.index(END)
    return text[start:end]


def core_digest() -> str:
    return hashlib.sha256(core_body().encode("utf-8")).hexdigest()


def job_id(value: str) -> str:
    if not host.JOB_ID.match(value):
        raise argparse.ArgumentTypeError(f"job ids look like J-0042, not {value!r}")
    return value


def absolute(value: str) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return os.path.realpath(path)


def cmd_job_add(args) -> dict:
    meta = {}
    if args.worktree:
        meta["worktrees"] = [absolute(p) for p in args.worktree]
    if args.waits_on:
        meta["waitingOn"] = {"job": args.waits_on}
    stamp = host.now()
    with host.db() as conn:
        if host.row(conn, "SELECT 1 FROM jobs WHERE id=?", (args.id,)):
            raise host.HostError(f"job {args.id} already exists", "use `trimreaper job status` to change it")
        finished = stamp if args.status in core.REAP_TERMINAL else None
        conn.execute("INSERT INTO jobs(id,title,status,cwd,created_at,started_at,finished_at,meta) VALUES(?,?,?,?,?,?,?,?)",
                     (args.id, args.title or "", args.status, absolute(args.cwd) if args.cwd else None, stamp, stamp, finished,
                      json.dumps(meta, sort_keys=True) if meta else None))
        host.log_event(conn, "job.added", args.id, host.actor_of({}), {"status": args.status})
        return host.row(conn, "SELECT * FROM jobs WHERE id=?", (args.id,))


def cmd_job_status(args) -> dict:
    with host.db() as conn:
        job = host.row(conn, "SELECT * FROM jobs WHERE id=?", (args.id,))
        if not job:
            raise host.HostError(f"no job {args.id}")
        finished = (job["finished_at"] or host.now()) if args.status in core.REAP_TERMINAL else None
        conn.execute("UPDATE jobs SET status=?, finished_at=? WHERE id=?", (args.status, finished, args.id))
        host.log_event(conn, "job.status", args.id, host.actor_of({}), {"from": job["status"], "to": args.status})
        return host.row(conn, "SELECT * FROM jobs WHERE id=?", (args.id,))


def cmd_job_list(args) -> dict:
    with host.db() as conn:
        return {"jobs": host.rows(conn, "SELECT * FROM jobs ORDER BY id")}


def cmd_status(args) -> dict:
    params = {"items": args.items, "budgetSeconds": args.budget, "limit": args.limit}
    if args.job:
        params["job"] = args.job
    if args.after:
        params["after"] = args.after
    return core.tool_reap_status(params)


def cmd_reap(args) -> dict:
    params = {"dryRun": args.dry_run, "actor": host.actor_of({})}
    if args.job:
        params["job"] = args.job
    for flag, key in (("releases", "releases"), ("scratch", "agentScratch"), ("release_evidence", "releaseEvidence"), ("archives", "archives")):
        if getattr(args, flag):
            params[key] = True
    if args.expire_evidence:
        params["expireEvidence"] = True
    return core.tool_reap(params)


def cmd_tick(args) -> dict:
    actions, errors = [], []
    with host.db() as conn:
        policy = host.load_policy()
        core.reap_tick(conn, policy, [], actions, errors)
        core.evidence_expiry_tick(conn, policy, [], actions, errors)
    return {"actions": actions, "errors": errors}


def cmd_owner(args) -> dict:
    if args.action == "status":
        return core.tool_reap_owner_status({})
    if args.action == "compact":
        return core.tool_reap_owner_compact({"run": args.run})
    return core.tool_reap_acknowledge({"run": args.run, "evidence": args.evidence, "actor": host.actor_of({})})


def cmd_mark(args) -> dict:
    """Give a folder its retention class: one marker file at its top level. `disposable` lets every other gate decide; any
    other class keeps the folder whatever else is true."""
    folder = Path(absolute(args.path))
    info = os.lstat(folder)
    if not stat.S_ISDIR(info.st_mode):
        raise host.HostError(f"{folder} is not a directory")
    present = [entry.name for entry in os.scandir(folder) if entry.name.casefold().startswith(core.REAP_MARKER_PREFIX)]
    name = core.REAP_MARKER_PREFIX + args.retention
    if present and present != [name]:
        raise host.HostError(f"{folder} already carries {', '.join(present)}", "remove the other marker first: one marker per folder")
    if not present:
        fd = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    return {"path": str(folder), "retention": args.retention, "marker": name}


def cmd_config(args) -> dict:
    return {"configFile": str(host.config_path()), "config": host.config(), "policy": host.load_policy(),
            "home": str(host.reap_home()), "stateDir": str(host.state_dir()),
            "cloneBases": [{"path": str(path), "prefix": prefix} for path, prefix in host.reap_clone_bases()],
            "cacheRoot": str(host.reap_cache_root()), "archiveRoot": str(host.reap_archive_root()),
            "scratchRoot": str(host.reap_agent_scratch_root()), "receipts": str(host.reap_receipt_dir())}


def cmd_core_digest(args) -> dict:
    return {"version": __version__, "sha256": core_digest(), "lines": core_body().count("\n")}


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="trimreaper", description="Reclaim disk that agent jobs left behind, through one guarded deletion path.")
    top.add_argument("--version", action="version", version=f"trimreaper {__version__}")
    sub = top.add_subparsers(dest="command", required=True)

    job = sub.add_parser("job", help="record the jobs whose folders TrimReaper may reap").add_subparsers(dest="job_command", required=True)
    add = job.add_parser("add", help="record a job (status running unless --status says otherwise)")
    add.add_argument("id", type=job_id)
    add.add_argument("--title")
    add.add_argument("--cwd", help="the job's working folder")
    add.add_argument("--worktree", action="append", help="a linked git worktree the job owns (repeatable)")
    add.add_argument("--waits-on", type=job_id, help="a job whose artifacts this one still needs")
    add.add_argument("--status", choices=host.JOB_STATUSES, default="running")
    add.set_defaults(run=cmd_job_add)
    status = job.add_parser("status", help="change a job's status (done/failed/cancelled finish it)")
    status.add_argument("id", type=job_id)
    status.add_argument("status", choices=host.JOB_STATUSES)
    status.set_defaults(run=cmd_job_status)
    job.add_parser("list", help="every recorded job").set_defaults(run=cmd_job_list)

    audit = sub.add_parser("status", help="metadata-only audit: what is reapable, what is kept and why (deletes nothing)")
    audit.add_argument("--job", type=job_id)
    audit.add_argument("--items", action="store_true", help="list every item")
    audit.add_argument("--budget", type=int, default=core.REAP_AUDIT_SECONDS, help="seconds the audit may take")
    audit.add_argument("--limit", type=int, default=40)
    audit.add_argument("--after", type=job_id, help="resume after this job (the previous call's cursorAfter)")
    audit.set_defaults(run=cmd_status)

    reap = sub.add_parser("reap", help="run the guarded reaper once")
    target = reap.add_mutually_exclusive_group(required=True)
    target.add_argument("--job", type=job_id, help="one finished job's clones and cache folders")
    target.add_argument("--scratch", action="store_true", help="idle agent scratch folders")
    target.add_argument("--archives", action="store_true", help="archives past archiveExpiryDays")
    target.add_argument("--release-evidence", action="store_true", help="superseded release evidence")
    target.add_argument("--releases", action="store_true", help="build targets of superseded release candidates")
    reap.add_argument("--expire-evidence", action="store_true", help="with --job: expire old screenshots, recordings and logs instead")
    reap.add_argument("--dry-run", action="store_true", help="run every gate, remove and write nothing")
    reap.set_defaults(run=cmd_reap)

    sub.add_parser("tick", help="one scheduled pass: every due job, scratch, archives and evidence expiry").set_defaults(run=cmd_tick)

    owner = sub.add_parser("owner", help="the single-owner cleanup handoff")
    owner.add_argument("action", choices=("status", "compact", "acknowledge"))
    owner.add_argument("--run", help="the run token from `owner status` (compact, acknowledge)")
    owner.add_argument("--evidence", help="acknowledge: the absolute path of your 0600 handoff report")
    owner.set_defaults(run=cmd_owner)

    mark = sub.add_parser("mark", help="give a folder a retention class")
    mark.add_argument("path")
    mark.add_argument("retention", choices=core.REAP_RETENTION_CLASSES)
    mark.set_defaults(run=cmd_mark)

    sub.add_parser("config", help="the effective configuration and folder layout").set_defaults(run=cmd_config)
    sub.add_parser("core-digest", help="SHA-256 of the engine an embedding host must vendor byte for byte").set_defaults(run=cmd_core_digest)
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command in ("owner",) and args.action in ("compact", "acknowledge") and not args.run:
        print(json.dumps({"error": f"owner {args.action} needs --run"}), file=sys.stderr)
        return 2
    try:
        out = args.run(args)
    except host.HostError as error:
        print(json.dumps({"error": str(error), "hint": error.hint}), file=sys.stderr)
        return 2
    except core.ReapRefused as refusal:
        print(json.dumps({"error": f"refused: {refusal}"}), file=sys.stderr)
        return 2
    except OSError as error:
        print(json.dumps({"error": f"{error.__class__.__name__}: {error}"}), file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    return 0
