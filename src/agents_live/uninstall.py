"""Remove host integrations before uninstalling the uv-managed tool."""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys

from . import heartbeat


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Uninstall agents-live safely")
    parser.add_argument("--distro")
    parser.add_argument("--retain-state", action="store_true")
    args = parser.parse_args(argv)
    try:
        heartbeat.uninstall(args.distro, retain_state=args.retain_state)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        selected = (
            args.distro or os.environ.get("WSL_DISTRO_NAME")
            or "<your-distro-name>")
        print(
            f"error: host cleanup failed; agents-live remains installed: {exc}\n"
            "recovery: "
            "uvx agents-live heartbeat uninstall --distro "
            f"{shlex.quote(selected)}",
            file=sys.stderr)
        return 1
    uv = shutil.which("uv")
    if not uv:
        print(
            "error: host cleanup succeeded, but uv was not found; restore or "
            "install uv, then run `uv tool uninstall agents-live`",
            file=sys.stderr)
        return 1
    completed = subprocess.run([uv, "tool", "uninstall", "agents-live"], check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
