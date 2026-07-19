#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML"]
# ///
from __future__ import annotations

import argparse
import sys

from .headless import (
    AgentsLiveError,
    load_agent_config,
    remove_cron_entries,
    remove_watcher_reboot_line,
    stop_watcher,
)

from . import preflight


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    args = parser.parse_args()

    try:
        trigger_type = ""
        try:
            trigger_type = load_agent_config(args.name).trigger_type
        except AgentsLiveError:
            trigger_type = ""

        if trigger_type in {"", "cron", "multi"}:
            removed = remove_cron_entries(args.name)
            if removed:
                print(f"Removed cron entry for '{args.name}'")
            elif trigger_type == "cron":
                print(f"No cron entry found for '{args.name}' (may not have been started)")

        if trigger_type in {"", "watcher", "multi"}:
            pid = stop_watcher(args.name)
            remove_watcher_reboot_line(args.name)
            if trigger_type in {"watcher", "multi"}:
                if pid is not None:
                    print(f"Stopped watcher for '{args.name}'")
                else:
                    print(f"No watcher found for '{args.name}'")

        print(f"Agent '{args.name}' stopped (config preserved)")
        return 0
    except AgentsLiveError as exc:
        # Layer 2 (§3.6): typed errors leave as the envelope in json
        # mode, one concise stderr line otherwise.
        preflight.emit_typed_error(exc, "stop")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
