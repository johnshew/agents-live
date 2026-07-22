"""Upgrade the runtime and refresh managed project skill payloads.

A package module (relative imports): runs via ``agents-live upgrade``,
never as a standalone ``uv run --script`` target.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__, init, paths, plugins, preflight, repos
from .spawn import find_uv


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
    from . import health_check  # noqa: PLC0415
    for root in health_check.persisted_roots():
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


def _upgrade_runtime(roots: list[Path] | None = None) -> int:
    try:
        uv = find_uv()
    except FileNotFoundError as exc:
        preflight.emit_failure("upgrade", str(exc))
        return 1
    status = subprocess.run(
        [uv, "tool", "upgrade", "agents-live"], check=False,
    ).returncode
    if status != 0:
        return status
    try:
        plugins.converge(roots or [])
    except (OSError, ValueError, plugins.PluginError) as exc:
        preflight.emit_failure("upgrade", str(exc))
        return 1
    return 0


def _refresh_with_installed_cli() -> int:
    # cli_shim_path prefers the entry point beside the interpreter (the
    # uv tool env), so a freshly installed shim is found even when
    # ~/.local/bin is not on PATH yet.
    from .headless import AgentsLiveError, cli_shim_path  # noqa: PLC0415

    try:
        executable = str(cli_shim_path())
    except AgentsLiveError as exc:
        preflight.emit_failure(
            "upgrade",
            f"agents-live executable not found after runtime upgrade: {exc}")
        return 1
    return subprocess.run(
        [executable, "upgrade", "--skills-only"],
        check=False,
    ).returncode


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
    args = parser.parse_args()
    print(f"Installed agents-live version: {__version__}")

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
        runtime_status = _upgrade_runtime(list(dict.fromkeys(target_roots)))
        if runtime_status != 0 or args.runtime_only:
            return runtime_status
        # After the runtime upgrade this process is still the old
        # version, so payload refresh must run in the freshly installed
        # CLI. One child covers every target: its own `_targets()`
        # resolves the current project and all registered repositories
        # (and honors AGENTS_LIVE_REPO), so per-repo children would only
        # multiply interpreter start-ups.
        return _refresh_with_installed_cli()

    for error in errors:
        print(f"warning: skipping registered repo {error}", file=sys.stderr)

    # Converge the built-in automatic maintenance crontab entries: a
    # runtime upgrade can re-home the pinned shim path they carry. This
    # branch runs in the freshly installed CLI, so the canonical lines
    # are the new install's. Best-effort: no crontab is not fatal.
    try:
        from . import health_check  # noqa: PLC0415
        if health_check.ensure_health_cron_lines():
            print("Converged the automatic maintenance schedule")
    except Exception as exc:
        print(f"warning: could not converge health-check crontab entries: "
              f"{exc}", file=sys.stderr)

    if not targets:
        print("No initialized or registered projects to refresh")
        return 1 if errors else 0

    failed = bool(errors)
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