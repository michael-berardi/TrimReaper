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
    # Marked bulky job-cache evidence expires separately; reports and receipts remain.
    "markedEvidenceExpiryDays": 5,
    # An archive goes this many days after it became the only copy of its clone.
    "archiveExpiryDays": 30,
    # "archive": save work no remote holds before an expired folder goes; anything else keeps such folders.
    "cloneArchive": "archive",
    # "remove": a whole clone's .env*/.dev.vars*/.wrangler/.vercel copies are archived owner-only and go with it.
    # Anything else (the default "keep") keeps every clone that holds one.
    "cloneProtectedCopies": "keep",
    # Disk pressure: at or above this used percent, idle (parked/interrupted) jobs lose their derived build output too.
    "diskWarnPercent": 85,
    # `tick` runs `trimreaper clean` when the volume reaches this used percent, then again only on new pressure (no timer); 0 = off.
    "cleanAtPercent": 85,
    # A clean pass stops at this low-water mark; manual clean uses 85 as its high mark when auto clean is off.
    "cleanToPercent": 80,
    # New pressure: this many more GB used since the last automatic pass, or free space under cleanFloorGB.
    "cleanGrowthGB": 5,
    "cleanFloorGB": 10,
    # Parked/blocked jobs give up their derived build output after this many hours without a report; 0 = only under pressure.
    "parkedReapHours": 24,
    # A finished job whose worker terminal is still open stops holding that job's folders once the job finished this many
    # hours ago and the terminal has been idle as long; the terminal itself is never closed. 0 = such a job keeps them.
    "reapOpenTerminalGraceHours": 0,
    # A linked git worktree no job owns goes after this many days without a change (and every other check); 0 keeps them all.
    "worktreeIdleDays": 7,
    "projectIdleHours": 48,
    # The budget one agent-scratch folder's idle walk may spend: how many entries it may list, how many bytes it may see
    # and how long it may take. A folder that outgrows its budget is kept, and the receipt names the budget the walk spent.
    # A byte budget of 0 is no byte bound at all: bytes bound no work, so the entry budget is what limits the walk.
    "agentScratchIdleMaxEntries": 200000,
    "agentScratchIdleMaxBytes": 0,
    "agentScratchIdleMaxSeconds": 10,
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
    "worktreeHomes": None,            # where agent tools keep worktrees; default: the known homes (docs/CONFIGURATION.md)
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
    homes = merged["worktreeHomes"]
    if homes is not None and (not isinstance(homes, list) or not all(isinstance(h, str) for h in homes)):
        bad("worktreeHomes", "a list of paths or null")
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
    paths += [b["path"] for b in bases] + [v for v in release.values() if v is not None] + list(homes or ())
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
    for key in ("cloneExpiryDays", "evidenceExpiryDays", "markedEvidenceExpiryDays", "archiveExpiryDays"):
        value = merged[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise HostError(f"policy {key} must be a whole number of days (0 = never), not {json.dumps(value)}")
    value = merged["agentScratchIdleMaxEntries"]
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise HostError(f"policy agentScratchIdleMaxEntries must be a whole number of at least 1, not {json.dumps(value)}")
    value = merged["agentScratchIdleMaxBytes"]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise HostError(f"policy agentScratchIdleMaxBytes must be a whole number of at least 0, not {json.dumps(value)}")
    value = merged["agentScratchIdleMaxSeconds"]
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise HostError(f"policy agentScratchIdleMaxSeconds must be a whole number of at least 1, not {json.dumps(value)}")
    value = merged["reapOpenTerminalGraceHours"]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise HostError(f"policy reapOpenTerminalGraceHours must be 0 (never) or more hours, not {json.dumps(value)}")
    value = merged["diskWarnPercent"]
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 100:
        raise HostError(f"policy diskWarnPercent must be a whole number from 1 to 100, not {json.dumps(value)}")
    high, low = merged["cleanAtPercent"], merged["cleanToPercent"]
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (high, low)):
        raise HostError("clean goals must be finite numbers satisfying 0 < cleanToPercent < cleanAtPercent <= 100 (cleanAtPercent=0 disables auto clean)")
    # Keep 0 in the effective policy so automatic clean remains off. Manual clean's effective high is 85.
    effective_high = DEFAULT_POLICY["cleanAtPercent"] if high == 0 else high
    if not 0 < low < effective_high <= 100:
        raise HostError("clean goals must satisfy 0 < cleanToPercent < cleanAtPercent <= 100 (auto off uses a manual high of 85)")
    for key, high in (("cleanGrowthGB", 1000), ("cleanFloorGB", 1000), ("parkedReapHours", 24 * 365), ("projectIdleHours", 24 * 365)):
        value = merged[key]
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= high:
            raise HostError(f"policy {key} must be a whole number from 0 to {high}, not {json.dumps(value)}")
    value = merged["worktreeIdleDays"]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise HostError(f"policy worktreeIdleDays must be a whole number of days (0 = never), not {json.dumps(value)}")
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


def reap_output_ownership(source: str, output: str) -> dict:
    # Only producer-written, identity-bound records activate the exact-output route.
    # A missing registry keeps legacy/unmanaged outputs on the existing strict route.
    from . import core
    return core.reap_registered_output_ownership(source, output)


def reap_worktree_homes() -> list[Path] | None:
    """Folders where agent tools keep worktrees, as configured; None means the clean pass's built-in list."""
    homes = config().get("worktreeHomes")
    return None if homes is None else [expand(path) for path in homes]


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
    """Read df Used/Available, not APFS shared-container total-minus-free."""
    from . import core
    volume = config().get("diskVolume")
    for path in ((volume,) if volume else ("/System/Volumes/Data", "/")):
        disk = core.reap_disk_state(path)
        if disk:
            return disk
    if volume:
        raise HostError(f"diskVolume {volume} cannot be measured", "fix diskVolume or remove it")
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


def clean_release_event_token(conn) -> int:
    """Stable release generation, not a timer or deletion authorization.

    Only an actual transition to a finished job status or an evidenced scope release can retry unresolved clean work.
    Reap results, blocked reports, ordinary job edits and ticks never advance this token. All clean safety gates still run.
    """
    # JSON may have been written by an older/external caller; malformed data cannot create a release or abort a tick.
    found = conn.execute("""
        SELECT COALESCE(MAX(id), 0) FROM events
        WHERE CASE WHEN json_valid(data) THEN
            (kind = 'job.status'
             AND json_extract(data, '$.to') IN ('done', 'failed', 'cancelled')
             AND json_extract(data, '$.from') IN
                 ('queued', 'approved', 'running', 'blocked', 'parked', 'interrupted'))
            OR (kind = 'cleanup.scope_released'
                AND json_type(data, '$.evidence') = 'text'
                AND length(trim(json_extract(data, '$.evidence'))) > 0)
        ELSE 0 END
    """).fetchone()
    return found[0]


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
