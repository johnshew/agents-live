"""Read runtime health and optionally invoke the one convergence path."""
from __future__ import annotations

import argparse
import json
import os
import sys

from ... import runtime, state
from ...state import registry as repos
from .. import lifecycle, update_check


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all-repos", action="store_true")
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    checks: list[dict[str, object]] = []
    try:
        registry = repos.load()
    except ValueError as exc:
        checks.append({"check": "repository registry", "ok": False, "detail": str(exc)})
        registry = None
    else:
        checks.append({"check": "repository registry", "ok": True,
                       "detail": f"{len(registry['repos'])} registered"})
    try:
        health = runtime.health()
    except (OSError, RuntimeError, ValueError) as exc:
        checks.append({
            "check": "host runtime", "ok": False, "detail": str(exc)})
    else:
        checks.append({
            "check": "host runtime",
            "ok": health.healthy,
            "detail": "; ".join(health.detail) or health.liveness,
        })
    if registry is not None:
        for name, value in sorted(registry["repos"].items()):
            root = state.resolve_root(value) if os.path.isdir(value) else None
            if root is None:
                checks.append({
                    "check": f"repository {name}", "ok": False,
                    "detail": "registered but cannot be read; its triggers are removed",
                })
                continue
            try:
                state.load(root)
            except state.StartedStateUnavailable as exc:
                checks.append({
                    "check": f"started state {name}", "ok": False, "detail": str(exc)})
            else:
                checks.append({
                    "check": f"started state {name}", "ok": True, "detail": "readable"})
        try:
            collected = lifecycle.collect(persist=False)
        except lifecycle.CollectionUnavailable as exc:
            checks.append({
                "check": "definition collection", "ok": False,
                "detail": str(exc),
            })
        else:
            for detail in collected.unavailable_repositories:
                checks.append({
                    "check": "definition collection", "ok": False,
                    "detail": detail,
                })
    if args.repair or args.dry_run:
        try:
            result = lifecycle.converge(dry_run=args.dry_run)
        except lifecycle.CollectionUnavailable as exc:
            checks.append({"check": "repair", "ok": False, "detail": str(exc)})
        else:
            checks.append({
                "check": "repair",
                "ok": not result.failed,
                "detail": f"{len(result.done)} changes, {len(result.failed)} failures",
            })
    ok = all(bool(item["ok"]) for item in checks)
    if os.environ.get("AGENTS_LIVE_JSON") == "1":
        print(json.dumps({"ok": ok, "checks": checks}))
    else:
        for item in checks:
            print(f"{'ok' if item['ok'] else 'ERROR'}: {item['check']}: {item['detail']}")
        if update_check.interactive():
            print(f"\n{update_check.status_text()}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
