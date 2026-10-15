"""`trimreaper mcp`: the same commands as tools for MCP clients, over stdio (JSON-RPC 2.0, one message per line).

Agents get the job lifecycle, the read-only audit, `reap` (a dry run unless `dryRun` is false) and `mark`. Acknowledging a
stopped run and changing the policy stay with a person on the command line.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from . import __version__, cli, core, host

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
JOB = {"type": "string", "pattern": host.JOB_ID.pattern, "description": "Job id, J-0042 (the folder name carries j0042)"}

TOOLS = [
    {"name": "job_add",
     "description": "Record a job when an agent starts work, so TrimReaper may later reap the folders named for it. "
                    "Name the clone folder with the job number (myapp-j0042).",
     "inputSchema": {"type": "object", "required": ["id"], "additionalProperties": False, "properties": {
         "id": JOB, "title": {"type": "string"},
         "cwd": {"type": "string", "description": "The job's working folder (absolute, or ~/ for the reap home)"},
         "worktrees": {"type": "array", "items": {"type": "string"}, "description": "Linked git worktrees the job owns"},
         "waitsOn": {**JOB, "description": "A job whose artifacts this one still needs"},
         "status": {"type": "string", "enum": list(host.JOB_STATUSES), "default": "running"}}}},
    {"name": "job_status",
     "description": "Change a job's status. done, failed or cancelled finish it; reaping starts 15 minutes later.",
     "inputSchema": {"type": "object", "required": ["id", "status"], "additionalProperties": False, "properties": {
         "id": JOB, "status": {"type": "string", "enum": list(host.JOB_STATUSES)}}}},
    {"name": "job_list", "description": "Every recorded job.",
     "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}}},
    {"name": "reap_status",
     "description": "Metadata-only audit: what is reapable, what is kept and why, with bytes. Deletes nothing. "
                    "summary.reasonActions says what would free each kept folder.",
     "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
         "job": JOB, "items": {"type": "boolean", "default": False, "description": "List every item"},
         "budgetSeconds": {"type": "integer", "minimum": 1, "default": core.REAP_AUDIT_SECONDS},
         "limit": {"type": "integer", "minimum": 1, "default": 40}}}},
    {"name": "reap",
     "description": "Run the guarded reaper once on one target. A dry run (the default) runs every check and removes nothing; "
                    "pass dryRun false to remove. Folders that fail a check are kept and listed with the reason.",
     "annotations": {"destructiveHint": True},
     "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
         "job": {**JOB, "description": "One finished job's clones and scratch folders"},
         "target": {"type": "string", "enum": ["job", "scratch", "archives", "release-evidence", "releases"], "default": "job"},
         "expireEvidence": {"type": "boolean", "default": False, "description": "With job: expire old screenshots, recordings and logs instead"},
         "dryRun": {"type": "boolean", "default": True}}}},
    {"name": "mark",
     "description": "Give a folder a retention class. evidence, customer or release-runtime-rollback keep it whatever else "
                    "is true; disposable allows removal, still subject to every other check.",
     "inputSchema": {"type": "object", "required": ["path", "retention"], "additionalProperties": False, "properties": {
         "path": {"type": "string"}, "retention": {"type": "string", "enum": list(core.REAP_RETENTION_CLASSES)}}}},
]


def namespace(**values) -> argparse.Namespace:
    return argparse.Namespace(**values)


def call(name: str, args: dict) -> dict:
    if name == "job_add":
        return cli.cmd_job_add(namespace(id=cli.job_id(args["id"]), title=args.get("title"), cwd=args.get("cwd"),
                                         worktree=args.get("worktrees"), waits_on=cli.job_id(args["waitsOn"]) if args.get("waitsOn") else None,
                                         status=args.get("status", "running")))
    if name == "job_status":
        if args["status"] not in host.JOB_STATUSES:
            raise host.HostError(f"status must be one of {', '.join(host.JOB_STATUSES)}")
        return cli.cmd_job_status(namespace(id=cli.job_id(args["id"]), status=args["status"]))
    if name == "job_list":
        return cli.cmd_job_list(namespace())
    if name == "reap_status":
        return cli.cmd_status(namespace(job=cli.job_id(args["job"]) if args.get("job") else None, items=bool(args.get("items")),
                                        budget=int(args.get("budgetSeconds", core.REAP_AUDIT_SECONDS)), limit=int(args.get("limit", 40)), after=None))
    if name == "reap":
        target = args.get("target", "job")
        if target == "job" and not args.get("job"):
            raise host.HostError("reap with target job needs a job id")
        if target != "job" and args.get("job"):
            raise host.HostError("give a job only with target job")
        dry_run = args.get("dryRun", True)
        if not isinstance(dry_run, bool):
            raise host.HostError("dryRun must be true or false")
        return cli.cmd_reap(namespace(job=cli.job_id(args["job"]) if target == "job" else None, scratch=target == "scratch",
                                      archives=target == "archives", release_evidence=target == "release-evidence",
                                      releases=target == "releases", expire_evidence=bool(args.get("expireEvidence")), dry_run=dry_run))
    if name == "mark":
        if args["retention"] not in core.REAP_RETENTION_CLASSES:
            raise host.HostError(f"retention must be one of {', '.join(core.REAP_RETENTION_CLASSES)}")
        return cli.cmd_mark(namespace(path=args["path"], retention=args["retention"]))
    raise KeyError(name)


def result(out: dict, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(out, indent=1, sort_keys=True, default=str)}],
            "structuredContent": json.loads(json.dumps(out, default=str)), "isError": error}


def handle(message: dict) -> dict | None:
    method, ident = message.get("method"), message.get("id")
    if ident is None:
        return None  # a notification (notifications/initialized, cancellations): nothing to answer
    def answer(value):
        return {"jsonrpc": "2.0", "id": ident, "result": value}
    def failure(code, text):
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": text}}
    params = message.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return answer({"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                       "capabilities": {"tools": {"listChanged": False}},
                       "serverInfo": {"name": "trimreaper", "version": __version__},
                       "instructions": "Record each agent job (job_add) and its end (job_status done). Read reap_status before "
                                       "asking why disk is full. reap is a dry run unless dryRun is false."})
    if method == "ping":
        return answer({})
    if method == "tools/list":
        return answer({"tools": TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in {tool["name"] for tool in TOOLS}:
            return failure(-32602, f"unknown tool: {name}")
        if not isinstance(args, dict):
            return failure(-32602, "arguments must be an object")
        cli.system_path()
        try:
            return answer(result(call(name, args)))
        except host.HostError as error:
            return answer(result({"error": str(error), "hint": error.hint}, True))
        except core.ReapRefused as refusal:
            return answer(result({"error": f"refused: {refusal}"}, True))
        except argparse.ArgumentTypeError as error:
            return answer(result({"error": str(error)}, True))
        except KeyError as error:
            return answer(result({"error": f"missing argument: {error}"}, True))
        except (OSError, sqlite3.Error, ValueError, TypeError) as error:
            return answer(result({"error": f"{error.__class__.__name__}: {error}"}, True))
    return failure(-32601, f"method not found: {method}")


def serve(stdin=None, stdout=None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError as error:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"parse error: {error}"}}
        else:
            reply = handle(message) if isinstance(message, dict) else \
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "a request must be a JSON object"}}
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
    return 0
