# Configuration

TrimReaper reads `$TRIMREAPER_CONFIG`, else `~/.config/trimreaper/config.json`. A missing file means every default
below. An unknown key is an error that names the key, so a typo never silently does nothing.

Paths are absolute or start with `~/`. Here `~` is the reap home (`home`), not the `HOME` of whoever runs the command.

| Key | Default | Meaning |
|---|---|---|
| `home` | your home folder | The home the reaper works below. `$TRIMREAPER_HOME` overrides it (tests, a second instance). |
| `stateDir` | `~/.local/state/trimreaper` | The job store (`state.sqlite3`) and the receipts (`reaper/`). The single-owner journal is always `~/.local/state/trimreaper/cleanup`, whatever `stateDir` says. |
| `cloneBases` | `[{"path": "~/dev"}]` | Folders that hold job clones. A clone is a direct child whose name carries a job number (`app-j0042`, `j0042-app`). With `"prefix": ".j"` a base only counts children whose names start with the prefix. |
| `cacheRoot` | `~/.cache/trimreaper/jobs` | Per-job scratch: `cacheRoot/J-0042/<anything>`. |
| `archiveRoot` | `~/.cache/trimreaper/archive` | Where unsaved work is archived before an expired folder goes. Created owner-only (0700). |
| `scratchRoot` | off | Agent scratch outside any job: `scratchRoot/<task>`. Each folder goes once nothing in it changed for `evidenceExpiryDays`. The idle proof covers the whole tree: every entry's mtime is read, never following a link, and the folder's own per-folder budget bounds the walk. The proof reads mtimes only: a tree moved or copied in with its old times (`mv`, `cp -a`, `rsync -a`, an archive) looks idle, and `trimreaper mark <folder> evidence` keeps it. |
| `releaseRoot` | off | Release candidates: `releaseRoot/<version>/...`. A Rust `target` folder of a version older than the installed one goes when it carries the `disposable` marker (a build queue writes it; `trimreaper mark <folder> disposable` by hand). |
| `releaseEvidenceRoot` | off | Per-version release evidence: `releaseEvidenceRoot/<version>` for the app, `releaseEvidenceRoot/<product>-<version>` for its components. App versions older than the installed one go, except the rollback; each component keeps its newest two. |
| `release.installedApp`, `release.rollbackApp` | unset | macOS app bundles identifying the installed product and the single rollback. Only known older artifacts of that same product may be reclaimed; installed, rollback and newer versions stay. Unknown or ambiguous identity/version, incomplete three-part versions, bundle-name/plist disagreement or incomplete inventory fail closed: affected artifacts stay. While either configured version is unknown, every release candidate and app-version evidence folder stays. Existing locks, live claims and Git safeguards still apply. |
| `buildQueueStateDir` | `stateDir/build-queue` | The lock folder of a build queue. Before removing build output, the reaper takes `projects/<name>-<sha256(path)[:16]>.lock` for every enclosing project, so a build in progress keeps its output. |
| `diskVolume` | `/System/Volumes/Data`, else `/` | The volume measured for disk pressure and for what `clean` freed. |
| `worktreeHomes` | the known agent folders | Folders where agent tools keep worktrees outside the repository, searched three levels deep for worktrees whose repository is then listed with `git worktree list`. Default: `$CODEX_HOME/worktrees` (or `~/.codex/worktrees`), `~/.cursor/worktrees`, `~/conductor/workspaces`, `~/.superset/worktrees`, `~/.claude-squad/worktrees`, `$TMPDIR/vibe-kanban/worktrees`. `[]` searches only the repositories in `cloneBases`. |
| `policy` | see below | Retention rules. |

## Policy

