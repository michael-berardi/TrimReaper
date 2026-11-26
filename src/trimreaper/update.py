"""Anonymous release checks, the post-command notice, and opt-in verified updates.

One anonymous GET to the public "latest release" record, at most daily, says whether a newer stable version exists;
nothing identifies the machine, the user or how the tool is used. Installing is explicit, or opt-in through the config
file, and always checks the release's SHA256SUMS before anything is swapped. Nothing is ever installed by package name.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib import request

from . import __version__
from .host import DEFAULT_CONFIG, HostError, check_config, clear_config_cache, config, config_path, state_dir

TOOL = "trimreaper"
USER_AGENT = "trimreaper-update-check"
DAY = 86400
CHECK_TIMEOUT = 4          # a check never waits longer than this on the network
DOWNLOAD_TIMEOUT = 60
INSTALL_TIMEOUT = 300
MAX_DOWNLOAD = 64 << 20
LOG_LIMIT = 256 << 10      # the background install log is one file of at most this many bytes
PREVIOUS_DAYS = 7          # the rollback copy never outlives this
INSTALLER_MARK = ".trimreaper-installer"  # scripts/install.sh writes it into the venv it owns (content: its bin folder)
AGENT_ENV = ("TRIMREAPER_AGENT", "CLAUDECODE", "CODEX_SANDBOX", "GEMINI_CLI")
SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
SHA_LINE = re.compile(r"([0-9a-fA-F]{64})\s+\*?(\S+)")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")  # release notes never send escape sequences to a terminal
NOTICE = (f"{TOOL} {{new}} available (you have {__version__}) · run: {TOOL} update",
          f"turn on automatic updates: {TOOL} update --auto on · skip: {TOOL} update --skip")


# ---- versions and the release record -------------------------------------------------------------------------------

def parse_version(tag: object) -> tuple[int, int, int] | None:
    """Only stable x.y.z counts (a leading `v` is fine); a prerelease or anything else is not a version."""
    if not isinstance(tag, str):
        return None
    text = tag.strip()
    match = SEMVER.fullmatch(text[1:] if text[:1] in ("v", "V") else text)
    return tuple(int(part) for part in match.groups()) if match else None


def newer(current: str, available: object) -> bool:
    """Strictly newer, and both sides parse as stable versions."""
    have, want = parse_version(current), parse_version(available)
    return have is not None and want is not None and want > have


def release_notes(body: object, limit: int = 5) -> str:
    """The first lines of the published notes, bounded so a long changelog cannot flood a terminal."""
    if not isinstance(body, str):
        return ""
    lines = [CONTROL.sub("", line).strip()[:200] for line in body.splitlines() if line.strip()]
    return "\n".join(lines[:limit])


def api_url() -> str:
    """The latest-release record of the repository the package metadata declares, never a name written here."""
    override = os.environ.get("TRIMREAPER_UPDATE_API", "").strip()
    if override:
        return override
    homepage = ""
    try:
        for value in importlib.metadata.metadata(TOOL).get_all("Project-URL") or ():
            label, _, url = value.partition(",")
            if label.strip().casefold() == "homepage":
                homepage = url.strip()
    except importlib.metadata.PackageNotFoundError:  # a source checkout: the same declaration in pyproject.toml
        source = Path(__file__).resolve().parents[2] / "pyproject.toml"
        found = re.search(r'^Homepage\s*=\s*"([^"]+)"', source.read_text(encoding="utf-8"), re.M) if source.is_file() else None
        homepage = found.group(1) if found else ""
    match = re.fullmatch(r"https://github\.com/([\w.-]+)/([\w.-]+)/?", homepage)
    if not match:
        raise HostError("the package metadata names no GitHub repository", "set TRIMREAPER_UPDATE_API to a release endpoint")
    return f"https://api.github.com/repos/{match.group(1)}/{match.group(2)}/releases/latest"


def fetch(url: str, timeout: float = CHECK_TIMEOUT, accept: str = "*/*") -> bytes:
    """One anonymous GET: a fixed User-Agent, no credentials, no query string, no body, a bounded size."""
    outgoing = request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with request.urlopen(outgoing, timeout=timeout) as response:
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise HostError(f"{url} is larger than {MAX_DOWNLOAD >> 20} MB", "nothing was installed")
    return data


def latest_release() -> dict:
    """The public record reduced to a strictly newer stable release (with its asset links), or a named cause."""
    try:
        payload = json.loads(fetch(api_url(), accept="application/vnd.github+json"))
    except (HostError, OSError, ValueError) as failure:  # URLError, HTTPError and timeouts are OSErrors
        return {"status": "failed", "cause": f"{failure.__class__.__name__}: {failure}"}
    if not isinstance(payload, dict):
        return {"status": "failed", "cause": "the release endpoint did not return a JSON object"}
    tag = payload.get("tag_name")
    if payload.get("draft") or payload.get("prerelease") or parse_version(tag) is None:
        return {"status": "current", "version": None, "cause": f"the latest release {tag!r} is not a stable x.y.z release"}
    version = ".".join(map(str, parse_version(tag)))
    if not newer(__version__, version):
        return {"status": "current", "version": version}
    assets = {a["name"]: a["browser_download_url"] for a in payload.get("assets") or ()
              if isinstance(a, dict) and isinstance(a.get("name"), str) and isinstance(a.get("browser_download_url"), str)}
    return {"status": "update", "version": version, "notes": release_notes(payload.get("body")), "assets": assets}


# ---- the one cache file ---------------------------------------------------------------------------------------------

def cache_path() -> Path:
    override = os.environ.get("TRIMREAPER_UPDATE_CACHE", "").strip()
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CACHE_HOME", "").strip()
    root = Path(base) if base else Path.home() / ("Library/Caches" if sys.platform == "darwin" else ".cache")
    return root / TOOL / "update.json"


def write_private(path: Path, data: bytes) -> None:
    """Atomic 0600 replacement: a reader sees the old file or the new one, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + "-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_cache() -> dict:
    """Last check (checkedAt, status, version, cause), skip, last notice and the last automatic attempt."""
    try:
        found = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def write_cache(record: dict) -> None:
    write_private(cache_path(), (json.dumps(record, indent=2, sort_keys=True) + "\n").encode())


