#!/usr/bin/env bash
# windows-heartbeat.sh - Keep WSL alive so cron-based Agents Live agents keep running.
# Called by Windows Task Scheduler every 5 minutes.
#
# Compatibility wrapper for legacy scheduled tasks.
#
CLI="${AGENTS_LIVE_EXECUTABLE:-$HOME/.local/bin/agents-live}"
if [[ ! -x "$CLI" ]]; then
    echo "windows-heartbeat.sh: agents-live uv shim not found: $CLI" >&2
    exit 1
fi
# A legacy task reaches this wrapper after an upgrade. Installing the
# canonical task verifies its beacon before removing the task currently
# running this wrapper, so migration needs no manual task deletion.
exec "$CLI" heartbeat install --distro "${WSL_DISTRO_NAME:?WSL_DISTRO_NAME is not set}"