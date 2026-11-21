# Cleanup reference

[Quick start](../README.md) · [Configuration](CONFIGURATION.md) · [Safety model](SAFETY.md)

## Clean the whole machine

`trimreaper clean` frees space that no job owns. It needs no setup.

```sh
trimreaper clean --dry-run   # what would go, what stays and why, plus a report of models and other large things
trimreaper clean             # do it
trimreaper clean --only npm,worktrees
trimreaper clean --deep      # also the Go module cache, unused Docker images and unavailable simulators
trimreaper clean --high-watermark 75 --low-watermark 60
trimreaper clean --no-escalate   # the routine rules only, never the ladder
trimreaper clean --steady        # the quiet pass: long-idle repository output and old finished-job artifacts, whatever the disk level
trimreaper clean --trim-vm       # also shrink a stopped Colima VM's disk file (the guided steps in the report)
```

| What | How |
|---|---|
| npm, pnpm, yarn (1.x), bun, uv, pip, Go build, Homebrew caches | The tool's own cleaner (`npm cache clean --force`, `uv cache prune`, `brew cleanup --prune=all` ...), run from your home folder and skipped while that tool runs. Homebrew also drops old versions of installed formulae; it never uninstalls one (`HOMEBREW_NO_AUTOREMOVE`) |
| Docker build cache | `docker builder prune -af`, only when Docker is already running on a local socket (never a remote `DOCKER_HOST` or context, never volumes) |
| Cargo's registry, Xcode's DerivedData | Folder by folder through the same guarded removal as job folders, skipped while Cargo, rust-analyzer or Xcode runs |
| Agent worktrees | A linked git worktree no job owns goes after `worktreeIdleDays` (7) without a change (HEAD, the last reflog entry and the working-tree files; never the index, which every `git status` rewrites), or after 6 quiet hours when every job its own name gives (`-jNNNN`, `-J-NNNN`) finished `cloneExpiryDays` ago and nothing still depends on it, when it is unlocked and clean (edits hidden by `--skip-worktree` or `--assume-unchanged` count as changes), git ignores nothing in it but build output, it holds no `.env*` or other protected name, no unfinished job declares it, no process uses it, and every commit its HEAD ever pointed at is on a branch, tag or remote. Its branch stays. A worktree whose folder is already gone is forgotten by its repository after 30 days, under the same commit check |
| Project dependencies and output | `node_modules`, `.next`, `.astro`, `.turbo`, `.svelte-kit`, `.nuxt`, `.parcel-cache`, `.vite`, `.venv` and Python tool caches inside any repository in the code homes, when git ignores the folder, its manifest (`package.json`, `pyproject.toml`, `requirements.txt` ...) sits beside it so one install brings it back, the project has gone `projectIdleHours` (48) without a change (6 hours for a copy named after a finished job), no running job works in it and no process uses anything in it. The routine rules also want the repository clean and pushed; a stash alone does not preserve ignored output. `dist` and `build` are touched only on the ladder (below) |
| Rust build output | `debug` and `incremental` folders of every `target` in a repository (release artifacts stay), skipped while Cargo or rust-analyzer runs. Discovery is breadth-first and never enters tool caches, virtual environments or dependency stores |
| Repository-output safeguards | Dependency and Rust output stays when Git cannot prove the repository clean and pushed, the folder ignored and untracked, a running job owns the project, or an enclosing folder up to the reap home carries a retention marker other than `disposable`. Removal holds the build queue's project lock and rechecks every proof |
| Temp folders | A folder of yours in the shared temp folder after a day without a change, and in your per-user temp folder after three days, when nothing in it changed in that time and no process uses it. Folders the system or running apps manage there (`com.apple.*`, sockets, locks) are never candidates |
| Browser code-sign clones (macOS) | A browser copies its app bundle into your per-user `X` folder at every launch and removes it on quit; a killed browser (automation, a crash) leaves one behind each time. Each abandoned copy goes after an hour when no process has it open |
| Go module cache, unused Docker images, unavailable simulators | Only with `--deep` (`xcrun simctl delete unavailable` also deletes the app data of simulators the selected Xcode cannot run) |
| Superseded Playwright browser builds | Older builds only, when a newer build of the same browser is installed, no installed Playwright still needs the old one, it is at least an hour old and no process uses it |
| Old releases of self-updating tools (`cli-releases`) | Side-by-side releases in `~/.local/share/<tool>/releases/<version>/` (or `$XDG_DATA_HOME`). The newest `cliReleasesKeep` (3) versions stay, by semantic version order, and so does every version that a process uses (working directory, open file, command line), that a link points into (the tool's own `current`, a command in `~/.local/bin` or Homebrew's `bin`, a Homebrew Cellar link), or that a settings file or wrapper names as `<tool>/releases/<version>`. Settings are read in `~/.<name>` and `~/.config/<name>` for the tool's name and each name after one of its hyphens (`acme-cli` also reads `~/.cli`), two levels deep, settings-like files only, never `.env*` and never backups (`*.bak`, `*.orig`, `*.before-*`). A settings file that cannot be read keeps every version. A release changed within the hour stays. Folders that are not versions are never touched |
| Build output in coordinator jobs (`coordinator-outputs`) | Rust `target` and `*-target`, `node_modules`, `dist` and `.cloudflare` inside `coordinatorRoots/J-NNNN/` (default `~/.cache/ut-coordinator`), up to eight levels down. The job's status comes from `coordinatorJobCommand <job>` (default `ut-coordinator job-get`, JSON): `done`, `failed` or `cancelled` once the output is an hour unchanged; `parked` or `blocked` only when the job reported nothing for 24 hours and its project is untouched as long. Any other status, or no answer, keeps it. Never inside `notes`, `reports`, `evidence` or `handoffs`. Each folder must be what its name says: a target Cargo wrote, or a `node_modules`, `dist` or `.cloudflare` beside its manifest (`package.json`, a Wrangler file), and a `dist` without a release artifact. In a git checkout git must ignore it and track nothing in it; a checkout whose git link is broken still allows only these names. Nothing may hold the project |
| Release scratch (`release-scratch`) | In each `rel-*` folder of `releaseScratchRoots` (default `~/.cache/agent-scratch`): the `src-tauri/target` of a release worktree whose HEAD is on `origin/main` (git ignores the target and tracks nothing in it), `unz` folders, and staged `out*` folders except the newest two across all release folders (links are never followed). Each must be an hour unchanged, carry no retention marker, and nothing may use its release folder |
| Temp build caches (`temp-build-caches`) | Node's `node-compile-cache` version folders, `*-publish-check.*` and `oss-leaks-*` folders in the shared and per-user temp folders, once an hour unchanged and unused |
| uv, npm and cargo-xwin caches over a size | `uv cache prune` as before, but `uv cache clean` once the cache is 2 GB or more; npm's cache is left alone under 256 MB; `cargo-xwin`'s downloaded SDK (`~/Library/Caches/cargo-xwin`, `~/.cache/cargo-xwin` or `$XWIN_CACHE_DIR`) goes folder by folder once it is 512 MB or more. Each only while its tool is not running |
| A Colima VM's disk file | Reported with its occupied size and the steps that shrink it (`trimSuggested` from 4 GB). `trimreaper clean --trim-vm` runs them only for a VM that is stopped: `colima start`, then inside the VM `docker image prune -a -f`, `docker volume prune -a -f` (unused volumes and their data), `docker builder prune -a -f` and `sudo fstrim -v /mnt/lima-colima` (the step that returns the freed blocks to the Mac), then `colima stop`, whatever a step returned. A running VM is never touched |
| Local models (Hugging Face, Ollama, LM Studio), Docker's disk image, local snapshots | Reported with their size. Never removed |

