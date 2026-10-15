"""The standalone host: configuration, the job store, policy and folder layout for the TrimReaper engine.

The engine (trimreaper.core) never decides where things live or which jobs exist. It asks the host through the names in
core.HOST_NAMES. This module answers from a JSON config file and a private SQLite job store, so TrimReaper runs on its
own; an embedding host such as UltraTerm answers from its own state instead.

Configuration: $TRIMREAPER_CONFIG, else ~/.config/trimreaper/config.json. A missing file means the defaults below. Paths
may start with `~`, which is the configured `home`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from . import __version__

VERSION = __version__
REAP_CLI = "trimreaper"
# The single-owner cleanup journal lives below the reap home in these folders (created 0700 when absent).
REAP_OWNER_DIRS = (".local", "state", "trimreaper", "cleanup")

JOB_ID = re.compile(r"^J-(?:\d{4}|[1-9]\d{4})$")  # the form a folder name jNNNN maps to
JOB_STATUSES = ("queued", "approved", "running", "blocked", "parked", "interrupted", "done", "failed", "cancelled")
TITLE_LIMIT = 160

DEFAULT_POLICY = {
    # `trimreaper tick` reaps only while this is on.
    "autoReap": True,
    # A whole clone goes this many days after every job named in it finished (archived first when it holds unsaved work).
    "cloneExpiryDays": 3,
    # Screenshots, recordings, traces and logs of a finished job expire after this many days; 0 turns expiry off.
    "evidenceExpiryDays": 14,
    # An archive goes this many days after it became the only copy of its clone.
    "archiveExpiryDays": 30,
    # "archive": save work no remote holds before an expired folder goes; anything else keeps such folders.
    "cloneArchive": "archive",
    # "remove": a whole clone's .env*/.dev.vars*/.wrangler/.vercel copies are archived owner-only and go with it.
    # Anything else (the default "keep") keeps every clone that holds one.
    "cloneProtectedCopies": "keep",
    # Disk pressure: at or above this used percent, idle (parked/interrupted) jobs lose their derived build output too.
    "diskWarnPercent": 85,
}

DEFAULT_CONFIG = {
    "home": None,                     # the home the reaper may work below; default: this user's home
    "stateDir": "~/.local/state/trimreaper",
    "cloneBases": [{"path": "~/dev"}],  # folders holding job clones named *-jNNNN*, jNNNN-* (a base may require a "prefix")
    "cacheRoot": "~/.cache/trimreaper/jobs",       # per-job scratch folders: <cacheRoot>/J-NNNN/<anything>
    "archiveRoot": "~/.cache/trimreaper/archive",  # where unsaved work is archived before a folder goes
    "scratchRoot": None,              # agent scratch: <scratchRoot>/<task>, removed once idle (off until set)
    "releaseRoot": None,              # superseded release candidates' build targets (off until set)
    "releaseEvidenceRoot": None,      # per-version release evidence (off until set)
    "release": {"installedApp": None, "rollbackApp": None},  # macOS app bundles whose versions are kept
    "buildQueueStateDir": None,       # a build queue's lock folder; default: <stateDir>/build-queue
    "diskVolume": None,               # volume measured for disk pressure; default: /System/Volumes/Data, else /
    "policy": {},
}


class HostError(Exception):
    """A failure with a named cause; `hint` says what to do about it."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


# ---- configuration -------------------------------------------------------------------------------------------------

def config_path() -> Path:
    override = os.environ.get("TRIMREAPER_CONFIG")
    return Path(override).expanduser() if override else Path.home() / ".config" / "trimreaper" / "config.json"


def config() -> dict:
    """The effective configuration: defaults, then the config file. Read on every call, so a test or a long-running caller
    always sees the current file."""
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    path = config_path()
    if path.exists():
        try:
            stored = json.loads(path.read_text())
        except (OSError, ValueError) as error:
            raise HostError(f"{path} is not readable JSON: {error}", "fix or delete the file; defaults apply when it is absent")
        if not isinstance(stored, dict):
            raise HostError(f"{path} must hold a JSON object")
        unknown = sorted(set(stored) - set(DEFAULT_CONFIG))
        if unknown:
            raise HostError(f"{path} has unknown keys: {', '.join(unknown)}", "see docs/CONFIGURATION.md for the keys")
        merged.update(stored)
        check_config(merged, path)
    return merged