def stamp(record: dict, key: str) -> float:
    value = record.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def check() -> tuple[dict, dict]:
    """One request; the result, or the failure with its cause, is cached with its time."""
    release = latest_release()
    record = {**read_cache(), "checkedAt": time.time(), "status": release["status"], "version": release.get("version"),
              "cause": release.get("cause", "")}
    write_cache(record)
    return record, release


def pending(record: dict) -> bool:
    return record.get("status") == "update" and newer(__version__, record.get("version"))


# ---- settings (the config file) and when to stay silent -------------------------------------------------------------

def settings() -> dict:
    return {**DEFAULT_CONFIG["updates"], **(config().get("updates") or {})}


def save_settings(**changes: bool) -> dict:
    """Persist switches in the tool's own config file, so they survive an update."""
    path = config_path()
    if path.is_symlink():
        raise HostError(f"{path} is a symlink; the update settings write a regular file", "edit the file it points to by hand")
    stored = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(stored, dict):
        raise HostError(f"{path} must hold a JSON object")
    stored["updates"] = {**(stored.get("updates") or {}), **changes}
    check_config({**json.loads(json.dumps(DEFAULT_CONFIG)), **stored}, path)  # the whole result, before anything is written
    write_private(path, (json.dumps(stored, indent=2, sort_keys=True) + "\n").encode())
    clear_config_cache()
    return settings()


