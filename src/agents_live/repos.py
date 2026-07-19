"""User-level repository registry and read-only aggregate collectors."""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    from . import preflight
except ImportError:
    import preflight

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_COLLECT_WORKERS = 4


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
    for name, value in repos.items():
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError(f"repository registry {path}: invalid repo name {name!r}")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"repository registry {path}: path for {name!r} must be a string")
        repo = Path(value).expanduser()
        if not repo.is_absolute():
            raise ValueError(
                f"repository registry {path}: path for {name!r} must be absolute")
        normalized[name] = str(repo.resolve())
    if default is not None and (
            not isinstance(default, str) or default not in normalized):
        raise ValueError(
            f"repository registry {path}: default_repo must name a registered repo")
    return {"repos": normalized, "default_repo": default}


def resolve_name(name: str) -> Path:
    registry = load()
    if name not in registry["repos"]:
        raise ValueError(
            f"repo {name!r} is not registered; run `agents-live repos list`")
    return _validated_path(registry["repos"][name], name)


def default_root() -> Path | None:
    registry = load()
    alias = registry["default_repo"]
    return None if alias is None else _validated_path(registry["repos"][alias], alias)


def entries(registry: dict | None = None) -> list[tuple[str, str, str | None]]:
    """Return name/path/error rows, preserving unavailable repositories.

    Pass an already-loaded *registry* to avoid a second read (two reads
    can observe different file states if another process writes between
    them)."""
    if registry is None:
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


@contextmanager
def _registry_lock() -> Iterator[None]:
    """Serialize load-modify-write registry mutations across processes.

    Without it, two concurrent ``repos add`` calls each rewrite the file
    from their own snapshot and the last rename silently drops the other
    repo."""
    lock_path = config_path().parent / ".config.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


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


def _register_path(registry: dict, value: str) -> str:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"repo path is not an existing directory: {path}")
    name = path.name
    if not _NAME.fullmatch(name):
        raise ValueError(
            f"cannot register {path}: the directory name must start with an "
            "alphanumeric character and contain only letters, numbers, "
            "'.', '_', or '-'")
    for existing, registered in registry["repos"].items():
        if registered == str(path):
            raise ValueError(f"{path} is already registered as {existing!r}")
    if name in registry["repos"]:
        raise ValueError(
            f"a repo named {name!r} is already registered "
            f"({registry['repos'][name]}); remove it first")
    registry["repos"][name] = str(path)
    return name


def _add(value: str) -> None:
    with _registry_lock():
        registry = load()
        _register_path(registry, value)
        _write(registry)


def _resolve_ref(registry: dict, ref: str) -> str:
    """Map a registered name or repository path to its registry name."""
    if ref in registry["repos"]:
        return ref
    candidate = str(Path(ref).expanduser().resolve())
    for name, value in registry["repos"].items():
        if value == candidate:
            return name
    raise ValueError(
        f"{ref!r} is not a registered repository path or name; "
        "run `agents-live repos list`")


def _set_default(ref: str) -> None:
    with _registry_lock():
        registry = load()
        try:
            name = _resolve_ref(registry, ref)
        except ValueError as exc:
            if not Path(ref).expanduser().resolve().is_dir():
                raise exc
            name = _register_path(registry, ref)
        _validated_path(registry["repos"][name], name)
        registry["default_repo"] = name
        _write(registry)


def _remove(ref: str) -> None:
    with _registry_lock():
        registry = load()
        name = _resolve_ref(registry, ref)
        if registry["default_repo"] == name:
            raise ValueError(
                f"repo {name!r} is the default; choose another default first")
        del registry["repos"][name]
        _write(registry)


def _cli_base() -> list[str]:
    """argv prefix for spawning the CLI in a child process.

    The module form matches the code currently running, but only works
    where ``agents_live`` is importable; the dashboard runs from an
    isolated ``uv run --script`` environment where it is not, so fall
    back to the installed shim there.
    """
    if importlib.util.find_spec("agents_live") is not None:
        return [sys.executable, "-m", "agents_live.cli"]
    shim = shutil.which("agents-live") or str(
        Path.home() / ".local" / "bin" / "agents-live")
    return [shim]


