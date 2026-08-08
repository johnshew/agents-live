"""Deterministic subprocess for provider and ChildRunner tests."""
from __future__ import annotations

import argparse
import json
import os


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", required=True)
    args = parser.parse_args()
    action = os.environ.get("AGENTS_LIVE_FAKE_ACTION", "success")
    if action == "crash":
        return 7
    if action == "empty":
        return 0
    print(json.dumps({"text": args.prompt, "structured": {"prompt": args.prompt}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
