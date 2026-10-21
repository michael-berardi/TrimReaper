# Safety model

Job, scratch, archive and release sweeps remove folders through `reap_item` in `src/trimreaper/core.py`.
Each folder passes all of these gates, in this order, or it is kept and the first failing gate is recorded as the reason.
Other removal paths, including `clean`, have their checks listed below.

Terms used below: the **reap home** is the home folder TrimReaper works below (`home` in the config, your home by
default). A **job** is a unit of agent work recorded with `trimreaper job add`. A job **settles** for 15 minutes after it
finishes, in case it is resumed. A **build queue** is any tool that takes a per-project lock file before it builds
(`buildQueueStateDir`); TrimReaper takes the same lock, so it never removes output a build is writing.

1. **One owner.** Only one removing process holds the cleanup journal lock
   (`<reap home>/.local/state/trimreaper/cleanup/disk-cleanup.lock`). A previous run that stopped part-way blocks the next
   until someone acknowledges it (below).
2. **Retention.** Only the `disposable` class is ever removed. It comes from a `.ut-retention-disposable` marker on the
   folder, or is derived in a few named cases: build output in a finished job's clone, a tool cache (valid
   `CACHEDIR.TAG`), an expired clone or scratch folder. A separate `markedEvidenceExpiryDays` (5) rule also derives disposable for marked job-cache children strictly after terminal completion plus that window, only with a surviving regular, owned, non-empty `report.md`. Reports, summary/outcome, handoffs, receipts, customer, release/runtime/rollback and protected-archive material stay. A name is a class: a record in any position, a camelCase name, a retained class (customer, release, runtime, rollback, credential, secret, signing, entitlement, keychain, provisioning, certificate, token, key, cloud or OAuth material) and a key, certificate or profile extension are all retained, and a folder holding such a name anywhere below it is kept whole, because a job cache subdirectory is removed as a unit. An evidence marker on the job root may expire its bulky children but the root and marker stay. Other preserving markers still win.
3. **Boundary.** The path is canonical, inside a configured root, named for the job that owns it, and below no protected
   name. The root is never your home, a system folder, or anything that encloses one.
4. **Fence.** Device and inode of the root and of every folder down to the item are recorded. The removal re-opens that
   chain with `O_NOFOLLOW` and stops at any difference, so a folder swapped for a symlink is never followed.
5. **Authority.** The job store is asked fresh: every job named by the folder is finished, and no unfinished job declares
   the folder or waits on its owner. Asked again before the removal and after any archive. Independent scratch tasks and
   release-evidence versions are checked at their own path, not their shared collection root. Exact, descendant and ancestor
   claims (cwd, worktrees and machine-recorded paths), including parked jobs, preserve them; a sibling's claim does not.
6. **Producer locks.** A build queue's per-project locks and Cargo's build locks are taken and held through the removal.
7. **Protected scan.** A names-only walk refuses on `.env*`, `.dev.vars*`, `.wrangler`, `.vercel`, a nested repository,
   a bare git folder, a second retention marker or another volume. Protected entries are never stat'ed, opened or entered.
   Immutable, no-unlink and restricted filesystem flags also refuse before removal; they are never cleared. Two protected-name exceptions, both for a whole expired clone: env templates git tracks (`.env.example` ...), which the remote also
   holds, go with it; and under `cloneProtectedCopies: "remove"` the protected copies are read once into an owner-only
   archive (`protected.tar.gz`), then go with it.