def _child_json(alias: str, path: str, command: str) -> dict:
    completed = subprocess.run(
        [*_cli_base(), "--repo", path, command, "--json"],
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


def _collect_children(command: str) -> list[dict]:
    """One child result per registered repo, in registry order.

    Children are independent read-only subprocesses; running them
    concurrently keeps ``--all-repos`` latency at the slowest child
    instead of the sum."""
    rows = entries()

    def one(alias: str, path: str) -> dict:
        try:
            return _child_json(alias, path, command)
        except Exception as exc:  # noqa: BLE001 - isolate per-repo failures
            # A child that cannot even launch (missing shim, fork
            # failure) is that repository's error row, never a reason to
            # abort the whole aggregate.
            return {"name": alias, "path": path, "ok": False,
                    "error": f"{type(exc).__name__}: {exc}"}

    with ThreadPoolExecutor(max_workers=_COLLECT_WORKERS) as pool:
        futures = {
            alias: pool.submit(one, alias, path)
            for alias, path, error in rows if not error
        }
        results = []
        for alias, path, error in rows:
            if error:
                results.append(
                    {"name": alias, "path": path, "ok": False, "error": error})
            else:
                results.append(futures[alias].result())
    return results


def collect_status() -> dict:
    results = _collect_children("status")
    for item in results:
        if "result" in item:
            for agent in item["result"].get("agents", []):
                agent["repo"] = item["name"]
                agent["repoPath"] = item["path"]
                agent["name"] = f"{item['name']}/{agent['name']}"
    return {"ok": all(item["ok"] for item in results), "repos": results}


def collect_doctor() -> dict:
    with tempfile.TemporaryDirectory() as empty:
        env = os.environ.copy()
        env.pop("AGENTS_LIVE_REPO", None)
        env["XDG_CONFIG_HOME"] = empty
        host_run = subprocess.run(
            [*_cli_base(), "--json", "doctor"],
            cwd=empty, env=env, capture_output=True, text=True, check=False,
        )
    try:
        host = json.loads(host_run.stdout)
    except json.JSONDecodeError:
        host = {"ok": False, "error": host_run.stderr.strip() or host_run.stdout.strip()}
    host_names = {check["name"] for check in host.get("checks", [])}
    results = _collect_children("doctor")
    for item in results:
        if "result" in item:
            item["result"]["checks"] = [
                check for check in item["result"].get("checks", [])
                if check.get("name") not in host_names
            ]
    ok = bool(host.get("ok")) and all(item["ok"] for item in results)
    return {"ok": ok, "host": host, "repos": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage registered repositories")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("list", help="List registered repositories")
    add = subparsers.add_parser("add", help="Register a repository")
    add.add_argument(
        "path",
        help="Repository root directory (registered under its directory name)")
    default = subparsers.add_parser(
        "default", help="Set the fallback repository, registering a path if needed")
    default.add_argument("repo", help="Repository path or registered directory name")
    remove = subparsers.add_parser("remove", help="Remove a registered repository")
    remove.add_argument("repo", help="Registered repository path or name")
    subparsers.add_parser("help", help="Show this help message")
    args = parser.parse_args(argv)
    try:
        if args.action == "help":
            parser.print_help()
        elif args.action == "add":
            from . import plugins  # noqa: PLC0415
            root = Path(args.path).expanduser().resolve()
            pending = [
                name for name, ok, _ in plugins.checks(root) if not ok
            ]
            _add(args.path)
            if pending:
                print(
                    f"Declared plugin(s) not installed: {', '.join(pending)}; "
                    "will be installed on init/start/upgrade")
        elif args.action == "default":
            _set_default(args.repo)
        elif args.action == "remove":
            _remove(args.repo)
        else:
            registry = load()
            if preflight.json_mode():
                print(json.dumps({
                    "ok": True,
                    "repositories": [
                        {
                            "name": alias,
                            "path": path,
                            "default": alias == registry["default_repo"],
                            "available": error is None,
                            "error": error,
                        }
                        for alias, path, error in entries(registry)
                    ],
                }))
            elif not registry["repos"]:
                print("No repositories registered")
            for alias, path, error in (
                    [] if preflight.json_mode() else entries(registry)):
                marker = " (default)" if alias == registry["default_repo"] else ""
                suffix = f" [unavailable: {error}]" if error else ""
                print(f"{alias}{marker}\t{path}{suffix}")
        return 0
    except (OSError, ValueError) as exc:
        preflight.emit_failure("repos", str(exc))
        return 1
