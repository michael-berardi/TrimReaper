#!/bin/sh
# Installer tests. TrimReaper supports macOS only, so:
#   macOS: a real install into a throwaway HOME from a local release base (version check, re-run upgrade,
#          corrupted wheel refused with the checksum error, missing/too-old Python named). Only --version runs.
#   Linux container (ubuntu:24.04, dash + bash + busybox ash): install.sh refuses, installs nothing.
#   PowerShell container (mcr.microsoft.com/powershell, pwsh 7 on Linux): install.ps1 refuses, installs nothing.
# Usage: scripts/test-install.sh   (needs docker, shellcheck optional; starts one short-lived server on 127.0.0.1)
set -eu
cd "$(dirname "$0")/.."
PORT="${TRIMREAPER_TEST_PORT:-18741}"
say() { printf '%s\n' "$*"; }
fail() { printf 'test-install: FAIL: %s\n' "$*" >&2; exit 1; }

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then fail "port $PORT is already in use"; fi
scripts/build-release.sh dist/release
set -- dist/release/trimreaper-*-py3-none-any.whl
[ -f "$1" ] || fail "build-release.sh produced no wheel"
WHEEL=${1##*/}
v=${WHEEL#trimreaper-}; VERSION=${v%-py3-none-any.whl}

# The work dir lives in the checkout: Docker Desktop shares /Users but not $TMPDIR (/var/folders).
WORK=$(mktemp -d "$PWD/.test-install.XXXXXX")
SERVER_PID=""
cleanup() {
  if [ -n "$SERVER_PID" ]; then kill "$SERVER_PID" 2>/dev/null || true; wait "$SERVER_PID" 2>/dev/null || true; fi
  rm -rf "$WORK"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "$WORK/base/good" "$WORK/base/bad"
cp dist/release/* "$WORK/base/good/"
cp dist/release/* "$WORK/base/bad/"
printf 'corrupted' >> "$WORK/base/bad/$WHEEL"
python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$WORK/base" >/dev/null 2>&1 &
SERVER_PID=$!
i=0
until curl -fsS -o /dev/null "http://127.0.0.1:$PORT/good/SHA256SUMS" 2>/dev/null; do
  i=$((i + 1)); [ "$i" -lt 50 ] || fail "the local release server did not start on port $PORT"
  sleep 0.2
done
GOOD="http://127.0.0.1:$PORT/good"
BAD="http://127.0.0.1:$PORT/bad"

# --- macOS: install into a throwaway HOME, the command prints its version ---
TH="$WORK/home1"; mkdir -p "$TH"
HOME="$TH" TRIMREAPER_RELEASE_BASE="$GOOD" sh scripts/install.sh >"$WORK/install1.log" 2>&1 \
  || { cat "$WORK/install1.log"; fail "first install failed"; }
got=$(HOME="$TH" "$TH/.local/bin/trimreaper" --version) || fail "the installed command does not run"
[ "$got" = "trimreaper $VERSION" ] || fail "version is '$got', not 'trimreaper $VERSION'"
say "ok: macOS install, $got"

# --- macOS: re-running upgrades in place ---
HOME="$TH" TRIMREAPER_RELEASE_BASE="$GOOD" sh scripts/install.sh >"$WORK/install2.log" 2>&1 \
  || { cat "$WORK/install2.log"; fail "re-run failed"; }
got=$(HOME="$TH" "$TH/.local/bin/trimreaper" --version) || fail "the command does not run after the re-run"
[ "$got" = "trimreaper $VERSION" ] || fail "version after re-run is '$got'"
[ -d "$TH/.trimreaper/venv" ] || fail "the venv is missing after the re-run"
for leftover in "$TH/.trimreaper"/venv.new.* "$TH/.trimreaper"/venv.old.*; do
  [ ! -e "$leftover" ] || fail "staging leftover: $leftover"
done
say "ok: macOS re-run upgrades in place"

# --- macOS: a corrupted wheel is refused with the checksum error, nothing installed ---
TH="$WORK/home2"; mkdir -p "$TH"
if HOME="$TH" TRIMREAPER_RELEASE_BASE="$BAD" sh scripts/install.sh >"$WORK/bad.log" 2>&1; then
  fail "a corrupted wheel was accepted"
fi
grep -q "checksum mismatch" "$WORK/bad.log" || { cat "$WORK/bad.log"; fail "no checksum error named"; }
[ ! -e "$TH/.trimreaper" ] || fail "a corrupted install left $TH/.trimreaper"
[ ! -e "$TH/.local/bin/trimreaper" ] || fail "a corrupted install left a shim"
say "ok: macOS corrupted wheel refused, nothing installed"

# --- macOS: a missing prerequisite is named (a PATH with no python3 at all) ---
FARM="$WORK/farm"; mkdir -p "$FARM"
for d in /usr/bin /bin; do
  for f in "$d"/*; do
    n=${f##*/}
    case "$n" in python3*) continue ;; esac
    [ -e "$FARM/$n" ] || ln -s "$f" "$FARM/$n"
  done
done
TH="$WORK/home3"; mkdir -p "$TH"
if PATH="$FARM" HOME="$TH" TRIMREAPER_RELEASE_BASE="$GOOD" sh scripts/install.sh >"$WORK/nopy.log" 2>&1; then
  fail "install succeeded without python3"
fi
grep -q "Python 3.10" "$WORK/nopy.log" || { cat "$WORK/nopy.log"; fail "missing Python was not named"; }
[ ! -e "$TH/.trimreaper" ] || fail "a refused install left $TH/.trimreaper"
say "ok: macOS missing python3 named ($(head -n 1 "$WORK/nopy.log"))"

# --- macOS: a too-old python3 is named (a fake python3 reporting 3.9) ---
FARM2="$WORK/farm2"; mkdir -p "$FARM2"
for f in "$FARM"/*; do ln -s "$f" "$FARM2/${f##*/}"; done
printf '#!/bin/sh\necho 3.9\n' > "$FARM2/python3"; chmod 755 "$FARM2/python3"
TH="$WORK/home4"; mkdir -p "$TH"
if PATH="$FARM2" HOME="$TH" TRIMREAPER_RELEASE_BASE="$GOOD" sh scripts/install.sh >"$WORK/oldpy.log" 2>&1; then
  fail "install succeeded with python 3.9"
