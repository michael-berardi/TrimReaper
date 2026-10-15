# Embedding the engine

The engine is the text between `# >>> trimreaper core >>>` and `# <<< trimreaper core <<<` in
`src/trimreaper/core.py`. It imports nothing. It reads the standard-library modules and the host names below as globals,
so a host can paste it, byte for byte, into its own module after defining them. That is how UltraTerm runs it.

`trimreaper core-digest` prints the SHA-256 of that text. An embedding host should record the version and digest next to
its copy and fail its tests when the copy no longer matches. Change the engine here, release it, then re-vendor; never
edit an embedded copy.

## Modules the host imports

`errno`, `fcntl`, `hashlib`, `json`, `os`, `plistlib`, `pwd`, `re`, `shutil`, `sqlite3`, `stat`, `subprocess`,
`tarfile`, `tempfile`, `threading`, `time`, `unicodedata`, `uuid`, and `ExitStack`, `contextmanager` (contextlib),
`Path` (pathlib). The host module needs `from __future__ import annotations` or Python 3.10+.

## Names the host defines

`core.HOST_NAMES` is the authoritative list; `tests/test_embedding.py` fails when the engine reads a name that is not on it.

| Name | What it returns or does |
|---|---|
| `HostError` | Exception class for named failures; `HostError(message, hint=None)`. |
| `VERSION` | The host's version string (recorded in archive manifests). |
| `REAP_CLI` | The host's command name, used in the next-step hints the engine prints. |
| `REAP_OWNER_DIRS` | Folder names below `reap_home()` that hold the single-owner cleanup journal. |
| `now()`, `iso(ts=None)` | Epoch seconds; local ISO-8601 time. |
| `db()` | Context manager yielding an autocommit `sqlite3` connection whose rows are `sqlite3.Row`. |
| `row(conn, sql, args)`, `rows(conn, sql, args)` | One row as a dict (or `None`); every row as dicts. |
| `txn(conn)` | Context manager: `BEGIN IMMEDIATE` ... `COMMIT`, `ROLLBACK` on error. |
| `log_event(conn, kind, subject, actor, data)` | Append an event. |
| `load_policy()` | The policy dict (keys in CONFIGURATION.md). |
| `require(params, key)`, `actor_of(params)` | Parameter helpers for the `tool_*` entry points. |
| `job_meta(job)`, `job_wait(job)` | A job's meta dict; the `{"job": id}` it waits on, or `None`. |
| `whole_title(text)` | A job title shortened for display. |
| `live_all()` | Live terminals as dicts with an `"id"`; a job whose `terminal_id` is live is not reaped. |
| `disk_state()`, `disk_level(policy, disk)` | Used percent and free bytes; `"ok"`, `"warn"` or `"hold"`. |
| `reap_home()` | The home the reaper works below. |
| `reap_clone_bases()` | `[(Path, prefix or None), ...]`: folders holding job clones. |
| `reap_cache_root()`, `reap_archive_root()`, `reap_agent_scratch_root()` | Per-job scratch, archives, agent scratch. |
| `reap_release_root()`, `reap_release_evidence_root()`, `reap_release_marks()` | Release candidates, release evidence, `{"installed": v, "rollback": v}`. |
| `reap_receipt_dir()` | Where receipts and per-job locks live. |
| `reap_queue_state()` | A build queue's lock folder. |

## Tables the engine reads and writes

The host's database must provide at least:

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT, status TEXT NOT NULL, cwd TEXT, created_at REAL NOT NULL,
                   finished_at REAL, terminal_id TEXT, meta TEXT);
CREATE TABLE cleanup_records (terminal_id TEXT PRIMARY KEY, status TEXT NOT NULL);
```

plus whatever `log_event` writes to. Job ids are `J-` and four or five digits. Finished statuses are `done`, `failed` and
`cancelled`. The engine keeps its own state in `meta` under keys starting `reaper:`, `reap:` and `evidence-expiry:`.

## Entry points

`reap_tick(conn, policy, live, actions, errors)` and `evidence_expiry_tick(...)` run one scheduled pass. `tool_reap(params)`,
`tool_reap_status(params)`, `tool_reap_owner_status`, `tool_reap_owner_compact` and `tool_reap_acknowledge` back the CLI
commands of the same names (`src/trimreaper/cli.py` shows the parameters). `reap_closed_job(conn, policy, job_id, actions,
errors)` reaps one job the moment its worker closes.

## On-disk formats

The retention marker prefix `.ut-retention-` and the archive manifest kind `ut-overseer-reap-archive` come from
UltraTerm, where the engine started. They stay as they are so existing markers and archives keep working.
