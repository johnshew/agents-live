#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# ///
"""Refresh a project's installed skill payload."""
from __future__ import annotations

import argparse

from . import init, paths


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upgrade the project skill payload")
    parser.parse_args()

    root = paths.resolve_root()
    status = init.install_skill(root)
    if status == "installed":
        print("Installed current skill payload: "
              ".claude/skills/agents-live/")
    elif status == "refreshed":
        print("Upgraded skill payload to match the installed package: "
              ".claude/skills/agents-live/")
    else:
        print("Skill payload already matches the installed package")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())