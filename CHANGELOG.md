# Changelog

## 0.8.0 (2026-10-24)

Fixes the causes of a 60 GB pileup of abandoned agent worktrees: on one machine the clean pass kept 184 abandoned
worktrees that had seen no work for up to six weeks.

- The worktree idle clock no longer reads the git index or the reflog file's mtime. Every `git status` (an IDE, an
  agent, an audit, the reaper's own gates) rewrites the index, and `git gc` rewrites every reflog, so abandoned worktrees
  read as `recent:0.0d` forever. Idleness now comes from the HEAD file, the timestamp in the HEAD reflog's last complete,
  well-formed entry, and a walk of the working tree using the later of mtime and ctime (a copy that preserves mtimes still
  counts as a change). Build output, caches and protected names are skipped, links are never followed and the walk stays on
  the folder's device. It is bounded (50,000 entries, 5 seconds); past the budget, on an unreadable entry or another device
  the folder is kept with a named reason (`idle-budget-exceeded`, `idle-budget-time-exceeded`, `idle-unreadable`,
  `idle-mountpoint`) instead of reading as just changed. One pass walks each folder once.
- A job-named worktree is left to the job reaper only when the job reaper can find it: a top-level folder of a clone
  base. Job-named worktrees nested in a repository (`<repo>/.worktrees/J-0042`), in scratch or in `/tmp` were deferred
  to a pass that never looks there and were kept for good; they are now judged as agent worktrees with every gate.
- A worktree whose own folder name gives only jobs that all finished `cloneExpiryDays` ago waits 6 quiet hours instead of
  `worktreeIdleDays`, provided the job reaper's own blocker clears every such job (settled, worker terminal gone, no live job
  sharing its terminal, nobody awaiting it) and no continuation or accepted successor of it is still live. Job names in
  parent folders never shorten a wait. Every other gate (clean, pushed, no protected name, no process, no unfinished claim)
  still holds.
- Job names in the uppercase dashed form `J-0042` / `app-J-0042` are recognised alongside `app-j0042`, `j0042-app` and
  `.j0042`. This applies to the job reaper too: a top-level clone-base folder named `*-J-NNNN` now belongs to that job,
  and once the job finished `cloneExpiryDays` ago it goes like any job clone (unsaved work is archived and verified
  first; `cloneArchive` decides). The lowercase `j-NNNN` form is not a job name.
- A scope release's `keep` list lapses once the releasing job, the source and every job its kept paths name, with their
  whole continuation and successor chains, finished `cloneExpiryDays` ago. A keep list whose owner is not a job, an unknown
  or live job and an unreadable record keep it in force. Before, one consolidation's keep list protected four cancelled
  jobs' clones indefinitely.

## 0.7.3 (2026-10-24)

- Git probes bind the exact `.git` of the repository being gated. A broken nested `.git` now fails the probe instead of
  letting Git discovery answer for an enclosing repository, so a clone inside another repository is never judged by its
  parent's state. Git metadata owned by another uid is refused before a probe runs, including a `.git` pointer file,
  its resolved git directory and common metadata. Linked worktree and submodule probes bind the resolved, gated directory;
  relative checkout paths are made absolute before Git changes directory.

## 0.7.2 (2026-10-22)

- Preserve three safeguards already shipped by the embedding host; this restores the existing behavior, not a new
  retention policy. A disposable marker on a job-cache child never overrides a retained credential, key or record name
  anywhere inside it; the whole folder stays as ambiguous.
- A job-root evidence marker does not shorten the ordinary cutoff for loose files. Unmarked files wait for
  `evidenceExpiryDays` after the job finished, regardless of their own ages, while marked child folders can expire under
  the separate `markedEvidenceExpiryDays` rule.
- Keep 0.7.1's positive product-identity release safeguards and the protected-copy archive rules unchanged.

## 0.7.1 (2026-10-22)

