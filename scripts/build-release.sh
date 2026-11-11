#!/bin/sh
# Builds the release assets for the current commit into <out> (default dist/release):
#   trimreaper-<version>-py3-none-any.whl   the package, installed by install.sh into a private venv
#   install.sh, install.ps1                 the installers
#   SHA256SUMS                              checksums of the three files above
# Usage: scripts/build-release.sh [out-dir]   (run from a clean checkout of the release tag)
# Needs python3 3.10+ with pip; pip fetches setuptools>=77 into an isolated build env (build-time only).
set -eu
cd "$(dirname "$0")/.."
OUT="${1:-dist/release}"
die() { printf 'build-release: %s\n' "$*" >&2; exit 1; }
[ -z "$(git status --porcelain --untracked-files=no)" ] || die "the working tree has changes; build from a clean checkout"
command -v python3 >/dev/null 2>&1 || die "needs python3 3.10 or newer"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "needs python3 3.10 or newer"
python3 -m pip --version >/dev/null 2>&1 || die "needs pip (python3 -m pip)"
commit=$(git rev-parse HEAD)
rm -rf "$OUT"; mkdir -p "$OUT"
stage=$(mktemp -d "${TMPDIR:-/tmp}/trimreaper-build.XXXXXX"); trap 'rm -rf "$stage"' EXIT
# Deterministic: fixed zip timestamps (SOURCE_DATE_EPOCH) and a fixed hash seed.
SOURCE_DATE_EPOCH=315532800 PYTHONHASHSEED=0 python3 -m pip wheel --no-deps --quiet --wheel-dir "$stage" . \
  || die "the wheel build failed (python3 -m pip wheel)"
set -- "$stage"/trimreaper-*-py3-none-any.whl
[ -f "$1" ] || die "no wheel was built"
mv "$1" "$OUT/"
cp scripts/install.sh scripts/install.ps1 "$OUT/"
( cd "$OUT" && if command -v sha256sum >/dev/null 2>&1; then sha256sum trimreaper-*-py3-none-any.whl install.sh install.ps1; else shasum -a 256 trimreaper-*-py3-none-any.whl install.sh install.ps1; fi > SHA256SUMS )
echo "built $OUT for $commit"; ls -l "$OUT"