Worktrees are found through `git worktree list` in every repository in `cloneBases`, and in the folders where agent tools
keep their own: `~/.codex/worktrees` (or `$CODEX_HOME/worktrees`), `~/.cursor/worktrees`, `~/conductor/workspaces`,
`~/.superset/worktrees`, `~/.claude-squad/worktrees` and Vibe Kanban's temporary folder. Claude Code's
`<repo>/.claude/worktrees` sit inside the repository and are found through it. `worktreeHomes` replaces the list.

Cleanup sizes candidates, tries the largest first and stops when measured occupancy reaches the low watermark.
`byRule` lists, for each rule, how many candidates it found and their size (`candidateBytes`) and what it kept, so
`trimreaper clean --dry-run` shows what each rule would reclaim.
`goalMet` and `remainingBytes` report the actual result; unknown disk measurements produce `null`, not success.
Protected or active data may prevent reaching the goal. Missing or unreadable candidates are skipped with a named reason;
independent candidates continue. A folder the filesystem refuses to release (an ACL that denies delete, immutable flags, another
owner) is kept with that cause and is not tried again for a day unless it changes. A tool's cleaner
that fails is listed with its exit status and last line of output, and the command exits 3.

The result reports what happened honestly. `summary.freedBytes` is the volume's free space after the pass minus before it (it can be
zero or negative while other programs write); `summary.removedBytes` is the size of what the pass actually removed; when the two
disagree `summary.freedNote` says so. `rungs` lists what each step of the ladder found and freed, and `topBlockers` names the ten
reasons that keep the most bytes (`recent`, `dirty`, `protected-leaf`, `job:input-pinned-by` ...), so a pass that frees nothing says why.