- Keep release targets that mix an older product bundle with a foreign installer such as `UltraTerm-Helper_1.0.0.pkg`.
  A separator followed by another word, a numeric or punctuation-led extension, or a foreign prefix word no longer
  proves the installer belongs to the installed product. Numeric release names such as `UltraTerm-3.8.4.pkg`,
  case-insensitive names and known packaging suffixes remain supported. Ambiguous formats stay in the inventory.
- Installed, rollback and newer releases, unknown versions, foreign bundles, locks and live claims keep their existing
  safeguards.

## 0.7.0 (2026-10-22)

- Add the opt-in `agentScratchProtectedCopies: "archive"` rule. An expired scratch task holding protected names can
  be reclaimed only after its protected material has a verified, owner-only archive. Scratch protected-copy archives
  never expire automatically; an archive failure or a change after the archive keeps the task.
- The exception applies only to top-level agent-scratch tasks, never job caches. Protected descendants share the
  existing idle-walk budget, and recent changes, live claims, preserving markers and filesystem protections still
  keep the task. The default remains `"keep"`; other folder classes keep their existing rules.

## 0.6.13 (2026-10-22)

- Agent-scratch idle proofs read `max(st_mtime, st_ctime)` for the root and every entry. A fresh copy with preserved
  modification times or a metadata-only change restarts the quiet window; `idleProof.newest` records that change time.
- Refuse mount roots and nested other-device entries as `idle-mountpoint`, without entering or counting them.
- Check idle deadlines while enumerating directory names and before and after walking, including empty folders. A
  caller-supplied absolute idle deadline is honored at every re-proof, without restarting its clock.
- Preserve 0.6.12's item-carried listing proofs, rotating discovery cursor and independent per-folder sweep budgets.
  The older unpublished pass-wide sweep deadline is superseded by 0.6.12's starvation fix, not reintroduced here.
- Tests synthesize old ctimes only for unchanged fixture identities; real-copy fixtures retain OS-assigned ctimes.

## 0.6.12 (2026-10-22)

- Keep the scratch listing's proof on the item it proved, so the pass that follows does not walk every retained folder a
  second time. A folder the listing could not reach in time stays, unproven, with a named budget reason and a zero-time
  proof instead of a silent refusal, and the listing records where it stopped so the next pass begins after that folder
  rather than at the same name every hour.
- The listing's own budget bounds the discovery only. The sweep it hands its items to visits every listed folder with the
  per-folder budget, so a slow or refused folder can never starve the folders behind it, and the last whole-tree proof a
  removal requires gets the whole per-folder budget instead of what the first proof left of it. A fresh whole-tree proof
  is still required immediately before removal, and a listed item's retention is re-derived from the directory itself, so
  a stale or forged listing proof never authorizes a deletion.
- Make the listing deadline regression deterministic instead of relying on a short wall-clock interval.

## 0.6.11 (2026-10-22)

- Fix retention of superseded release targets: only known older artifacts of the installed product may be reclaimed.
  Installed, rollback and newer versions stay; unknown or ambiguous identity/version and incomplete inventories fail
  closed. Versions must be complete three-part versions, and bundle-name/plist disagreement keeps the artifact.
  This corrects the existing reaper rule, not a new retention class; locks, live claims and Git safeguards are unchanged.

## 0.6.10 (2026-10-21)

- The listing of the agent-scratch root keeps the budget of the caller that asked for it. The idle proof of 0.6.9 reads
  every entry's mtime of a whole tree, so a listing that walked every folder with the full per-folder budget could spend
  ten seconds per folder and overrun the budget of the pass that asked for the listing (the hourly sweep and a host's
  read-only sizing pass, which has seconds, not minutes). The listing now takes a deadline: every folder's walk gets what
  is left of it, so one listing never spends more than its caller's budget on the whole root, and a folder the listing
  could not prove is kept, unproven, and is proved again on the next pass. A folder that is marked still says so: the
  marker decides, exactly as before. Nothing else changes: the rule, the proof, the budgets, the reasons and every other
  gate are the ones of 0.6.9.

