"""Record started intent, then converge the complete runtime."""
from __future__ import annotations

import argparse
import sys

from . import agent, lifecycle, paths, repos, state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start automatic runs for an agent.")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--name")
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", "-n", action="store_true")
    args = parser.parse_args(argv)
    root = paths.resolve_root()
    repos.ensure_registered(root)
    try:
        names = _names(root) if args.all else [agent.load(args.name, root=root).name]
        result = lifecycle.converge(
            additions={root: set(names)}, dry_run=args.dry_run)
    except (agent.DefinitionError, lifecycle.CollectionUnavailable,
            state.StartedStateUnavailable, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    verb = "Would start" if args.dry_run else "Started"
    for name in names:
        print(f"{verb} '{name}'.")
    for operation, message in result.failed:
        print(f"{operation.key}: {message}", file=sys.stderr)
    return 1 if result.failed else 0


def _names(root):
    directory = root / "Agents"
    if not directory.is_dir():
        return []
    return [
        item.name for item in sorted(directory.iterdir())
        if item.is_dir() and (item / "SKILL.md").is_file()
    ]


if __name__ == "__main__":
    raise SystemExit(main())
