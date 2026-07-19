"""TRANSITIONAL state cutover - DELETE after fleet convergence.

One-time move of machine-local runtime state out of the project tree
into the user-level XDG state home (2026-07-19 layout change):

- ``Agents/logs/*``            -> ``<state home>/repos/<key>/logs/``
- ``Agents/data/*-watch-hashes.json`` -> ``<state home>/repos/<key>/``
- ``Agents/data/health.ok``, ``Agents/data/smoketest-framework.lock``
  are regenerable and simply deleted.

``agent-owners.json`` is deliberately untouched: it is git-synced shared
state and stays in the tree.

Runs from ``migrate.main`` on every pass (idempotent, no-op once the
legacy locations are empty), so every host converges automatically via
the hourly built-in health-check loop. The whole transition lives in
this module plus one call site in ``migrate.py``: to retire it, delete
this file and that call.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import paths

_EPHEMERAL = ("health.ok", "smoketest-framework.lock")


def plan(root: Path) -> dict[str, list]:
    """{"moves": [(src, dest)], "deletes": [path]} for legacy state."""
    moves: list[tuple[Path, Path]] = []
    deletes: list[Path] = []
    state_dir = paths.repo_state_dir(root)
    legacy_logs = root / "Agents" / "logs"
    if legacy_logs.is_dir():
        for src in sorted(p for p in legacy_logs.rglob("*") if p.is_file()):
            if src.name == ".gitkeep":
                continue
            moves.append((src, state_dir / "logs" / src.relative_to(legacy_logs)))
    legacy_data = root / "Agents" / "data"
    for name in _EPHEMERAL:
        candidate = legacy_data / name
        if candidate.is_file():
            deletes.append(candidate)
    if legacy_data.is_dir():
        for src in sorted(legacy_data.glob("*-watch-hashes.json")):
            moves.append((src, state_dir / src.name))
    return {"moves": moves, "deletes": deletes}


def apply(root: Path, *, dry_run: bool = False) -> int:
    """Execute (or print) the cutover; returns the number of actions."""
    planned = plan(root)
    if not planned["moves"] and not planned["deletes"]:
        return 0
    verb = "Would move" if dry_run else "Moving"
    for src, dest in planned["moves"]:
        print(f"{verb} legacy state {src} -> {dest}")
        if dry_run:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            if dest.suffix == ".log":
                # Both exist: the new location already collected events
                # after the upgrade. Legacy content is older, so it goes
                # first; qlog/timeline order by timestamp regardless.
                merged = src.read_bytes() + dest.read_bytes()
                dest.write_bytes(merged)
                src.unlink()
            else:
                src.unlink()  # regenerated at the new home already; drop
        else:
            shutil.move(str(src), str(dest))
    verb = "Would delete" if dry_run else "Deleting"
    for path in planned["deletes"]:
        print(f"{verb} regenerable legacy state {path}")
        if not dry_run:
            path.unlink(missing_ok=True)
    if not dry_run:
        # Tidy now-empty legacy directories (kept when .gitkeep or
        # anything else remains).
        legacy_logs = root / "Agents" / "logs"
        if legacy_logs.is_dir():
            for directory in sorted(
                    (p for p in legacy_logs.rglob("*") if p.is_dir()),
                    key=lambda p: len(p.parts), reverse=True):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            try:
                legacy_logs.rmdir()
            except OSError:
                pass
    return len(planned["moves"]) + len(planned["deletes"])
