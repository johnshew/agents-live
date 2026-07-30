#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Write a validated JSON files array beneath the current project root."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"[handler] ERROR: input is not valid JSON: {exc}", file=sys.stderr)
        return 1

    files = payload.get("files", []) if isinstance(payload, dict) else []
    if not isinstance(files, list) or not files:
        print("[handler] WARNING: no files in output", file=sys.stderr)
        summary = payload.get("summary") if isinstance(payload, dict) else None
        print(summary or "No summary provided", file=sys.stderr)
        return 0

    root = Path.cwd().resolve()
    for entry in files:
        if not isinstance(entry, dict):
            print("[handler] WARNING: skipping invalid file entry", file=sys.stderr)
            continue
        relative = entry.get("path")
        content = entry.get("content")
        if not isinstance(relative, str) or not relative:
            print("[handler] WARNING: skipping entry with no path", file=sys.stderr)
            continue
        if not isinstance(content, str):
            print(f"[handler] WARNING: skipping {relative}: content is not text",
                  file=sys.stderr)
            continue
        destination = (root / relative).resolve()
        if Path(relative).is_absolute() or not destination.is_relative_to(root):
            print(f"[handler] ERROR: rejecting unsafe path: {relative}",
                  file=sys.stderr)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content + "\n", encoding="utf-8")
        print(f"[handler] wrote: {relative}", file=sys.stderr)

    summary = payload.get("summary")
    if isinstance(summary, str) and summary:
        print(f"[handler] {summary}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
