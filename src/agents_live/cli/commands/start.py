"""Record started intent, then converge the complete runtime."""
from __future__ import annotations

import argparse
import sys

from ... import agent, paths, state
from ...state import registry as repos
from .. import lifecycle


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
        if args.all:
            discovery = agent.discover(root)
            specs = tuple(
                spec for spec in discovery.specs if spec.execution is not None)
            unloadable = discovery.broken
        else:
            specs = (agent.load(args.name, root=root),)
            unloadable = ()
        identifiers = [spec.identifier for spec in specs]
        result = lifecycle.converge(
            additions={root: set(identifiers)}, dry_run=args.dry_run)
    except (agent.DefinitionError, lifecycle.CollectionUnavailable,
            state.StartedStateUnavailable, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    verb = "Would start" if args.dry_run else "Started"
    for spec in specs:
        print(f"{verb} '{spec.name}' ({spec.identifier}).")
        if not spec.execution.schedules and not spec.execution.watch:
            print(
                f"  note: '{spec.name}' declares no schedule or watch, so "
                "nothing runs it automatically.",
                file=sys.stderr,
            )
    for item in unloadable:
        print(f"Skipped '{item.name}': {item.message}", file=sys.stderr)
    for operation, message in result.failed:
        print(f"{operation.key}: {message}", file=sys.stderr)
    return 1 if result.failed else 0

if __name__ == "__main__":
    raise SystemExit(main())