### When the routine rules are not enough: the escalation ladder

The routine rules wait for time (seven days for a worktree, 48 hours for a project). If they cannot reach the low watermark, the pass
climbs these rungs one at a time, re-plans with each rung's wider windows and stops at the first rung that reaches the goal. A dry run
plans every rung. Nothing is escalated when the disk cannot be measured.

| Rung | What it widens |
|---|---|
| 0 `routine` | Everything above, with its own windows. |
| any `dedupe-clones` | Not a removal: on APFS (macOS) byte-identical data files in job folders whose job is not running, and in agent scratch, become clones of one copy. Every path keeps its bytes, mode and times and stays writable. Never executables, Mach-O files, `.git`, symlinks, hard links or files changed in the last six hours. Also runs in the steady pass. |
| any `compress-logs` | Not a removal: idle history files under the folders you list in `compressLogsRoots` (none by default) get transparent, lossless APFS compression with `afsctool`, through a verified clone renamed over the original only if the file did not change. Same bytes for every reader; never open files, links, hard links or executables. Also runs in the steady pass. |
| any `tagged-caches` | Folders that declare themselves caches with a valid `CACHEDIR.TAG` (other tools' build caches), wherever agents leave them: the job cache at any depth, agent scratch, worktree homes, code homes and the user temp folder. Cargo targets (`.rustc_info.json`, a tag naming Cargo, or a `.fingerprint` folder) are never taken here: they are left to `rust-build-output`, which removes only `debug` and `incremental` and keeps `release`. A job's own folder, a top-level scratch task, system-named temp entries and any folder holding a record, key, certificate or evidence name stay. Idle `taggedCacheIdleHours` (6), nothing open in it, no build tool running, no release artifact inside (a release scan that cannot finish keeps it), no protected names, not inside a running job. The steady pass searches only the job cache, agent scratch and the user temp folder, not code homes. |
| any `overflow-retention` | The overflow volume is not exempt from retention: regenerable output found there (`node_modules`, `.next`, `.open-next`, `.astro`, `.turbo`, `__pycache__`, Cargo `debug`/`incremental`/`release/{deps,build}`) and the folders of jobs finished more than `archiveExpiryDays` ago go through the guarded removal; a retention marker or protected name keeps a folder, and its link at home goes with it. |
| any `project-dependencies` (job folders) | Dependency and framework output (`node_modules`, `.next`, `.astro`, `.turbo`, `.svelte-kit`) nested at any depth inside a job folder whose job is finished, cancelled, failed, interrupted, parked or blocked, once its repository is idle 6 hours, with the usual git ignore/untracked proof. |
| 1 `idle-repositories` | Regenerable output (`node_modules`, `.next`, `.venv`, Rust `debug`/`incremental`, `dist` and `build` that git ignores and tracks nothing of, with a manifest beside and no release artifact inside) of repositories idle for `cleanEscalateRepoHours` (2), whatever their age, even when the repository is dirty or has a stash: output git ignores is not unsaved work. Temp debris after 6 hours. |
| 2 `worktrees-and-finished-jobs` | Linked worktrees idle for `cleanEscalateWorktreeHours` (24), clean and pushed. Fixtures, candidates, deploy and similar folders of jobs finished `cleanEscalateJobHours` (24) ago, and the clones of those jobs that hold nothing a remote lacks (`cloneExpiryDays` shortened to the same window), through the job reaper's own guarded removal (without a tick's two-jobs limit). |
| 3 `pinned-folders` | Folders another job lists as an input or waits on, once that job finished, failed, was cancelled or was interrupted `cleanEscalatePinnedHours` (24) ago (a verified interruption clock; a parked, blocked, queued or running job keeps its pin). |
| 4 `unmarked-retention` | Every unmarked folder of a job finished 24 hours ago, not only the regenerable names. A retention marker, a secret or a key name still keeps the folder. |
| 5 `archive-then-remove` | Worktrees and job clones with uncommitted files, commits no remote holds, ignored data of unknown kind or a git directory with no `.git` entry: archived first (a git bundle verified in an empty repository, a tar read back against its SHA-256 manifest, stamped as the only copy), compared with the archive again, then removed. The branch stays. A failed or stale archive keeps the folder. |

