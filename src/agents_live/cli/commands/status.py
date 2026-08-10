"""Report definitions in the user's started, stopped, and run vocabulary."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ... import agent, state
from ...state import registry as repos


def _policy(spec: agent.AgentSpec) -> dict[str, object] | None:
    """How the definition runs, for callers that would otherwise parse it.

    Deliberately omits env, whose values are the definition's secrets.
    """
    config = spec.execution
    if config is None:
        return None
    return {
        "selector": config.selector.canonical,
        "provider": config.selector.provider,
        "model": config.selector.model,
        "mode": config.mode,
        "schedules": list(config.schedules),
        "watch": config.watch,
        "mcps": list(config.mcps),
        "pre_processor": config.pre_processor,
        "post_processor": config.post_processor,
    }


def _rows(root: Path, selected: str | None = None) -> list[dict[str, object]]:
    try:
        started = state.load(root)
        started_names = started.agents if started.initialized else frozenset()
        state_error = None
    except state.StartedStateUnavailable as exc:
        started_names = frozenset()
        state_error = str(exc)
    try:
        discovery = agent.discover(root)
        discovered = {spec.identifier: spec for spec in discovery.specs}
        unloadable = discovery.broken
    except agent.DefinitionError as exc:
        discovered = {}
        unloadable = (agent.BrokenDefinition(root, str(exc)),)
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
        policy = None
        prompt_path = None
        unknown_metadata: list[str] = []
        name = identifier
        try:
            spec = discovered.get(identifier) or agent.load(identifier, root=root)
            name = spec.name
            description = spec.properties.description
            policy = _policy(spec)
            prompt_path = str(spec.prompt_path)
            unknown_metadata = list(spec.unknown_metadata)
        except agent.DefinitionError as exc:
            load_error = str(exc)
        rows.append({
            "repository": str(root),
            "name": name,
            "identifier": identifier,
            "state": "started" if identifier in started_names else "stopped",
            "loadable": load_error is None,
            "description": description,
            "path": prompt_path,
            "execution": policy,
            "unknown_metadata": unknown_metadata,
            "error": load_error,
        })
    for item in unloadable:
        if selected and item.name != selected:
            continue
        rows.append({
            "repository": str(root),
            "name": item.name,
            "identifier": "",
            "state": "unloadable",
            "loadable": False,
            "description": None,
            "path": str(item.path),
            "execution": None,
            "unknown_metadata": [],
            "error": item.message,
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
            label = row["identifier"] or "unreadable"
            print(f"{row['name']} ({label}): {row['state']}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