| Key | Default | Meaning |
|---|---|---|
| `autoReap` | `true` | `trimreaper tick` reaps only while this is on. |
| `cloneExpiryDays` | `3` | A whole clone goes this many days after every job named in it finished, and after any later job that worked in it finished. `0`: clones never expire. |
| `evidenceExpiryDays` | `14` | Screenshots, recordings, traces and logs of a finished job expire after this many days. Also the idle time for scratch folders. `0` turns both off. |
| `markedEvidenceExpiryDays` | `5` | Marked bulky job-cache evidence expires strictly after this many days from terminal completion, only with an existing regular, owned `report.md`. Keeps reports, summary/outcome, handoffs, receipts and retained material. Uses the guarded folder reaper and automatic ticks. `0` disables marked expiry without changing unmarked expiry. |
| `archiveExpiryDays` | `30` | An archive goes this many days after it became the only copy of its clone. `0`: archives are kept. |
| `cloneArchive` | `"archive"` | `"archive"` saves work no remote holds before an expired folder goes. Any other value keeps such folders. |
| `agentScratchProtectedCopies` | `"keep"` | `"keep"` retains scratch holding protected names. `"archive"` allows an expired top-level scratch task outside the job-cache and archive roots to go only after every protected file has a verified owner-only copy in `protected.tar.gz`. These scratch archives never expire automatically. Needs `cloneArchive: "archive"`. Recent changes, live claims, preserving markers and all other removal gates still keep the task. |
| `cloneProtectedCopies` | `"keep"` | `"keep"` keeps every clone that holds `.env*`, `.dev.vars*`, `.wrangler` or `.vercel`. `"remove"` archives those owner-only (`protected.tar.gz`) and lets them go with the clone; it needs `cloneArchive` `"archive"`. Copies inside dependency or build folders (`node_modules/.env` ...) are not archived: they go with the folder. |
| `diskWarnPercent` | `85` | At or above this used percent, the build output of parked, blocked or interrupted jobs goes too. Checked on every `tick` that has idle jobs. |
| `parkedReapHours` | `24` | A parked or blocked job with no report, start or park for this many hours gives up its derived build output (`node_modules`, `target`, `.next`, `.open-next`, `.turbo`, `.astro`); its clone, cache and evidence stay. `0`: only under disk pressure. |
| `reapOpenTerminalGraceHours` | `0` | A finished job whose worker terminal is still open stops holding that job's folders once the job finished this many hours ago and the terminal has been idle as long. The terminal is never closed; a resumed terminal keeps the wait, and every other proof still runs. `0`: such a job keeps every folder while its terminal is open. |
| `cleanAtPercent` | `85` | High watermark: starts a safe clean pass. Sustained pressure retries after the ten-minute cooldown until the low watermark is met. `0` disables automatic cleaning. Otherwise it must exceed `cleanToPercent` and be at most 100. |
| `cleanToPercent` | `80` | Low watermark: try largest disposable candidates first until measured occupancy reaches this goal. Must be positive and below the high watermark. Reaching this level or lower re-arms the high crossing. Unmet goals retry while pressure remains, or on a changed release event, after the cooldown. |
| `cleanGrowthGB` | `5` | New pressure after an automatic pass: this many more GB used than when that pass ended. |
| `cleanFloorGB` | `10` | At or above the high watermark, further decline below this free-space floor counts as new pressure. |
| `projectIdleHours` | `48` | A project's dependency and output folders (`node_modules`, `.next`, `.venv` ...) are removed by `clean` after this many hours without a change in the project; a copy named after a finished job needs 6. `0` keeps them all. |
| `worktreeIdleDays` | `7` | A linked git worktree no job owns is removed by `clean` after this many days without a change, and every other check. `0` keeps them all. |
| `agentScratchIdleMaxEntries` | `200000` | How many entries one agent-scratch folder's idle walk may list. A folder that outgrows its budget is kept, and the receipt names the budget the walk spent. A whole number of at least 1. |
| `agentScratchIdleMaxBytes` | `0` | How many bytes one agent-scratch folder's idle walk may see. `0` is no byte bound at all: bytes bound no work, so the entry budget is what limits the walk. A folder that outgrows a bound you set is kept, and the receipt names the bound and what the walk spent. A whole number of at least 0. |
| `agentScratchIdleMaxSeconds` | `10` | How long one agent-scratch folder's idle walk may take. A walk that runs out of time is not idle: the folder is kept, and the receipt names the seconds it spent. A whole number of at least 1. |

## Jobs

`trimreaper job add J-0042 --cwd <folder> [--worktree <folder>]... [--waits-on J-0041] [--status running]`

- `--cwd` and `--worktree` declare folders as the job's own. A declared linked git worktree inside a clone base is
  reaped with the job. While the job is unfinished, nothing inside a declared folder is touched.
- `--waits-on` keeps the other job's artifacts while this job is unfinished.
- Statuses: `queued`, `approved`, `running`, `blocked` (unfinished), `parked` (set aside to resume later) and
  `interrupted` (stopped part-way; also unfinished), and `done`, `failed`, `cancelled` (finished).
  `trimreaper job status J-0042 done` records the finish time; setting an unfinished status reopens the job.
- Job ids are `J-` and four digits, or five without a leading zero: the forms a folder name `j0042` or `j10042` maps to.
- Events record who acted: `$TRIMREAPER_ACTOR`, else `cli`.
