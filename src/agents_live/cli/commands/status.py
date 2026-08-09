"""Report definitions in the user's started, stopped, and run vocabulary."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ... import agent, state
from ...state import registry as repos


def _rows(root: Path, selected: str | None = None) -> list[dict[str, object]]:
    try:
        started = state.load(root)
        started_names = started.agents if started.initialized else frozenset()
        state_error = None
    except state.StartedStateUnavailable as exc:
        started_names = frozenset()
        state_error = str(exc)
    try:
        discovered = {spec.identifier: spec for spec in agent.discover(root)}
    except agent.DefinitionError:
        discovered = {}
    identifiers = set(discovered) | set(started_names)
    if selected:
        try:
            identifiers &= {agent.load(selected, root=root).identifier}
        except agent.DefinitionError:
            identifiers &= {selected}
    rows = []
    for identifier in sorted(identifiers):
        load_error = state_error
        description = None
        name = identifier
        try:
            spec = discovered.get(identifier) or agent.load(identifier, root=root)
            name = spec.name
            description = spec.properties.description
        except agent.DefinitionError as exc:
            load_error = str(exc)
        rows.append({
            "repository": str(root),
            "name": name,
            "identifier": identifier,
            "state": "started" if identifier in started_names else "stopped",
            "loadable": load_error is None,
            "description": description,
            "error": load_error,
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("name", nargs="?")
    parser.add_argument("--all-repos", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.all_repos:
            registry = repos.load()
            roots = [Path(value) for value in registry["repos"].values()]
        else:
            roots = [state.resolve_root(allow_sole_registered=True)]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    rows = [row for root in roots for row in _rows(root, args.name)]
    if os.environ.get("AGENTS_LIVE_JSON") == "1":
        print(json.dumps({"ok": True, "agents": rows}))
    elif not rows:
        print("No agent definitions found.")
    else:
        for row in rows:
            suffix = f" ({row['error']})" if row["error"] else ""
            print(f"{row['name']} ({row['identifier']}): {row['state']}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
