"""Project-declared plugin inspection and uv tool-environment convergence."""
from __future__ import annotations

import hashlib
import importlib.metadata
import re
import subprocess
import sys
import tomllib
import zipfile
from dataclasses import dataclass, replace
from email.parser import BytesParser
from pathlib import Path

from . import paths
from .spawn import find_uv

# Kernel extension points a declared distribution must provide.
ENTRY_POINT_GROUPS = frozenset({"agents_live.agents", "agents_live.ownership"})


class PluginError(RuntimeError):
    """A plugin declaration cannot be safely resolved or installed."""


@dataclass(frozen=True)
class Plugin:
    """Resolved declaration; version is unknown while its wheel is absent."""
    name: str
    path: Path
    sha256: str | None
    version: str | None
    metadata_error: str | None = None


@dataclass(frozen=True)
class ReceiptRequirement:
    value: str
    editable: bool = False


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _wheel_identity(path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as wheel:
            metadata_names = [
                name for name in wheel.namelist()
                if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise PluginError(
                    f"plugin wheel must contain exactly one METADATA file: {path}")
            metadata = BytesParser().parsebytes(wheel.read(metadata_names[0]))
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise PluginError(f"plugin wheel is unreadable: {path}: {exc}") from exc
    name, version = metadata.get("Name"), metadata.get("Version")
    if not name or not version:
        raise PluginError(f"plugin wheel has incomplete metadata: {path}")
    return name, version


_sha256_cache: dict[tuple[str, int, int], str] = {}


def _sha256(path: Path) -> str:
    """Digest of *path*, memoized per process on (path, size, mtime) so
    one command never hashes the same unchanged wheel twice."""
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    digest = _sha256_cache.get(key)
    if digest is None:
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        _sha256_cache[key] = digest
    return digest


def declared(root: Path, *, require_exists: bool = False) -> dict[str, Plugin]:
    """Resolve declarations, retaining configured names for absent wheels."""
    declarations = paths.validated_plugins(
        root, paths.load_config(root).get("plugins", {}),
        require_exists=require_exists)
    result = {}
    for configured_name, declaration in declarations.items():
        wheel_name = configured_name
        version = None
        metadata_error = None
        if declaration["path"].is_file():
            try:
                wheel_name, version = _wheel_identity(declaration["path"])
                if _canonical(configured_name) != _canonical(wheel_name):
                    raise PluginError(
                        f"plugin {configured_name!r} wheel declares distribution "
                        f"{wheel_name!r}: {declaration['path']}")
            except PluginError as exc:
                if require_exists:
                    raise
                wheel_name = configured_name
                version = None
                metadata_error = str(exc)
        key = _canonical(configured_name)
        result[key] = Plugin(
            name=wheel_name,
            path=declaration["path"],
            sha256=declaration["sha256"],
            version=version,
            metadata_error=metadata_error,
        )
    return result


def union(roots: list[Path], *, require_exists: bool = False) -> dict[str, Plugin]:
    """Combine declarations, preferring available wheel metadata."""
    result = {}
    for root in roots:
        for key, plugin in declared(
                root, require_exists=require_exists).items():
            previous = result.get(key)
            if previous is not None:
                if (
                    previous.sha256 is not None
                    and plugin.sha256 is not None
                    and previous.sha256.lower() != plugin.sha256.lower()
                ):
                    raise PluginError(
                        f"conflicting sha256 declarations for plugin "
                        f"{plugin.name!r}: {previous.path} and {plugin.path}")
                merged_sha256 = previous.sha256 or plugin.sha256
                if previous.version is None and plugin.version is None:
                    selected = (
                        plugin
                        if not previous.path.is_file() and plugin.path.is_file()
                        else previous
                    )
                    result[key] = replace(selected, sha256=merged_sha256)
                    continue
                if previous.version is None:
                    result[key] = replace(plugin, sha256=merged_sha256)
                    continue
                if plugin.version is None:
                    result[key] = replace(previous, sha256=merged_sha256)
                    continue
                try:
                    same_artifact = (
                        previous.version == plugin.version
                        and _sha256(previous.path) == _sha256(plugin.path)
                    )
                except OSError as exc:
                    raise PluginError(
                        f"cannot compare plugin declarations: {exc}") from exc
                if not same_artifact:
                    raise PluginError(
                        f"conflicting declarations for plugin {plugin.name!r}: "
                        f"{previous.path} and {plugin.path}")
                result[key] = replace(previous, sha256=merged_sha256)
                continue
            result[key] = plugin
    return result


def _integrity_error(plugin: Plugin) -> str | None:
    if plugin.sha256 is None:
        return None
    try:
        actual = _sha256(plugin.path)
    except OSError as exc:
        return f"cannot hash {plugin.path}: {exc}"
    if actual.lower() != plugin.sha256.lower():
        return f"sha256 mismatch for {plugin.path}"
    return None


def inspect(plugin: Plugin) -> tuple[bool, str]:
    if plugin.path.is_file():
        integrity_error = _integrity_error(plugin)
        if integrity_error:
            return False, integrity_error
    return _installed_state(plugin)


def _installed_state(plugin: Plugin) -> tuple[bool, str]:
    """Installed-environment convergence, without artifact integrity."""
    try:
        distribution = importlib.metadata.distribution(plugin.name)
    except importlib.metadata.PackageNotFoundError:
        return False, f"distribution {plugin.name} is not installed"
    if plugin.version is not None and distribution.version != plugin.version:
        return False, (
            f"installed version {distribution.version}, declared wheel "
            f"version {plugin.version}")
    entry_points = [
        ep for ep in distribution.entry_points if ep.group in ENTRY_POINT_GROUPS
    ]
    if not entry_points:
        return False, "distribution exposes no agents-live entry points"
    for entry_point in entry_points:
        try:
            entry_point.load()
        except Exception as exc:
            return False, (
                f"entry point {entry_point.group}:{entry_point.name} failed: {exc}")
    return True, (
        f"version {distribution.version}; entry points "
        + ", ".join(f"{ep.group}:{ep.name}" for ep in entry_points))


def checks(root: Path, *, require_exists: bool = True) -> list[tuple[str, bool, str]]:
    declarations = declared(root, require_exists=require_exists)
    invalid = next(
        (plugin.metadata_error for plugin in declarations.values()
         if plugin.metadata_error),
        None,
    )
    if invalid:
        raise PluginError(invalid)
    return [
        (plugin.name, *inspect(plugin))
        for plugin in declarations.values()
    ]


def _receipt_path() -> Path | None:
    candidate = Path(sys.prefix) / "uv-receipt.toml"
    return candidate if candidate.is_file() else None


def _receipt_requirement(requirement: dict) -> str:
    """Reconstruct a uv receipt requirement as a PEP 508/path argument."""
    for field in ("path", "directory", "url"):
        if field in requirement:
            return str(requirement[field])
    name = requirement.get("name")
    if not isinstance(name, str):
        raise PluginError("uv receipt contains a requirement without a name")
    if "git" in requirement:
        return f"{name} @ git+{requirement['git']}"
    extras = requirement.get("extras", [])
    if extras:
        name += "[" + ",".join(extras) + "]"
    name += str(requirement.get("specifier", ""))
    marker = requirement.get("marker")
    if marker:
        name += f"; {marker}"
    return name


def _receipt_requirements() -> tuple[
        ReceiptRequirement, dict[str, ReceiptRequirement]]:
    receipt = _receipt_path()
    if receipt is None:
        raise PluginError(
            "plugin convergence requires an uv tool installation of agents-live; "
            "run `uv tool install agents-live`, then retry")
    try:
        with receipt.open("rb") as handle:
            requirements = tomllib.load(handle)["tool"]["requirements"]
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PluginError(f"uv tool receipt is unreadable: {receipt}: {exc}") from exc
    except (KeyError, TypeError) as exc:
        raise PluginError(
            f"uv tool receipt has no valid tool.requirements table: {receipt}") from exc
    result = {}
    primary = None
    for requirement in requirements:
        name = requirement.get("name")
        if not isinstance(name, str):
            raise PluginError(f"uv tool receipt has an invalid requirement: {receipt}")
        parsed = ReceiptRequirement(
            _receipt_requirement(requirement),
            editable=bool(requirement.get("editable", False)),
        )
        if _canonical(name) == "agents-live":
            primary = parsed
        else:
            result[_canonical(name)] = parsed
    if primary is None:
        raise PluginError(
            f"uv tool receipt has no agents-live requirement: {receipt}")
    return primary, result


def converge(roots: list[Path]) -> bool:
    """Converge the host-global uv tool environment.

    Return True when plugins were installed and False when already converged.
    """
    declarations = union(roots, require_exists=False)
    # Pending detection deliberately skips artifact hashing: when every
    # plugin is installed at its declared version there is nothing to
    # install, so the wheels are not consumed and re-verifying them on
    # every activation buys nothing (doctor still surfaces mismatches).
    pending = {
        key: plugin for key, plugin in declarations.items()
        if not _installed_state(plugin)[0]
    }
    if not pending:
        return False
    for plugin in declarations.values():
        if plugin.metadata_error:
            raise PluginError(plugin.metadata_error)
        if not plugin.path.is_file():
            raise PluginError(
                f"plugin {plugin.name!r} wheel does not exist: {plugin.path}")
    # An install will consume the artifacts: an integrity mismatch must
    # fail before uv sees any of them rather than being treated like an
    # installable stale plugin.
    for plugin in declarations.values():
        integrity_error = _integrity_error(plugin)
        if integrity_error:
            raise PluginError(integrity_error)
    primary, requirements = _receipt_requirements()
    requirements.update({
        key: ReceiptRequirement(str(plugin.path))
        for key, plugin in declarations.items()
    })
    try:
        uv = find_uv()
    except FileNotFoundError as exc:
        raise PluginError(str(exc)) from exc
    command = [uv, "tool", "install", "--force"]
    if primary.editable:
        command.append("--editable")
    command.append(primary.value)
    for requirement in requirements.values():
        # uv distinguishes the positional tool's --editable flag from the
        # --with-editable option used for co-installed requirements.
        flag = "--with-editable" if requirement.editable else "--with"
        command.extend([flag, requirement.value])
    completed = subprocess.run(command, check=False)
    if completed.returncode:
        raise PluginError(
            f"plugin convergence failed with exit code {completed.returncode}; "
            "run `agents-live upgrade` to retry")
    return True
