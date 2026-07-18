"""User-level repository registry and read-only aggregate collectors."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def config_path() -> Path:
    """Return the XDG user configuration path."""
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    config_home = Path(base).expanduser() if base else Path.home() / ".config"
    return config_home / "agents-live" / "config.toml"


def load() -> dict:
    """Load and structurally validate the registry without requiring paths to exist."""
    path = config_path()
    if not path.exists():
        return {"repos": {}, "default_repo": None}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"repository registry unreadable: {path}: {exc}") from exc
    repos = data.get("repos", {})
    default = data.get("default_repo")
    if not isinstance(repos, dict):
        raise ValueError(f"repository registry {path}: [repos] must be a table")
    normalized: dict[str, str] = {}
    for alias, value in repos.items():
        if not isinstance(alias, str) or not _ALIAS.fullmatch(alias):
            raise ValueError(f"repository registry {path}: invalid alias {alias!r}")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"repository registry {path}: path for {alias!r} must be a string")
        repo = Path(value).expanduser()
        if not repo.is_absolute():
            raise ValueError(
                f"repository registry {path}: path for {alias!r} must be absolute")
        normalized[alias] = str(repo.resolve())
    if default is not None and (
            not isinstance(default, str) or default not in normalized):
        raise ValueError(
            f"repository registry {path}: default_repo must name a registered repo")
    return {"repos": normalized, "default_repo": default}


def resolve_alias(alias: str) -> Path:
    registry = load()
    if alias not in registry["repos"]:
        raise ValueError(
            f"repo alias {alias!r} is not registered; run `agents-live repos list`")
    return _validated_path(registry["repos"][alias], alias)


def default_root() -> Path | None:
    registry = load()
    alias = registry["default_repo"]
    return None if alias is None else _validated_path(registry["repos"][alias], alias)


def entries() -> list[tuple[str, str, str | None]]:
    """Return alias/path/error rows, preserving unavailable repositories."""
    registry = load()
    rows = []
    for alias, value in sorted(registry["repos"].items()):
        try:
            path = str(_validated_path(value, alias))
            error = None
        except ValueError as exc:
            path, error = value, str(exc)
        rows.append((alias, path, error))
    return rows


def _validated_path(value: str | Path, alias: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(
            f"registered repo {alias!r} is not an existing directory: {path}")
    return path


def _write(registry: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if registry["default_repo"] is not None:
        lines.append(f"default_repo = {json.dumps(registry['default_repo'])}")
    lines.append("")
    lines.append("[repos]")
    for alias, value in sorted(registry["repos"].items()):
        lines.append(f"{json.dumps(alias)} = {json.dumps(value)}")
    content = "\n".join(lines) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".config.", dir=path.parent, text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _add(alias: str, value: str) -> None:
    if not _ALIAS.fullmatch(alias):
        raise ValueError(
            "repo alias must start with an alphanumeric character and contain "
            "only letters, numbers, '.', '_', or '-'")
    registry = load()
    if alias in registry["repos"]:
        raise ValueError(f"repo alias {alias!r} is already registered")
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"repo path is not an existing directory: {path}")
    registry["repos"][alias] = str(path)
    _write(registry)


def _set_default(alias: str) -> None:
    registry = load()
    if alias not in registry["repos"]:
        raise ValueError(f"repo alias {alias!r} is not registered")
    _validated_path(registry["repos"][alias], alias)
    registry["default_repo"] = alias
    _write(registry)


def _remove(alias: str) -> None:
    registry = load()
    if alias not in registry["repos"]:
        raise ValueError(f"repo alias {alias!r} is not registered")
    if registry["default_repo"] == alias:
        raise ValueError(
            f"repo alias {alias!r} is the default; choose another default first")
    del registry["repos"][alias]
    _write(registry)


def _child_json(alias: str, path: str, command: str) -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", "agents_live.cli", "--repo", path, command, "--json"],
        capture_output=True, text=True, check=False,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        detail = completed.stderr.strip() or completed.stdout.strip() or (
            f"{command} exited {completed.returncode}")
        return {"name": alias, "path": path, "ok": False, "error": detail}
    return {
        "name": alias, "path": path, "ok": completed.returncode == 0,
        "result": payload,
    }


def collect_status() -> dict:
    results = []
    for alias, path, error in entries():
        if error:
            results.append({"name": alias, "path": path, "ok": False, "error": error})
            continue
        item = _child_json(alias, path, "status")
        if "result" in item:
            for agent in item["result"].get("agents", []):
                agent["repo"] = alias
                agent["repoPath"] = path
                agent["name"] = f"{alias}/{agent['name']}"
        results.append(item)
    return {"ok": all(item["ok"] for item in results), "repos": results}


def collect_doctor() -> dict:
    with tempfile.TemporaryDirectory() as empty:
        env = os.environ.copy()
        env.pop("AGENTS_LIVE_REPO", None)
        env["XDG_CONFIG_HOME"] = empty
        host_run = subprocess.run(
            [sys.executable, "-m", "agents_live.cli", "--json", "doctor"],
            cwd=empty, env=env, capture_output=True, text=True, check=False,
        )
    try:
        host = json.loads(host_run.stdout)
    except json.JSONDecodeError:
        host = {"ok": False, "error": host_run.stderr.strip() or host_run.stdout.strip()}
    host_names = {check["name"] for check in host.get("checks", [])}
    results = []
    for alias, path, error in entries():
        if error:
            results.append({"name": alias, "path": path, "ok": False, "error": error})
            continue
        item = _child_json(alias, path, "doctor")
        if "result" in item:
            item["result"]["checks"] = [
                check for check in item["result"].get("checks", [])
                if check.get("name") not in host_names
            ]
        results.append(item)
    ok = bool(host.get("ok")) and all(item["ok"] for item in results)
    return {"ok": ok, "host": host, "repos": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage registered repositories")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("list", help="List registered repositories")
    add = subparsers.add_parser("add", help="Register a repository")
    add.add_argument("alias")
    add.add_argument("path")
    default = subparsers.add_parser("default", help="Set the fallback repository")
    default.add_argument("alias")
    remove = subparsers.add_parser("remove", help="Remove a registered repository")
    remove.add_argument("alias")
    args = parser.parse_args(argv)
    try:
        if args.action == "add":
            _add(args.alias, args.path)
        elif args.action == "default":
            _set_default(args.alias)
        elif args.action == "remove":
            _remove(args.alias)
        else:
            registry = load()
            if not registry["repos"]:
                print("No repositories registered")
            for alias, path, error in entries():
                marker = " (default)" if alias == registry["default_repo"] else ""
                suffix = f" [unavailable: {error}]" if error else ""
                print(f"{alias}{marker}\t{path}{suffix}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