def interactive() -> bool:
    """Notices and background checks are for a person at a terminal: never redirected output, CI, agents, or opted out."""
    return (sys.stdout.isatty() and sys.stderr.isatty() and not os.environ.get("CI")
            and not any(os.environ.get(name) for name in AGENT_ENV)
            and os.environ.get("TRIMREAPER_NO_UPDATE_CHECK", "") in ("", "0") and settings()["checkAutomatically"])


def maybe_notice() -> bool:
    """Two lines on stderr after a command, at most once per version per 24 hours; never for a skipped version."""
    if not interactive():
        return False
    record, now = read_cache(), time.time()
    new = record.get("version")
    if not pending(record) or new == record.get("skippedVersion") \
            or (record.get("shownVersion") == new and now - stamp(record, "shownAt") < DAY):
        return False
    dim = ("", "") if os.environ.get("NO_COLOR") else ("\x1b[2m", "\x1b[0m")
    print(NOTICE[0].format(new=new), file=sys.stderr)
    print(dim[0] + NOTICE[1] + dim[1], file=sys.stderr)
    write_cache({**read_cache(), "shownVersion": new, "shownAt": now})
    return True


def after_command() -> None:
    """The notice, then a detached check when one is due: never on the command's critical path, never printed."""
    maybe_notice()
    record = read_cache()
    if interactive() and time.time() - max(stamp(record, "checkedAt"), stamp(record, "attemptedAt")) >= DAY:
        write_cache({**record, "attemptedAt": time.time()})  # at most one attempt a day, even if the check dies
        spawn_check()


