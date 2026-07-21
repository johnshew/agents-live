"""TRANSITIONAL state cutover - DELETE after fleet convergence.

One-time move of machine-local runtime state out of the project tree
into the user-level XDG state home (2026-07-19 layout change):

- ``Agents/logs/*``            -> ``<state home>/repos/<key>/logs/``
- ``Agents/data/*-watch-hashes.json`` -> ``<state home>/repos/<key>/``
- ``Agents/data/health.ok``, ``Agents/data/smoketest-framework.lock``
  are regenerable and simply deleted.

``agent-owners.json`` is deliberately untouched: it is git-synced shared
state and stays in the tree.

Runs from ``migrate.main`` and each upgrade target refresh (idempotent,
no-op once the legacy locations are empty), so hosts converge through
ordinary upgrades or the hourly built-in health-check loop. To retire
the transition, delete this module and both call sites.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from . import paths

_EPHEMERAL = ("health.ok", "smoketest-framework.lock")


def _append_legacy_log(src: Path, dest: Path) -> None:
    """Append once across retries, including failure after the append."""
    raw_legacy = src.read_bytes()
    while True:
        current = src.read_bytes()
        if current == raw_legacy:
            break
        raw_legacy = current
    journal = dest.with_name(f".{dest.name}.legacy-migration.json")
    start = dest.stat().st_size
    if journal.is_file():
        try:
            receipt = json.loads(journal.read_text(encoding="utf-8"))
            original_size = int(receipt["source_size"])
            original_digest = str(receipt["source_sha256"])
            if hashlib.sha256(raw_legacy[:original_size]).hexdigest() != original_digest:
                raise OSError(f"legacy log changed before migration retry: {src}")
            start = int(receipt["destination_size"])
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raise
    else:
        original_size = len(raw_legacy)
        original_digest = hashlib.sha256(raw_legacy).hexdigest()
        receipt = {
            "source_size": original_size,
            "source_sha256": original_digest,
            "destination_size": start,
            "batches": [],
        }
        paths.atomic_write_text(journal, json.dumps(receipt) + "\n")

    def append_missing(payload: bytes) -> None:
        if not payload:
            return
        if not payload.endswith(b"\n"):
            payload += b"\n"
        digest = hashlib.sha256(payload).hexdigest()
        for batch in receipt.get("batches", []):
            if batch.get("sha256") != digest or batch.get("size") != len(payload):
                continue
            with dest.open("rb") as existing:
                existing.seek(int(batch["offset"]))
                if existing.read(len(payload)) == payload:
                    return
        with dest.open("ab") as merged:
            merged.write(payload)
            merged.flush()
            os.fsync(merged.fileno())
            offset = merged.tell() - len(payload)
        receipt.setdefault("batches", []).append({
            "offset": offset,
            "size": len(payload),
            "sha256": digest,
        })
        paths.atomic_write_text(journal, json.dumps(receipt) + "\n")

    append_missing(raw_legacy[:original_size])
    consumed = original_size
    while True:
        current = src.read_bytes()
        if hashlib.sha256(current[:original_size]).hexdigest() != original_digest:
            raise OSError(f"legacy log changed during migration: {src}")
        if len(current) > original_size and raw_legacy[:original_size] and not raw_legacy[:original_size].endswith(b"\n"):
            raise OSError(f"unterminated legacy log grew during migration: {src}")
        append_missing(current[consumed:])
        consumed = len(current)
        if src.read_bytes() == current:
            break
    src.unlink()
    journal.unlink(missing_ok=True)


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
                # after the upgrade. Append the legacy content rather
                # than rewriting dest, so concurrent appenders at the new
                # home are never truncated; qlog/timeline order by
                # timestamp, not file position. Guard the join so a
                # legacy file cut mid-write cannot fuse two JSONL records
                # into one unparseable line.
                _append_legacy_log(src, dest)
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
