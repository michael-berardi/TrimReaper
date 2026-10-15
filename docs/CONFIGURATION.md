# Configuration

TrimReaper reads `$TRIMREAPER_CONFIG`, else `~/.config/trimreaper/config.json`. A missing file means every default
below. An unknown key is an error that names the key, so a typo never silently does nothing.

Paths are absolute or start with `~/`. Here `~` is the reap home (`home`), not the `HOME` of whoever runs the command.

| Key | Default | Meaning |
|---|---|---|
| `home` | your home folder | The home the reaper works below. `$TRIMREAPER_HOME` overrides it (tests, a second instance). |
| `stateDir` | `~/.local/state/trimreaper` | The job store (`state.sqlite3`) and the receipts (`reaper/`). |
| `cloneBases` | `[{"path": "~/dev"}]` | Folders that hold job clones. A clone is a direct child whose name carries a job number (`app-j0042`, `j0042-app`). With `"prefix": ".j"` a base only counts children whose names start with the prefix. |
| `cacheRoot` | `~/.cache/trimreaper/jobs` | Per-job scratch: `cacheRoot/J-0042/<anything>`. |
| `archiveRoot` | `~/.cache/trimreaper/archive` | Where unsaved work is archived before an expired folder goes. Created owner-only (0700). |
| `scratchRoot` | off | Agent scratch outside any job: `scratchRoot/<task>`. Each folder goes once nothing in it changed for `evidenceExpiryDays`. |
| `releaseRoot` | off | Release candidates: `releaseRoot/<version>/...`. A Rust `target` folder of a version older than the installed one goes when it carries the `disposable` marker (a build queue writes it; `trimreaper mark <folder> disposable` by hand). |
| `releaseEvidenceRoot` | off | Per-version release evidence: `releaseEvidenceRoot/<version>` for the app, `releaseEvidenceRoot/<product>-<version>` for its components. App versions older than the installed one go, except the rollback; each component keeps its newest two. |
| `release.installedApp`, `release.rollbackApp` | unset | macOS app bundles whose versions are kept. While either is unknown, every release candidate and every app-version evidence folder is kept. |
| `buildQueueStateDir` | `stateDir/build-queue` | The lock folder of a build queue. Before removing build output, the reaper takes `projects/<name>-<sha256(path)[:16]>.lock` for every enclosing project, so a build in progress keeps its output. |
| `diskVolume` | `/System/Volumes/Data`, else `/` | The volume measured for disk pressure. |
| `policy` | see below | Retention rules. |

## Policy

| Key | Default | Meaning |
|---|---|---|
| `autoReap` | `true` | `trimreaper tick` reaps only while this is on. |
| `cloneExpiryDays` | `3` | A whole clone goes this many days after every job named in it finished, and after any later job that worked in it finished. |
| `evidenceExpiryDays` | `14` | Screenshots, recordings, traces and logs of a finished job expire after this many days. Also the idle time for scratch folders. `0` turns expiry off. |
| `archiveExpiryDays` | `30` | An archive goes this many days after it became the only copy of its clone. |
| `cloneArchive` | `"archive"` | `"archive"` saves work no remote holds before an expired folder goes. Any other value keeps such folders. |
| `cloneProtectedCopies` | `"keep"` | `"keep"` keeps every clone that holds `.env*`, `.dev.vars*`, `.wrangler` or `.vercel`. `"remove"` archives those owner-only (`protected.tar.gz`) and lets them go with the clone. |
| `diskWarnPercent` | `85` | At or above this used percent, the build output of parked or interrupted jobs goes too. |
| `diskHoldPercent` | `92` | Reported as the `hold` disk level. |

## Jobs

`trimreaper job add J-0042 --cwd <folder> [--worktree <folder>]... [--waits-on J-0041] [--status running]`

- `--cwd` and `--worktree` declare folders as the job's own. A declared linked git worktree inside a clone base is
  reaped with the job. While the job is unfinished, nothing inside a declared folder is touched.
- `--waits-on` keeps the other job's artifacts while this job is unfinished.
- Statuses: `queued`, `approved`, `running`, `blocked`, `parked`, `interrupted` (unfinished), and `done`, `failed`,
  `cancelled` (finished). `trimreaper job status J-0042 done` records the finish time.
