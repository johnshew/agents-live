"""Best-effort, cached PyPI release checks for interactive CLI use."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import __version__

CACHE_INTERVAL = 60 * 60  # Check hourly so available releases are reported promptly.
NETWORK_TIMEOUT = 1.0
PYPI_URL = "https://pypi.org/pypi/agents-live/json"
# Stable SemVer only: the absent ``-prerelease`` production deliberately
# rejects alpha, beta, and release-candidate metadata.
_STABLE_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:\+[0-9A-Za-z.-]+)?$"
)


def cache_path() -> Path:
    root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return root / "agents-live" / "update-check.json"


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty()


def _version(value: object) -> tuple[int, int, int] | None:
    match = _STABLE_SEMVER.fullmatch(str(value))
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _read_cache() -> dict[str, Any] | None:
    try:
        value = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("checked_at"), (int, float)):
        return None
    return value


def _write_cache(value: dict[str, Any]) -> bool:
    path = cache_path()
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
        return True
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def cached_result() -> dict[str, Any] | None:
    return _read_cache()


def _is_fresh(cache: dict[str, Any] | None, now: float) -> bool:
    return cache is not None and now - float(cache["checked_at"]) < CACHE_INTERVAL


def refresh(
    *,
    now: float | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any] | None:
    """Refresh the cache synchronously. All failures are recorded and suppressed."""
    checked_at = time.time() if now is None else now
    result: dict[str, Any] = {
        "checked_at": checked_at,
        "latest_version": None,
    }
    try:
        request = urllib.request.Request(
            PYPI_URL, headers={"Accept": "application/json", "User-Agent": "agents-live"}
        )
        with opener(request, timeout=NETWORK_TIMEOUT) as response:
            metadata = json.load(response)
        candidates = [metadata.get("info", {}).get("version")]
        releases = metadata.get("releases", {})
        if isinstance(releases, dict):
            candidates.extend(releases)
        stable_versions = [(parsed, str(value)) for value in candidates
                           if (parsed := _version(value)) is not None]
        if not stable_versions:
            raise ValueError("no stable semantic version in PyPI metadata")
        result["latest_version"] = max(stable_versions)[1]
    except Exception as exc:
        result["error"] = type(exc).__name__
    return result if _write_cache(result) else None


def launch_if_stale(*, now: float | None = None) -> None:
    """Start a detached refresh without delaying the requested command."""
    current = time.time() if now is None else now
    if _is_fresh(_read_cache(), current):
        return
    try:
        subprocess.Popen(
            [sys.executable, "-m", __name__],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def consume_notice(installed: str = __version__, *, now: float | None = None) -> str | None:
    current = time.time() if now is None else now
    cache = _read_cache()
    if not _is_fresh(cache, current):
        return None
    assert cache is not None
    latest = cache.get("latest_version")
    installed_semver = _version(installed)
    latest_semver = _version(latest)
    if (
        installed_semver is None
        or latest_semver is None
        or latest_semver <= installed_semver
        or cache.get("notified_for") == cache["checked_at"]
    ):
        return None
    cache["notified_for"] = cache["checked_at"]
    if not _write_cache(cache):
        return None
    return (
        f"agents-live {installed} is installed; {latest} is available.\n"
        "Upgrade with: agents-live upgrade"
    )


def status_text(installed: str = __version__) -> str:
    cache = _read_cache()
    if cache is None:
        return "Update check: never completed"
    latest = cache.get("latest_version")
    installed_semver = _version(installed)
    latest_semver = _version(latest)
    if latest_semver is None:
        return f"Update check: last attempt failed ({cache.get('error', 'invalid metadata')})"
    if installed_semver is not None and latest_semver > installed_semver:
        return (
            f"Update check: agents-live {installed} installed; {latest} available\n"
            "  Upgrade with: agents-live upgrade"
        )
    return f"Update check: agents-live {installed} is current (latest: {latest})"


def _background_refresh() -> int:
    refresh()
    return 0


if __name__ == "__main__":
    raise SystemExit(_background_refresh())
