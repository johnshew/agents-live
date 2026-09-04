"""Inspect, activate, and collect self-managed runtime generations."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ... import deploy, preflight
from ...runtime.hosts import system as hostruntime
from ...runtime.hosts.processes import within


def _holders(root: Path) -> dict[str, tuple[str, ...]]:
    found: dict[str, list[str]] = {}
    try:
        processes = hostruntime.process_command_lines()
    except OSError:
        return {}
    for pid, command in processes:
        for argument in hostruntime.split_command_line(command):
            generation = deploy.layout.generation_of(argument, root)
            if generation is not None and within(argument, deploy.layout.generation_dir(generation, root)):
                found.setdefault(generation, []).append(f"process {pid}")
                break
    return {name: tuple(processes) for name, processes in found.items()}


def _require_self_managed() -> deploy.ownership.Installation:
    installation = deploy.ownership.describe()
    if not installation.self_managed:
        raise deploy.generation.GenerationError(
            "generation changes require the self-managed agents-live command")
    return installation


def _list(root: Path) -> int:
    active, _, _ = deploy.pointer.status(deploy.layout.current_path(root))
    rows = []
    for name in deploy.layout.installed_generations(root):
        generation = deploy.generation.load(name, root=root)
        rows.append({
            "generation": name,
            "active": active is not None and active.generation == name,
            "validated": generation.validated,
            "channel": generation.provenance.channel if generation.provenance else None,
            "artifact": generation.provenance.artifact if generation.provenance else None,
        })
    if os.environ.get("AGENTS_LIVE_JSON") == "1":
        print(json.dumps({"ok": True, "generations": rows}))
    elif not rows:
        print("No installed generations")
    else:
        for row in rows:
            marker = "*" if row["active"] else " "
            provenance = row["channel"] or "unknown source"
            print(f"{marker} {row['generation']}  {provenance}  {row['validated']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manage installed runtime generations")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List installed generations")
    activate = commands.add_parser(
        "activate", help="Select an installed generation")
    activate.add_argument("version")
    remove = commands.add_parser(
        "remove", help="Remove one inactive generation")
    remove.add_argument("version")
    collect = commands.add_parser(
        "collect", help="Remove old inactive generations")
    collect.add_argument(
        "--retain", type=int, default=deploy.plan.RETAINED_PREVIOUS,
        help="Number of inactive rollback generations to retain")
    args = parser.parse_args(argv)

    root = deploy.layout.installation_root()
    if args.command == "list":
        try:
            return _list(root)
        except deploy.generation.GenerationError as exc:
            preflight.emit_failure("generations", str(exc))
            return 1

    try:
        _require_self_managed()
        holders = _holders(root)
        if args.command == "activate":
            selected = deploy.generation.load(args.version, root=root)
            deploy.generation.activate(selected, root=root)
            print(f"Activated generation {selected.name}")
        elif args.command == "remove":
            deploy.generation.remove(
                args.version, root=root, held=holders.get(args.version, ()))
            print(f"Removed generation {args.version}")
        else:
            removed = deploy.generation.collect(
                root=root, held=holders, retain=args.retain)
            if removed:
                print(f"Collected {len(removed)} generation(s): {', '.join(removed)}")
            else:
                print("No generations to collect")
    except (OSError, ValueError, deploy.generation.GenerationError) as exc:
        preflight.emit_failure("generations", str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())