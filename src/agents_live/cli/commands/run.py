"""Execute one definition through the dispatch handoff."""
from __future__ import annotations

import argparse
import json
import sys

from ... import paths
from ...dispatch import Firing, dispatch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--changed-files")
    parser.add_argument("--scheduled", action="store_true")
    parser.add_argument("--boot", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--artifact-marker")
    parser.add_argument("--runtime-role")
    parser.add_argument("--subscription-key", default="")
    parser.add_argument("--subscription-fingerprint")
    args = parser.parse_args(argv)
    try:
        changed = tuple(json.loads(args.changed_files)) if args.changed_files else ()
        if not all(isinstance(item, str) for item in changed):
            raise ValueError("--changed-files must be a JSON string array")
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    origin = (
        "boot" if args.boot else
        "clock" if args.scheduled else
        "watch" if changed else
        "manual"
    )
    result = dispatch(Firing(
        args.name,
        str(paths.resolve_root()),
        origin,
        args.subscription_key,
        changed,
    ))
    if not args.quiet:
        if result.ok and result.text:
            print(result.text)
        elif not result.ok:
            print(result.message, file=sys.stderr)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
