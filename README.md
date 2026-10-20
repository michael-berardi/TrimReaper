# TrimReaper

AI coding agents leave a lot behind. Every job clones a repository, installs `node_modules`, builds a Rust `target`,
writes screenshots and logs, and stages scratch folders. Run a few dozen agents and the disk fills in days.

TrimReaper takes that space back without guessing. It removes a folder only when it can prove the folder is
disposable: the job that made it has finished, git shows nothing would be lost, no process is using it, and it holds no
secrets. When a folder still holds unsaved work, TrimReaper archives that work first and verifies the archive. Before it
removes anything of yours, it writes the plan to a durable receipt.

It also cleans the rest of a developer machine the same careful way: tool caches through each tool's own cleaner,
and the worktrees that Claude Code, Codex, Cursor and other agents leave behind. It can run by itself when the disk
fills up. Discovery uses names, sizes and times. Files selected for an archive are read to preserve your work.

It is the reaper engine of UltraTerm, released on its own so any agent setup can use it.

## Install

Requires macOS, Python 3.10 or newer, and `git`, `lsof` and `ps`. The `python3` that ships with macOS is 3.9; a
newer one comes from [python.org](https://www.python.org/downloads/) or Homebrew (`brew install python`).

```sh
pipx install "git+https://github.com/michael-berardi/TrimReaper@v0.6.1"
trimreaper --version
```

Or run it from a checkout with no install: `PYTHONPATH=src python3 -m trimreaper --help`.

## Quick start

The job reaper only touches folders that belong to jobs it knows about (`trimreaper clean`, below, needs no jobs). A job has an id like `J-0042`, and its clone's
folder name carries the same number (`myapp-j0042`, `j0042-myapp`).

```sh
# When an agent starts working in ~/dev/myapp-j0042:
trimreaper job add J-0042 --cwd ~/dev/myapp-j0042 --title "Fix the login form"

# When it finishes:
trimreaper job status J-0042 done

# See what would go, what stays, and why. Deletes nothing:
trimreaper status --items

# 15 minutes after the job finished (in case it is resumed), reap it. Try --dry-run first:
trimreaper reap --job J-0042 --dry-run
trimreaper reap --job J-0042

# Or reap everything that is due in one pass:
trimreaper tick
```

`tick` runs once and exits. `trimreaper schedule install` runs it every 30 minutes with launchd (macOS); elsewhere,
call it from cron or your agent loop. At 85% used, `tick` also runs `trimreaper clean`, targeting 80% used.
While pressure remains, it retries at most once per ten minutes until the measured 80% goal is met. Changed job-release events can also trigger a retry after that cooldown.

## Clean the whole machine

`trimreaper clean` frees space that no job owns. It needs no setup.

```sh
trimreaper clean --dry-run   # what would go, what stays and why, plus a report of models and other large things
trimreaper clean             # do it
trimreaper clean --only npm,worktrees
trimreaper clean --deep      # also the Go module cache, unused Docker images and unavailable simulators
trimreaper clean --high-watermark 85 --low-watermark 80
```

| What | How |
|---|---|
| npm, pnpm, yarn (1.x), bun, uv, pip, Go build, Homebrew caches | The tool's own cleaner (`npm cache clean --force`, `uv cache prune`, `brew cleanup --prune=all` ...), run from your home folder and skipped while that tool runs. Homebrew also drops old versions of installed formulae; it never uninstalls one (`HOMEBREW_NO_AUTOREMOVE`) |
| Docker build cache | `docker builder prune -af`, only when Docker is already running on a local socket (never a remote `DOCKER_HOST` or context, never volumes) |
| Cargo's registry, Xcode's DerivedData | Folder by folder through the same guarded removal as job folders, skipped while Cargo, rust-analyzer or Xcode runs |
| Agent worktrees | A linked git worktree no job owns goes after `worktreeIdleDays` (7) without a change, when it is unlocked and clean (edits hidden by `--skip-worktree` or `--assume-unchanged` count as changes), git ignores nothing in it but build output, it holds no `.env*` or other protected name, no unfinished job declares it, no process uses it, and every commit its HEAD ever pointed at is on a branch, tag or remote. Its branch stays. A worktree whose folder is already gone is forgotten by its repository after 30 days, under the same commit check |
| Project dependencies and output | `node_modules`, `.next`, `.astro`, `.turbo`, `.svelte-kit`, `.nuxt`, `.parcel-cache`, `.vite`, `.venv` and Python tool caches inside any repository in the code homes, when git ignores the folder, its manifest (`package.json`, `pyproject.toml`, `requirements.txt` ...) sits beside it so one install brings it back, the project has gone `projectIdleHours` (48) without a change (6 hours for a copy named after a finished job), no running job works in it and no process uses anything in it. `dist` and `build` are never touched: some projects commit them |
| Rust build output | `debug` and `incremental` folders of every `target` in a repository (release artifacts stay), skipped while Cargo or rust-analyzer runs. Discovery is breadth-first and never enters tool caches, virtual environments or dependency stores |
| Repository-output safeguards | Dependency and Rust output stays when Git cannot prove the repository clean and pushed, the folder ignored and untracked, a running job owns the project, or an enclosing folder up to the reap home carries a retention marker other than `disposable`. Removal holds the build queue's project lock and rechecks every proof |
| Temp folders | A folder of yours in the shared temp folder after a day without a change, and in your per-user temp folder after three days, when nothing in it changed in that time and no process uses it. Folders the system or running apps manage there (`com.apple.*`, sockets, locks) are never candidates |
| Browser code-sign clones (macOS) | A browser copies its app bundle into your per-user `X` folder at every launch and removes it on quit; a killed browser (automation, a crash) leaves one behind each time. Each abandoned copy goes after an hour when no process has it open |
| Go module cache, unused Docker images, unavailable simulators | Only with `--deep` (`xcrun simctl delete unavailable` also deletes the app data of simulators the selected Xcode cannot run) |
| Superseded Playwright browser builds | Older builds only, when a newer build of the same browser is installed and no process uses the old one |
| Local models (Hugging Face, Ollama, LM Studio), Docker's disk image, local snapshots | Reported with their size. Never removed |

Worktrees are found through `git worktree list` in every repository in `cloneBases`, and in the folders where agent tools
keep their own: `~/.codex/worktrees` (or `$CODEX_HOME/worktrees`), `~/.cursor/worktrees`, `~/conductor/workspaces`,
`~/.superset/worktrees`, `~/.claude-squad/worktrees` and Vibe Kanban's temporary folder. Claude Code's
`<repo>/.claude/worktrees` sit inside the repository and are found through it. `worktreeHomes` replaces the list.

Cleanup sizes candidates, tries the largest first and stops when measured occupancy reaches the low watermark.
`goalMet` and `remainingBytes` report the actual result; unknown disk measurements produce `null`, not success.
Protected or active data may prevent reaching the goal. Missing or unreadable candidates are skipped with a named reason;
independent candidates continue. What was freed is the volume's free space after the pass minus before it, not a sum of folder sizes. A tool's cleaner
that fails is listed with its exit status and last line of output, and the command exits 3.

Every command prints one JSON document on stdout. Exit status:

- `0`: done. Folders that were kept are listed with their reason (`"deleted": 0` is not an error).
- `2`: refused or failed. A JSON `error` naming the cause, and a `hint`, go to stderr. Example:
  `{"error": "J-0042 is not reapable: job-running", "hint": "..."}`.
- `3`: the pass ran but reported `errors` or stopped part-way (both named in the JSON).

## What it removes

| What | When |
|---|---|
| `node_modules`, `.next`, `.open-next`, `.turbo`, `.astro`, `target` in a job's clone | 15 minutes after the job finishes, when git ignores the folder (untracked `.astro` metadata is also recognized without an ignore rule) and tracks nothing in it. Uncommitted or stashed work in the clone does not preserve build output that rebuilds from the source that stays |
| `dist` in a job's clone | Once the clone itself expires (`dist` can hold a release) |
| The whole clone | `cloneExpiryDays` (3) after every job named in it finished. Unsaved work is archived first |
| Per-job scratch folders (`cacheRoot/J-NNNN/*`) | Tool caches (with a `CACHEDIR.TAG`) once the job finishes; anything else `evidenceExpiryDays` (14) after it finished |
| Screenshots, recordings, traces and logs in a job's scratch | `evidenceExpiryDays` after the job finished. Each file's name and SHA-256 are logged first |
| Marked bulky job-cache evidence | Strictly more than `markedEvidenceExpiryDays` (5) after the job finished, only with an existing, non-empty `report.md`. Reports, summary/outcome, handoffs and receipts stay. Retained material, protected names and active claims still prevent removal |
| Agent scratch folders (`scratchRoot/*`) | Once nothing in them changed for `evidenceExpiryDays`. Active or parked job claims preserve that task, not unrelated siblings. Off until you set `scratchRoot` |
| Its own archives | `archiveExpiryDays` (30) after the archive became the only copy |
| Old release builds and release evidence | Off until configured: [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |

A host with the producer registry enabled can reclaim an exact `target` or `node_modules` of a settled finished,
failed, cancelled or verifiably interrupted job even when a continuation keeps the source worktree. This requires the
producer's source/output identity record, shared build locks, no live claim on the exact output, no open files and
no running builder. A source working directory alone is not an output handle. Source and retained data stay;
legacy outputs without producer proof stay on the stricter route. See [the embedding contract](docs/EMBEDDING.md).

Under disk pressure (at or above `diskWarnPercent`, 85%), the build output of parked or interrupted jobs goes too;
they rebuild it when they resume. The pressure check runs during a `tick` that has at least one finished job due.

## What it never removes

- Anything of a job that is queued, approved, running, blocked, parked or interrupted, or that another job waits on
  (`job add ... --waits-on`). Exceptions are the guarded exact-output and disk-pressure rules above; neither grants permission to delete source.
- `.env*`, `.dev.vars*`, `.wrangler` and `.vercel` local files. A clone that holds one is kept whole, and TrimReaper
  does not enter or read them. Two exceptions: env *templates* that git tracks (`.env.example`, `.env.sample`,
  `.dev.vars.template` ...) go with an expired clone, since the remote holds the same file; and with
  `cloneProtectedCopies` set to `"remove"`, the local copies are archived owner-only, then go with the clone.
- A folder that carries a retention marker other than `disposable`, except expired per-job `evidence` under the five-day rule above: a file named `.ut-retention-<class>` at its top
  level (`ut` is from UltraTerm, where the format started). Write one with `trimreaper mark <folder> <class>`:
  `evidence` (proof kept for the marked-evidence window; set `markedEvidenceExpiryDays=0` for indefinite retention), `customer` (customer data), `release-runtime-rollback` (what a release or
  its rollback needs). An `evidence` marker on a job's own cache cannot outlive the job's promise: once that job is
  cancelled and `evidenceExpiryDays` has passed, the cache keeps only the normal window instead of forever. `disposable`
  is the opposite: it allows removal, still subject to every other check.
- Anything a process has open or names on its command line. A source-only working directory is exempt only for the guarded exact-output route described above.
- Anything reached through a symlink, on another volume, owned by another user, outside the configured folders, or carrying immutable, no-unlink or restricted filesystem flags. System-protected temp directories stay; flags are never cleared.
- Your home folder or a system folder as a root, even if the configuration says so.

## Archive before delete

An expired clone with uncommitted changes, untracked files, stashes or commits no remote holds is not deleted outright.
TrimReaper first writes an archive to `archiveRoot/J-NNNN/<folder>-<time>-<run>/`:

- `repo-0.bundle` (when there are commits no remote holds): a self-contained git bundle, verified in an empty repository;
- `files.tar.gz`: the files git does not hold unchanged, each checked against its SHA-256. Dependency and build folders
  (`node_modules`, `target`, `.venv` ...) and evidence files past `evidenceExpiryDays` are left out and listed;
- `manifest.json`: what was saved, what was left out and why, and how to restore it.

The folder goes only after the archive verifies and a final check finds the folder unchanged. An archive larger than
4 GiB or 200,000 files is refused, and the folder is kept. To restore:

```sh
git init restored && cd restored
git fetch <archive>/repo-0.bundle 'refs/heads/*:refs/heads/*'
git checkout ut-archive/<n>   # manifest.json: repositories[].bundle.refs maps each saved branch to where it came from ("HEAD", ...)
tar -xzf <archive>/files.tar.gz
```

## Receipts and one owner at a time

Each removal writes a plan record before it starts and a result record after it ends, to
`stateDir/reaper/receipts-YYYYMMDD.jsonl` (private, append-only). Only one TrimReaper process removes things at a time.
If a run stops part-way (a crash, a full disk, a folder that changed during removal), every later run refuses until
someone reviews the receipts and acknowledges them: `trimreaper owner status`, then `trimreaper owner acknowledge`
([docs/SAFETY.md](docs/SAFETY.md#acknowledging-a-stopped-run)).

## Configuration

`~/.config/trimreaper/config.json` (or `$TRIMREAPER_CONFIG`). Every key is optional, and a wrong key or value is an
error that names it:

```json
{
  "cloneBases": [{"path": "~/dev"}],
  "scratchRoot": "~/.cache/agent-scratch",
  "policy": {"cloneExpiryDays": 3, "evidenceExpiryDays": 14, "cleanAtPercent": 85, "cleanToPercent": 80}
}
```

`trimreaper policy-set evidenceExpiryDays=7` changes one policy value. All keys and defaults:
[docs/CONFIGURATION.md](docs/CONFIGURATION.md). `trimreaper config` prints the effective configuration.

## For agents

- Record the job when it starts (`job add`) and when it ends (`job status <id> done|failed|cancelled`). Name clone
  folders with the job number.
- Prefer the MCP tools when your client has them (below); the command line works the same way.
- Low on disk? `clean` with a dry run first, then without. Leave models and other reported items to a person.
- Read `trimreaper status --items` before asking a person why the disk is full. `summary.reasonActions` says what would
  free each kept folder.
- Never delete agent output with `rm -rf`. Mark evidence that needs the five-day window (`trimreaper mark <folder> evidence`); retain each job's `report.md`, handoff, summary/outcome and receipts. Customer and release/runtime/rollback markers never expire through this rule.

## MCP server

`trimreaper mcp` serves the same commands as tools to any MCP client over stdio: `job_add`, `job_status`, `job_list`,
`reap_status` (read-only), `reap`, `clean` and `mark`. `reap` and `clean` are dry runs unless the call passes
`"dryRun": false`, and every check above still applies. Acknowledging a stopped run, changing the policy and installing
the schedule stay on the command line, with a person.

Claude Code:

```sh
claude mcp add trimreaper -- trimreaper mcp
```

Other clients (Claude Desktop, Cursor and similar), in their MCP config:

```json
{"mcpServers": {"trimreaper": {"command": "trimreaper", "args": ["mcp"]}}}
```

## Embedding

The engine (`src/trimreaper/core.py`) and the clean pass (`src/trimreaper/clean.py`) run either with the standalone host
in this package or vendored byte for byte into another program that supplies its own job store and folder layout. See
[docs/EMBEDDING.md](docs/EMBEDDING.md).

## More

- [docs/SAFETY.md](docs/SAFETY.md): every gate a removal passes, and the limits of what it can detect.
- [SECURITY.md](SECURITY.md): reporting a vulnerability.
- [CONTRIBUTING.md](CONTRIBUTING.md): running the tests and changing the engine.

## License

MIT. See [LICENSE](LICENSE).
