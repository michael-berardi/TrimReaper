# Changelog

## 0.5.1 (2026-10-18)

- Re-arm the high-water trigger when disk use reaches the low watermark exactly, not only below it. This preserves the next high-water crossing on smaller volumes where the growth threshold may not otherwise trigger.

## 0.5.0 (2026-10-18)

- Automatic cleanup starts at 85% used and targets 80%, with measured `goalMet` and `remainingBytes`. Largest disposable candidates go first; active or protected data can leave the goal unmet. Retries remain event-driven.
- Vanished and unreadable candidates are skipped independently instead of aborting the whole plan. Final removal checks remain fail-closed.
- A separate exact-output route supports finished, failed, cancelled and verifiably interrupted jobs, including detached build targets and source worktrees used by a continuation. It requires identity-bound producer records, canonical build locks, fresh exact-output claims and process checks. Source-root cleanup keeps its existing safeguards; old or unmanaged outputs without producer proof remain preserved.
- Embedded hosts can report the engine version and verify both vendored region digests. The standalone install does not update an embedded host.

## 0.4.1 (2026-10-18)

- Repository dependency and Rust build cleanup now refuses dirty, unpushed, tracked or unproved output, including changes hidden by Git index flags. Retention markers on enclosing folders and running jobs protect their output too.
- Project build-queue locks are acquired for actual repository-output removal, including when no previous build created the lock. Git, retention, ownership and process checks run again while the locks are held.
- The install example names the current release. Browser documentation distinguishes superseded builds from report-only models and disk images.

## 0.4.0 (2026-10-16)

- New clean target `project-dependencies`: `node_modules`, `.next`, `.astro`, `.turbo`, `.svelte-kit`, `.nuxt`, `.parcel-cache`, `.vite`, `.venv` and Python tool caches in idle repositories, only when git ignores the folder and its manifest is beside it. A project is idle after `projectIdleHours` (48, new policy key) without a change, or 6 hours for a copy named after a finished job. Only a job with a live worker protects a project's output; a declared folder that is a whole code home protects nothing.
- New clean target `browser-clones` (macOS): abandoned browser code-sign clones in the per-user `X` folder, one left behind by every killed browser launch.
- `agent-leftovers` also removes idle temp folders of yours: in the shared temp folder after a day, in the per-user temp folder after three days. System-managed names are never candidates.
- Idle time no longer counts a folder's own timestamp or its regenerable output folders, so a clean pass removing one output never makes a project or worktree look freshly worked on.

## 0.3.3 (2026-10-16)

- Rust build-output discovery finishes on large machines: it walks breadth-first, never enters tool caches (uv, npm, pip, Hugging Face and the like), virtual environments, dependency stores or generated web output, searches only four levels deep outside a repository, and has its own names-only budget of 3,000,000 entries. On the machine that reported `discovery-incomplete` on every pass it found 19 targets instead of 1, in about 20 seconds. The error now says how many entries were listed.

## 0.3.2 (2026-10-16)

- Rust discovery no longer walks the temp folder (unreadable system folders made every pass report an error); temp build roots stay with the agent-leftovers target.

## 0.3.1 (2026-10-16)

- Rust build-output discovery searches the usual code and build homes (`~/dev`, `~/Developer`, `~/src`, `~/code`, `~/projects`, `~/repos`, `~/work`, `~/.cache`, TMPDIR) plus clone bases and worktree homes, instead of walking the whole home, which ran out of its entry budget on a real machine and found nothing.

## 0.3.0 (2026-10-16)

- Automatic clean is event-driven: one pass when the volume reaches `cleanAtPercent`, then only on new pressure (`cleanGrowthGB`, `cleanFloorGB`); dropping below re-arms it. `clean_auto_observe` decides without starting anything. No hourly repeat.
- Parked or blocked jobs give up derived build output after `parkedReapHours` (24) without activity; disk pressure now reaps blocked jobs' output too.
- Expired job cache folders are no longer kept because their job folder carries a `disposable` marker.
- A folder whose `.env*`/`.envrc` files are all listed with matching hashes in its own `FILES.sha256` (a pinned vendored source copy) is no longer kept for them; `.wrangler`/`.vercel` still always block.
- `clean` also removes Rust `debug` and incremental output no compiler uses (release artifacts stay), stale agent session folders under `/private/tmp/claude-<uid>` and build roots in TMPDIR (six hours idle), old Playwright browser builds no process uses, and the Docker build cache with a Colima `fstrim`.
- An automatic pass reports the space it actually freed.

## 0.2.0 (2026-10-15)

- `trimreaper clean`: frees space no job owns, on any machine and for any user, with no setup. Tool caches go through the
  tool's own cleaner (npm, pnpm, yarn 1.x, bun, uv, pip, Go, Homebrew without autoremove, Docker build cache on a local
  socket) and only while that tool is not running; Cargo's registry and Xcode's DerivedData go through the guarded
  removal; `--deep` adds the Go module cache, unused Docker images and unavailable simulators. Local models, Playwright browsers, Docker's disk image and local snapshots are
  reported, never removed. What was freed is measured as the volume's free space after minus before.
- Agent worktrees: linked git worktrees no job owns (Claude Code, Codex, Cursor, Conductor, Superset, claude-squad, Vibe
  Kanban, or any `git worktree add`) are removed once idle `worktreeIdleDays` (7), unlocked, clean (hidden index edits
  included), still exactly the worktree their repository records, holding only build output as ignored files, holding no
  protected name, declared by no unfinished job, unused by any process, and with every commit their HEAD ever pointed at
  still on a branch, tag or remote. The branch is kept.
- `tick` runs `clean` when the volume is at or above `cleanAtPercent` (95), at most once an hour. `tick --record` writes
  the last result to `<stateDir>/last-tick.json`.
- `trimreaper schedule install|remove|status`: a launchd agent that runs `tick` every 30 minutes (macOS).
- MCP: a `clean` tool, a dry run unless `dryRun` is false.
- The clean pass is a second vendorable section (`clean.py`); `core-digest` prints its SHA-256 too. The engine is
  unchanged.

## 0.1.0 (2026-10-15)

First public release: UltraTerm's reaper engine as a standalone package.

- The engine (`src/trimreaper/core.py`) plus a standalone host: a JSON config file with every value checked, a private
  SQLite job store and the `trimreaper` command line. `trimreaper core-digest` prints the engine's SHA-256 for embedding.
- `trimreaper mcp`: the job, audit, reap and mark commands as MCP tools over stdio; `reap` is a dry run unless asked.
- Removes build output of finished jobs, expired clones (archiving unsaved work first), idle agent scratch, expired
  evidence files and expired archives, each through the same gates and with durable receipts.
- Changes from the engine in UltraTerm 3.3.11: folder layout comes from the host (`reap_clone_bases` and friends); a
  configured reap home is refused as a root, like the real home and system folders already were; a clone base that is
  itself a symlink is never followed; comments describe rules instead of their history.