8. **Git proofs.** For build output: git ignores it (or it is an unignored `.astro` that holds Astro's own cache files) and tracks nothing in it. Uncommitted or
   stashed tracked work, and an unpushed HEAD, are the clone's business, not its output's: they do not preserve build output that
   rebuilds from the source that stays, and the folder is still proved free of tracked files, matched case-insensitively against the index (a tracked file inside is refused even
   when `--assume-unchanged` or `--skip-worktree` hides it from `git status`). The clean, stash-free and pushed proofs still gate
   `dist` and every whole-clone removal.
   For a whole clone: every branch, reflog entry and unreachable commit is on a remote, there are no tags or other refs,
   no linked worktree depends on it, and every ignored file is known build output. Git runs with hooks, fsmonitor,
   includes, filters and global config disabled. Repository config that could run a program is refused before git reads it.
9. **Size.** Bounded metadata walk; an incomplete size refuses (and the next pass gets twice the budget).
10. **Plan receipt.** A complete JSON line is appended and flushed to disk (`F_FULLFSYNC` on macOS) before anything goes.
    If it cannot be written, nothing is removed.
11. **No process.** `lsof` and `ps` are probed fresh: an open file, a working directory or a command line inside the
    folder keeps it.
12. **Archive** (expired folders with unsaved work): bundle, files and manifest are written, read back and verified, then
    the folder is checked again for any change since.
13. **Last look.** Identity, protected scan, retention and authority are checked once more.
14. **Removal.** Descriptor-relative, never following a symlink, never crossing a volume, stopping at any protected name or
    `.git` that appeared since the scan.
15. **After.** The identities of protected files next to the item are re-taken. Any difference stops the whole sweep.
    The result receipt is written while the locks are still held.

## The metadata-only audit

`trimreaper status --items` removes nothing and takes no lock it would have to create. When a sizing pass runs out of budget,
the jobs it did not reach are shown from an earlier pass, marked `cached`. That view is a hint, never an authority: every path
in it is proved again, metadata only, before it can count as reapable. A path that is gone counts for nothing; a path that
cannot be read, that was replaced or that is a link now is kept, is never reported as reapable and is never sized again. The
revalidation reads no content, enters no folder and spends no sizing budget. No removal gate changes: the audit never
authorizes a removal, and every removal is proved again, freshly, by the reaper itself.

## Agent scratch: the idle proof

An agent-scratch folder (`scratchRoot/<task>`) is disposable only when its own idle proof says so: nothing in it changed
within `evidenceExpiryDays`. A marker decides as it does everywhere else: a folder marked `disposable` is disposable
whatever the proof says (every other gate - claims, protected leaves, git, locks - still runs), and a folder marked
anything else is kept.

The proof covers the whole tree. The walk is descriptor-relative end to end: the folder is opened with no symlink in any
component, every child directory is opened with `O_NOFOLLOW` and compared with the `lstat` that chose it, so a directory
swapped for a link is never entered, and a link is never followed. It reads every entry's mtime, so a change anywhere in
the tree is seen, however deep. A protected name (`.env*`, `.dev.vars*`, `.wrangler`, `.vercel`) is named but never
stat'ed, opened or entered, and it keeps the folder.

The walk is bounded per folder, and the bound is on the record. `agentScratchIdleMaxEntries` (200000) limits how many entries
one folder's walk may list, `agentScratchIdleMaxBytes` (0: no byte bound) how many bytes it may see, and
`agentScratchIdleMaxSeconds` (10) how long one walk may take; the walk also stops at the reaper's own depth cap (128
levels). A walk that runs out of its budget is not idle: the folder is kept, and the receipt records the reason, the
entries and bytes the walk spent, the seconds it took and the budget it had. A budget stop can hide a protected name or
a recent change further on, so raising the budget can turn `idle-budget-exceeded` into `idle-protected-leaf`. A budget
that cannot be read fails closed: it becomes the strictest value there is, never a more permissive one. The walk's own
failures keep a folder that would otherwise be idle: an entry it cannot read, a root that is not a folder, a root that
was swapped under the walk, a tree deeper than the walk may go, and expiry turned off. The proof is taken again before
the removal, and a folder that changed in between is kept.

Every folder the reaper keeps says why, except the ones another gate keeps (a marker, a claim, a protected name, a git
proof): an unmarked folder kept by its idle proof names it (`idle-recent-change`, `idle-budget-exceeded`,
`idle-budget-time-exceeded`, `idle-budget-bytes-exceeded`, `idle-unreadable`, `idle-protected-leaf`, `idle-too-deep`,
`idle-expiry-off`). A folder that aged out between the listing and the removal is refused as `retention-changed` and
goes on the next pass.

A folder marked `disposable` should be marked when the work is done, not when the folder is created: the marker makes
the folder disposable whatever the proof says.

Every folder the reaper keeps says why: `idle-recent-change`, `idle-budget-exceeded`, `idle-budget-time-exceeded`,
`idle-budget-bytes-exceeded`, `idle-unreadable`, `idle-protected-leaf`, `idle-too-deep` or `idle-expiry-off`.

## Narrow exact-output route

`job-derived-output` still uses `reap_item`, receipts, descriptor removal and all final checks. Unlike a source-root
sweep it can reclaim only a named `target` or `node_modules` whose producer registry matches the source/output
identities. The owner must be settled and terminal; an interrupted owner needs a verified interruption clock. Every
live exact/descendant output claim is checked, including machine-recorded path claims. A continuation's source-root
claim alone is not an output claim. Producer and Cargo locks cover all proofs and removal.