def check_config(merged: dict, path: Path) -> None:
    """Every value has the type its key needs; a wrong one is named instead of being read as something else."""
    def bad(key, need):
        raise HostError(f"{path}: {key} must be {need}, not {json.dumps(merged.get(key) if '.' not in key else None)}",
                        "see docs/CONFIGURATION.md")
    for key in ("stateDir", "cacheRoot", "archiveRoot"):
        if not isinstance(merged[key], str) or not merged[key].strip():
            bad(key, "a path")
    for key in ("home", "scratchRoot", "releaseRoot", "releaseEvidenceRoot", "buildQueueStateDir", "diskVolume"):
        if merged[key] is not None and (not isinstance(merged[key], str) or not merged[key].strip()):
            bad(key, "a path or null")
    bases = merged["cloneBases"]
    if not isinstance(bases, list) or not all(isinstance(b, dict) for b in bases):
        bad("cloneBases", 'a list of {"path": ..., "prefix": ...} objects')
    for base in bases:
        extra = sorted(set(base) - {"path", "prefix"})
        if extra or not isinstance(base.get("path"), str):
            raise HostError(f"{path}: cloneBases entry {json.dumps(base)} needs a \"path\" and may have a \"prefix\" (unknown: {', '.join(extra) or 'none'})")
        if "prefix" in base and (not isinstance(base["prefix"], str) or not base["prefix"]):
            raise HostError(f"{path}: cloneBases prefix must be a non-empty string, not {json.dumps(base['prefix'])}")
    release = merged["release"]
    if not isinstance(release, dict) or set(release) - {"installedApp", "rollbackApp"} \
            or any(v is not None and not isinstance(v, str) for v in release.values()):
        bad("release", '{"installedApp": path or null, "rollbackApp": path or null}')
    if not isinstance(merged["policy"], dict):
        bad("policy", "an object")
    check_policy(merged["policy"])
    # Every path is checked now, at load: a bad one found later would fail inside a sweep and stop it.
    paths = [merged[k] for k in ("stateDir", "cacheRoot", "archiveRoot", "scratchRoot", "releaseRoot", "releaseEvidenceRoot",
                                 "buildQueueStateDir", "diskVolume") if merged[k] is not None]
    paths += [b["path"] for b in bases] + [v for v in release.values() if v is not None]
    for value in paths:
        path_form(value)
    if merged["home"] is not None and not Path(merged["home"]).expanduser().is_absolute():
        bad("home", "an absolute path")
    if merged["diskVolume"] is not None and not os.path.isdir(merged["diskVolume"]):
        raise HostError(f"{path}: diskVolume {merged['diskVolume']} is not a folder that can be measured", "fix diskVolume or remove it")


def path_form(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise HostError(f"configured path must be a non-empty string, not {value!r}")
    if ".." in Path(value).parts:
        raise HostError(f"configured path {value!r} must not contain '..'", "name the folder directly")
    if not (value == "~" or value.startswith("~/") or Path(value).is_absolute()):
        raise HostError(f"configured path {value!r} must be absolute or start with ~/")


def check_policy(policy: dict) -> None:
    """Policy values, checked before use: a string "false" is not off, "3" is not three days."""
    unknown = sorted(set(policy) - set(DEFAULT_POLICY))
    if unknown:
        raise HostError(f"unknown policy keys: {', '.join(unknown)}", "see docs/CONFIGURATION.md for the policy keys")
    merged = {**DEFAULT_POLICY, **policy}
    if not isinstance(merged["autoReap"], bool):
        raise HostError(f"policy autoReap must be true or false, not {json.dumps(merged['autoReap'])}")
    for key in ("cloneExpiryDays", "evidenceExpiryDays", "archiveExpiryDays"):
        value = merged[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise HostError(f"policy {key} must be a whole number of days (0 = never), not {json.dumps(value)}")
    value = merged["diskWarnPercent"]
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 100:
        raise HostError(f"policy diskWarnPercent must be a whole number from 1 to 100, not {json.dumps(value)}")
    if merged["cloneArchive"] not in ("archive", "keep"):
        raise HostError(f'policy cloneArchive must be "archive" or "keep", not {json.dumps(merged["cloneArchive"])}')
    if merged["cloneProtectedCopies"] not in ("keep", "remove"):
        raise HostError(f'policy cloneProtectedCopies must be "keep" or "remove", not {json.dumps(merged["cloneProtectedCopies"])}')
    if merged["cloneProtectedCopies"] == "remove" and merged["cloneArchive"] != "archive":
        raise HostError('policy cloneProtectedCopies "remove" needs cloneArchive "archive": secret copies are archived before they go',
                        'set cloneArchive to "archive", or cloneProtectedCopies to "keep"')


def reap_home() -> Path:
    """The home whose folders the reaper may touch. TRIMREAPER_HOME (tests, a second instance) wins over the config file."""
    override = os.environ.get("TRIMREAPER_HOME") or config().get("home")
    if not override:
        return Path.home()
    home = Path(override).expanduser()
    if not home.is_absolute():
        raise HostError(f"the reap home must be an absolute path, not {override!r}", "set TRIMREAPER_HOME or `home` to an absolute path")
    return home


def expand(value: str) -> Path:
    """A configured path: `~` is the reap home, never the HOME variable of whoever runs the command."""
    path_form(value)
    if value == "~" or value.startswith("~/"):
        return reap_home() / value[2:]
    return Path(value)


def configured(key: str) -> Path | None:
    value = config().get(key)
    return expand(value) if value is not None else None


def state_dir() -> Path:
    return configured("stateDir")


def disabled_root(name: str) -> Path:
    """A root that is switched off: a path inside the state folder that is never created, so nothing is ever found there."""
    return state_dir() / "disabled" / name


# ---- folder layout the engine asks for ------------------------------------------------------------------------------

def reap_clone_bases() -> list[tuple[Path, str | None]]:
    return [(expand(base["path"]), base.get("prefix")) for base in config()["cloneBases"]]


def reap_cache_root() -> Path:
    return configured("cacheRoot")


def reap_archive_root() -> Path:
    return configured("archiveRoot")


def reap_agent_scratch_root() -> Path:
    return configured("scratchRoot") or disabled_root("scratch")


def reap_release_root() -> Path:
    return configured("releaseRoot") or disabled_root("releases")


def reap_release_evidence_root() -> Path:
    return configured("releaseEvidenceRoot") or disabled_root("release-evidence")


def reap_receipt_dir() -> Path:
    return state_dir() / "reaper"


def reap_queue_state() -> Path:
    return configured("buildQueueStateDir") or state_dir() / "build-queue"


def app_version(app: Path | None) -> str | None:
    if app is None:
        return None
    import plistlib
    try:
        with open(app / "Contents" / "Info.plist", "rb") as stream:
            version = plistlib.load(stream).get("CFBundleShortVersionString")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return version if isinstance(version, str) and re.match(r"^\d+\.\d+\.\d+", version) else None


def reap_release_marks() -> dict:
    """The installed version and the single rollback. Unknown (unset or unreadable) keeps every release candidate."""
    release = config().get("release") or {}
    installed, rollback = release.get("installedApp"), release.get("rollbackApp")
    return {"installed": app_version(expand(installed) if installed else None),
            "rollback": app_version(expand(rollback) if rollback else None)}


# ---- time, policy, disk ---------------------------------------------------------------------------------------------

def now() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts if ts is not None else now()))