def spawn_check() -> None:
    """A detached, silent `update --check --background`, run from the folder holding this package so that `-m` imports
    this copy and never one in the user's current folder."""
    subprocess.Popen([sys.executable, "-m", TOOL, "update", "--check", "--background"], cwd=Path(__file__).resolve().parents[1],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


# ---- install kinds, verified installs, rollback ---------------------------------------------------------------------

def install_kind() -> dict:
    """Who owns this copy: the release installer, pipx, uv, pip, or nobody (a source checkout)."""
    module = Path(__file__).resolve()
    if "site-packages" not in module.parts:
        return {"kind": "source"}
    venv = next((parent for parent in module.parents if (parent / "pyvenv.cfg").is_file()), None)
    if venv is not None and (venv / INSTALLER_MARK).is_file():
        bin_dir = (venv / INSTALLER_MARK).read_text(encoding="utf-8").strip()
        return {"kind": "installer", "home": venv.parent, "binDir": bin_dir}
    if "/pipx/venvs/" in str(module):
        return {"kind": "pipx", "command": ["pipx", "install", "--force"]}
    if "/uv/tools/" in str(module):
        return {"kind": "uv", "command": ["uv", "tool", "install", "--force"]}
    return {"kind": "pip", "command": [sys.executable, "-m", "pip", "install", "--upgrade", "--no-deps", "--no-index",
                                       "--disable-pip-version-check"]}


def tail(done: subprocess.CompletedProcess) -> str:
    return "\n".join((done.stderr or done.stdout or "").strip().splitlines()[-8:])[:1500]


def install(release: dict) -> dict:
    """Download into a temporary folder, verify against SHA256SUMS, then hand the verified file to the copy's owner:
    the release installer (which verifies the wheel again and swaps the venv atomically) or the package manager."""
    kind, version, assets = install_kind(), release["version"], release.get("assets") or {}
    if kind["kind"] == "source":
        raise HostError("this is a source checkout, not an installed copy", "update it with git, or install with scripts/install.sh")
    if "SHA256SUMS" not in assets:
        raise HostError(f"release {version} publishes no SHA256SUMS", "nothing was installed")
    sums = {name: digest.lower() for line in fetch(assets["SHA256SUMS"], DOWNLOAD_TIMEOUT).decode("utf-8", "replace").splitlines()
            for digest, name in SHA_LINE.findall(line)}
    name = "install.sh" if kind["kind"] == "installer" else f"{TOOL}-{version}-py3-none-any.whl"
    if name not in sums or name not in assets:
        raise HostError(f"release {version} has no {name} listed in SHA256SUMS", "nothing was installed")
    with tempfile.TemporaryDirectory(prefix=f"{TOOL}-update-") as folder:  # removed on success and on failure
        data = fetch(assets[name], DOWNLOAD_TIMEOUT)
        if hashlib.sha256(data).hexdigest() != sums[name]:
            raise HostError(f"checksum mismatch for {name}; nothing was installed", "retry later; the current version is unchanged")
        path = Path(folder) / name
        path.write_bytes(data)
        env = {key: value for key, value in os.environ.items() if not key.startswith("TRIMREAPER_")}
        if kind["kind"] == "installer":
            command = ["/bin/sh", str(path)]
            env.update(TRIMREAPER_HOME=str(kind["home"]), TRIMREAPER_RELEASE_BASE=assets["SHA256SUMS"].rsplit("/", 1)[0])
            if kind["binDir"]:
                env["TRIMREAPER_BIN_DIR"] = kind["binDir"]
        else:
            command = [*kind["command"], str(path)]
        try:
            # From the download folder: `python -m` must never import a copy sitting in the caller's folder.
            done = subprocess.run(command, cwd=folder, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  errors="replace", timeout=INSTALL_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as failure:
            raise HostError(f"{command[0]} could not finish: {failure}", "the current version is unchanged") from failure
    if done.returncode != 0:
        if kind["kind"] == "installer":
            raise HostError(f"install.sh exited {done.returncode}: {tail(done)}", "its last lines above say what was kept")
        raise HostError(f"{kind['kind']} exited {done.returncode}: {tail(done)}", "the current version is unchanged")
    return {"method": kind["kind"], "version": version, "output": tail(done)}


def installer_home() -> Path | None:
    kind = install_kind()
    return kind["home"] if kind["kind"] == "installer" else None


def newest_running() -> bool:
    """False in an older process still running after a swap: its module path now leads into the new venv."""
    return (Path(__file__).resolve().parents[1] / f"{TOOL}-{__version__}.dist-info").is_dir()


def rollback() -> dict:
    """Swap the one kept previous venv back in; the newer one becomes the rollback copy and its version is skipped."""
    home = installer_home()
    if home is None:
        raise HostError("rollback needs a copy installed by scripts/install.sh", "a package manager undoes its own installs")
    current, previous, staging = home / "venv", home / "venv.previous", home / f"venv.rollback.{os.getpid()}"
    if not previous.is_dir():
        raise HostError("no previous version is kept", "the rollback copy goes after the new version's first successful run")
    current.rename(staging)
    try:
        previous.rename(current)
    except OSError:
        staging.rename(current)
        raise
    staging.rename(previous)
    os.utime(previous)
    write_cache({**read_cache(), "skippedVersion": __version__})  # the version rolled back from is not offered again
    try:
        done = subprocess.run([str(current / "bin" / "python"), "-m", TOOL, "--version"], capture_output=True, text=True, timeout=30)
        restored = done.stdout.strip().removeprefix(f"{TOOL} ") or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        restored = "unknown (the restored copy did not report its version)"
    return {"status": "rolled-back", "from": __version__, "to": restored}


def prune(succeeded: bool) -> None:
    """The rollback copy goes after the new version's first successful command, or after seven days; staging leftovers
    of an interrupted install go after an hour."""
    home = installer_home()
    if home is None or not newest_running():
        return
    now = time.time()
    for path in [home / "venv.previous", *home.glob("venv.new.*"), *home.glob("venv.old.*"), *home.glob("venv.rollback.*")]:
        try:
            age = now - path.lstat().st_mtime
        except OSError:
            continue
        previous = path.name == "venv.previous"
        if (previous and succeeded) or age > (PREVIOUS_DAYS * DAY if previous else 3600):
            shutil.rmtree(path, ignore_errors=True)


def log(line: str) -> None:
    """Append to <stateDir>/update.log, keeping only its newest LOG_LIMIT bytes."""
    try:
        path = state_dir() / "update.log"
        kept = path.read_bytes() if path.is_file() else b""
        data = kept + f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line[:2000]}\n".encode()
        if len(data) > LOG_LIMIT:
            data = data[-LOG_LIMIT:]
            data = data[data.find(b"\n") + 1:]
        write_private(path, data)
    except (HostError, OSError):
        pass  # the log is a courtesy; the cache records the result either way


def auto_install(record: dict, release: dict) -> dict:
    """Opt-in only: one low-priority attempt per version from the detached check, logged; a failure keeps the notice."""
    version = record.get("version")
    if not pending(record) or not settings()["installAutomatically"] or record.get("autoTried") == version:
        return record
    record = {**read_cache(), "autoTried": version}
    write_cache(record)  # recorded first: an attempt that dies is not repeated
    if hasattr(os, "nice"):
        os.nice(10)
    try:
        result = install(release)
        record["autoResult"] = f"installed {version}; it applies on the next start"
        log(f"installed {version} ({result['method']}): {result['output']}")
    except Exception as failure:  # any failure is recorded, never retried in a loop, and the notice stays
        record["autoResult"] = f"failed: {failure}"
        log(f"failed {version}: {failure}")
    write_cache(record)
    return record


# ---- commands -------------------------------------------------------------------------------------------------------

def ask(question: str, default: bool) -> bool:
    """A question on stderr (stdout stays one JSON document); without a terminal the default stands."""
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return default
    try:
        print(question, file=sys.stderr, end="", flush=True)
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return default if not answer else answer.startswith("y")


def cmd_check(args) -> dict:
    record, release = check()
    out = {"status": record["status"], "current": __version__, "latest": record.get("version"),
           "notes": release.get("notes", ""), "cause": record.get("cause", ""), "cacheFile": str(cache_path())}
    if args.background:
        out["autoResult"] = auto_install(record, release).get("autoResult", "")
    return out


def check_exit(out: dict) -> int:
    return {"update": 10, "failed": 1}.get(out.get("status"), 0)


def cmd_skip(args) -> dict:
    record = read_cache()
    if not pending(record):
        record, _ = check()
    if not pending(record):
        raise HostError("there is no newer version to skip", f"`{TOOL} update --check` names the cause")
    write_cache({**record, "skippedVersion": record["version"]})
    return {"status": "skipped", "skippedVersion": record["version"], "current": __version__}


def cmd_auto(args) -> dict:
    current = settings() if args.auto == "status" else save_settings(installAutomatically=args.auto == "on")
    return {"installAutomatically": current["installAutomatically"], "checkAutomatically": current["checkAutomatically"],
            "current": __version__, "configFile": str(config_path())}


def cmd_update(args) -> dict:
    """Check now, show current -> new and the notes, ask on a terminal, install, then ask once about automatic installs."""
    record, release = check()
    if record["status"] == "failed":
        raise HostError(f"update check failed: {record['cause']}", f"`{TOOL} update --check` retries and reports")
    if record["status"] != "update":
        return {"status": "current", "current": __version__, "latest": record.get("version")}
    new, notes = record["version"], release.get("notes", "")
    summary = {"current": __version__, "latest": new, "notes": notes}
    if sys.stderr.isatty():
        print(f"{TOOL} {__version__} → {new}" + (f"\n{notes}" if notes else ""), file=sys.stderr)
    if not ask(f"Install {new} now? [Y/n] ", True):
        return {"status": "declined", **summary}
    result = install(release)
    current = settings()
    if not current["installAutomatically"] and not current["autoPrompted"] and sys.stdin.isatty() and sys.stderr.isatty():
        current = save_settings(autoPrompted=True, installAutomatically=ask("Install future updates automatically? [y/N] ", False))
    return {"status": "installed", **summary, **result, "installAutomatically": current["installAutomatically"]}