Git must prove in-repository output ignored and untracked; source need not be clean or pushed because no source is
removed. Detached cache output instead requires safe source Git metadata and producer association. A source-only cwd
may remain, but output handles, source non-cwd handles, unknown builders, protected names and preserving retention
markers still refuse. A release-bearing target without an explicit disposable output marker stays. Legacy or unmanaged
outputs without producer proof stay. See [EMBEDDING.md](EMBEDDING.md#exact-terminal-job-outputs) for the cooperative
producer contract; this is not an OS sandbox against a writer that deliberately bypasses the queue.

## Removals outside `reap_item`

These also remove files, each with its own checks:

- **`trimreaper clean`** holds the same single-owner lock and writes a plan receipt before and a result receipt after
  every action.
  - A tool cache is cleaned only by its own tool, run from the home folder, and only when no process of that tool runs
    (checked again right before the command). TrimReaper does not delete inside it. Docker is touched only on a local
    socket; Homebrew runs with `HOMEBREW_NO_AUTOREMOVE`.
  - A Cargo registry or Xcode DerivedData folder goes through the same fence, protected scan (no `.env*`, `.wrangler`,
    `.vercel`, nested repository or retention marker other than `disposable`), fresh `lsof`/`ps` probe and
    descriptor-relative removal as a job folder, with the identities of protected names beside it re-taken afterwards. A
    retention marker on the cache root or anywhere inside the folder keeps it.
  - An agent worktree passes, in order: unlocked; not named for a job (`*-jNNNN*` folders are the job reaper's); its
    parent may be a removal root; no retention marker but `disposable`; `worktreeIdleDays` without a change to the
    folder, its top-level entries, its HEAD, index or HEAD reflog; no unfinished job declares it or a folder in or
    around it; the protected scan; it is still exactly the linked worktree its repository records (a `.git` file naming
    `<repo>/.git/worktrees/<n>`, whose `gitdir` names the folder back, and git agrees), so a full repository standing at
    an old worktree path is never removed; no lock file now; the engine's closed clean check (no index flag that hides
    an edit, no tracked change, no untracked file); every ignored file known build output; every commit in its HEAD and
    a readable HEAD reflog contained in a branch, tag or remote-tracking ref; and, last, no process uses it. The removal
    re-runs all of them with fresh probes, then removes the folder through the fence and makes its repository forget
    exactly that worktree (never `git worktree prune`). The branch is never deleted.
  - A worktree whose folder is gone is forgotten only when the folder is still gone, its entry is 30 days old and
    unlocked, the folder's parent exists (an empty mount point passes this, as in the engine's archive rule) and the
    commit check passes.
  - Repository dependencies and Rust debug/incremental output must be ignored and untracked, in a repository whose Git state is clean (including hidden index changes) and whose HEAD is already held by a remote. An enclosing retention marker other than `disposable`, or a running job that owns the project, keeps the output. Collection-root working directories such as the home or code folder do not name a project. Actual removal holds the canonical build-queue project lock, creating it if needed, plus existing Cargo locks for Rust, through the fresh Git, retention, process and boundary checks and result receipt. Release artifacts stay.
  - Older Playwright builds may be removed when a newer build of that same browser exists and no process uses the old one. Models, Docker's disk image and local snapshots are only measured.

- **Evidence expiry** (`reap --job ... --expire-evidence`, and `tick`) unlinks old screenshots, recordings, traces and
  logs one file at a time. It holds the same owner lock and job authority, writes plan and result receipts, records each
  file's name and SHA-256 in `evidence-expired.jsonl` first, rechecks ancestor retention markers and protected names before unlink, and skips any folder holding a marker, `.git`, a protected
  name or a tool cache, except report-backed evidence markers past the separate five-day window. Bulky marked folders use `reap_item`, not the file-unlink route: shared build-queue and discovered Cargo locks, all Git/archive proofs, protected scans and fresh claims/process checks still apply. Retained record names stop a whole-folder removal, including if they appear during removal. Marked expiry never uses the protected-template exception. Automatic scans retry marker-preserved folders hourly.
- **Its own leftovers**, without receipts: a `.partial-*` staging folder of an archive that crashed more than a day ago;
  older archives of the same clone once a newer one verified (never one that is already the only copy); and a removed
  linked worktree's entry in its main repository (`.git/worktrees/<name>`, only when it points at the folder just
  removed and is not locked).

## What it cannot detect

- A protected file's identity is its own metadata (type, device, inode, mode, links, size, times). TrimReaper never reads
  inside a protected folder next to the item it removes, so a change deep inside one is not seen; nothing inside it is
  removed either (the two whole-clone exceptions above aside).
- Git config is checked before and after each git call. A config rewritten during a call is detected afterwards, and that
  answer is thrown away, but git may already have read it.
- In-use detection is a snapshot. A process that starts working in a folder after the final probe and before the
  removal finishes is not seen. The window is short: the removal follows the last probe directly.
- Linux is not tested. The code uses `O_NOFOLLOW_ANY` and `F_FULLFSYNC` where available and falls back to `O_NOFOLLOW`
  and `fsync`.

## Acknowledging a stopped run

`trimreaper owner status` shows the last run's `phase` and its token, `expectedRun`. Phases `none`, `finished` and
`acknowledged` need nothing. Any other phase blocks every later run until it is acknowledged: `started` (a run that
never ended), `interrupted`, `stopped`, or a damaged journal (`fragment`, `invalid`, `compaction-incomplete`):

1. Read that run's receipts in `stateDir/reaper/`.
2. Write a report file: a regular file you own, mode `0600`, one link, at most 64 KiB, at an absolute path with no `..`,
   no symlink and no protected name in it, containing these lines:

```
Cleanup owner run: <expectedRun>
Receipts reviewed: yes
Unresolved paths preserved: yes
Deleted paths: <list or none>
Retained paths: <list or none>
Receipt references: <receipt file and lines>
Active consumers: <processes still using any path, or none>
Next safe step: <what happens next>
```

3. Run `trimreaper owner acknowledge --run <expectedRun> --evidence <absolute path of the report>`.

The journal grows by a few lines per run. Past 4 MiB it is archived and shrunk automatically; `trimreaper owner compact
--run <expectedRun>` does it by hand for a settled journal.
