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

Its entry points are `clean_pass(dry_run, deep=False, only=None, report=True, policy=None, escalate=None, steady=False)`,
`clean_auto_observe(conn, policy, disk, event=None)` (why a pass is due; it starts nothing),
`clean_steady_due(conn, policy)` (whether the quiet steady pass is due; it starts nothing either) and
`clean_auto(conn, policy, actions, errors, event=None)`. The standalone host's policy defaults are `cleanAtPercent=75`,
`cleanToPercent=60`, `cleanGrowthGB=5`, `cleanFloorGB=10`, `worktreeIdleDays=7` (the engine reads a missing goal key as 85 and 80,
as before, so a host sets its own), plus the ladder keys (`cleanEscalate`,
`cleanEscalate{Repo,Worktree,Job,Pinned}Hours`, `cleanSteady{Hours,RepoDays,JobHours}`). A host whose `load_policy()` predates a
key gets the shipped default for it (`CLEAN_POLICY`), so a stored policy file never has to name the new keys; the one exception is
`cleanSteadyHours`, which must be present (and positive) for the steady pass to run, so a host's tests that call `clean_auto` with a
partial policy never start a real pass. `cleanAtPercent=0`
disables automatic cleaning. The `meta` keys `clean:auto` (measured progress, hysteresis and the bytes the last pass removed),
`clean:steady` (when the steady pass last ran) and `clean:refused:<digest>` (removals the filesystem refused, kept for a day) are the
clean pass's.
Supply a stable `event` token that changes only when jobs finish or outputs are released; an unmet goal may retry
once for each changed token after the cooldown. Never use the current time or ordinary tick events as this token.
An embedded copy uses the host's single-owner lock; never add a second cleaner.

`clean_auto` runs the ladder when `clean_auto_observe` names a reason, and otherwise the steady pass when `clean_steady_due`. A host
that runs `clean_auto` in a child process (UltraTerm does) must start that child when either says so, or the steady pass never
runs. A host that wraps `clean_pass` to watch pressure passes sees only those: the steady pass calls the unwrapped function
through `CLEAN_PASS`, because its result says nothing about a goal.

The ladder's job rungs read the host's job store through the names above and through three engine functions a host can use too:
`reap_escalated(windows, pinned_hours)` (a context manager that shortens `cloneExpiryDays` and `evidenceExpiryDays` for the
calls inside it, never lengthens one and never turns one on), `reap_policy()` (the policy with those windows applied; the engine
reads it wherever it read `load_policy()` for a window) and `reap_pin_released(conn, job)`. A host that pins one job's folders for
another (UltraTerm's artifact inputs) should ask `reap_pin_released` for the pinning job, so a finished or failed job never pins and
an interrupted one stops pinning only inside a rung, after `cleanEscalatePinnedHours`.

Results include `initialOccupancy`, `finalOccupancy`, `remainingBytes` and `goalMet`.
`disk_state()` must supply exact `usedBytes` and `freeBytes`: occupancy uses used/(used+free), not an APFS
volume's nominal total or a rounded percentage. `reap_disk_state(volume)` obtains these columns with a bounded
`df -kP` probe. A failed or malformed probe includes `measurementError` and leaves `usedBytes` unknown in its
conservative admission fallback; goals remain `null`, never inferred from shared-container allocation.

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
| `meta_json(conn, key)` | The JSON object stored under `key` in the meta table, or `{}`. |
| `load_policy()` | The policy dict with **every** key in CONFIGURATION.md present. The engine's fallbacks for a missing key are not the standalone defaults (a missing `cloneProtectedCopies` reads as `"remove"`). |
| `require(params, key)`, `actor_of(params)` | Parameter helpers for the `tool_*` entry points. |
| `job_meta(job)`, `job_wait(job)` | A job's meta dict; the `{"job": id}` it waits on, or `None`. |
| `whole_title(text)` | A job title shortened for display. |
| `live_all()` | Live terminals as dicts with an `"id"`. A host that runs agents in terminals records a job's `terminal_id`; while that terminal is live, or its `cleanup_records` row is not settled, the job is not reaped, and the hints name `<REAP_CLI> cleanup-status` and `cleanup-record`. The standalone host runs no terminals. |
| `disk_state()`, `disk_level(policy, disk)` | Exact `usedBytes`, `freeBytes`, rounded `usedPercent`; `"ok"`, or any other word for disk pressure. |
| `reap_home()` | The home the reaper works below. |
| `reap_clone_bases()` | `[(Path, prefix or None), ...]`: folders holding job clones. |
| `reap_cache_root()`, `reap_archive_root()`, `reap_agent_scratch_root()` | Per-job scratch, archives, agent scratch. |
| `reap_release_root()`, `reap_release_evidence_root()`, `reap_release_marks()` | Release candidates, release evidence, `{"installed": v, "rollback": v}`. |
| `reap_receipt_dir()` | Where receipts and per-job locks live. |
| `reap_queue_state()` | A build queue's lock folder. |

## Exact terminal-job outputs

The optional host callback `reap_output_ownership(source, output)` activates the narrow output-only route.
The supplied standalone adapter reads producer-written records with `reap_registered_output_ownership` from
`<buildQueueStateDir>/output-producers/<sha256(canonical output)>.json`. Missing or unsafe records preserve the output.

A cooperating build queue must take the same permanent `reap_queue_lock(project)` locks before any build/install access,
never unlink their inodes, and hold them through subprocess exit. Register output paths before work starts under those
locks. Lock projects include the canonical source and output ancestors through the exact owning root, never a collection
root or `/`. Producers record protocol `trimreaper-project-locks-v1`, source, output, owner, kind (`target` or
`node_modules`), `lockProjects`, `claimInventory=job-meta-paths-v1`, source/output `{dev,ino}` identities,
producer `{pid,start}`, and state `active` or `finished`. The private registry directory is 0700 and records are atomic
0600 regular files. A dead interrupted producer still needs fresh job, claim, identity, Cargo-lock and process proofs.
Historical outputs are never silently adopted.

The callback returns a fresh structured proof with protocol, source, output, owner, kind, `projects` (the engine's
`reap_output_projects` list), and claimInventory. A bare `True` is not a proof. This is cooperative build exclusion,
not an OS sandbox; an unknown builder preserves output. An embedding host may register exact `(absolute executable,
absolute control-agent entrypoint)` pairs in `REAP_OUTPUT_CONTROL_AGENTS`; this is never a general Node exemption.

Only typed regenerable output can use this route. Source-root, Git, release, retention, protected-leaf, receipt,
identity and no-follow safeguards remain in force. `meta.paths`, cwd/worktrees and explicit output declarations are
checked freshly; a source-root claim alone is distinguished from an exact/descendant output claim.

## Tables the engine reads and writes

The host's database must provide at least:

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT, status TEXT NOT NULL, cwd TEXT, created_at REAL NOT NULL,
                   finished_at REAL, terminal_id TEXT, meta TEXT);
