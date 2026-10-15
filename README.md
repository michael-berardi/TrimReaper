# TrimReaper

AI coding agents leave a lot behind. Every job clones a repository, installs `node_modules`, builds a Rust `target`,
writes screenshots and logs, and stages scratch folders. Run a few dozen agents and the disk fills in days.

TrimReaper takes that space back without guessing. It removes a folder only when it can prove the folder is
disposable: the job that made it has finished, git shows nothing would be lost, no process is using it, and it holds no
secrets. When a folder still holds unsaved work, TrimReaper archives that work first and verifies the archive. Before it
removes anything of yours, it writes the plan to a durable receipt.

It is the reaper engine of UltraTerm, released on its own so any agent setup can use it.

## Install

Requires macOS, Python 3.10 or newer, and `git`, `lsof` and `ps`. The `python3` that ships with macOS is 3.9; a
newer one comes from [python.org](https://www.python.org/downloads/) or Homebrew (`brew install python`).

```sh
pipx install "git+https://github.com/michael-berardi/TrimReaper@v0.1.0"
trimreaper --version
```

Or run it from a checkout with no install: `PYTHONPATH=src python3 -m trimreaper --help`.

## Quick start

TrimReaper only touches folders that belong to jobs it knows about. A job has an id like `J-0042`, and its clone's
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

`tick` runs once and exits. To run it on a schedule, call it from your own cron job, launchd agent or agent loop.

Every command prints one JSON document on stdout. Exit status:

- `0`: done. Folders that were kept are listed with their reason (`"deleted": 0` is not an error).
- `2`: refused or failed. A JSON `error` naming the cause, and a `hint`, go to stderr. Example:
  `{"error": "J-0042 is not reapable: job-running", "hint": "..."}`.
- `3`: the pass ran but reported `errors` or stopped part-way (both named in the JSON).

## What it removes

| What | When |
|---|---|
| `node_modules`, `.next`, `.open-next`, `.turbo`, `.astro`, `target` in a job's clone | 15 minutes after the job finishes, when git ignores the folder and the clone is clean and pushed |
| `dist` in a job's clone | Once the clone itself expires (`dist` can hold a release) |
| The whole clone | `cloneExpiryDays` (3) after every job named in it finished. Unsaved work is archived first |
| Per-job scratch folders (`cacheRoot/J-NNNN/*`) | Tool caches (with a `CACHEDIR.TAG`) once the job finishes; anything else `evidenceExpiryDays` (14) after it finished |
| Screenshots, recordings, traces and logs in a job's scratch | `evidenceExpiryDays` after the job finished. Each file's name and SHA-256 are logged first |
| Agent scratch folders (`scratchRoot/*`) | Once nothing in them changed for `evidenceExpiryDays`. Off until you set `scratchRoot` |
| Its own archives | `archiveExpiryDays` (30) after the archive became the only copy |
| Old release builds and release evidence | Off until configured: [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |

Under disk pressure (at or above `diskWarnPercent`, 85%), the build output of parked or interrupted jobs goes too;
they rebuild it when they resume. The pressure check runs during a `tick` that has at least one finished job due.

## What it never removes

- Anything of a job that is queued, approved, running, blocked, parked or interrupted, or that another job waits on
  (`job add ... --waits-on`). The one exception is the disk-pressure rule above.
- `.env*`, `.dev.vars*`, `.wrangler` and `.vercel` local files. A clone that holds one is kept whole, and TrimReaper
  does not enter or read them. Two exceptions: env *templates* that git tracks (`.env.example`, `.env.sample`,
  `.dev.vars.template` ...) go with an expired clone, since the remote holds the same file; and with
  `cloneProtectedCopies` set to `"remove"`, the local copies are archived owner-only, then go with the clone.
- A folder that carries a retention marker other than `disposable`: a file named `.ut-retention-<class>` at its top
  level (`ut` is from UltraTerm, where the format started). Write one with `trimreaper mark <folder> <class>`:
  `evidence` (proof of work you want kept), `customer` (customer data), `release-runtime-rollback` (what a release or
  its rollback needs). `disposable` is the opposite: it allows removal, still subject to every other check.
- Anything a process has open, uses as its working directory, or names on its command line.
- Anything reached through a symlink, on another volume, owned by another user, or outside the configured folders.
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
  "policy": {"cloneExpiryDays": 3, "evidenceExpiryDays": 14}
}
```

`trimreaper policy-set evidenceExpiryDays=7` changes one policy value. All keys and defaults:
[docs/CONFIGURATION.md](docs/CONFIGURATION.md). `trimreaper config` prints the effective configuration.

## For agents

- Record the job when it starts (`job add`) and when it ends (`job status <id> done|failed|cancelled`). Name clone
  folders with the job number.
- Prefer the MCP tools when your client has them (below); the command line works the same way.
- Read `trimreaper status --items` before asking a person why the disk is full. `summary.reasonActions` says what would
  free each kept folder.
- Never delete agent output with `rm -rf`. Mark folders you need to keep (`trimreaper mark <folder> evidence`).

## MCP server

`trimreaper mcp` serves the same commands as tools to any MCP client over stdio: `job_add`, `job_status`, `job_list`,
`reap_status` (read-only), `reap` and `mark`. `reap` is a dry run unless the call passes `"dryRun": false`, and every
check above still applies. Acknowledging a stopped run and changing the policy stay on the command line, with a person.

Claude Code:

```sh
claude mcp add trimreaper -- trimreaper mcp
```

Other clients (Claude Desktop, Cursor and similar), in their MCP config:

```json
{"mcpServers": {"trimreaper": {"command": "trimreaper", "args": ["mcp"]}}}
```

## Embedding

The engine (`src/trimreaper/core.py`) runs either with the standalone host in this package or vendored byte for byte
into another program that supplies its own job store and folder layout. See [docs/EMBEDDING.md](docs/EMBEDDING.md).

## More

- [docs/SAFETY.md](docs/SAFETY.md): every gate a removal passes, and the limits of what it can detect.
- [SECURITY.md](SECURITY.md): reporting a vulnerability.
- [CONTRIBUTING.md](CONTRIBUTING.md): running the tests and changing the engine.

## License

MIT. See [LICENSE](LICENSE).
