"""Remove host integrations before uninstalling the uv-managed tool."""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys

from . import health_check, heartbeat, preflight
from .spawn import find_uv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Uninstall agents-live safely")
    parser.add_argument("--distro")
    parser.add_argument("--retain-state", action="store_true")
    args = parser.parse_args(argv)
    if heartbeat.is_wsl():
        try:
            heartbeat.uninstall(args.distro, retain_state=args.retain_state)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            selected = (
                args.distro or os.environ.get("WSL_DISTRO_NAME")
                or "<your-distro-name>")
            preflight.emit_failure(
                "uninstall",
                "host cleanup failed; agents-live remains installed: "
                f"{exc}; recovery: uvx agents-live heartbeat uninstall "
                f"--distro {shlex.quote(selected)}")
            return 1
    else:
        # Non-WSL hosts have no Windows heartbeat task to remove; a hard
        # dependency here would make uninstall impossible off WSL.
        print("no WSL host integrations to remove; uninstalling the tool")
    # After host cleanup succeeded (never before: a failed uninstall must
    # not strand an installed tool without its check-and-repair loop).
    try:
        if health_check.remove_health_cron_lines():
            print("Removed the health-check loop crontab entries")
    except Exception as exc:
        print(f"warning: could not remove health-check crontab entries: "
              f"{exc}", file=sys.stderr)
    try:
        uv = find_uv()
    except FileNotFoundError:
        preflight.emit_failure(
            "uninstall",
            "host cleanup succeeded, but uv was not found; restore or install "
            "uv, then run `uv tool uninstall agents-live`")
        return 1
    completed = subprocess.run([uv, "tool", "uninstall", "agents-live"], check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