## 0.6.9 (2026-10-21)

- The idle proof of an agent-scratch folder scales with the tree instead of giving up on its size. The walk that decides whether a scratch folder changed inside the evidence window gave up when the tree held more than 20000 entries, so a big scratch tree could never age out: the walk's own budget decided the folder's fate, not its age, and the folder was refused as an unmarked one, which said nothing about why. The proof now covers the whole tree: the walk is descriptor-relative (a link is never followed, a directory swapped for one is never entered) and it reads every entry's mtime, so a change anywhere in the tree is seen, however deep - as far as an mtime shows it: a tree that arrives with its old times preserved (moved in with `mv`, copied with `cp -a` or `rsync -a`, unpacked from an archive that keeps them) looks idle, and marking it (`trimreaper mark <folder> evidence`) keeps it. The walk is bounded per folder (`agentScratchIdleMaxEntries`, 200000; `agentScratchIdleMaxBytes`, 0: no byte bound, because bytes bound no work; `agentScratchIdleMaxSeconds`, 10; and the reaper's own depth cap of 128 levels), and the bound is on the record: a walk that runs out of budget is not idle, the folder is kept, and the receipt records the reason, the entries and bytes the walk spent, the seconds it took and the budget it had. A budget that cannot be read fails closed. The walk's own failures keep a folder that would otherwise be idle: an entry it cannot read, a root that is not a folder, a root that was swapped under the walk, a tree deeper than the walk may go, a protected name it must not enter, and expiry turned off. Every unmarked folder the reaper keeps now says why (`idle-recent-change`, `idle-budget-exceeded`, `idle-budget-time-exceeded`, `idle-budget-bytes-exceeded`, `idle-unreadable`, `idle-protected-leaf`, `idle-too-deep`, `idle-expiry-off`) instead of naming an unmarked folder and nothing else; a folder that aged out between the listing and the removal is refused as `retention-changed` and goes on the next pass. A marker decides as it does everywhere else: a folder marked `disposable` is disposable whatever the proof says, and every other gate still runs. Every other proof - retention, protected leaves, git, claims, producer locks - is unchanged, and a folder that changed between the listing and the removal is kept. The result and plan receipts of a folder that has an idle proof carry it in a new `idleProof` field (the reason, the counts, the newest mtime, the seconds spent and the budget); the receipts of every other item are unchanged.

## 0.6.8 (2026-10-21)

- An interrupted job is finished work when its host continued the work. A job whose worker terminal vanished without a report was never reclaimable, so the scratch and the clone of every such job were held for good while the host carried the work on in a new job. With the host's own continuation record (`interrupt-heir:<job>`, the id of the continuation it queued) and a verified interruption clock (the job's own record, or the matching `job.interrupted` event), the ordinary windows apply again: the evidence window releases the job's scratch, the clone window releases its checkout, and the windows run from the last finish of the whole continuation chain. The chain must have finished, whatever each job's status is: a continuation that is queued, waiting for approval or working may still need the source's folders, so the source keeps everything until that work is done, and a live job that claims a folder keeps it exactly as before. The clock has to belong to the job's own lifetime, the continuation has to have been queued after the job started, and a missing job, a cycle, a chain deeper than the cap, an unreadable record or an unprovable clock keeps everything. Every other proof - retention, protected leaves, git, claims, producer locks - is unchanged, and a job without that record keeps everything, exactly as before.

## 0.6.7 (2026-10-21)

- The audit's cached view is a hint, never an authority. When a sizing pass runs out of budget, the jobs it did not reach are shown from an earlier pass for up to a day, and every path in that view is now proved again, metadata only, before it can count. A path that is gone counts for nothing at all; a path that cannot be read, that was replaced or that is a link now is kept, is never reported as reapable and is never sized again. Older cached entries without device, inode or mtime proof are also kept with unknown size until a fresh audit refreshes them. Nothing else moves: the fresh audit, every removal gate and every authority stay exactly as they were.

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
