# Embedding the engine

The engine is the text between `# >>> trimreaper core >>>` and `# <<< trimreaper core <<<` in
`src/trimreaper/core.py`. It imports nothing. It reads the standard-library modules and the host names below as globals,
so a host can paste it, byte for byte, into its own module after defining them. That is how UltraTerm vendors it.

`trimreaper core-digest` prints the SHA-256 of that text. An embedding host should record the version and digest next to
its copy and fail its tests when the copy no longer matches. Change the engine here, release it, then re-vendor; never
edit an embedded copy.

## The clean pass

`src/trimreaper/clean.py` holds a second section, between `# >>> trimreaper clean >>>` and `# <<< trimreaper clean <<<`.
It is vendored the same way, after the engine, and calls the engine's gates. Every name it defines starts with `clean_` or
`CLEAN_`. `trimreaper core-digest` prints its SHA-256 under `clean`. Besides the engine's needs it reads `Counter`
(collections), `ThreadPoolExecutor` (concurrent.futures) and one more host name:

| Name | What it returns |
|---|---|
| `reap_worktree_homes()` | Folders where agent tools keep worktrees, as `Path`s, or `None` for the built-in list. |

Its entry points are `clean_pass(dry_run, deep=False, only=None, report=True)` and `clean_auto(conn, policy, actions,
errors)`, which a host's scheduled pass calls (it reads `cleanAtPercent` and `worktreeIdleDays` from the policy, defaulting
to 95 and 7, and the `meta` key `clean:auto`). An embedded copy runs under the host's own single-owner lock, so a host
never adds a second cleaner.

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
| `load_policy()` | The policy dict with **every** key in CONFIGURATION.md present. The engine's fallbacks for a missing key are not the standalone defaults (a missing `cloneProtectedCopies` reads as `"remove"`). |
| `require(params, key)`, `actor_of(params)` | Parameter helpers for the `tool_*` entry points. |
| `job_meta(job)`, `job_wait(job)` | A job's meta dict; the `{"job": id}` it waits on, or `None`. |
| `whole_title(text)` | A job title shortened for display. |
| `live_all()` | Live terminals as dicts with an `"id"`. A host that runs agents in terminals records a job's `terminal_id`; while that terminal is live, or its `cleanup_records` row is not settled, the job is not reaped, and the hints name `<REAP_CLI> cleanup-status` and `cleanup-record`. The standalone host runs no terminals. |
| `disk_state()`, `disk_level(policy, disk)` | Used percent and free bytes; `"ok"`, or any other word for disk pressure. |
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
`cancelled`.

The engine reads and writes:

- `meta` keys starting `reaper:`, `reap:` and `evidence-expiry:` (its own pacing and audit state);
- `meta` keys `cleanup:successor:<job>` (read: a JSON `{"job", "successor"}` handing a finished job's folders to a
  successor job, which keeps them while it is unfinished) and `cleanup:released:<job>` (written when that chain ends);
- `jobs.cwd` and the `jobs.meta` JSON: reads `cwd` and `worktrees` (declared folders) and `waitingOn`, writes `reap` (its last word on the job).

## Entry points

`reap_tick(conn, policy, live, actions, errors)` and `evidence_expiry_tick(...)` run one scheduled pass. `tool_reap(params)`,
`tool_reap_status(params)`, `tool_reap_owner_status`, `tool_reap_owner_compact` and `tool_reap_acknowledge` back the CLI
commands of the same names (`src/trimreaper/cli.py` shows the parameters). `reap_closed_job(conn, policy, job_id, actions,
errors)` reaps one job the moment its worker closes.

## On-disk formats

The retention marker prefix `.ut-retention-` and the archive manifest kind `ut-overseer-reap-archive` come from
UltraTerm, where the engine started. They stay as they are so existing markers and archives keep working.
