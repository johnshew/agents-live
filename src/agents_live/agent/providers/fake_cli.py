"""Deterministic subprocess for provider and ChildRunner tests."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--settings")
    parser.add_argument("--mcp-config", dest="mcp_config")
    args = parser.parse_args()
    action = os.environ.get("AGENTS_LIVE_FAKE_ACTION", "success")
    if action == "crash":
        return 7
    if action == "empty":
        return 0
    payload: dict[str, object] = {
        "text": args.prompt,
        "structured": {"prompt": args.prompt},
    }
    # Proof that run-scoped configuration was materialized before launch
    # and is reachable from the child, not merely named in its argv.
    for key, value in (("settings", args.settings), ("mcp", args.mcp_config)):
        if value:
            payload[key] = json.loads(Path(value).read_text(encoding="utf-8"))
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
