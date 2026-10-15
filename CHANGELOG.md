# Changelog

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
