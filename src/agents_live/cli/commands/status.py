"""Report definitions in the user's started, stopped, and run vocabulary."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ... import __version__, agent, obs, paths, state
from ...state import ownership, registry as repos
from .. import agent_view, identity, resolve


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
    logs = paths.repo_state_dir(root) / "logs"
    failure_streaks = obs.consecutive_failures(obs.files(logs))
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
    ownership_available = True
    owner_by_identifier: dict[str, str | None] = {
        identifier: None for identifier in discovered
    }
    is_owner_by_identifier: dict[str, bool] = {
        identifier: True for identifier in discovered
    }
    try:
        if not ownership.local_only(root):
            owners = ownership.load_owners(root=root)
            owner_by_identifier.update(ownership.resolve_owners(
                ((spec.identifier, spec.name) for spec in discovered.values()),
                owners,
            ))
            is_owner_by_identifier = {
                identifier: ownership.owns(owner) if owner is not None else True
                for identifier, owner in owner_by_identifier.items()
            }
    except ownership.OwnershipUnavailableError:
        ownership_available = False
        is_owner_by_identifier = {
            identifier: False for identifier in discovered
        }
    observations = agent_view.runtime_observations(
        root, tuple(discovered.values()), started_names,
        is_owner_by_identifier,
    )
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
        owner = owner_by_identifier.get(identifier)
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
            "owner": owner,
            "is_owner": is_owner_by_identifier.get(identifier, False),
            "ownership_available": ownership_available,
            "consecutive_failures": observations.get(
                identifier, (failure_streaks.get(identifier, 0),
                             "not-required"))[0],
            "watcher_liveness": observations.get(
                identifier, (0, "not-required"))[1],
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
            "owner": None,
            "is_owner": False,
            "ownership_available": ownership_available,
            "consecutive_failures": 0,
            "watcher_liveness": "not-required",
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
    if args.name and not rows and not args.all_repos:
        rows = _elsewhere(args.name, roots[0])
    if os.environ.get("AGENTS_LIVE_JSON") == "1":
        print(json.dumps({
            "ok": True,
            "runtime": identity.details(__version__),
            "agents": rows,
        }))
    else:
        print(identity.label(__version__))
        if not rows:
            print("No agent definitions found.")
        else:
            for row in rows:
                suffix = f" ({row['error']})" if row["error"] else ""
                raw_failures = row["consecutive_failures"]
                failures = raw_failures if isinstance(raw_failures, int) else 0
                if failures:
                    suffix += f"; {failures} consecutive failure(s)"
                label = row["identifier"] or "unreadable"
                print(f"{row['name']} ({label}): {row['state']}{suffix}")
    return 0


def _elsewhere(name: str, root: Path) -> list[dict[str, object]]:
    """Rows for a name this repository does not have, if a peer does.

    A read of a registered repository owns no state, so the fallback is
    safe to run whenever the local lookup came back empty (#388); an
    explicit repository selection still narrows it away. Reporting is
    not selection, so several answers are all listed rather than refused:
    the caller sees each qualified identifier and can pick one.
    """
    if resolve.repository_pinned():
        return []
    found: list[dict[str, object]] = []
    for candidate in resolve.registered_roots(exclude=root):
        found.extend(_rows(candidate, name))
    return found


if __name__ == "__main__":
    raise SystemExit(main())
