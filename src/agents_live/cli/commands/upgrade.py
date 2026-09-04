"""Upgrade the runtime and refresh managed project skill payloads.

A package module (relative imports): runs via ``agents-live upgrade``,
never as a standalone ``uv run --script`` target.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import zipfile
from email.parser import Parser
from pathlib import Path

from ... import __version__, agent, deploy, paths, plugins, preflight
from ...legacy import migrate as legacy_migration
from ...runtime.spawn import cli_executable_path
from ...state import registry as repos
from .. import lifecycle
from . import init, install_generation, install_release


def _targets() -> tuple[list[tuple[str, Path]], list[str]]:
    local = paths.local_root()
    if os.environ.get(paths.ENV_VAR, "").strip():
        return [("selected project", local)], []

    targets: dict[Path, str] = {}
    global_root = paths.global_root()
    if paths.config_source(global_root) is not None:
        targets[global_root] = "global workspace"
    if local is not None:
        targets[local] = "current project"

    errors = []
    for alias, value, error in repos.entries():
        if error:
            errors.append(f"{alias}: {error}")
            continue
        root = Path(value)
        targets.setdefault(root, alias)
    for root in legacy_migration.persisted_roots():
        targets.setdefault(root, f"active workspace {root.name}")
    return [(label, root) for root, label in targets.items()], errors


def _refresh_payload(root: Path) -> None:
    status = init.install_skill(root)
    if status == "installed":
        message = "installed current skill payload"
    elif status == "refreshed":
        message = "upgraded skill payload to match the installed package"
    else:
        message = "skill payload already matches the installed package"
    print(f"{root}: {message}")


def _migrate_triggers(root: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "agents_live.cli", "--repo", str(root),
         "internal", "migrate"],
        check=False,
    )
    if completed.returncode != 0:
        raise OSError(
            f"trigger migration failed with exit {completed.returncode}")


def _wheel_identity(wheel: Path) -> tuple[str, str]:
    """Return the exact Agents Live version and digest carried by a wheel."""
    try:
        with zipfile.ZipFile(wheel) as archive:
            metadata_files = [
                name for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")]
            if len(metadata_files) != 1:
                raise ValueError("wheel does not contain exactly one METADATA file")
            metadata = Parser().parsestr(
                archive.read(metadata_files[0]).decode("utf-8"))
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise ValueError(f"could not read wheel metadata: {exc}") from exc
    if metadata.get("Name", "").lower().replace("_", "-") != "agents-live":
        raise ValueError("wheel metadata does not identify agents-live")
    version = deploy.layout.generation_name(metadata.get("Version", ""))
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    return version, digest


def _upgrade_self_managed(
    source: Path | None,
) -> int:
    """Activate a new immutable generation through the stable current path."""
    if source is None:
        return install_release.main(["--activate"])
    if not source.is_file() or source.suffix.lower() != ".whl":
        preflight.emit_failure(
            "upgrade",
            "a self-managed `upgrade --from` requires a built wheel; run "
            "`uv build --wheel` and pass the resulting .whl",
            code="invalid_arguments",
        )
        return 1
    try:
        version, digest = _wheel_identity(source)
        provenance = deploy.generation.Provenance(
            "local-artifact", source.name, digest)
        try:
            built = deploy.generation.load(version)
        except deploy.generation.GenerationError:
            target = deploy.layout.generation_dir(version)
            if target.exists() or target.is_symlink():
                raise
            built = install_generation.install(
                version, source=source, provenance=provenance)
        else:
            if built.provenance != provenance:
                raise deploy.generation.GenerationError(
                    f"generation {version} is already installed from different "
                    "artifact bytes and will not be overwritten")
            install_generation.validate(built)
        install_generation.activate_generation(built)
        deploy.ownership.write_record(deploy.ownership.SELF)
    except (OSError, ValueError, deploy.generation.GenerationError) as exc:
        preflight.emit_failure("upgrade", str(exc))
        return 1
    print(f"Activated self-managed generation {version} from {source}")
    return 0


def _compatibility_errors(roots: list[Path], registry_errors: list[str], *,
                          source: Path | None
                          ) -> tuple[str, ...]:
    """Unsafe registered state that must be resolved before replacement."""
    errors = [f"registered repository is unavailable: {item}"
              for item in registry_errors]
    readable = []
    for root in dict.fromkeys(path.resolve() for path in roots):
        if not root.is_dir():
            errors.append(f"registered repository is unavailable: {root}")
            continue
        readable.append(root)
        try:
            discovery = agent.discover(root)
        except (OSError, ValueError, agent.DefinitionError) as exc:
            errors.append(f"cannot inspect {root}: {exc}")
            continue
        errors.extend(
            f"{item.path}: {item.message}"
            for item in discovery.broken
            if "retired 5.x fields:" in item.message
        )
    runtime_requirement = (str(source) if source is not None
                           else f"agents-live>={__version__}")
    errors.extend(plugins.compatibility_errors(
        readable, runtime_requirement=runtime_requirement))
    return tuple(dict.fromkeys(errors))


def _refresh_with_installed_cli(*, refresh_skills: bool,
                                executable: Path | None = None) -> int:
    # cli_executable_path prefers the entry point beside the interpreter (the
    # uv tool env), so a freshly installed shim is found even when
    # ~/.local/bin is not on PATH yet.
    try:
        executable_path = executable or cli_executable_path()
    except RuntimeError as exc:
        detail = f"agents-live executable not found after runtime upgrade: {exc}"
        if refresh_skills:
            preflight.emit_failure("upgrade", detail)
            return 1
        print(f"warning: could not update shell completions: {detail}",
              file=sys.stderr)
        return 0
    try:
        completion_status = subprocess.run(
            [str(executable_path), "completions", "--update"], check=False,
        ).returncode
    except OSError as exc:
        completion_status = None
        print(f"warning: could not update shell completions after runtime "
              f"upgrade: {exc}", file=sys.stderr)
    if completion_status not in (None, 0):
        print("warning: could not update shell completions after runtime "
              f"upgrade (exit {completion_status})", file=sys.stderr)
    if not refresh_skills:
        return 0
    try:
        return subprocess.run(
            [str(executable_path), "upgrade", "--skills-only"], check=False,
        ).returncode
    except OSError as exc:
        preflight.emit_failure("upgrade", f"skill refresh failed: {exc}")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upgrade the runtime and managed project skill payloads")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--runtime-only", action="store_true",
        help="Upgrade the uv tool without refreshing project skill payloads",
    )
    mode.add_argument(
        "--skills-only", action="store_true",
        help="Refresh project skill payloads without upgrading the uv tool",
    )
    # Not in the mode group: --from selects where the runtime comes
    # from, not whether it is installed, so it composes with
    # --runtime-only. Only --skills-only contradicts it.
    parser.add_argument(
        "--from", dest="source", metavar="PATH",
        help="Install the runtime from a local project directory or built "
             "artifact instead of PyPI",
    )
    args = parser.parse_args()
    print(f"Installed agents-live version: {__version__}")

    source: Path | None = None
    if args.source is not None:
        if args.skills_only:
            preflight.emit_failure(
                "upgrade", "--from installs a runtime; it cannot be combined "
                "with --skills-only", code="invalid_arguments")
            return 1
        source = Path(args.source).expanduser()
        if not source.exists():
            preflight.emit_failure(
                "upgrade", f"no such path to install from: {source}",
                code="source_missing")
            return 1
        source = source.resolve()

    installation = deploy.ownership.describe()
    ownership_refusal = deploy.ownership.refusal(installation)
    if installation.contested and ownership_refusal is not None:
        preflight.emit_failure("upgrade", ownership_refusal)
        return 1

    try:
        targets, errors = _targets()
        target_roots = [root for _, root in targets]
        if os.environ.get(paths.ENV_VAR, "").strip():
            # Explicit --repo and AGENTS_LIVE_REPO both set this environment
            # value. They narrow payload refresh, but plugins share one
            # host-global tool and still include every registered project.
            for alias, value, error in repos.entries():
                if error:
                    errors.append(f"{alias}: {error}")
                else:
                    target_roots.append(Path(value))
    except (OSError, ValueError) as exc:
        # The message already names its source (registry file vs an
        # invalid AGENTS_LIVE_REPO); no prefix that could mislabel it.
        preflight.emit_failure("upgrade", str(exc))
        return 1

    if not args.skills_only:
        compatibility_errors = _compatibility_errors(
            target_roots, errors, source=source)
        if compatibility_errors:
            for error in compatibility_errors:
                preflight.emit_failure("upgrade", error,
                                       code="upgrade_preflight_failed")
            return 1
        if not installation.self_managed:
            preflight.emit_failure(
                "upgrade",
                "upgrade requires a self-managed installation; install the "
                "current release with the official bootstrap, then run the "
                "stable agents-live command",
                code="unsupported_installation",
            )
            return 1
        runtime_status = _upgrade_self_managed(source)
        if runtime_status != 0:
            return runtime_status
        stable_command = deploy.layout.command_path("agents-live")
        return _refresh_with_installed_cli(
            refresh_skills=not args.runtime_only,
            executable=stable_command,
        )

    for error in errors:
        print(f"warning: skipping registered repo {error}", file=sys.stderr)

    # This branch runs in the freshly installed CLI. Converge every desired
    # subscription before refreshing payloads so persisted commands and live
    # watchers move to the current runtime metadata contract immediately.
    convergence_failed = False
    try:
        result = lifecycle.converge()
        if result.failed or not result.health.healthy:
            detail = "; ".join(
                f"{operation.key}: {error}"
                for operation, error in result.failed
            ) or "; ".join(result.health.detail) or "runtime health is degraded"
            raise RuntimeError(detail)
        if result.done:
            print(f"Converged {len(result.done)} runtime subscription change(s)")
    except Exception as exc:
        convergence_failed = True
        print(f"warning: could not converge runtime subscriptions: {exc}",
              file=sys.stderr)

    if not targets:
        print("No initialized or registered projects to refresh")
        return 1 if errors or convergence_failed else 0

    failed = bool(errors) or convergence_failed
    for label, root in targets:
        print(f"Refreshing {label}: {root}")
        try:
            _migrate_triggers(root)
            _refresh_payload(root)
        except (OSError, ValueError) as exc:
            preflight.emit_failure(
                "upgrade", f"{label} ({root}): {exc}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())