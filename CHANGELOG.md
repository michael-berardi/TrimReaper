# Changelog

## 0.23.1 (2026-11-21)

- Review fixes for 0.23.0. A finished job's fixtures are disposable only under a missing or evidence-class job marker: a customer,
  release or ambiguous job marker keeps them. The model-weight skip in an archive tests the file's own path, so a weight in a
  repository outside any fixtures folder is still saved. Offload also refuses a folder whose children hold a fixtures folder.

## 0.23.0 (2026-11-21)

- Fixtures of finished jobs are disposable scaffolding, never evidence (a finished job kept 14.7 GB of model weights). Once the job
  finished evidenceExpiryDays ago, its `fixtures` folder is reapable even under a job-level evidence marker; only a marker on the
  fixtures folder itself keeps it. Model weights (.safetensors, .gguf, .pt, .ckpt, .bin over 100 MB) in a fixture are never saved
  in an archive. Every other gate (protected leaves, secret material, open handles, owner proof) still applies.
- Offload never moves a fixtures folder or a job folder holding one: delete or keep only.
- The cleanup lock wait backs off (0.05 s doubling to 2 s) and the fixture wait default is 120 s instead of 2 s.

## 0.22.3 (2026-11-21)

- Third review round. The overflow sweep also checks the home folder a moved folder came from: a retention marker or repository at a
  job's home root (the kept folder that holds links) keeps the moved children. Before a folder whose output offload deleted is swapped
  for its link, the open-handle check runs again.

## 0.22.2 (2026-11-21)

- Second review round. Offload plans which folders will move (size, goal, limit and free-space rules) before it deletes anything, so a
  folder that stays loses no output; its own deletions no longer make a kept folder's siblings look active. The overflow sweep looks at
  what stayed behind in home: a repository above a moved folder, or a retention marker above it, keeps the moved copy. A job folder
  holding a release artifact is never expired on the overflow.

## 0.22.1 (2026-11-21)

- Review fixes for 0.22.0. Offload deletes regenerable output only in folders it then moves: a folder it keeps (dirty git, recent change,
  release workspace, live sibling, size or goal limits) loses nothing. The git proof now reaches the repository above the candidate
  (not only inside it), and output whose git dir is gone is moved whole instead of deleted. Reads are ignored only for folders that
  name a job. The overflow sweep skips folders of jobs that are not finished, checks the tree is quiet, and removes the keepFolder
  links left in home.

## 0.22.0 (2026-11-21)

- `offload run` deletes regenerable output instead of moving it. Inside each candidate folder, `node_modules`, `.next`, `.open-next`,
  `.astro`, `.turbo`, `__pycache__` and a Cargo target's `debug`, `incremental` and `release/{deps,build}` go through the cleaner's guarded
  removal first (protected leaves never entered, git must ignore it and track none of it, no process holding it, a manifest beside a
  dependency folder, no release artifact), and only what remains is copied to the overflow volume. Release bundles stay. A live or unknown
  job's folder is untouched. The result lists them under `regenerable` (a dry run shows what would be deleted, not moved).
- A job folder's reads no longer hold it back. TrimReaper's and the job tool's own scans reset access times, so `read-within-5min` kept
  folders nobody used. Job sources are judged by content changes and open handles only; other sources keep the read window.
- A git error is named: `git-error:metadata-unavailable:<cause>`. A finished job's worktree whose git dir was deleted has no history to
  lose and now moves instead of staying forever.
- The overflow volume has retention. New clean rule `overflow-retention` (also in the steady pass) removes regenerable output found there
  and the folders of jobs finished more than `archiveExpiryDays` ago, unless a retention marker or protected name is inside; the link left
  at home goes with it.

## 0.21.1 (2026-11-20)

- A status audit whose time budget is already spent no longer reports a job as having nothing to reap. The job's name scan got a
  deadline that had already passed, found no folders, and the audit remembered that as "no items". The scan now always gets a
  short first look, and a scan cut short is never remembered as empty.

## 0.21.0 (2026-11-20)

- A folder a running job merely started in no longer holds other jobs' finished work. A declared path that is a collection root
  (`/`, the home folder, a clone base such as `~/dev`, `~/.cache`, the job-cache root, the agent-scratch root, or any folder above one)
  now claims nothing below it; only a specific worktree or folder still does. Before, one job running from the home folder kept
  about 6 GB of finished jobs' folders under `claimed-by`.
- The CLI-release proof no longer fails on a large Homebrew Cellar. The Cellar has its own allowance (60000 entries) apart from the
  tool's own places (5000), so `references-unproved:more-than-5000-entries` no longer keeps every old release. A Cellar past its
  own allowance still keeps everything.