CREATE TABLE cleanup_records (terminal_id TEXT PRIMARY KEY, status TEXT NOT NULL);
```

plus whatever `log_event` writes to. Job ids are `J-` and four or five digits. Finished statuses are `done`, `failed` and
`cancelled`. The narrow output-only route additionally accepts `interrupted` when its interruption clock is verified;
this does not make interrupted source roots terminal.

The engine reads and writes:

- `meta` keys starting `reaper:`, `reap:` and `evidence-expiry:` (its own pacing and audit state);
- `meta` keys `cleanup:successor:*` (read: a JSON `{"job", "successor"}` handing a finished job's folders to a successor job,
  which keeps them while it is unfinished) and `cleanup:released:<job>` (written when that chain ends). The key suffix is not
  relied on: the record is found by the job it names in its `job` field, and the successor it names is followed in the chain. One
  record that cannot be read disables the whole rule, so a host that writes these records must write them as a JSON object;
- `meta` key `interrupt-heir:<job>` (read: the id of the continuation the host queued for an interrupted job). With that
  record, a verified interruption clock and a continuation chain that has finished, the interrupted job is finished work for
  the evidence and clone windows, aged from the chain's last finish; without them, the job keeps everything. A record naming
  a job that does not exist proves nothing, and a continuation that is still queued or running keeps the source's folders;
- `jobs.meta` (read: the `resume`, `redrive` and `automaticRecovery` records of a job that continues another one - their `from`
  field names the source, which keeps the chain whole even when the host's own record was overwritten);
- `jobs.cwd` and the `jobs.meta` JSON: reads `cwd` and `worktrees` (declared folders) and `waitingOn`, writes `reap` (its last word on the job).

## Entry points

`reap_tick(conn, policy, live, actions, errors)` and `evidence_expiry_tick(...)` run one scheduled pass. `tool_reap(params)`,
`tool_reap_status(params)`, `tool_reap_owner_status`, `tool_reap_owner_compact` and `tool_reap_acknowledge` back the CLI
commands of the same names (`src/trimreaper/cli.py` shows the parameters). `reap_closed_job(conn, policy, job_id, actions,
errors)` reaps one job the moment its worker closes.

## On-disk formats

The retention marker prefix `.ut-retention-` and the archive manifest kind `ut-overseer-reap-archive` come from
UltraTerm, where the engine started. They stay as they are so existing markers and archives keep working.
