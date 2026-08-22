"""Manage registered repositories through the state registry port."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ... import plugins, preflight
from ...state import registry


def _converge_registered(root: Path) -> None:
    try:
        if plugins.converge([root], trigger="repos-register"):
            print("Converged declared plugins in the agents-live tool environment")
    except (OSError, ValueError, plugins.PluginError) as exc:
        print(
            f"warning: declared plugins could not be installed: {exc}; "
            "run `agents-live doctor` for details",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage registered repositories")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("list", help="List registered repositories")
    add = subparsers.add_parser("add", help="Register a repository")
    add.add_argument(
        "path", help="Repository root directory (registered under its directory name)")
    default = subparsers.add_parser(
        "default", help="Set or clear the fallback repository")
    default.add_argument(
        "repo", nargs="?",
        help="Repository path or registered directory name")
    default.add_argument(
        "--clear", action="store_true",
        help="Clear the configured default repository")
    remove = subparsers.add_parser("remove", help="Remove a registered repository")
    remove.add_argument("repo", help="Registered repository path or name")
    subparsers.add_parser("help", help="Show this help message")
    args = parser.parse_args(argv)
    try:
        if args.action == "help":
            parser.print_help()
        elif args.action == "add":
            _converge_registered(registry._add(args.path))
        elif args.action == "default":
            if args.clear:
                if args.repo:
                    default.error(
                        "--clear cannot be combined with a repository name or path")
                registry._clear_default()
            elif not args.repo:
                default.error("default requires a repository or --clear")
            else:
                _converge_registered(registry._set_default(args.repo))
        elif args.action == "remove":
            registry._remove(args.repo)
        else:
            current = registry.load()
            rows = registry.entries(current)
            if preflight.json_mode():
                print(json.dumps({
                    "ok": True,
                    "repositories": [
                        {
                            "name": alias,
                            "path": path,
                            "default": alias == current["default_repo"],
                            "available": error is None,
                            "error": error,
                        }
                        for alias, path, error in rows
                    ],
                }))
            elif not current["repos"]:
                print("No repositories registered")
            else:
                for alias, path, error in rows:
                    marker = " (default)" if alias == current["default_repo"] else ""
                    suffix = f" [unavailable: {error}]" if error else ""
                    print(f"{alias}{marker}\t{path}{suffix}")
        return 0
    except (OSError, ValueError) as exc:
        preflight.emit_failure("repos", str(exc))
        return 1