- The automatic clean pass names its top five blockers by bytes (was three) so a pass that frees nothing says why.

## 0.20.3 (2026-11-19)

- The installer no longer reports success while the `trimreaper` command on PATH is still an older copy. When
  `~/.local/bin/trimreaper` belongs to something else (for example a pipx install), the new version is still installed in its
  private folder, but the installer now exits 3 and says how to fix it (`pipx uninstall trimreaper`, or remove the file, then run
  the installer again). Before, it exited 0, so an upgrade looked done while every `trimreaper` call ran the old version.

## 0.20.2 (2026-11-18)

- Disk pressure now reaches the build output that actually fills the disk. 70 GB of job cache folders held `node_modules`, `.next`,
  `dist` and Cargo `target` dirs that pressure never saw, because each `J-NNNN/<dir>` was one opaque item.
  - A git worktree or clone inside a job's cache folder is scanned: its derived dirs are items of their own
    (`job-clone-regenerable`, ownership `cache-worktree`), released under pressure by parked and interrupted jobs and
    after a finished job settles (`dist`: under disk pressure only, never because a job finished or a timer elapsed). A cache folder
    that is itself a Cargo target dir (`J-NNNN/target-rust`) is released the same way.
  - What counts as derived (review fixes): the dir's OWN path must be what git ignores (an ignored ancestor refuses it, for named clones
    too), it must not sit below a `vendor`/`.vendor`/`runtime`/`third_party` folder, and it must sit at a package root
    (`node_modules` and the other JS outputs beside a `package.json`; `dist` beside one or a recognised build config). So a
    vendored runtime's `lib/node_modules` or a `.vendor` copy is never offered. A `target` counts only with Cargo's own mark: a valid
    `CACHEDIR.TAG` or a `.rustc_info.json` that parses as Cargo's cache (`{}` no longer does).
  - `dist` is reclaimable only when EVERYTHING in it is recognisable web/build output (an allowlist: `.js .mjs .cjs .map .css .html .json
    .wasm .d.ts`, images, fonts, and only `robots.txt`, `LICENSE*` and `*.LICENSE.txt` as text). Anything else keeps it, in or beside it: an installer, an archive of any format (`.zip`, `.tar.*`,
    `.7z`, `.vsix`, `.jar`, `.snap`, `.flatpak`, `.nupkg`, `.crx`, `.xpi`...), a `.blockmap`, a channel manifest (`latest*.yml`,
    `beta-mac.yml`, `release.json`, `appcast.xml`), an app bundle, a release folder, a link, a nested git repository (a gh-pages worktree)
    or a file of a type nobody listed. A `node_modules`/`dist` below a packaged `*.app`, `*.framework` or `Contents` folder is a shipped copy
    and never offered. A disposable marker on a derived dir no longer lifts these fences (Cargo's mark, placement, the `dist` allowlist
    and its pressure rule).
  - A `dist` also needs a positive build signal beside it, or it stays as possibly hand-made: a bundler config (vite, webpack, rollup, esbuild,
    astro, next, tsup, parcel, svelte), a tsconfig whose `outDir` is `dist`, or a `package.json` whose `scripts.build` is set or whose
    `main`/`module`/`types`/`bin`/`exports`/`files` point into `dist`. A versioned subfolder (`v1.2.0`, `1.2.0`) is a release shape and keeps it.
    The dist scan stops at the audit's hard ceiling and keeps the dist when time runs out.
  - A parked or interrupted pinner (`awaited-by`, or a host's input pin through `reap_pin_released`) stops holding pure derived output under
    pressure only when it rebuilds that output itself: it works inside the very checkout, and an interrupted one has no live continuation.
    A producer's output that a consumer merely reads stays pinned. Every pinner is asked, not only the first one named, and the audit and the
    removal ask the same question for a host's input pin (the pinner its blocker text names); the audit looks a job's pinners up once.
  - The git proofs of a derived dir (git-ignored, tracked-free, no ignored ancestor) are asked again immediately before the removal, with the
    locks held; retention, Cargo's mark, placement, the release fence and the pressure reading were already recomputed there.
  - Agent-scratch checkouts lose their derived dirs (and `dist`, with the same fences) under pressure once quiet, whatever the checkout's own
    unpushed commits or edits; the checkout and its commits are never touched.
- The audit budget is the policy key `reapAuditSeconds` (60). The tick's audit escalates it (2x, up to 8x, never above 480 s whatever
  the policy says) while its previous audit was incomplete. Agent scratch is audited first, within 40% of the budget, and the job audits keep
  their own 40%; each folder gets an even share of what is left, and nothing is promised after the deadline: the whole audit stops
  sizing at 1.25x its budget (a size past it is a lower bound). A size that hit its limit is a lower bound (`bytesLowerBound`,
  `summary.unreapedBytesIsLowerBound`), and a folder's size is never floored to 0. The worktree scans and every git probe of an audit stop at
  that same ceiling too, and an audit outside the tick (`reap-status`, explicit `--budget` included) is capped at 90 s whatever the policy
  says, so even its 1.25x ceiling returns inside the 120 s shell guard.