fi
grep -q "found 3.9" "$WORK/oldpy.log" || { cat "$WORK/oldpy.log"; fail "the old Python version was not named"; }
[ ! -e "$TH/.trimreaper" ] || fail "a refused install left $TH/.trimreaper"
say "ok: macOS too-old python3 named"

# --- Linux container: install.sh refuses under dash, bash and busybox ash, installs nothing ---
docker run --rm -v "$WORK/base:/rel:ro" ubuntu:24.04 /bin/sh -c '
  set -eu
  apt-get update -qq >/dev/null 2>&1
  apt-get install -y -qq busybox >/dev/null 2>&1
  for shell in /bin/sh /bin/bash "busybox sh"; do
    if $shell /rel/good/install.sh >/tmp/out 2>&1; then echo "FAIL: $shell accepted Linux"; exit 1; fi
    grep -q "macOS only" /tmp/out || { cat /tmp/out; echo "FAIL: refusal does not name macOS"; exit 1; }
  done
  test ! -e "$HOME/.trimreaper" || { echo "FAIL: left ~/.trimreaper"; exit 1; }
  test ! -e "$HOME/.local/bin/trimreaper" || { echo "FAIL: left a shim"; exit 1; }
  echo "ok: Linux refusal (dash, bash, busybox ash), nothing installed"' \
  || fail "the Linux container refusal test failed"

# mcr.microsoft.com/powershell publishes no arm64 Linux image (amd64 and 32-bit arm only), so the
# PowerShell 7 tests run in a small local image: ubuntu:24.04 plus Microsoft's official tarball.
PS_VERSION=7.4.6
PWSH_IMAGE="pwsh-install-test:$PS_VERSION"
if ! docker image inspect "$PWSH_IMAGE" >/dev/null 2>&1; then
  docker build -q -t "$PWSH_IMAGE" - >/dev/null <<DOCKERFILE || fail "cannot build the PowerShell $PS_VERSION test image"
FROM ubuntu:24.04
RUN apt-get update -qq && apt-get install -y -qq ca-certificates curl libicu74 >/dev/null \\
 && case "\$(uname -m)" in aarch64) a=arm64 ;; x86_64) a=x64 ;; *) echo "unsupported CPU \$(uname -m)" >&2; exit 1 ;; esac \\
 && curl -fsSL --retry 3 -o /tmp/ps.tgz "https://github.com/PowerShell/PowerShell/releases/download/v$PS_VERSION/powershell-$PS_VERSION-linux-\$a.tar.gz" \\
 && mkdir -p /opt/powershell && tar -xzf /tmp/ps.tgz -C /opt/powershell && chmod +x /opt/powershell/pwsh \\
 && ln -s /opt/powershell/pwsh /usr/bin/pwsh && rm /tmp/ps.tgz
DOCKERFILE
fi
# --- PowerShell 7 on Linux container: install.ps1 refuses (exit non-zero), installs nothing ---
docker run --rm -v "$WORK/base:/rel:ro" "$PWSH_IMAGE" pwsh -NoProfile -Command '
  & pwsh -NoProfile -File /rel/good/install.ps1
  if ($LASTEXITCODE -eq 0) { Write-Host "FAIL: install.ps1 exited 0"; exit 1 }
  if (Test-Path (Join-Path $HOME ".trimreaper")) { Write-Host "FAIL: left ~/.trimreaper"; exit 1 }
  if (Test-Path (Join-Path $HOME ".local/bin/trimreaper")) { Write-Host "FAIL: left a shim"; exit 1 }
  Write-Host "ok: pwsh refusal (exit $LASTEXITCODE), nothing installed"' \
  >"$WORK/ps1.log" 2>&1 || { cat "$WORK/ps1.log"; fail "the pwsh refusal test failed"; }
grep -q "macOS only" "$WORK/ps1.log" || { cat "$WORK/ps1.log"; fail "the ps1 refusal does not name macOS"; }
cat "$WORK/ps1.log"

# --- PSScriptAnalyzer when it installs quickly; otherwise skipped ---
if docker run --rm -v "$WORK/base:/rel:ro" "$PWSH_IMAGE" pwsh -NoProfile -Command '
  Install-Module PSScriptAnalyzer -Force -Scope CurrentUser -ErrorAction Stop
  $r = Invoke-ScriptAnalyzer -Path /rel/good/install.ps1
  if ($r) { $r | Format-Table | Out-String | Write-Host; exit 1 }
  Write-Host "ok: PSScriptAnalyzer clean"'; then
  :
else
  say "skipped: PSScriptAnalyzer (could not install or run it quickly)"
fi

# --- stop the server, prove the port is released ---
kill "$SERVER_PID" 2>/dev/null || true
wait "$SERVER_PID" 2>/dev/null || true
SERVER_PID=""
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then fail "port $PORT is still in use"; fi
say "ok: server stopped, port $PORT released"
say "all installer tests passed"