#!/usr/bin/env bash
# windows-heartbeat.sh - Keep WSL alive so cron-based Agents Live agents keep running.
# Called by Windows Task Scheduler every 5 minutes.
#
# Compatibility wrapper for legacy scheduled tasks.
#
# A self-managed installation answers through its stable current link; a
# uv-managed one put a shim in ~/.local/bin. Prefer the former, because a
# generation install never creates the latter.
CLI="${XDG_DATA_HOME:-$HOME/.local/share}/agents-live/current/bin/agents-live"
if [[ ! -x "$CLI" ]]; then
    CLI="$HOME/.local/bin/agents-live"
fi
if [[ ! -x "$CLI" ]]; then
    echo "windows-heartbeat.sh: no agents-live command found: $CLI" >&2
    exit 1
fi
# Always perform the actual heartbeat first: it has zero Windows-side
# dependencies, so a failed or impossible migration below can never cost
# the keep-alive (systemd poke + beacon write) this task exists for.
"$CLI" internal liveness
heartbeat_status=$?
if [[ -z "${WSL_DISTRO_NAME:-}" ]]; then
    echo "windows-heartbeat.sh: WSL_DISTRO_NAME is not set; to migrate this legacy task, run agents-live doctor --repair inside the target distro" >&2
    exit $heartbeat_status
fi
# A legacy task reaches this wrapper after an upgrade. Installing the
# canonical task verifies its beacon before removing the task currently
# running this wrapper, so migration needs no manual task deletion.
# Migration failure (PowerShell interop off, registration policy) is
# only a warning: the heartbeat above already did the real work.
if ! "$CLI" internal install-liveness --distro "$WSL_DISTRO_NAME"; then
    echo "windows-heartbeat.sh: legacy-task migration failed; heartbeat still recorded" >&2
fi
exit $heartbeat_status