- Tests no longer scan this machine's shared and per-user temp folders.

## 0.20.1 (2026-11-15)

- Cleanup owners can wait briefly for a busy lock and preview interrupted handoffs without changing them. A timed-out wait says how long it waited and when to retry; any journal snapshot is explicitly unverified.

## 0.20.0 (2026-11-15)

- New clean targets for space that had to be reclaimed by hand (a disk at 88% brought to 70%). Each goes through the guarded
  removal and its own proofs; `clean --dry-run` lists what each rule would reclaim in `byRule`.
  - `cli-releases`: side-by-side releases of self-updating tools in `~/.local/share/<tool>/releases/<version>/`. The newest
    three stay (`cliReleasesKeep`), and so does every version a process uses (working directory, open file, command line),
    a link points into (the tool's `current`, a bin folder's command, a Homebrew Cellar keg) or a settings file or wrapper
    names. A settings file that cannot be read keeps every version.
  - `coordinator-outputs`: Rust `target`/`*-target`, `node_modules`, `dist` and `.cloudflare` inside coordinator job folders
    (`coordinatorRoots`, default `~/.cache/ut-coordinator`), once the job is done, failed or cancelled (asked through
    `coordinatorJobCommand`, default `ut-coordinator job-get`), or parked or blocked and untouched for 24 hours. Never notes,
    reports, evidence, handoffs or sources; in a git checkout only folders git ignores and tracks nothing of.
  - `release-scratch`: in `rel-*` release folders (`releaseScratchRoots`, default `~/.cache/agent-scratch`), the
    `src-tauri/target` of a release worktree whose HEAD is on `origin/main`, `unz` folders and staged `out*` folders but
    the newest two, when nothing uses the release folder.
  - `temp-build-caches`: `node-compile-cache` (was a day, now an hour), `*-publish-check.*` and `oss-leaks-*` in the temp
    folders after an hour unused. They are no longer part of `agent-leftovers`.
- Package caches have size thresholds: `uv cache clean` from 2 GB (the routine `uv cache prune` below), npm's cache is left
  alone under 256 MB, and the new `cargo-xwin` target removes cargo-xwin's downloaded SDK from 512 MB. Each only while its
  tool is not running.
- The report lists each Colima VM's disk file with its occupied size and the steps that shrink it. `clean --trim-vm` runs
  them for a VM that was stopped: start, prune unused images, volumes and build cache, `fstrim` the data disk inside the VM
  (the step that returns the space to the Mac), stop. A running VM is never touched.

## 0.19.8 (2026-11-15)

- Offload no longer moves a folder that is in use for work. An hour without writes and no process holding the folder
  open counted as idle, so a pause between two steps of a release was enough: a release's worktree, its build folders and
  its artifacts awaiting publication moved to the external drive after its tests passed, and the next step refused to build
  from another volume. The folder is now read for signs of work, with no marker to remember. A git repository or worktree
  inside it with uncommitted changes, unpushed commits on its branch, or a branch with no remote upstream stays
  (`git-dirty`, `git-unpushed`). A file read within the quiet time counts as use where the volume records access times
  (`read-within-<n>min`). Where it does not, the receipt says only changes counted. A release workspace holding built
  output (`out`, `dist`, `release`, `unz`) stays with all its subfolders (`release-artifacts`); the names are
  configurable (`releaseArtifacts`, `releaseMarkers`, `releaseNamePattern`). A clean, fully pushed, idle worktree still moves.

## 0.19.7 (2026-11-15)

