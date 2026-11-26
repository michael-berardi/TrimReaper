# Privacy

TrimReaper runs on your machine. It sends no telemetry, has no accounts and keeps no usage data: no analytics, crash
reporting, install IDs or cloud service. It sends no prompts, file contents, paths or identifiers anywhere; it reads
local folders, Git metadata and local tool caches.

## The release check

Its only network request is the release check:

- One anonymous `GET` to the public GitHub "latest release" record of the repository named in the package metadata
  (`https://api.github.com/repos/<owner>/<repo>/releases/latest`).
- Headers: a fixed `User-Agent: trimreaper-update-check` and an `Accept` header. No version, id, cookie, token, query
  string or request body; nothing about the machine, its folders or how the tool is used.
- At most once per 24 hours per machine, in a detached background process after a command, with a 4-second timeout.
  A failure is recorded with its cause and not retried until the next day.
- Only from a run at a terminal. Never when output is redirected, in CI (`CI` set), in an agent session
  (`CLAUDECODE`, `CODEX_SANDBOX`, `GEMINI_CLI` or `TRIMREAPER_AGENT` set), or from the MCP server.
- Off with `TRIMREAPER_NO_UPDATE_CHECK=1` or `"updates": {"checkAutomatically": false}` in the config file.

GitHub sees the request like any web request (your IP address and the time); see GitHub's privacy statement.

## Installing and updating

The installer in the README downloads the release wheel and its `SHA256SUMS` from GitHub, checks the checksum and
installs offline (`pip --no-index --no-deps`). It and the release-build script run with `DO_NOT_TRACK=1`,
`HOMEBREW_NO_ANALYTICS=1` and `PIP_DISABLE_PIP_VERSION_CHECK=1`.

`trimreaper update`, or an automatic install you turned on, downloads `SHA256SUMS` and then `install.sh` or the wheel
from the same release's assets, the same files the installer downloads. Each file is checked against `SHA256SUMS`
before it is used.

## Commands TrimReaper starts

- The tool cleaners `clean` starts (Homebrew, npm, uv, pip and the rest) run locally with analytics, update notices
  and version checks off: `HOMEBREW_NO_ANALYTICS=1`, `DO_NOT_TRACK=1`, `HF_HUB_DISABLE_TELEMETRY=1`,
  `NPM_CONFIG_UPDATE_NOTIFIER=false`, `PIP_DISABLE_PIP_VERSION_CHECK=1` (with `HOMEBREW_NO_AUTO_UPDATE=1`).
- Git and Docker probes use local data and a local socket; TrimReaper never fetches or pushes.
- If you configure `coordinatorJobCommand`, `coordinatorJobListCommand` or `offload.jobStatusCommand`, TrimReaper runs
  the command you chose; whatever it sends over a network is up to that command.

## Files it writes

State files are created `0600` in `0700` folders.

| Path | Holds | Kept |
|---|---|---|
| `~/.config/trimreaper/config.json` | folders, policy and the `updates` switches | until you change it |
| `~/.local/state/trimreaper/state.sqlite3` | registered job ids, titles and paths, and size and expiry bookkeeping | until you delete it |
| `~/.local/state/trimreaper/reaper/receipts-YYYYMMDD.jsonl` | each removal's paths and outcome | 30 days; scheduled ticks remove older files once no cleanup run awaits review |
| `~/.local/state/trimreaper/last-tick.json` | the last scheduled pass | replaced on every tick |
| `~/.local/state/trimreaper/schedule-errors.log` | launchd's stderr for the schedule | truncated above 1 MiB |
| `~/.local/state/trimreaper/update.log` | automatic installs only | one file of at most 256 KB |
| `~/Library/Caches/trimreaper/update.json` | the last check's time, result and cause, a skipped version, the last notice and automatic attempt | replaced on each check |
| `~/.cache/trimreaper/` | archives of unsaved work and per-job scratch | until their retention window removes them |
| `~/.trimreaper/venv.previous` | the one rollback copy after an update | removed after the new version's first successful command, or after 7 days |

The state folder also holds the cleanup lock and the owner journal of the run in progress. Archives hold copies of
your files; keep any copy you still need before its retention window ends. The `updates`
switches are written only by `trimreaper update --auto` and the one question after an install. Deleting
`update.json` and `update.log` resets the update state.

## Removing everything

Run `trimreaper schedule remove`, then delete `~/.config/trimreaper`, `~/.local/state/trimreaper`,
`~/.cache/trimreaper`, `~/Library/Caches/trimreaper`, `~/.trimreaper` and `~/.local/bin/trimreaper`. If you configured
offload, also delete the mirrored folders under its configured `root`.