No rung touches a running or starting job's folder, a folder a process uses, `.env*`, `.dev.vars*`, `.wrangler` or `.vercel`, a release
artifact, a retention marker, another user's files or a primary clone's own source. A window you set to `0` stays off on every rung.
`cleanEscalate: false` (or `--no-escalate`) keeps the routine rules only.

### The steady pass

Below `cleanAtPercent` a quiet pass runs every `cleanSteadyHours` (6; `0` turns it off) so the disk never has to reach the watermark: it
removes regenerable output of repositories idle for `cleanSteadyRepoDays` (3) and the artifacts of jobs finished `cleanSteadyJobHours`
(48) ago. It also runs deduplication, configured log compression and tagged-cache cleanup (including tagged caches in the user temp folder). It does not run tool cleaners or remove worktrees or ordinary temp folders. `trimreaper clean --steady` runs it by hand.

Operational commands print a JSON document on stdout; `--help` and `--version` print text, and `mcp` uses JSON-RPC messages. Exit status:

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
| Per-job scratch folders (`cacheRoot/J-NNNN/*`) | Tool caches (with a `CACHEDIR.TAG`) once the job finishes; anything else `evidenceExpiryDays` (14) after it finished. An interrupted job whose work a durable continuation took over counts as finished, from its verified interruption clock; one nothing continues counts as failed `interruptedExpiryDays` (7) after that clock. A finished job's folder holding only unsaved git work goes after `unsavedArchiveDays` (7), archived first |
| Screenshots, recordings, traces and logs in a job's scratch | `evidenceExpiryDays` after the job finished. Each file's name and SHA-256 are logged first |
| Marked bulky job-cache evidence | Strictly more than `markedEvidenceExpiryDays` (5) after the job finished, only with an existing, non-empty `report.md`. A job-root evidence marker does not shorten the `evidenceExpiryDays` window for loose files. Reports, summary/outcome, handoffs and receipts stay. Retained material, protected names and active claims still prevent removal |
| Agent scratch folders (`scratchRoot/*`) | Once nothing in them changed for `evidenceExpiryDays`. The idle proof covers the root and whole tree (the later of every entry's mtime and ctime, never following a link), so a folder ages out like a small one instead of being kept for good by the walk's own entry limit - up to its per-folder budget (3000000 entries, 60 seconds by default). A folder past that budget is kept as `idle-budget-exceeded` (too many entries) or `idle-budget-time-exceeded` (too slow), and the receipt names the budget and what the walk spent. A fresh copy with preserved modification times still starts a new quiet window, as do metadata changes. A mount keeps the folder without being entered. Caller-supplied absolute idle deadlines also bound later rechecks; `trimreaper mark <folder> evidence` keeps needed evidence. Every unmarked folder the reaper keeps names the reason it was kept (`idle-recent-change`, `idle-budget-exceeded`, `idle-unreadable`, `idle-protected-leaf`, ...). Active or parked job claims preserve that task, not unrelated siblings. Off until you set `scratchRoot`. Protected names keep the task unless `agentScratchProtectedCopies: "archive"` saves a verified private copy first; those archives never expire automatically. A folder quiet for `unsavedArchiveDays` (7), shorter than that window, whose only blocker is unsaved git work (dirty or untracked files, commits no remote holds, a stash, a linked worktree inside it, a bare repository nested in it) is archived first (see "Archive before delete"), then removed in the routine pass; a folder with nothing unsaved still waits for `evidenceExpiryDays`. The listing of a big root is resumable: a cursor and per-folder proofs keyed by the folder's device, inode and ctime (a hint only; the removal walks the folder afresh) make every folder proved within a few passes, and `reap --scratch` / `reap-status` report the cursor and how many folders remain unproved |
| Its own archives | `archiveExpiryDays` (30) after a job archive became the only copy; the clean pass's `clean-worktrees` archives only with `cleanWorktreeArchiveExpiryDays` set (default 0: kept); scratch protected-copy archives stay indefinitely |
| Old release builds and release evidence | Off until configured: [docs/CONFIGURATION.md](CONFIGURATION.md) |

Scratch listings share the caller's time budget and carry their proof into the pass. A folder not checked in time stays;
subsequent passes give other folders a turn. A listing proof never replaces the fresh check before removal.

Release-target retention allows only known older artifacts of the installed product to be reclaimed. The installed
version, rollback and anything newer stay. Unknown or ambiguous product identity or version, an incomplete inventory,
a version without all three numeric parts, or disagreement between a bundle's name and plist fail closed: the affected
artifacts stay. A name such as `UltraTerm-Helper_1.0.0.pkg` is not proof of an UltraTerm release: a separator followed
by another word can name a different product, so a known installer with that name keeps the whole target, even beside
an older valid UltraTerm bundle. Numeric names (`UltraTerm-3.8.4.pkg`), case-insensitive matching and known packaging
suffixes (`UltraTerm.app.tgz`) remain supported. Ambiguous numeric or punctuation-led product extensions and foreign
prefix words also keep the target. This fixes the existing reaper rule, not a new retention class; locks, live claims
and Git safeguards are unchanged.

A host with the producer registry enabled can reclaim an exact `target` or `node_modules` of a settled finished,
failed, cancelled or verifiably interrupted job even when a continuation keeps the source worktree. This requires the
producer's source/output identity record, shared build locks, no live claim on the exact output, no open files and
no running builder. A source working directory alone is not an output handle. Source and retained data stay;
legacy outputs without producer proof stay on the stricter route. See [the embedding contract](EMBEDDING.md).

Under disk pressure (at or above `diskWarnPercent`, 75%), the derived output of parked or interrupted jobs goes too; they
rebuild it when they resume. The pressure check runs during a `tick`. Where it applies (0.20.2):

- the job's named clones (`node_modules`, `.next`, `.open-next`, `.turbo`, `.astro`, `target`; never `dist` there);
- a git worktree or clone inside the job's cache folder (`<cacheRoot>/J-NNNN/<dir>` with a `.git`): the same dirs plus `dist`, which goes
  under disk pressure only, and only when everything in it is recognisable web output (anything else, or an installer, archive, signed bundle or release manifest in or beside it, keeps it);
- a cache folder that is itself a Cargo target dir (`J-NNNN/target-rust`);
- a git checkout under agent scratch (`scratchRoot/<task>`), once the checkout has been quiet for `scratchRegenIdleHours`
  (2); its own edits, untracked files, stash and unpushed commits are not touched.

A `target` counts only when Cargo marked it (a valid `CACHEDIR.TAG` or a `.rustc_info.json` that parses as Cargo's cache), and only dirs
whose own path git ignores (no ignored ancestor) and tracks nothing of go, at a package root (beside a `package.json`; `dist` also beside a
recognised build config, and a `dist` also needs a positive build signal beside it: a bundler config (vite, webpack, rollup, esbuild, astro, next, tsup, parcel, svelte), a tsconfig whose `outDir` is `dist`, or a `package.json` with a `build` script or a `main`/`module`/`types`/`bin`/`exports`/`files` entry pointing into `dist`; a hand-made `dist` with no signal stays, as does one holding a versioned folder such as `v1.2.0`) and not below a `vendor`, `.vendor`, `runtime` or `third_party` folder; a dir holding a protected leaf (`.env*`, `.dev.vars*`, `.wrangler`, `.vercel`), a release artifact
in a `target`, or a job folder or worktree carrying a retention marker stays. A running, starting (queued, approved) job
keeps everything, and a job that is only *blocked* keeps the pins it holds. A parked or interrupted job that another job waits on does not hold derived output under pressure only when that job itself
rebuilds it (it works inside the same checkout and has no live continuation); output a consumer merely reads, and a pin on source, stay.

## What it never removes

- Anything of a job that is queued, approved, running, blocked, parked or interrupted, or that another job waits on
  (`job add ... --waits-on`). Exceptions are the guarded exact-output and disk-pressure rules above; neither grants permission to delete source.
  One more exception: an interrupted job whose work a durable continuation took over (the host recorded the continuation it
  queued, the job's own interruption clock is verified, and the whole continuation chain has finished) is treated as finished
  work, so the ordinary evidence and clone windows release its scratch and its checkout, aged from the chain's last finish.
  An interrupted job nothing continues (no continuation, successor or resumed job) is treated as failed `interruptedExpiryDays`
  (7, `0` = never) after its verified interruption: its scratch and clone age by the ordinary windows, archived first when they
  hold unsaved work. Another unfinished job waiting on it still pins it.
  A continuation that is still queued, waiting for approval or working keeps the source's folders. Every other proof still
  applies, and a live job that claims the folder keeps it. Its own evidence files (screenshots, recordings, traces, logs) still
  expire only through the host's evidence tick, which walks the finished jobs.
- `.env*`, `.dev.vars*`, `.wrangler` and `.vercel` local files. A clone that holds one is kept whole, and TrimReaper
  does not enter or read them. Two exceptions: env *templates* that git tracks (`.env.example`, `.env.sample`,
  `.dev.vars.template` ...) go with an expired clone, since the remote holds the same file; and with
  `cloneProtectedCopies` set to `"remove"`, the local copies are archived owner-only, then go with the clone.
  Separately, `agentScratchProtectedCopies: "archive"` preserves protected material from an expired scratch task in a
  verified owner-only archive before the task goes. Those archives are kept indefinitely. This exception never applies
  to a job's own cache, and never removes the only copy of protected material.
- A folder that carries a retention marker other than `disposable`, except expired per-job `evidence` under the five-day rule above: a file named `.ut-retention-<class>` at its top
  level (`ut` is from UltraTerm, where the format started). Write one with `trimreaper mark <folder> <class>`:
  `evidence` (proof kept for the marked-evidence window; set `markedEvidenceExpiryDays=0` for indefinite retention), `customer` (customer data), `release-runtime-rollback` (what a release or
  its rollback needs). An `evidence` marker on a job's own cache cannot outlive the job's promise: once that job is
  cancelled and `evidenceExpiryDays` has passed, the cache keeps only the normal window instead of forever. `disposable`
  is the opposite: it allows removal, still subject to every other check. In a job-cache child, it never overrides retained
  credential, key or record names anywhere inside the folder.
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
4 GiB or 200,000 files is refused, and the folder is kept.

Idle agent scratch (and a finished job's folder) with unsaved git work is archived the same way once quiet for `unsavedArchiveDays`
(7), into `archiveRoot/scratch-unsaved/`; those archives expire like any other, `archiveExpiryDays` after they became the only copy.
A linked worktree inside the folder has its HEAD bundled and, once the folder is gone, its main repository forgets exactly that
worktree entry (the branch stays). A bare repository nested in it (a `.git-backup`) is bundled whole (`git bundle --all`) and
verified in an empty repository as an extra copy; its raw files are always saved in `files.tar.gz`. `unsavedArchiveDays: 0` or `cloneArchive: "keep"` keeps
such folders, and `agentScratchProtectedCopies: "archive"` keeps its own protected-only route.

By default every bundle is self-contained: the whole history, restorable anywhere (1.2 GB for a large repository, even when the
unsaved commits are a few megabytes). With `archiveBundleMode: "thin-local"` the bundle of a linked worktree leaves out what the
remote's default branch holds (`refs/remotes/<remote>/HEAD`, else `main`/`master`) when its main repository (the worktree's
common git directory, outside the job-cache, scratch and archive roots) also holds it. The manifest records the prerequisite
commits and the main repository, the bundle is checked with `git bundle verify` inside that repository and every saved tip must
be reachable from the bundle heads. The trade-off: restoring a thin bundle needs that main repository
(`git -C <main> fetch <archive>/repo-0.bundle 'refs/*:refs/ut-archive/*'`). A missing main repository, one inside a reaped root, or
a failed check writes the self-contained bundle instead (the manifest names why: `thinFallback`). To restore:

```sh
git init restored && cd restored
git fetch <archive>/repo-0.bundle 'refs/heads/*:refs/heads/*'
git checkout ut-archive/<n>   # manifest.json: repositories[].bundle.refs maps each saved branch to where it came from ("HEAD", ...)
tar -xzf <archive>/files.tar.gz
```

## Moving cold folders to an external drive (offload)

`trimreaper offload run` moves cold folders from the configured sources to an external drive and leaves a symlink, so
every path still works; `trimreaper offload restore <path>` brings one back and `--dry-run` only plans. A folder moves
only when nothing is using it. Each folder that stays is listed under `kept` with its reason:
Regenerable output inside a candidate (`node_modules`, `.next`, `.open-next`, `.astro`, `.turbo`, `__pycache__`, a Cargo target's `debug`, `incremental` and `release/{deps,build}`) is deleted first through the guarded removal, never copied; only what remains moves. A job folder is judged by changes and open handles, not reads.

| Reason | Why the folder stayed |
|---|---|
| `job-live:<job>` / `job-unknown:<job>` | its job is still running, or the job list does not know it |
| `changed-within-<n>min` | something inside was written within the quiet time |
| `read-within-<n>min` | something inside was read within the quiet time (only where the drive records reads; otherwise the entry carries `reads: not-recorded: ...`) |
| `git-dirty:<detail>` | a git repository or worktree inside has uncommitted or untracked changes, or its state cannot be proved |
| `git-unpushed:<detail>` | its checked-out branch has commits the upstream lacks, or has no upstream on a remote |
| `release-artifacts:<names>` | a release workspace holding built output (`out`, `dist`, `release`, `unz`) waiting to be published |
| `parent-active: ...` | with `keepFolder`: another entry in the same folder is in use |
| `in-use:...` | a process has it open |

A clean, fully pushed, idle worktree still moves. See [docs/SAFETY.md](SAFETY.md#offload-a-folder-in-use-stays-on-the-internal-disk-0198)
for the details and [docs/CONFIGURATION.md](CONFIGURATION.md) for `releaseArtifacts`, `releaseMarkers` and `releaseNamePattern`.

## Receipts and one owner at a time

Each removal writes a plan record before it starts and a result record after it ends, to
`stateDir/reaper/receipts-YYYYMMDD.jsonl` (private, append-only). Only one TrimReaper process removes things at a time.
If a run stops part-way (a crash, a full disk, a folder that changed during removal), every later run refuses until
someone reviews the receipts and acknowledges them: `trimreaper owner status`, then `trimreaper owner acknowledge`
([docs/SAFETY.md](SAFETY.md#acknowledging-a-stopped-run)).

