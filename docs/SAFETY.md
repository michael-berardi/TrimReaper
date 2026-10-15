# Safety model

Every folder of yours that TrimReaper removes goes through one function, `reap_item` in `src/trimreaper/core.py`.
Every sweep (a job, scratch, archives, release evidence, releases) hands it one folder at a time. Each folder passes all
of these gates, in this order, or it is kept and the first failing gate is recorded as the reason.

Terms used below: the **reap home** is the home folder TrimReaper works below (`home` in the config, your home by
default). A **job** is a unit of agent work recorded with `trimreaper job add`. A job **settles** for 15 minutes after it
finishes, in case it is resumed. A **build queue** is any tool that takes a per-project lock file before it builds
(`buildQueueStateDir`); TrimReaper takes the same lock, so it never removes output a build is writing.

1. **One owner.** Only one removing process holds the cleanup journal lock
   (`<reap home>/.local/state/trimreaper/cleanup/disk-cleanup.lock`). A previous run that stopped part-way blocks the next
   until someone acknowledges it (below).
2. **Retention.** Only the `disposable` class is ever removed. It comes from a `.ut-retention-disposable` marker on the
   folder, or is derived in a few named cases: build output in a finished job's clone, a tool cache (valid
   `CACHEDIR.TAG`), an expired clone or scratch folder. Any other marker keeps the folder.
3. **Boundary.** The path is canonical, inside a configured root, named for the job that owns it, and below no protected
   name. The root is never your home, a system folder, or anything that encloses one.
4. **Fence.** Device and inode of the root and of every folder down to the item are recorded. The removal re-opens that
   chain with `O_NOFOLLOW` and stops at any difference, so a folder swapped for a symlink is never followed.
5. **Authority.** The job store is asked fresh: every job named by the folder is finished, and no unfinished job declares
   the folder or waits on its owner. Asked again before the removal and after any archive.
6. **Producer locks.** A build queue's per-project locks and Cargo's build locks are taken and held through the removal.
7. **Protected scan.** A names-only walk refuses on `.env*`, `.dev.vars*`, `.wrangler`, `.vercel`, a nested repository,
   a bare git folder, a second retention marker or another volume. Protected entries are never stat'ed, opened or entered.
8. **Git proofs.** For build output: git ignores it, tracks nothing in it, and the clone is clean, stash-free and pushed.
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

## Removals outside `reap_item`

These also remove files, each with its own checks:

- **Evidence expiry** (`reap --job ... --expire-evidence`, and `tick`) unlinks old screenshots, recordings, traces and
  logs one file at a time. It holds the same owner lock and job authority, writes plan and result receipts, records each
  file's name and SHA-256 in `evidence-expired.jsonl` first, and skips any folder holding a marker, `.git`, a protected
  name or a tool cache.
- **Its own leftovers**, without receipts: a `.partial-*` staging folder of an archive that crashed more than a day ago;
  older archives of the same clone once a newer one verified (never one that is already the only copy); and a removed
  linked worktree's entry in its main repository (`.git/worktrees/<name>`, only when it points at the folder just
  removed and is not locked).

## What it cannot detect

- A protected file's identity is its own metadata (type, device, inode, mode, links, size, times). TrimReaper never reads
  inside a protected folder, so a change deep inside one is not seen. Nothing inside it is ever removed either.
- Git config is checked before and after each git call. A config rewritten during a call is detected afterwards, and that
  answer is thrown away, but git may already have read it.
- In-use detection is a snapshot. A process that starts working in a folder after the final probe and before the
  removal finishes is not seen. The window is short: the removal follows the last probe directly.
- Linux is not tested. The code uses `O_NOFOLLOW_ANY` and `F_FULLFSYNC` where available and falls back to `O_NOFOLLOW`
  and `fsync`.

## Acknowledging a stopped run

`trimreaper owner status` shows the last run's `phase` and its token, `expectedRun`. Phases `none`, `finished` and
`acknowledged` need nothing. Any other phase (`started` with no end, `interrupted`, `stopped`, or a damaged journal)
blocks every later run until it is acknowledged:

1. Read that run's receipts in `stateDir/reaper/`.
2. Write a report file: a regular file you own, mode `0600`, one link, at most 64 KiB, containing these lines:

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
