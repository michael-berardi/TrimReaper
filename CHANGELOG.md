# Changelog

## 0.6.7 (2026-10-21)

- The audit's cached view is a hint, never an authority. When a sizing pass runs out of budget, the jobs it did not reach are shown from an earlier pass for up to a day, and every path in that view is now proved again, metadata only, before it can count. A path that is gone counts for nothing at all; a path that cannot be read, that was replaced or that is a link now is kept, is never reported as reapable and is never sized again. Nothing else moves: the fresh audit, every removal gate and every authority stay exactly as they were.

## 0.6.6 (2026-10-20)

- The names that keep a job's material are a class, not a spelling. A record (`report`, `summary`, `outcome`, `handoff`, `receipts`) in any position and any number, a camelCase name (`AppleCerts`, `SubscriptionKey_X.p8`), a retained class (customer, release, runtime, rollback, credential, secret, signing, entitlement, keychain, provisioning, certificate, token, key, ssh, gpg, cloud and OAuth material) and a key, certificate or profile extension (`.p8`, `.pem`, `.pfx`, `.cer`, `.crt`, `.key`, `.csr`, `.jks`, `.keystore`, `.p7b`, `.p7c`, `.srl`, `.mobileprovision`, `.ovpn`, `.ppk`, `.app`, `.dmg`, `.pkg`, `.ipa`, `.zip`, `.tgz`, `.xcarchive`, `.sig`, `.asc`) are all retained. A folder is kept whole when such a name appears anywhere below it, because a job cache subdirectory is removed as a unit and its top-level name alone cannot prove it holds no key material. Over-retention is the safe answer: a name that cannot be judged keeps the folder.
- A job's `report.md` is its durable record only when it holds something. An empty placeholder no longer counts as a surviving record for the marked-evidence window.
- The `reap_tick` fallback for a policy without an `autoReap` key now matches every other automatic path: reaping stays off unless the policy asks for it. Hosts that set `autoReap` are unaffected.

## 0.6.5 (2026-10-20)

- A cancelled job's `evidence` marker on its own cache no longer keeps that cache forever. The acceptance or verification the marker was written for can never complete once the job is cancelled, so the cache keeps the normal `evidenceExpiryDays` window instead. Done, failed, parked and running jobs keep their markers exactly as written, and every other proof (protected leaves, retained classes, live producers) still runs.

## 0.6.4 (2026-10-20)

- `reapOpenTerminalGraceHours` (default `0`, off) implements the rule hosts already documented: once a finished job finished that long ago and its worker terminal has been idle as long, that still-open terminal stops holding the job's folders. The terminal is never closed or touched, a resumed terminal (or unknown idleness) keeps the old wait, and every other proof (a live process using the folder, a retention marker, git state, a terminal shared with live work) still runs. The grace is one snapshot: ids and idle seconds come from the same read.
- Interrupted jobs keep their separately guarded, exact-output route; the new grace applies to their output exactly as before, never to their clone or cache.

## 0.6.3 (2026-10-19)

- The untracked-`.astro` proof now needs Astro's own cache files inside the folder (`data-store.json`, `content-assets.mjs`, `content-modules.mjs` or `types.d.ts`) rather than only its name: a clone that is no longer preserved by its own dirt would otherwise hand over untracked work kept under a `.astro` name.
- The tracked-file proof matches the index case-insensitively and literally. On a case-insensitive filesystem the index entry `NODE_MODULES/x` is the file inside `node_modules/`, and a case-sensitive pathspec called that folder untracked.

## 0.6.2 (2026-10-19)

- A finished job's regenerable build output (`node_modules`, `.next`, `.open-next`, `.turbo`, `.astro`, `target`) no longer waits for a clean, stash-free, pushed clone: its own git-ignored and untracked proofs still stand, and a tracked, nested, protected or release-bearing folder still preserves it. `dist` and whole-clone removal keep the full proofs.

## 0.6.1 (2026-10-19)

- Sustained disk pressure retries cleanup after the existing ten-minute cooldown until the measured low watermark is met, without requiring another 5 GiB of growth or less than 10 GiB free.
- Recognize untracked `.astro` metadata as regenerable output even without an ignore rule; tracked files, dirty source, protected names and all other Git proofs still preserve it.
- Refuse immutable, no-unlink and restricted filesystem flags before removal instead of repeatedly attempting system-protected temp folders. Never clear those flags.
- Recheck evidence-file ancestor markers and protected names immediately before unlink; a newly preserving marker stops expiry.

## 0.6.0 (2026-10-19)

- Marked bulky job-cache evidence expires strictly more than five days after a job finishes, with a separate `markedEvidenceExpiryDays` policy. The job must retain a private `report.md`. Reports, summary/outcome, handoffs, receipts and the job root stay; customer, release/runtime/rollback and protected-archive material do not expire.
- Marked folders use the existing guarded reaper, producer/Cargo locks and per-item receipts. Protected leaves and unfinished claims, including ancestor claims, still preserve the candidate. `reap --expire-evidence` and automatic ticks use the same rule; zero disables it.

## 0.5.3 (2026-10-19)

- A live scratch task or release-evidence version no longer pins every sibling in its shared collection. Exact, descendant and ancestor job claims still preserve the candidate, including parked jobs and machine-recorded path claims.
- Scratch idle audits refuse protected leaves before entering them.

## 0.5.2 (2026-10-18)

- Measure goals from `df -kP` Used and Available columns. On APFS, Python's total-minus-free value can include shared-container allocation and is not the volume's Used value. Failed or malformed probes expose a named fallback with unknown goal progress, never false success.

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
