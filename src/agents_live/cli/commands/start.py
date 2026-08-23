"""Record started intent, then converge the complete runtime."""
from __future__ import annotations

import argparse
import sys

from ... import agent, paths, state
from ...state import ownership, registry as repos
from .. import lifecycle, resolve
from . import init


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start automatic runs for an agent.")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--name")
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", "-n", action="store_true")
    transfer = parser.add_mutually_exclusive_group()
    transfer.add_argument("--transfer-here", action="store_true")
    transfer.add_argument("--transfer-to")
    args = parser.parse_args(argv)
    root = paths.resolve_root()
    repos.ensure_registered(root)
    if (args.transfer_here or args.transfer_to) and args.all:
        print("--transfer-here and --transfer-to act on one agent; use --name",
              file=sys.stderr)
        return 2
    try:
        if args.all:
            discovery = agent.discover(root)
            specs = tuple(
                spec for spec in discovery.specs if spec.execution is not None)
            unloadable = discovery.broken
        else:
            # Starting is persistent, so the repository that answers the
            # name is the one enrolled - never the one the caller happened
            # to stand in (#388).
            resolution = resolve.resolve(args.name, root=root, action="start")
            root = resolution.root
            if resolution.warning:
                print(resolution.warning, file=sys.stderr)
            if resolution.fallback:
                print(f"Starting '{resolution.spec.name}' in {root}.",
                      file=sys.stderr)
            repos.ensure_registered(root)
            specs = (resolution.spec,)
            unloadable = ()
        if args.transfer_here or args.transfer_to:
            return _transfer(root, specs[0], args)
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
    return 1 if result.failed or unloadable else 0

def _transfer(root, spec, args) -> int:
    """Move one agent's ownership, then converge what that implies."""
    owner = (ownership.current_owner_id() if args.transfer_here
             else args.transfer_to)
    mine = args.transfer_here or ownership.owns(owner)
    try:
        if not ownership.registry_available():
            raise ownership.OwnershipUnavailableError(
                "multi-host ownership is a private plugin exposing the "
                f"'{ownership.ENTRY_POINT_GROUP}' entry point; the public "
                "kernel is local-only")
        if not mine and not ownership.owner_uuid(owner):
            print(f"'{owner}' is not a runtime identity "
                  "(expected hostname/runtime/uuid, as `agents-live status "
                  "--json` reports for an owned agent)", file=sys.stderr)
            return 2
        if args.dry_run:
            print(f"Would assign '{spec.name}' to {ownership.display_owner(owner)}.")
            return 0
        if ownership.local_only(root):
            # Transferring is the declaration of multi-host intent; there
            # is deliberately no init-time flag for it.
            init.declare_ownership(root, "registry")
        ownership.set_owner(spec.name, owner, root=root)
    except (ownership.OwnershipUnavailableError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Assigned '{spec.name}' to {ownership.display_owner(owner)}.")
    result = lifecycle.converge(
        additions={root: {spec.identifier}} if mine else None,
        removals=None if mine else {root: {spec.identifier}})
    for operation, message in result.failed:
        print(f"{operation.key}: {message}", file=sys.stderr)
    if not mine:
        print(f"  note: its triggers are withdrawn here; run "
              f"`agents-live start --name {spec.name}` on that host.",
              file=sys.stderr)
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
