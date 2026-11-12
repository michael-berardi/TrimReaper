#!/bin/sh
# TrimReaper installer for macOS:  curl -fsSL https://github.com/michael-berardi/TrimReaper/releases/latest/download/install.sh | sh
# TrimReaper runs on macOS only (its scheduler needs launchd, its dedupe and compression need APFS). On any other
# system this script refuses with one sentence and installs nothing.
# On macOS it downloads the release wheel, checks its SHA-256 before anything is installed, installs it into a
# private venv inside ~/.trimreaper, and puts `trimreaper` in ~/.local/bin. No sudo, no shell profile edits.
# Re-running upgrades in place.
# Environment: TRIMREAPER_HOME (install folder, default ~/.trimreaper), TRIMREAPER_BIN_DIR (default ~/.local/bin),
# TRIMREAPER_VERSION (pin a release, e.g. 0.19.0), TRIMREAPER_RELEASE_BASE (override the download base, used by tests).
set -eu

REPO="michael-berardi/TrimReaper"
HOME_DIR="${TRIMREAPER_HOME:-$HOME/.trimreaper}"
case "$HOME_DIR" in /*) ;; *) HOME_DIR="$(pwd)/$HOME_DIR" ;; esac
BIN_DIR="${TRIMREAPER_BIN_DIR:-$HOME/.local/bin}"
case "$BIN_DIR" in /*) ;; *) BIN_DIR="$(pwd)/$BIN_DIR" ;; esac
if [ -n "${TRIMREAPER_RELEASE_BASE:-}" ]; then BASE="$TRIMREAPER_RELEASE_BASE"
elif [ -n "${TRIMREAPER_VERSION:-}" ]; then BASE="https://github.com/$REPO/releases/download/v${TRIMREAPER_VERSION#v}"
else BASE="https://github.com/$REPO/releases/latest/download"; fi

say() { printf '%s\n' "$*"; }
die() { printf 'trimreaper install: %s\n' "$*" >&2; exit 1; }

main() {
SYS=$(uname -s)
[ "$SYS" = "Darwin" ] || die "TrimReaper supports macOS only (its scheduler needs launchd and its dedupe and compression need APFS), so nothing was installed on $SYS; on a Mac run this same script."

command -v python3 >/dev/null 2>&1 || die "needs Python 3.10 or newer; install it with: brew install python (or the installer from https://www.python.org/downloads/), then run this again"
PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) || die "python3 is on PATH but does not run; install Python 3.10 or newer with: brew install python, then run this again"
PYMAJOR=${PYVER%%.*}; PYMINOR=${PYVER#*.}
[ "$PYMAJOR" -gt 3 ] || { [ "$PYMAJOR" -eq 3 ] && [ "$PYMINOR" -ge 10 ]; } || die "needs Python 3.10 or newer, found $PYVER; install it with: brew install python (or the installer from https://www.python.org/downloads/), then run this again"
command -v git >/dev/null 2>&1 || die "needs git (TrimReaper proves work is saved through it); install it with: xcode-select --install, then run this again"
command -v lsof >/dev/null 2>&1 || die "needs lsof (ships with macOS; it proves no process uses a folder)"
command -v ps >/dev/null 2>&1 || die "needs ps (ships with macOS; it proves no process uses a folder)"
if command -v curl >/dev/null 2>&1; then fetch() { curl -fsSL --retry 3 -o "$2" "$1" || die "download failed: $1"; }
elif command -v wget >/dev/null 2>&1; then fetch() { wget -q -O "$2" "$1" || die "download failed: $1"; }
else die "needs curl or wget"; fi
if command -v sha256sum >/dev/null 2>&1; then sha256() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }
else die "needs sha256sum or shasum"; fi

TMP=$(mktemp -d "${TMPDIR:-/tmp}/trimreaper-install.XXXXXX") || die "cannot create a temporary folder"
NEW=""; OLD=""
trap 'rm -rf "$TMP" "$NEW" "$OLD"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

say "Downloading TrimReaper..."
fetch "$BASE/SHA256SUMS" "$TMP/SHA256SUMS"
WHEEL=$(awk '{ f=$2; sub(/^\*/, "", f); if (f ~ /^trimreaper-.*-py3-none-any\.whl$/) print f }' "$TMP/SHA256SUMS")
[ -n "$WHEEL" ] || die "no TrimReaper wheel is listed in SHA256SUMS at $BASE"
fetch "$BASE/$WHEEL" "$TMP/$WHEEL"
want=$(awk -v n="$WHEEL" '{ f=$2; sub(/^\*/, "", f); if (f == n) print $1 }' "$TMP/SHA256SUMS")
[ -n "$want" ] || die "no checksum for $WHEEL in SHA256SUMS"
[ "$(sha256 "$TMP/$WHEEL")" = "$want" ] || die "checksum mismatch for $WHEEL; nothing was installed"
v=${WHEEL#trimreaper-}; VERSION=${v%-py3-none-any.whl}

say "Installing TrimReaper $VERSION into $HOME_DIR ..."
mkdir -p "$HOME_DIR" "$BIN_DIR" || die "cannot create $HOME_DIR or $BIN_DIR"
NEW="$HOME_DIR/venv.new.$$"
rm -rf "$NEW"
python3 -m venv "$NEW" || die "cannot create the private venv (python3 -m venv failed); nothing was changed"
"$NEW/bin/python" -m pip install --quiet --disable-pip-version-check --no-cache-dir "$TMP/$WHEEL" \
  || { rm -rf "$NEW"; die "the wheel did not install into the venv; nothing was changed"; }
# The venv is moved into place below, so the check runs it the way the shim will: bin/python -m trimreaper.
got=$("$NEW/bin/python" -m trimreaper --version 2>&1 </dev/null) || { rm -rf "$NEW"; die "the installed tool does not run: $got"; }
[ "$got" = "trimreaper $VERSION" ] || { rm -rf "$NEW"; die "the installed tool reports '$got', not 'trimreaper $VERSION'; nothing was changed"; }
OLD="$HOME_DIR/venv.old.$$"
rm -rf "$OLD"
if [ -d "$HOME_DIR/venv" ]; then mv "$HOME_DIR/venv" "$OLD" || die "cannot move the old venv aside; nothing was changed"; fi
if ! mv "$NEW" "$HOME_DIR/venv"; then
  [ ! -d "$OLD" ] || mv "$OLD" "$HOME_DIR/venv"
  die "cannot move the new venv into place; the previous install was kept"
fi
rm -rf "$OLD"

# The shim calls the venv's python directly (no console-script shebang), so the moved venv keeps working.
cat > "$BIN_DIR/trimreaper.tmp.$$" <<SHIM
#!/bin/sh
exec "$HOME_DIR/venv/bin/python" -m trimreaper "\$@"
SHIM
chmod 755 "$BIN_DIR/trimreaper.tmp.$$"
mv -f "$BIN_DIR/trimreaper.tmp.$$" "$BIN_DIR/trimreaper" || die "cannot put the trimreaper command in $BIN_DIR"

got=$("$BIN_DIR/trimreaper" --version 2>&1 </dev/null) || die "the installed command does not run: $got"
say "$got installed ($BIN_DIR/trimreaper)."
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) say "Add $BIN_DIR to your PATH:  export PATH=\"$BIN_DIR:\$PATH\"" ;; esac
say "Next: trimreaper clean --dry-run"
}

main "$@"