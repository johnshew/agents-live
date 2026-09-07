"""Inspect, activate, and collect self-managed runtime generations."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from ... import deploy, preflight
from .. import identity
from . import install_generation
from ...runtime.hosts import system as hostruntime
from ...runtime.hosts.processes import within
from . import install_generation


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
        status = deploy.generation.release_status(generation, root=root)
        channel = identity.channel(name)
        if channel == "release":
            channel = ("release" if status == "released" else "release-candidate"
                       if status in {"candidate", "rejected"} else "release"
                       if generation.provenance and generation.provenance.channel == "github-release"
                       else "unclassified")
        rows.append({
            "version": name,
            "active": active is not None and active.generation == name,
            "validated": datetime.fromisoformat(generation.validated).astimezone(
                timezone.utc).isoformat().replace("+00:00", "Z"),
            "channel": channel,
            "status": status,
            "source": generation.provenance.channel if generation.provenance else None,
            "artifact": generation.provenance.artifact if generation.provenance else None,
        })
    if os.environ.get("AGENTS_LIVE_JSON") == "1":
        print(json.dumps({"ok": True, "versions": rows}))
    elif not rows:
        print("No installed versions")
    else:
        print(f"  {'Version':<28}  {'Release':<35}  {'Source':<10}  Validated")
        for row in rows:
            marker = "*" if row["active"] else " "
            source = {"local-artifact": "local", "github-release": "GitHub", "pypi": "PyPI"}.get(
                row["source"], row["source"] or "unknown")
            moment = datetime.fromisoformat(row["validated"]).astimezone()
            zone = moment.tzname() or "UTC"
            if " " in zone:
                zone = "".join(word[0].upper() for word in zone.split())
            date = f"{moment.strftime('%b')} {moment.day}, {moment.year} "
            date += f"{moment.hour % 12 or 12}:{moment.minute:02d} {moment.strftime('%p')} {zone}"
            label = {"bake": "bake release", "release-candidate": "release candidate"}.get(
                row["channel"], row["channel"])
            if row["source"] == "local-artifact" and row["channel"] == "release-candidate":
                label = "local " + label
            if row["status"] == "rejected":
                label += " (rejected)"
            print(f"{marker} {row['version']:<28}  {label:<35}  {source:<10}  {date}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manage installed runtime versions")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List installed versions")
    activate = commands.add_parser(
        "activate", help="Select an installed version")
    activate.add_argument("version")
    classify = commands.add_parser("classify", help="Record a version's release status")
    classify.add_argument("version")
    classify.add_argument("status", choices=("candidate", "rejected", "released"))
    remove = commands.add_parser(
        "remove", help="Remove one inactive version")
    remove.add_argument("version")
    collect = commands.add_parser(
        "collect", help="Remove old inactive versions")
    collect.add_argument(
        "--retain", type=int, default=deploy.plan.RETAINED_PREVIOUS,
        help="Number of inactive rollback versions to retain")
    args = parser.parse_args(argv)

    root = deploy.layout.installation_root()
    if args.command == "list":
        try:
            return _list(root)
        except (OSError, ValueError, deploy.generation.GenerationError) as exc:
            preflight.emit_failure("versions", str(exc))
            return 1

    try:
        _require_self_managed()
        holders = _holders(root)
        if args.command == "classify":
            deploy.generation.classify(args.version, args.status, root=root)
            print(f"Classified version {args.version} as {args.status}")
        elif args.command == "activate":
            selected = deploy.generation.load(args.version, root=root)
            install_generation.activate_generation(selected, root=root)
            print(f"Activated version {selected.name}")
        elif args.command == "remove":
            deploy.generation.remove(
                args.version, root=root, held=holders.get(args.version, ()))
            print(f"Removed version {args.version}")
        else:
            removed = deploy.generation.collect(
                root=root, held=holders, retain=args.retain)
            if removed:
                print(f"Collected {len(removed)} version(s): {', '.join(removed)}")
            else:
                print("No versions to collect")
    except (OSError, ValueError, deploy.generation.GenerationError) as exc:
        preflight.emit_failure("versions", str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())