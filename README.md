# TrimReaper

AI coding agents leave a lot behind. Every job clones a repository, installs `node_modules`, builds a Rust `target`,
writes screenshots and logs, and stages scratch folders. Run a few dozen agents and the disk fills in days.

TrimReaper takes that space back without guessing. It removes a folder only when it can prove the folder is
disposable: the job that made it has finished, git shows nothing would be lost, no process is using it, and it holds no
secrets. When a folder still holds unsaved work, TrimReaper archives that work first and verifies the archive. Every
removal is planned in a durable receipt before the first byte goes.

It is the reaper UltraTerm runs for its agent fleet: the same code, released here.

## Install

Requires macOS, Python 3.10 or newer, and `git`, `lsof` and `ps` on the `PATH`.

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

# Reap one job. Try it with --dry-run first:
trimreaper reap --job J-0042 --dry-run
trimreaper reap --job J-0042

# Or reap everything that is due in one pass:
trimreaper tick
```

`tick` runs once and exits. To run it on a schedule, call it from your own cron job or agent loop.

Every command prints one JSON document. A refusal exits with status 2 and names its cause, for example
`{"error": "J-0042 is not reapable: job-running"}`.

## What it removes

| What | When |
|---|---|
| `node_modules`, `.next`, `.open-next`, `.turbo`, `.astro`, `target` in a job's clone | 15 minutes after the job finishes, when git ignores the folder and the clone is clean and pushed |
| `dist` in a job's clone | Once the clone itself expires (`dist` can hold a release) |
| The whole clone | `cloneExpiryDays` (3) after every job named in it finished. Unsaved work is archived first |
| Per-job scratch folders (`cacheRoot/J-NNNN/*`) | Tool caches (with a `CACHEDIR.TAG`) once the job finishes; anything else after `evidenceExpiryDays` (14) |
| Screenshots, recordings, traces and logs in a job's scratch | `evidenceExpiryDays` (14) after the job finished. Each file's name and SHA-256 are logged first |
| Agent scratch folders (`scratchRoot/*`, off until configured) | Once nothing in them changed for `evidenceExpiryDays` |
| Its own archives | `archiveExpiryDays` (30) after the archive became the only copy |

Under disk pressure (at or above `diskWarnPercent`, 85%), the build output of parked or interrupted jobs goes too.
Those jobs rebuild it when they resume.

## What it never removes

- Anything of a job that is queued, approved, running or blocked, or that another job is waiting on.
- `.env*`, `.dev.vars*`, `.wrangler` and `.vercel`. TrimReaper never enters, reads or deletes them. A clone that holds one
  is kept whole (unless you set `cloneProtectedCopies` to `"remove"`, which archives them owner-only first).
- A folder carrying a retention marker other than `disposable`. Mark one with `trimreaper mark <folder> evidence`
  (classes: `disposable`, `evidence`, `customer`, `release-runtime-rollback`).
- Anything a process has open, uses as its working directory, or names on its command line.
- Anything reached through a symlink, on another volume, owned by another user, or outside the configured roots.
- Your home folder or a system folder as a root, even if the configuration says so.

## Archive before delete

An expired clone with uncommitted changes, untracked files, stashes or commits no remote holds is not deleted outright.
TrimReaper first writes an archive to `archiveRoot/J-NNNN/<folder>-<time>-<run>/`:

- `repo-0.bundle`: a self-contained git bundle of every commit no remote holds, verified in an empty repository;
- `files.tar.gz`: every file git does not hold unchanged, each checked against its SHA-256;
- `manifest.json`: what was saved, what was left out and why, and how to restore it.

Only after the archive verifies, and a final check finds the folder unchanged, does the folder go. To restore:

```sh
git init restored && cd restored
git fetch <archive>/repo-0.bundle 'refs/*:refs/restored/*'
tar -xzf <archive>/files.tar.gz
```

## Receipts and one owner at a time

Each removal writes a plan record before it starts and a result record after it ends, both to
`stateDir/reaper/receipts-YYYYMMDD.jsonl` (private, append-only). Only one reaper process deletes at a time. If a run
stops part-way, the next run refuses until someone reviews the receipts and acknowledges them with
`trimreaper owner status` and `trimreaper owner acknowledge`.

## Configuration

`~/.config/trimreaper/config.json` (or `$TRIMREAPER_CONFIG`). Every key is optional:

```json
{
  "cloneBases": [{"path": "~/dev"}],
  "scratchRoot": "~/.cache/agent-scratch",
  "policy": {"cloneExpiryDays": 3, "evidenceExpiryDays": 14}
}
```

All keys and defaults: [docs/CONFIGURATION.md](docs/CONFIGURATION.md). `trimreaper config` prints the effective
configuration.

## For agents

- Record the job when it starts (`job add`) and its end (`job status <id> done|failed|cancelled`). Name clone folders
  with the job number.
- Read `trimreaper status --items` before asking a person why disk is full. `summary.reasonActions` says what would
  free each kept folder.
- Never delete agent output with `rm -rf`. Mark folders you need to keep (`trimreaper mark <folder> evidence`).

## Embedding

The engine (`src/trimreaper/core.py`) runs either with the standalone host in this package or vendored byte for byte
into another program that supplies its own job store and folder layout. UltraTerm runs it that way.
See [docs/EMBEDDING.md](docs/EMBEDDING.md).

## More

- [docs/SAFETY.md](docs/SAFETY.md): every gate a removal passes, and the limits of what it can detect.
- [SECURITY.md](SECURITY.md): reporting a vulnerability.
- [CONTRIBUTING.md](CONTRIBUTING.md): tests and how changes reach UltraTerm.

## License

MIT. See [LICENSE](LICENSE).
