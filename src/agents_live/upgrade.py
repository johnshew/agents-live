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

from . import init, paths, repos


def _targets() -> tuple[list[tuple[str, Path]], list[str]]:
    selected = os.environ.get(paths.ENV_VAR, "").strip()
    if selected:
        return [("selected project", paths.resolve_root())], []

    targets: dict[Path, str] = {}
    local = paths._walk_for_marker(Path.cwd())
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


def _upgrade_runtime() -> int:
    uv = shutil.which("uv") or "uv"
    return subprocess.run(
        [uv, "tool", "install", "--force", "agents-live@latest"], check=False,
    ).returncode


def _refresh_with_installed_cli(root: Path) -> int:
    executable = shutil.which("agents-live")
    if executable is None:
        print(
            "error: agents-live executable not found after runtime upgrade",
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

    if not args.skills_only:
        runtime_status = _upgrade_runtime()
        if runtime_status != 0 or args.runtime_only:
            return runtime_status

    try:
        targets, errors = _targets()
    except (OSError, ValueError) as exc:
        print(f"error: cannot read repository registry: {exc}", file=sys.stderr)
        return 1

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