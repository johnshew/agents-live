"""Project-declared plugin inspection and uv tool-environment convergence."""
from __future__ import annotations

import configparser
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

from . import __version__, paths
from .agent import providers as provider_plugins
from .obs import admin as adminlog
from .runtime.hosts import system as hostruntime
from .runtime.spawn import find_uv
from .state import ownership

# Kernel extension points a declared distribution must provide. Each seam
# owns its own group name, so validating one group while another is read
# cannot happen (a provider plugin was silently never discovered).
ENTRY_POINT_GROUPS = frozenset({
    provider_plugins.ENTRY_POINT_GROUP,
    ownership.ENTRY_POINT_GROUP,
})
# 5.x adapter plugins named this group. Recognised only to say so; the
# diagnostic expires with the rest of the 5.x support in 7.0.
RETIRED_ENTRY_POINT_GROUPS = frozenset({"agents_live.agents"})


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


def _retired_groups_in_wheel(plugin: Plugin) -> list[str]:
    """Retired entry-point groups a wheel declares, read without installing."""
    try:
        with zipfile.ZipFile(plugin.path) as archive:
            names = [
                name for name in archive.namelist()
                if name.endswith(".dist-info/entry_points.txt")]
            if not names:
                return []
            parser = configparser.ConfigParser()
            parser.read_string(archive.read(names[0]).decode("utf-8"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile,
            UnicodeDecodeError, configparser.Error):
        # Unreadable metadata is the installed-state check's problem.
        return []
    return sorted(set(parser.sections()) & RETIRED_ENTRY_POINT_GROUPS)


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
    # Checked before the recognised groups: a distribution declaring both a
    # retired and a current group would otherwise validate on the current one
    # while the retired half is silently never loaded.
    retired = sorted({
        ep.group for ep in distribution.entry_points
        if ep.group in RETIRED_ENTRY_POINT_GROUPS})
    if retired:
        return False, (
            f"declares retired entry-point group {', '.join(retired)}; "
            f"port it to {provider_plugins.ENTRY_POINT_GROUP} and "
            "the 6.0 provider protocol")
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


def _receipt_path(environment: Path | None = None) -> Path | None:
    candidate = Path(environment or sys.prefix) / "uv-receipt.toml"
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


def _pinned_primary(
        requirement: dict, parsed: ReceiptRequirement) -> ReceiptRequirement:
    """Pin an unconstrained ``agents-live`` requirement to the running version.

    The uv receipt records the tool as a bare name, so ``uv tool install
    --force agents-live`` resolves to whatever is newest on PyPI: convergence
    would silently upgrade the kernel as a side effect of installing a plugin.
    Convergence changes plugins; `agents-live upgrade` changes versions.
    A requirement that already names a source or a specifier is left alone.
    """
    if parsed.editable or any(
            field in requirement
            for field in ("path", "directory", "url", "git")):
        return parsed
    if requirement.get("specifier") or requirement.get("marker"):
        return parsed
    return replace(parsed, value=f"{parsed.value}=={__version__}")


def _receipt_requirements(*, pin_primary: bool = True,
                          environment: Path | None = None) -> tuple[
        ReceiptRequirement, dict[str, ReceiptRequirement]]:
    receipt = _receipt_path(environment)
    if receipt is None:
        searched = Path(environment or sys.prefix)
        raise PluginError(
            "plugin convergence requires an uv tool installation of agents-live; "
            f"no uv receipt was found in {searched}. Deactivate any active "
            "virtualenv so the uv-managed agents-live command is used, then retry")
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
            primary = (
                _pinned_primary(requirement, parsed) if pin_primary else parsed)
        else:
            result[_canonical(name)] = parsed
    if primary is None:
        raise PluginError(
            f"uv tool receipt has no agents-live requirement: {receipt}")
    return primary, result


# ---------------------------------------------------------------------------
# Replacing our own executable while one of them is running
# ---------------------------------------------------------------------------

# What a uv tool install writes for this package. uv copies entry points
# on Windows rather than symlinking them, so the same executable exists
# in the tool environment and in uv's executable directory, and both are
# rewritten by an install or an upgrade.
_SHIM_NAME = hostruntime.executable_filename("agents-live")

# Long enough for a cold uv to answer, short enough that a command
# blocked on it does not look hung.
_TOOL_DIR_TIMEOUT_S = 15


def tool_environment() -> Path | None:
    """Where uv keeps this tool, or ``None`` if it will not say.

    Asked of uv rather than derived from ``sys.prefix``: a command can be
    run from an ephemeral ``uvx`` environment or from a checkout, neither
    of which is the installation it is about to change.
    """
    try:
        uv = find_uv()
        completed = subprocess.run(
            [uv, "tool", "dir"], capture_output=True, **hostruntime.CHILD_TEXT,
            check=True, timeout=_TOOL_DIR_TIMEOUT_S)
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    environment = Path(completed.stdout.strip()) / "agents-live"
    return environment if environment.is_dir() else None


def converge(roots: list[Path], *, trigger: str = "unspecified",
             pin_primary: bool = True,
             receipt_environment: Path | None = None) -> bool:
    """Converge the host-global uv tool environment.

    Return True when plugins were installed and False when already converged.

    ``pin_primary`` keeps convergence from moving the agents-live version;
    only ``upgrade``, which has just resolved a new release on purpose, passes
    False.
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
    for key, plugin in pending.items():
        retired = _retired_groups_in_wheel(plugin)
        if retired:
            raise PluginError(
                f"plugin {plugin.name!r} declares retired entry-point group "
                f"{', '.join(retired)} and cannot run under this release; "
                f"update the declaration in .agents-live.toml to a wheel "
                f"ported to {provider_plugins.ENTRY_POINT_GROUP}")
    primary, requirements = _receipt_requirements(
        pin_primary=pin_primary, environment=receipt_environment)
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
    with adminlog.operation(
            "plugin-converge",
            trigger=trigger,
            version_before=__version__,
            primary=primary.value,
            plugins=sorted(
                f"{plugin.name}=={plugin.version}" if plugin.version
                else plugin.name for plugin in declarations.values()),
            pending=sorted(plugin.name for plugin in pending.values()),
    ) as end:
        launcher_before = launcher_stamp()
        completed = subprocess.run(command, check=False)
        if (completed.returncode
                and not only_the_launcher_failed(launcher_before)):
            raise PluginError(
                f"plugin convergence failed with exit code {completed.returncode}; "
                "run `agents-live upgrade` to retry")
        end["version_after"] = installed_version()
    return True


def launcher_stamp() -> int | None:
    """The generated launcher's modification time, or None if absent.

    Recorded before an install so :func:`only_the_launcher_failed` can
    compare the launcher against itself. A wall-clock reading cannot
    serve here: on Windows a file's timestamp comes from the coarse
    system clock while ``time.time()`` reads the precise one, so a
    launcher written in the same tick as the reading can carry a stamp
    fractionally behind it and be mistaken for one uv never touched.
    """
    try:
        return (hostruntime.executable_dir() / _SHIM_NAME).stat().st_mtime_ns
    except OSError:
        return None


def only_the_launcher_failed(before: int | None) -> bool:
    """Whether a failed install still left the environment upgraded.

    uv builds the environment first and installs the launchers last, so
    a launcher it cannot replace fails the command over an environment
    that is already correct. Windows holds a lock on the launcher while
    any agents-live process runs, including the one doing the install,
    which makes that the ordinary outcome on a host that is running
    agents rather than a rare one (#179).

    Windows is also the only place that conclusion is safe to draw.
    Replacing a running executable is unremarkable on POSIX, so a
    failed install there has some other cause and keeps its exit code.
    Excusing it would trade a loud failure for a silent one.

    What is left is measured rather than read out of uv's message,
    which is not an interface. uv builds the environment, generates the
    launcher inside it, and only then publishes that launcher to the
    directory on PATH, so a launcher that changed during this install
    places the failure at the last step and proves everything before it
    finished. The check fails safe, because an install that stopped
    earlier leaves the generated launcher exactly as it was.

    The environment's own files are not evidence here: convergence
    installs a plugin beside a runtime that is already satisfied, so uv
    has no reason to rewrite the runtime, and its timestamp says
    nothing about whether this install got anywhere.
    """
    if not hostruntime.locks_running_image():
        return False
    after = launcher_stamp()
    return after is not None and after != before


def installed_version() -> str:
    """The agents-live version present in the tool environment right now.

    Read back from installed metadata rather than assumed: after a
    convergence or upgrade the running process still holds the old code, so
    ``__version__`` reports the version that started the command, not the one
    left behind on disk.
    """
    try:
        return importlib.metadata.version("agents-live")
    except importlib.metadata.PackageNotFoundError:
        return __version__
