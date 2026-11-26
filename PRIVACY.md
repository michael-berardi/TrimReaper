# Privacy

TrimReaper runs on your machine. It sends no telemetry, has no accounts and keeps no usage data.

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

## Installing an update

`trimreaper update`, or an automatic install you turned on, downloads `SHA256SUMS` and then `install.sh` or the wheel
from the same release's assets, the same files the installer in the README downloads. Each file is checked against
`SHA256SUMS` before it is used.

## Files it writes

- `~/Library/Caches/trimreaper/update.json` (mode `0600`): the last check's time, result and cause, a skipped
  version, when the last notice was shown, and the last automatic attempt.
- `~/.local/state/trimreaper/update.log`: automatic installs only, one file of at most 256 KB.
- The `updates` switches in the config file, written only by `trimreaper update --auto` and the one question after
  an install.

Deleting these files resets the update state; nothing else needs to be removed.