def load_policy() -> dict:
    """Every key, always: the engine's own fallbacks for a missing key are not this host's defaults."""
    stored = config()["policy"]
    check_policy(stored)
    return {**DEFAULT_POLICY, **stored}


def disk_state() -> dict | None:
    """Used percent and free bytes of the data volume (used / (used + available), rounded up, the way df reports it)."""
    volume = config().get("diskVolume")
    for path in ((volume,) if volume else ("/System/Volumes/Data", "/")):
        try:
            usage = shutil.disk_usage(path)
        except OSError as error:
            if volume:
                raise HostError(f"diskVolume {volume} cannot be measured: {error.__class__.__name__}", "fix diskVolume or remove it")
            continue
        total = usage.used + usage.free
        if total > 0:
            return {"path": path, "usedPercent": -(-usage.used * 100 // total), "freeBytes": usage.free}
    return None


def disk_level(policy: dict, disk: dict | None) -> str:
    """"warn" at or above diskWarnPercent (the engine treats anything but "ok" as disk pressure), else "ok"."""
    if not disk:
        return "ok"
    return "warn" if disk["usedPercent"] >= policy["diskWarnPercent"] else "ok"


def live_all() -> list[dict]:
    """Live terminals. The standalone host runs no terminals; a job's running processes are caught by the in-use probes."""
    return []


# ---- the job store --------------------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, cwd TEXT, created_at REAL NOT NULL,
  started_at REAL, finished_at REAL, terminal_id TEXT, meta TEXT
);
CREATE TABLE IF NOT EXISTS cleanup_records (terminal_id TEXT PRIMARY KEY, status TEXT NOT NULL, recorded_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, subject TEXT, actor TEXT, data TEXT
);
"""


@contextmanager
def db():
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "state.sqlite3"
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        yield conn
    finally:
        conn.close()


def row(conn, sql: str, args=()):
    found = conn.execute(sql, args).fetchone()
    return dict(found) if found else None


def rows(conn, sql: str, args=()):
    return [dict(found) for found in conn.execute(sql, args).fetchall()]


@contextmanager
def txn(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def log_event(conn, kind: str, subject: str | None = None, actor: str | None = None, data: dict | None = None):
    conn.execute("INSERT INTO events(ts,kind,subject,actor,data) VALUES(?,?,?,?,?)",
                 (now(), kind, subject, actor, json.dumps(data or {}, sort_keys=True)))


def job_meta(job: dict | None) -> dict:
    try:
        meta = json.loads(job["meta"]) if job and job.get("meta") else {}
    except (ValueError, TypeError):
        return {}
    return meta if isinstance(meta, dict) else {}


def job_wait(job: dict | None) -> dict | None:
    """The job this job waits on ({"job": "J-NNNN"}): a job that waits on another keeps the other's artifacts."""
    wait = job_meta(job).get("waitingOn")
    return wait if isinstance(wait, dict) and isinstance(wait.get("job"), str) else None


def whole_title(text, limit: int = TITLE_LIMIT) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut and not text[limit - 1].isspace():
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.-") + "…"


def require(params: dict, key: str, kind=str):
    value = params.get(key)
    if value is None or (kind is str and not str(value).strip()):
        raise HostError(f"missing required parameter: {key}")
    if kind is int:
        try:
            return int(value)
        except (TypeError, ValueError):
            raise HostError(f"{key} must be an integer")
    return str(value) if kind is str else value


def actor_of(params: dict) -> str:
    return str(params.get("actor") or os.environ.get("TRIMREAPER_ACTOR") or "cli")[:64]
