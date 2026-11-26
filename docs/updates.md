<!-- update-contract: v1 -->

# Updates

When a newer stable release exists, a command run at a terminal ends with two lines on stderr, after its JSON:

```
trimreaper 0.27.0 available (you have 0.26.0) · run: trimreaper update
turn on automatic updates: trimreaper update --auto on · skip: trimreaper update --skip
```

The second line is dimmed (plain with `NO_COLOR`). A version is shown at most once a day; a skipped version is not
shown again, a newer one is. Nothing is printed when output is redirected, in CI, in an agent session or from the
MCP server, and stdout always stays one JSON document.

## Commands

```sh
trimreaper update               # check now, show current → new and the first lines of the notes, ask, install
trimreaper update --check       # report only: exit 0 current, 10 update available, 1 check failed (cause in the JSON)
trimreaper update --skip        # hide this version's notice until a newer release
trimreaper update --auto on     # install future releases automatically (off by default)
trimreaper update --auto off
trimreaper update --auto status
trimreaper update --rollback    # installs from the release installer: go back to the previous version
```

At a terminal `update` asks `Install <new> now? [Y/n]`, and after the first successful install asks once
`Install future updates automatically? [y/N]`; the answer is kept and the question never comes back. Without a
terminal `update` installs without asking: running the command is the consent.

## How an update is installed

The check reads the public GitHub "latest release" record; only a stable `x.y.z` release strictly newer than the
running one counts. `update` then downloads the release's `SHA256SUMS` into a temporary folder that is removed on
success and on failure, and checks every file it uses against it before anything changes:

| Installed with | `update` runs |
|---|---|
| the release installer (`install.sh`) | the release's verified `install.sh`, which verifies the wheel again, swaps the venv atomically and keeps the replaced one as `venv.previous` |
| `pip` | `python -m pip install --upgrade --no-deps --no-index <verified wheel>` |
| `uv tool` | `uv tool install --force <verified wheel>` |
| `pipx` | `pipx install --force <verified wheel>` |
| a source checkout | nothing: update it with `git` |

A package is never installed by name from a package index. A failed install leaves the current version in place and
names its cause. The new version applies on the next run; a running command is never touched.

`update --rollback` swaps `venv.previous` back in and skips the version it rolled back from. Only one rollback copy
exists: it is removed after the new version's first successful command (update commands excepted), or after seven
days. Leftovers of an interrupted install are removed after an hour.

## Automatic installs

Off by default. Turned on by `update --auto on`, the question after an install, or the config key below. When on,
the daily background check installs a newer release at low priority, once per version, and logs to
`<stateDir>/update.log` (one file, at most 256 KB). A failed attempt keeps the current version, records the cause
(`update --check` shows the latest state) and the notice appears as usual.

## Settings and files

| `updates` key in the config file | Default | Meaning |
|---|---|---|
| `checkAutomatically` | `true` | The daily background check and the notice. |
| `installAutomatically` | `false` | Install newer releases from the background check. |
| `autoPrompted` | `false` | The question after an install has been asked. |

`TRIMREAPER_NO_UPDATE_CHECK=1` also turns the check and the notice off. The check's state (last result and cause,
skipped version, last notice, last automatic attempt) is one `0600` file, `~/Library/Caches/trimreaper/update.json`.
What is sent: [PRIVACY.md](../PRIVACY.md). For tests, `TRIMREAPER_UPDATE_API` points the check at another release
endpoint and `TRIMREAPER_UPDATE_CACHE` moves the cache file.
