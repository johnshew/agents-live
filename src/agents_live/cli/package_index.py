"""Read-only safety checks for the package index uv is configured to use."""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ..runtime.spawn import find_uv

_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def _version(value: str | None) -> tuple[int, int, int] | None:
    match = _VERSION.fullmatch(value or "")
    return tuple(int(item) for item in match.groups()) if match else None


@dataclass(frozen=True)
class IndexCheck:
    ok: bool
    required: str
    resolved: str | None
    detail: str


def configured() -> bool:
    """Whether uv has an explicit non-public default package index."""
    value = os.environ.get("UV_DEFAULT_INDEX") or os.environ.get("UV_INDEX_URL")
    if value:
        return "pypi.org" not in value.casefold()
    for path in _config_paths():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            continue
        tables = [data]
        tool = data.get("tool") if isinstance(data, dict) else None
        if isinstance(tool, dict) and isinstance(tool.get("uv"), dict):
            tables.insert(0, tool["uv"])
        for table in tables:
            default = table.get("default-index")
            if default:
                return "pypi.org" not in str(default).casefold()
            indexes = table.get("index", [])
            for item in indexes if isinstance(indexes, list) else []:
                if isinstance(item, dict) and item.get("default") is True:
                    url = str(item.get("url", ""))
                    return bool(url) and "pypi.org" not in url.casefold()
    return False


def check(
    installed: str,
    *,
    latest: str | None = None,
    command: tuple[str, ...] | None = None,
    timeout: float = 120,
) -> IndexCheck:
    """Ask uv's configured resolver for a non-downgrading package version."""
    installed_version = _version(installed)
    latest_version = _version(latest)
    if installed_version is None:
        return IndexCheck(False, installed, None, "installed version is not stable semver")
    required = latest if latest_version and latest_version > installed_version else installed
    assert required is not None
    try:
        prefix = command or (find_uv(),)
    except FileNotFoundError as exc:
        return IndexCheck(False, required, None, f"configured index check failed: {exc}")
    with tempfile.TemporaryDirectory(prefix="agents-live-index-check-") as temporary:
        directory = Path(temporary)
        requirements = directory / "requirements.in"
        output = directory / "requirements.txt"
        requirements.write_text(f"agents-live>={required}\n", encoding="utf-8")
        try:
            completed = subprocess.run(
                [
                    *prefix,
                    "pip",
                    "compile",
                    str(requirements),
                    "--output-file",
                    str(output),
                    "--no-header",
                    "--no-annotate",
                    "--refresh",
                    "--no-cache",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return IndexCheck(False, required, None, f"configured index check failed: {exc}")
        if completed.returncode:
            lines = completed.stderr.strip().splitlines()
            reason = lines[-1] if lines else f"uv exited {completed.returncode}"
            return IndexCheck(
                False, required, None,
                f"configured index cannot supply agents-live>={required}: {reason}",
            )
        try:
            lines = output.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            return IndexCheck(False, required, None, f"resolver output is unreadable: {exc}")
    resolved = next((line.split("==", 1)[1].strip() for line in lines
                     if line.startswith("agents-live==")), None)
    resolved_version = _version(resolved)
    required_version = _version(required)
    if resolved_version is None or required_version is None or resolved_version < required_version:
        return IndexCheck(
            False, required, resolved,
            f"configured index resolved {resolved or 'no version'}; "
            f"agents-live>={required} is required",
        )
    return IndexCheck(
        True, required, resolved,
        f"configured index resolves agents-live {resolved} (required: >={required})",
    )


def _config_paths() -> tuple[Path, ...]:
    explicit = os.environ.get("UV_CONFIG_FILE")
    if explicit:
        return (Path(explicit).expanduser(),)
    found: list[Path] = []
    for parent in (Path.cwd(), *Path.cwd().parents):
        found.extend((parent / "uv.toml", parent / "pyproject.toml"))
    appdata = os.environ.get("APPDATA")
    if appdata:
        found.append(Path(appdata) / "uv" / "uv.toml")
    xdg = os.environ.get("XDG_CONFIG_HOME")
    found.append(Path(xdg).expanduser() / "uv" / "uv.toml" if xdg
                 else Path.home() / ".config" / "uv" / "uv.toml")
    return tuple(dict.fromkeys(found))


__all__ = ["IndexCheck", "check", "configured"]