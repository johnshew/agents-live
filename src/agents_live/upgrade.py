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

from . import init, paths, plugins, repos
from .spawn import find_uv


def _targets() -> tuple[list[tuple[str, Path]], list[str]]:
    local = paths.local_root()
    if os.environ.get(paths.ENV_VAR, "").strip():
        return [("selected project", local)], []

    targets: dict[Path, str] = {}
    if local is not None:
        targets[local] = "current project"

    errors = []
    for alias, value, error in repos.entries():
        if error:
            errors.append(f"{alias}: {error}")
            continue
        root = Path(value)
        targets.setdefault(root, alias)
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


def _upgrade_runtime(roots: list[Path] | None = None) -> int:
    try:
        uv = find_uv()
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    status = subprocess.run(
        [uv, "tool", "upgrade", "agents-live"], check=False,
    ).returncode
    if status != 0:
        return status
    try:
        plugins.converge(roots or [])
    except (OSError, ValueError, plugins.PluginError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _refresh_with_installed_cli(root: Path) -> int:
    # cli_shim_path prefers the entry point beside the interpreter (the
    # uv tool env), so a freshly installed shim is found even when
    # ~/.local/bin is not on PATH yet.
    from .headless import AgentsLiveError, cli_shim_path  # noqa: PLC0415

    try:
        executable = str(cli_shim_path())
    except AgentsLiveError as exc:
        print(
            f"error: agents-live executable not found after runtime "
            f"upgrade: {exc}",
            file=sys.stderr,
        )
        return 1
    return subprocess.run(
        [executable, "--repo", str(root), "upgrade", "--skills-only"],
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

    try:
        targets, errors = _targets()
        plugin_roots = [root for _, root in targets]
        if os.environ.get(paths.ENV_VAR, "").strip():
            for alias, value, error in repos.entries():
                if error:
                    errors.append(f"{alias}: {error}")
                else:
                    plugin_roots.append(Path(value))
    except (OSError, ValueError) as exc:
        # The message already names its source (registry file vs an
        # invalid AGENTS_LIVE_REPO); no prefix that could mislabel it.
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not args.skills_only:
        runtime_status = _upgrade_runtime(list(dict.fromkeys(plugin_roots)))
        if runtime_status != 0 or args.runtime_only:
            return runtime_status

    for error in errors:
        print(f"warning: skipping registered repo {error}", file=sys.stderr)

    if not targets:
        print("No initialized or registered projects to refresh")
        return 1 if errors else 0

    failed = bool(errors)
    for label, root in targets:
        print(f"Refreshing {label}: {root}")
        if args.skills_only:
            try:
                _refresh_payload(root)
            except (OSError, ValueError) as exc:
                print(f"error: {label} ({root}): {exc}", file=sys.stderr)
                failed = True
        elif _refresh_with_installed_cli(root) != 0:
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())