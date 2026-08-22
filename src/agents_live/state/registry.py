"""User-level repository registry and read-only aggregate collectors."""
from __future__ import annotations

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
    from ..runtime.hosts import system as hostruntime
except ImportError:
    from runtime.hosts import system as hostruntime  # type: ignore[no-redef]


def _paths_module():
    """The paths module under either layout (see hostruntime above: this
    module is also imported flat by ``uv run --script`` dispatches)."""
    try:
        from .. import paths
    except ImportError:
        import paths
    return paths

def _adminlog():
    """Import lazily so registry reads do not initialize observability."""
    from ..obs import admin as adminlog
    return adminlog

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


def resolve_name(name: str, registry: dict | None = None) -> Path:
    """Resolve a registered alias. Pass an already-loaded *registry* to
    avoid a second read (see :func:`entries`)."""
    if registry is None:
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

    Without it, two concurrent repository registrations each rewrite the file
    from their own snapshot and the last rename silently drops the other
    repo."""
    with hostruntime.exclusive_lock(
        config_path().parent / ".config.lock", blocking=True,
    ):
        yield


def _write(registry: dict) -> None:
    lines = []
    if registry["default_repo"] is not None:
        lines.append(f"default_repo = {json.dumps(registry['default_repo'])}")
    lines.append("")
    lines.append("[repos]")
    for alias, value in sorted(registry["repos"].items()):
        lines.append(f"{json.dumps(alias)} = {json.dumps(value)}")
    _paths_module().atomic_write_text(
        config_path(), "\n".join(lines) + "\n", mode=0o600)


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


def _add(value: str) -> Path:
    with _registry_lock():
        registry = load()
        name = _register_path(registry, value)
        _write(registry)
    root = registry["repos"][name]
    _adminlog().record("repo-register", repo=name, root=root)
    return Path(root)


def ensure_registered(value: str | Path) -> bool:
    """Register *value* once; return True when the registry changed."""
    path = str(Path(value).expanduser().resolve())
    with _registry_lock():
        registry = load()
        if path in registry["repos"].values():
            return False
        name = _register_path(registry, path)
        _write(registry)
    _adminlog().record("repo-register", repo=name, root=path)
    return True


def ensure_default(value: str | Path) -> bool:
    """Register *value* and select it as default in one locked update."""
    path = str(Path(value).expanduser().resolve())
    with _registry_lock():
        registry = load()
        name = next(
            (name for name, registered in registry["repos"].items()
             if registered == path),
            None,
        )
        registered = name is None
        if name is None:
            name = _register_path(registry, path)
        if registry["default_repo"] == name:
            return False
        registry["default_repo"] = name
        _write(registry)
    if registered:
        _adminlog().record("repo-register", repo=name, root=path)
    _adminlog().record("repo-default", repo=name, root=path)
    return True


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


def _set_default(ref: str) -> Path:
    registered = False
    with _registry_lock():
        registry = load()
        try:
            name = _resolve_ref(registry, ref)
        except ValueError as exc:
            if not Path(ref).expanduser().resolve().is_dir():
                raise exc
            name = _register_path(registry, ref)
            registered = True
        _validated_path(registry["repos"][name], name)
        registry["default_repo"] = name
        _write(registry)
    root = registry["repos"][name]
    if registered:
        _adminlog().record("repo-register", repo=name, root=root)
    _adminlog().record("repo-default", repo=name, root=root)
    return Path(root)


def _remove(ref: str) -> None:
    with _registry_lock():
        registry = load()
        name = _resolve_ref(registry, ref)
        # A default only means something when there is a choice to make.
        # Guard the removal while another entry could inherit the role;
        # when this is the last one, clear the default and let the
        # registry go empty, which is the only way back out of the state
        # a first `init --repo` creates.
        if registry["default_repo"] == name:
            if len(registry["repos"]) > 1:
                raise ValueError(
                    f"repo {name!r} is the default; choose another default first")
            registry["default_repo"] = None
        root = registry["repos"][name]
        del registry["repos"][name]
        _write(registry)
    _adminlog().record("repo-remove", repo=name, root=root)


def _clear_default() -> bool:
    with _registry_lock():
        registry = load()
        if registry["default_repo"] is None:
            return False
        registry["default_repo"] = None
        _write(registry)
    _adminlog().record("repo-default-clear")
    return True


CLI_ENV_VAR = "AGENTS_LIVE_CLI"


def _environment_shim() -> Path | None:
    """The ``agents-live`` entry point of the environment running this code.

    Anchored on ``pyvenv.cfg`` so only a real environment root answers;
    a bare ``~/bin`` on the way up is not one.
    """
    filename = hostruntime.executable_filename("agents-live")
    beside = Path(sys.executable).with_name(filename)
    if beside.is_file():
        return beside.resolve()
    package = Path(__file__).resolve().parents[1]
    for parent in package.parents[:4]:
        if not (parent / "pyvenv.cfg").is_file():
            continue
        for directory in ("bin", "Scripts"):
            candidate = parent / directory / filename
            if candidate.is_file():
                return candidate.resolve()
    return None


def cli_base() -> list[str]:
    """argv prefix for spawning the CLI in a child process.

    ``find_spec`` cannot answer this. A ``uv run --script`` dispatch such
    as the dashboard puts the package directory on its own ``sys.path``,
    so the check succeeds in-process while a child interpreter in that
    isolated environment has no ``agents_live`` to import (#288). What a
    child can run is the entry point of the environment that provides
    the package, or an explicit prefix handed down by the dispatching
    CLI - which is the only form that stays on the source tree when the
    caller was itself editable.
    """
    declared = os.environ.get(CLI_ENV_VAR)
    if declared:
        try:
            argv = json.loads(declared)
        except json.JSONDecodeError:
            argv = None
        if isinstance(argv, list) and argv and all(
                isinstance(item, str) for item in argv):
            return list(argv)
    shim = _environment_shim()
    if shim is not None:
        return [str(shim)]
    return [sys.executable, "-m", "agents_live.cli"]


def _child_json(alias: str, path: str, command: str) -> dict:
    completed = subprocess.run(
        [*cli_base(), "--repo", path, command, "--json"],
        capture_output=True, check=False, **hostruntime.CHILD_TEXT,
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
            [*cli_base(), "--json", "doctor"],
            cwd=empty, env=env, capture_output=True, check=False,
            **hostruntime.CHILD_TEXT,
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
