# TrimReaper

TrimReaper reclaims disk space left by coding agents on macOS: job clones, build output,
scratch folders and tool caches. It checks job state, Git, retention markers and running
processes before removal. Unsaved work can be archived and verified before its folder goes.
Each guarded removal writes a receipt so you can see what happened and why.

Use it when agent jobs leave more cleanup than you want to handle by hand. Preview a pass
before deleting anything. Read the [safety model and its limits](docs/SAFETY.md) before scheduling it.

## Privacy

TrimReaper makes no network requests during normal use. It has no telemetry, analytics, crash reporting, update checks,
accounts or install IDs, and no cloud service. TrimReaper itself sends no prompts, file contents, paths or identifiers
anywhere: it reads local folders, Git metadata and local tool caches.

Installing is the one network step you start: the installer downloads the release wheel and its SHA-256 list from
GitHub, checks the checksum and installs offline. The tools `clean` starts (Homebrew, npm, uv, pip and the rest) run
locally with analytics, update notices and version checks turned off. Git and Docker probes use local data and a local
socket; TrimReaper never fetches, pushes or checks for updates. If you configure `coordinatorJobCommand`,
`coordinatorJobListCommand` or `offload.jobStatusCommand`, TrimReaper runs those commands you chose; whatever they send
over a network is up to that command.

Local state: `~/.config/trimreaper/config.json` (folders and policy),
`~/.local/state/trimreaper/` (job titles and paths in `state.sqlite3`, `reaper/receipts-YYYYMMDD.jsonl` with removal
paths and outcomes, `last-tick.json` and `schedule-errors.log`), and `~/.cache/trimreaper/` (archives of unsaved work
and per-job scratch). State files are created 0600 in 0700 folders. Scheduled ticks remove receipts older than 30 days
once no cleanup run is awaiting review and truncate the error log above 1 MiB. Archives hold copies of your files until
their retention window removes them; keep any copy you still need.

To delete everything: run `trimreaper schedule remove`, then delete `~/.config/trimreaper`,
`~/.local/state/trimreaper`, `~/.cache/trimreaper`, `~/.trimreaper` and `~/.local/bin/trimreaper`. If you configured
offload, also delete the mirrored folders under its configured `root`.

## Install

Requires macOS, Python 3.10 or newer, `git`, `lsof` and `ps`. If needed, install Python
with `brew install python` or from [python.org](https://www.python.org/downloads/).

```sh
curl -fsSL https://github.com/michael-berardi/TrimReaper/releases/latest/download/install.sh | sh
```

The installer verifies the release wheel's SHA-256, installs into `~/.trimreaper`, and
puts the command in `~/.local/bin`. It needs no sudo and changes no shell profiles.
Follow its PATH hint if needed. Re-run to upgrade; set `TRIMREAPER_VERSION=0.20.0` on the
installer process to pin a release. Linux and Windows are not supported.

Installer overrides: `TRIMREAPER_HOME` (install directory) and `TRIMREAPER_BIN_DIR`
(command directory). Set them only for installation: at runtime, `TRIMREAPER_HOME`
means the home folder TrimReaper may clean. Remove the default installation by deleting
`~/.trimreaper` and `~/.local/bin/trimreaper`.

### From source

From a checkout, no installation or runtime dependencies are needed:

```sh
PYTHONPATH=src python3 -m trimreaper --help
```

## First use

Preview cleanup without registering jobs:

```sh
trimreaper audit              # bounded top-down disk hog diagnostic (APFS purgeable, snapshots, VM disks, coverage)
trimreaper clean --dry-run
trimreaper clean --only npm,worktrees --dry-run
trimreaper offload prune-dangling --dry-run
```

Review the kept items and their reasons. Remove `--dry-run` only when ready to delete.
`clean` may escalate to shorter idle windows and archive unsaved work when routine cleanup
cannot reach its disk-space goal. Add `--no-escalate` to stay with routine rules.
Models, Docker's disk image and local snapshots are reported, not removed.

For job-specific cleanup, give each clone the job number in its name:

```sh
trimreaper job add J-0042 --cwd /path/to/code/myapp-j0042 --title "Fix login form"
trimreaper job status J-0042 done
trimreaper status --items
# After the 15-minute settling window:
trimreaper reap --job J-0042 --dry-run
```

The clone must be inside a configured `cloneBases` root. Configure that before registering
jobs; the example path above is a placeholder. `status --items` is a read-only audit.
`tick` runs one scheduled pass; `schedule install` installs a launchd schedule every 30 minutes.

## Configuration and retention

Settings live in `~/.config/trimreaper/config.json`, or the file named by `TRIMREAPER_CONFIG`:

```json
{
  "cloneBases": [{"path": "/path/to/code"}],
  "policy": {"cloneExpiryDays": 3, "evidenceExpiryDays": 14}
}
```

Use `trimreaper config` to inspect effective settings and
`trimreaper policy-set evidenceExpiryDays=7` to change one policy value.
Scratch cleanup is off until `scratchRoot` is configured. Retention and protected-name
exceptions are documented in the [cleanup reference](docs/USAGE.md); do not assume a
marker or an archive keeps data forever. Customer and release/runtime/rollback markers
preserve folders; `disposable` permits removal subject to the other checks.

Operational commands print JSON. Exit codes: `0` completed (kept folders are not errors),
`2` refused or failed, `3` partial pass or reported errors. Help and version output are text.
A busy cleanup lock reports that its holder and age are unknown; retry after it is released. A stopped cleanup may require receipt review and [owner acknowledgment](docs/SAFETY.md#acknowledging-a-stopped-run).

## MCP

```sh
claude mcp add trimreaper -- trimreaper mcp
```

For other stdio MCP clients:

```json
{"mcpServers": {"trimreaper": {"command": "trimreaper", "args": ["mcp"]}}}
```

Tools: `job_add`, `job_status`, `job_list`, `reap_status`, `reap`, `clean`, `mark`, `audit`.
`reap` and `clean` default to dry runs; deletion requires `"dryRun": false`.
Policy changes, scheduling and owner acknowledgment stay on the CLI.

## Reference

- [Cleanup rules, escalation and archive restoration](docs/USAGE.md)
- [Configuration keys and defaults](docs/CONFIGURATION.md)
- [Safety gates and limitations](docs/SAFETY.md)
- [Embedding the engine](docs/EMBEDDING.md)
- [Tests and contributions](CONTRIBUTING.md)
- [Security reports](SECURITY.md) · [Changelog](CHANGELOG.md) · [MIT license](LICENSE)

---

Part of [Pareto Cybernetics](https://paretocybernetics.com/projects/trimreaper/): free, private, open-source tools for working with AI agents.