- Browser builds that an installed Playwright still needs are kept. When two projects on different Playwright versions
  shared one browser cache, the `playwright` cleanup removed the older project's builds because the newer project had
  newer ones, and that project's browser tests then failed with `Executable doesn't exist`. Cleanup now reads the
  installs Playwright registers in the cache (`.links`) and keeps every build one of them lists, checking again just
  before removal. A build less than an hour old is also kept, so a download in progress is never removed. The newest
  build of each browser stays, as before.

## 0.19.6 (2026-11-15)

- A busy cleanup lock now explains that the holder and lock age cannot be verified until the lock is released. Locking and removal checks are unchanged.

## 0.19.5 (2026-11-15)

- MCP rejects explicitly supplied non-object params and tool arguments, including null, false, zero, empty strings and empty arrays, instead of silently treating them as omitted. Omitted objects still use the existing defaults. No removal rules changed.
- Setup uses the published installer URL. The shorter README links to a cleanup reference with corrected archive, stash and steady-pass descriptions.

## 0.19.4 (2026-11-14)

- The in-use check now sees a temp folder that a running process names by its short path. A process names its temp folder
  the way it was given it (`/var/folders/...`, `/tmp/...`), and the check compared only the resolved `/private/...` form
  against command lines, so a folder a live program still passed to its helper looked unused and the leftovers pass removed
  it (2026-11-14: a terminal session's generated config folder; every restart of that session then failed at start). Both
  spellings are looked for, and the match must start at a path boundary as well as end at one.

## 0.19.3 (2026-11-14)

- `offload` with `keepFolder` no longer moves a subfolder while the kept folder is in use through a sibling that an earlier
  pass already moved. That sibling is now a link; the scan used to skip it, and a process working inside it was seen at its
  real path on the overflow volume, not under the kept folder. Links whose target is under the overflow root are now
  checked for a working directory or open file too, and a link that cannot be resolved keeps the folder.
- The kept folder is checked again just before the original is swapped for a link, so a sibling that changes during a long
  copy stops the move (nothing is removed; the error names the cause).
- One scan bound (time and entries) now covers all siblings of a kept folder instead of resetting for each, and siblings
  the pass already measured as candidates are not walked twice. A bound hit still keeps.
- A command line only counts as naming a folder at a path boundary: `.../J-00012/x` no longer holds `.../J-0001`.

## 0.19.2 (2026-11-14)

- `offload` with `keepFolder` no longer moves a subfolder while its kept folder is active. A cold subfolder stays when a
  sibling entry in the kept folder changed within the quiet time (the scan stops at the first recent change), or when a
  process has its working directory or a file open under the kept folder. `kept` names the cause, e.g.
  `parent-active: wt changed 12 min ago`, so `offload status` and `--dry-run` explain it. Before, a release folder whose
  `wt` was being built could lose its sibling checkout to a symlink and the packaging step refused it.
  Sources without `keepFolder` behave as before.
- `install.ps1` (the macOS-only refusal script) no longer sets `$ErrorActionPreference` at top level, so it cannot leave the user's PowerShell session in Stop mode when run with `irm | iex`.

## 0.19.1 (2026-11-11)

- `install.sh` installs the wheel with `--no-index --no-deps`, so pip can never fetch anything from PyPI during an install
  (the package has no dependencies). It also never overwrites a `trimreaper` in the bin folder that it did not write:
  its shim now carries a marker line, and a shim written by 0.19.0 that points into the tool home counts as its own.
  If another `trimreaper` is there, the install finishes and says the command was left alone.

## 0.19.0 (2026-11-11)

- One-step installers. `scripts/install.sh` (macOS) downloads the release wheel, verifies its SHA-256 before
  anything is installed, and installs it into a private venv in `~/.trimreaper` with the `trimreaper` command in
  `~/.local/bin`; re-running upgrades in place, no sudo, no shell profile edits. TrimReaper supports macOS only
  (launchd scheduling, APFS dedupe and compression, macOS ACL protection), so `install.sh` refuses on Linux and
  `scripts/install.ps1` refuses on Windows and under PowerShell 7 on Linux, each naming why and installing nothing.
  `scripts/build-release.sh` builds the release assets (wheel, both installers, SHA256SUMS) deterministically, and
  `scripts/test-install.sh` tests the installers on macOS, in a Linux container and under PowerShell 7 on Linux.

## 0.18.3 (2026-11-11)

- `reap_note_epoch` reads the colon UTC offset form `YYYY-MM-DDTHH:MM:SS+ZZ:ZZ` as well as the legacy `+ZZZZ`, so an embedding
  host whose iso() emits the canonical colon form (as UltraTerm's now does, for python 3.9 `fromisoformat`) round-trips through
  the engine. A colon-offset owner note with no recorded `pidStart` parsed as None, so a cleanup-owner pid reused by a later
  process could not be proven dead and the unfinished handoff blocked every clean until a person acknowledged it. Malformed
  times still return None; the legacy form is unchanged.
- Tests: the engine's disk reading is faked where a test needs a known level — the scratch-regeneration classes see a disk
  above the clean watermark, the steady tick/gate classes a disk below it, and the ladder classes their own fixture bytes.
  On a machine whose real disk sat at or above the warn watermark, five steady-state tests failed and the scratch tests
  silently depended on the real disk being full; both directions are now hermetic.

## 0.18.2 (2026-11-11)

- An agent-scratch folder is held only by a process inside it (cwd, open handle, or a command line naming it). The holder scope
  was the whole scratch root, so one dev server in one folder pinned every idle sibling (66 folders on 2026-11-10).
- Offloaded job-cache folders expire. A link `<cacheRoot>/J-NNNN/<name>` whose target is exactly its mirror under
  `<offload.root>/home` is listed as that job's cache folder, judged by the same retention rules (job-folder markers included),
  removed on the overflow volume through the guarded path, and then its link is unlinked. Before, links were never listed, so
  offloaded folders never expired and the overflow volume only grew.
- Roots strictly below the configured overflow home (`<offload.root>/home`, a folder below a mounted volume in /Volumes) pass
  the allowed-root check. Expired archives whose archive root was offloaded were refused as `allowed-root-inside-system-root`
  on every pass. Any other /Volumes path is still refused. Embedding hosts opt in by defining `reap_offload_root`.
- `offload run` prunes orphans first: a moved folder whose home owner is gone (the reaper expired the folder that held the
  link) goes unless it holds a preserving `.ut-retention-*` marker, its job is live, or a process holds it. A folder whose owner
  is still at home but whose link is missing is kept and reported. Receipts `offload-prune-plan`/`offload-prune-done`.

## 0.18.1 (2026-11-09)

- A merged scratch worktree is now removed under the same discipline as a whole clone. The protected-name scan runs before any
  `git status --ignored=matching`, so git never lists ignored or untracked paths in a tree that may hold a protected leaf. The
  worktree's own build-queue lock and every Cargo lock in its `target` directories are held from the plan receipt through
  `git worktree remove` (a busy lock keeps the worktree, `producer-locks` is recorded).
- `reap_pid_start` reads elapsed time (`ps -o etime=`) instead of `lstart`, which `ps` prints in the system zone while the
  caller's `TZ` could differ: with `TZ=UTC` in a UTC+ zone a live cleanup owner read as a reused pid and its note was acknowledged
  automatically.

## 0.18.0 (2026-11-09)

- A cleanup run left `interrupted` by a dead process no longer blocks every later clean until a person acknowledges it. When the
  recorded owner pid is gone, or was reused by a process that started later (compared with the `pidStart` now recorded in every
  owner note; old notes are judged by the note time), the next clean writes the acknowledgment itself: a private 0600 report built
  from the run's own receipts (deleted, retained and unresolved paths, receipt files, next safe step) and a journal line marked
  `auto` with the report path and sha256. Nothing is deleted by it: every path is re-gated by the normal sweep, including paths the
  dead run never reached. A live or unknown owner, unreadable receipts and a deliberate `stopped` handoff still need a person.
- New `scratch-regenerable` class and `reap --scratch-regen [--dry-run]` (also run by `tick`, after the other passes): at
  `cleanAtPercent`, `node_modules .astro .next .turbo target .open-next` (never `dist`, never protected leaves) of a git checkout under
  agent scratch are removed when the checkout is clean with every commit pushed and no stash, nothing in it or its git state changed
  for `scratchRegenIdleHours` (2) and nothing has it open. A linked worktree fully merged into its remote-tracking upstream, clean, idle
  `scratchWorktreeIdleHours` (6), is removed with `git worktree remove` (never `--force`). Both go through the single deletion path,
  with plan and result receipts, and stop once the disk is at `cleanToPercent`. `0` turns either rule off.

## 0.17.2 (2026-11-07)

- `tagged-caches` discovery gave every root an equal slice of the entry budget, and every linked worktree is a root, so a code
  home such as `~/dev` got about 47,000 of 3,000,000 entries and reported `discovery-incomplete` on every pass. Each root now has
  the whole budget, and one pass stops at 12,000,000 entries in total, still naming the cause.

## 0.17.1 (2026-11-07)

- `offload` source option `keepFolder`: with `children`, each child stays a real folder holding real files and only its
  subfolders move. A job store that refuses symlinks (`J-0042/coordination.jsonl`) kept failing when a whole job folder became
  a link. Links made by 0.16/0.17.0 are split, or copied back for single files, on the next pass.
- `offload status` and the return-when-live step now follow per-entry links.

## 0.17.0 (2026-11-07)

- `offload` has no fixed age. A finished job's folder moves as soon as nothing has it open; a live job's folder never moves and a
  moved one returns when its job is live again; other folders move after `quietMinutes` without a change. Largest first, stopping
  at `cleanToPercent` (`--all` moves everything that qualifies). Job state comes from the configurable `jobStatusCommand`; if it
  fails, nothing moves. `idleDays` is gone.

## 0.16.0 (2026-11-07)

- New `offload` config key and `trimreaper offload run|status|restore`: cold folders move to an external overflow volume and a
  symlink stays behind, so tools keep working. Guarded by idle time, open-file checks, a verified copy, a free-space floor on the
  volume and a durable receipt. `tick` runs it after the routine passes and skips it, with a named cause, when the drive is absent.

## 0.15.1 (2026-11-06)

Fixture removal may clear a nested Git store inside a proven scoped folder, when a host scope is set by the caller: a Git store
at the fixture root, under a protected name or under another Git store is still refused, and with no scope the old refusal stands.

- `tagged-caches` discovery no longer reports an error (and `tick` no longer exits 3) when a folder in a shared temp root is removed by
  its owner between being listed and being scanned: a folder that is gone is skipped, not refused.

## 0.15.0 (2026-11-05)

Finds the debris agents leave where earlier versions never looked, without loosening any proof.

- New `tagged-caches` clean target: folders that declare themselves caches with a valid `CACHEDIR.TAG`, found at any depth in
  the job cache, agent scratch, worktree homes, code homes and the user temp folder. Idle `taggedCacheIdleHours` (6), nothing
  open, no build tool running, no release artifact, no retained or protected names, not a job or task root. Cargo targets stay
  with `rust-build-output`; code homes are not swept by the steady pass.
- Dependency and framework output nested anywhere inside a job folder of a job that is no longer live goes after 6 idle hours,
  with the usual git proofs.
- Agent scratch and finished jobs whose only blocker is unsaved git work are archived (unpushed commits as a verified bundle,
  dirty and untracked files, files outside every repository, stray git directories whole) and then removed after
  `unsavedArchiveDays` (7), in the routine pass. Linked worktree entries are forgotten in the main repository; nothing is
  removed while that repository is busy.
- `interruptedExpiryDays` (7): an interrupted job with no continuation or successor ages like a failed one.
- The agent-scratch listing resumes where the last pass stopped and reuses idle proofs only while a folder is unchanged, so
  every folder is proved within a few passes; removal always proves afresh.
- `archiveBundleMode: "thin-local"` stores only what a persistent main repository lacks, pins that history there with
  `refs/trimreaper/thin/<archive-id>` until the archive expires, and falls back to a self-contained bundle whenever it cannot.

## 0.14.1 (2026-11-05)

An archive whose git bundle step fails (`bundle-init`, `bundle-refs`, `bundle-create`, `bundle-verify`) is remembered for a day like
a size, space or time refusal, so an unchanged folder is named once instead of being retried on every pass.

## 0.14.0 (2026-11-04)

Archive manifests now carry the neutral kind `trimreaper-archive`. Manifests written by earlier versions keep their old kind and
are still read, so existing archives keep expiring and superseding as before. The neutrality test now also refuses the name of
the agent stack the engine was first built inside.

## 0.13.0 (2026-11-04)

- New `compress-logs` target (every pass, including the quiet steady pass): history files (transcripts, receipts, logs) that
  have not changed for `compressLogsIdleDays` (2) under the folders you list in `compressLogsRoots` (none by default) get
  transparent, lossless APFS compression through `afsctool`. Nothing is removed and every reader sees the same bytes. The
  original is never rewritten in place: a private APFS clone is compressed, verified, compared byte for byte, and renamed over
  the original only if its inode, size, mtime and ctime are unchanged. Files a process holds open (matched by device and
  inode), links, hard links, executables and files under 128 KiB are skipped. Each batch takes the cleanup-owner lock on its own,
  a pass spends at most five minutes, and a busy reaper defers the rest to the next pass. On a real machine this saved 5.9 GB
  of 11 GB of agent history. macOS only; without `afsctool` the target says why it skipped.

## 0.12.0 (2026-11-04)

Closes the gaps that let a disk reach 93% while tens of GB of provably regenerable agent debris sat on it.

- Git-ignored, untracked build output (Rust `target/debug`, `incremental`, project dependencies) is no longer kept because the
  repository has a stash: a stash lives in the git dir and is untouched by removing ignored output. The dirty-tree proof stays.
- Agent-scratch idle walks get a larger default budget (3,000,000 entries, 60 seconds), so a large idle task folder is proved
  instead of being kept for `idle-budget-exceeded` forever.
- Node's compile cache in the user temp folder is pruned per version folder after 24 idle hours.
- New `cleanWorktreeArchiveExpiryDays` (default 0, kept): opt in to expiring the clean pass's `clean-worktrees` archives,
  counted from the later of their sole-copy time and the first pass that counted them.
- New `cleanEmergencyPercent` (90) and `cleanEmergencyHours` (6): at that occupancy the worktree and finished-job rungs accept
  six idle hours; archive-then-remove and pinned inputs keep their windows, and every other gate is unchanged.

## 0.11.2 (2026-11-04)

Stops the clean pass from removing dependencies and build output a project is actively using. The idle walk skips
`node_modules` and `.git`, so a project whose sources were old read as idle even right after `npm ci`, a fetch or a merge, and
its `node_modules` was deleted mid-deploy. An install marker (`node_modules/.package-lock.json`, the lockfile) and git's
`ORIG_HEAD`, `FETCH_HEAD`, `MERGE_HEAD`, `REBASE_HEAD` and `COMMIT_EDITMSG` now count as activity. Debris inside an
agent-scratch task or a job folder also needs that folder's full idle proof, its linked worktree's git metadata to be quiet,
and no process working in it (new reason `in-use:worktree-process`).

## 0.11.1 (2026-11-01)

Fixes two gaps the 0.11.0 neutrality pass missed. The README install command still named the operator's GitHub handle; it now uses the neutral `your-github-username` placeholder and points at the current release. The neutrality test's personal-name regex was case-sensitive, so a lowercase handle in a URL slipped past it; the regex now matches case-insensitively.

## 0.11.0 (2026-11-01)

Removes operator-name annotations from shipped source comments. Adds a neutrality test that scans shipped source and documentation for model IDs, provider preferences, and personal names.

## 0.10.3 (2026-10-31)

Fixes the dead-owner auto-acknowledgment acknowledging a cleanup run that had already written plan receipts. A sweep whose final note failed
left `started` as its last line, and the dead-owner path then acknowledged it even though receipts show it may already have removed work.
Auto-acknowledgment now refuses when a receipt names the dead run, and fails closed when the receipts cannot be read.

## 0.10.2 (2026-10-30)

Fixes `dedupe-clones` overstating what it saved and redoing finished work. It counted every byte-identical pair as freed, even pairs
that already shared their storage from an earlier pass, and cloned them again. On one Mac a pass reported 30 GB while the disk gained
3.8 GB. It now reads each file's private size (APFS `ATTR_CMNEXT_PRIVATESIZE`): a file that already shares every block is skipped and
counted as `alreadyShared`, and the reported bytes are only the bytes a clone actually frees.

## 0.10.1 (2026-10-30)

Brings four fixes UltraTerm made in its embedded copy back into TrimReaper, so both run the same engine:

- The job reaper sweeps the largest finished job folders first, and takes up to eight jobs per tick while the disk is under
  pressure. Oldest-first with a two-job budget never reached the large recent folders.
- Past `evidenceExpiryDays`, a finished job's folder is no longer kept for names that only sound like records (`report.py`,
  `tokenizer`); key, credential and signing material still keeps it.
- A sweep that was killed (timeout, crash, reboot) and whose process is gone no longer blocks every later cleanup until a person
  acknowledges it. An error or a deliberate stop still needs the acknowledgment.
- `reap-status` stays under a two-minute shell guard by default (60 s sizing budget).

Hosts now provide `meta_json` (docs/EMBEDDING.md).

## 0.10.0 (2026-10-30)

New clean target `dedupe-clones`. Agent jobs that check out the same repository each keep their own copy of its large tracked files: on
one Mac about 25 parked or cancelled jobs held about 45 GB of byte-identical files. On APFS the target replaces each duplicate data file
with a clone of one copy, so the space is shared until a file is written. Nothing is deleted, and every path keeps its exact bytes, mode
and times. It never touches executables or Mach-O files, `.git`, symlinks, hard links, files changed in the last six hours, or a running
job's folder. Each swap is verified and atomic: a file that changed since the scan is left as it was. Off macOS it reports why it skipped.

## 0.9.1 (2026-10-29)

Fixes a dependency folder being removed while a live job still used it. A job's worktree outside the repository can symlink its
`node_modules` to the repository's copy; the output gate only checked job paths inside the repository, so the folder was reaped and every
sibling job then failed with `ERR_MODULE_NOT_FOUND`. The gate now refuses while any linked worktree of the repository still points at the
folder (`linked-worktree-symlink:<name>`), and a test covers it. Once the symlink is gone the folder is eligible again.

## 0.9.0 (2026-10-26)

Fixes a disk that stayed full. On one machine at 97% used the automatic clean logged `goalMet: false, freedBytes: -12460032` on every
tick for days; its dry run found 328 MB to take from 175 worktrees (77 GB), 85 dependency folders (30 GB) and 3,200 temp folders that
every rule kept, and 60 temp folders were attempted and logged as a bare `refused` error on every pass.

Root causes, in the code:

- **The clean pass could only wait.** Every rule was a time window (7 days for a worktree, 48 hours for a project, 1 and 3 days for temp),
  checked once. When the windows kept everything, the pass freed nothing, reported `goalMet: false` and the next tick did the same
  thing. Nothing widened a window as the disk filled.
- **Ignored output waited on unrelated state.** `node_modules`, `.next` and Rust `target` folders were kept when their repository was
  dirty, had a stash or had unpushed commits (57 dirty, 27 with a stash: 30 GB), although git ignoring a folder and tracking nothing in it
  already proves it is not unsaved work, and the job reaper has long said so for a job's clone.
- **Job folders were out of the clean pass's reach and slow in the job reaper's.** A name that carries a job number is "reaped with its
  job" and skipped; the job reaper takes two jobs per tick, keeps an unmarked job folder until `evidenceExpiryDays`, and (in a host that
  pins a producer's folders for a consumer) keeps a finished job's folders for as long as a later job that lists them as an input has not
  finished, even if it failed or was interrupted days ago. Disk pressure changed none of it.
- **A deny-delete ACL made `rmdir` fail on every pass.** The removal error was reported as `refused` with its cause dropped, retried and
  receipted on every tick, and a removal that had started could stop the whole sweep.
- **The retry cadence ignored yield.** A pass that removed nothing was retried after ten minutes, and again, filling the log.
- **The defaults (clean at 85%, to 80%) started late and stopped early.**

What changed:

- **Escalation ladder.** If the routine rules cannot reach `cleanToPercent`, `clean` climbs six rungs, re-planning with each rung's wider
  windows and stopping at the first that reaches the goal (README, "the escalation ladder"): regenerable output of repositories idle for
  2 hours (`cleanEscalateRepoHours`), whatever their age and even when dirty or stashed, with `dist` and `build` added when git ignores
  and tracks none of them and no release artifact is inside; linked worktrees idle for 24 hours and fixtures, candidates, deploy folders
  of jobs finished 24 hours ago; folders pinned only by a finished, failed, cancelled or long-interrupted job; unmarked retention of jobs
  finished 24 hours ago; last, archive-then-remove for worktrees and job clones holding unsaved work (a verified git bundle and a
  hashed tar, stamped as the only copy, compared again before removal; a failed archive keeps the folder). No rung touches a running or
  starting job's folder, a folder a process uses, `.env*`, `.dev.vars*`, `.wrangler`, `.vercel`, a retention marker, a release artifact
  or a primary clone's source. A window set to `0` stays off. Nothing escalates when the disk cannot be measured.
- **Defaults.** `cleanAtPercent` 75, `cleanToPercent` 60, `diskWarnPercent` 75.
- **Steady pass.** Below the high watermark a quiet pass runs every `cleanSteadyHours` (6): regenerable output of repositories idle for
  3 days (`cleanSteadyRepoDays`) and the artifacts of jobs finished 48 hours ago (`cleanSteadyJobHours`); nothing else. `trimreaper clean --steady` runs it by hand; `clean_steady_due` lets a host that runs the pass in a child start it.
- **Honest report.** `summary.removedBytes` is what the pass removed, `summary.freedBytes` the net change of free space (with a note when
  they disagree), `rungs` what each rung found and freed, `topBlockers` the ten reasons keeping the most bytes. The automatic action
  carries the same, instead of `freedBytes` alone.
- **Refusals.** A folder holding an entry whose ACL denies delete is kept as `filesystem-protected-acl:<entry>` before anything is tried
  (the ACL is never cleared); any other removal the filesystem refuses is kept with its cause, remembered for a day, and is not an error.
  Failed items in `errors` now carry their `reason`.
- **Retry cadence.** After a pass that removed nothing the next waits an hour; a finished job ends the wait.
- **Engine.** `reap_escalated`, `reap_policy` and `reap_pin_released` (the one place a pin ends: a done, failed or cancelled job never pins;
  an interrupted one stops pinning only inside a rung, after 24 hours on a verified clock); `reap_acl_denies`; a removal that meets an ACL
  names it. Embedding hosts: see docs/EMBEDDING.md ("the clean pass").

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
