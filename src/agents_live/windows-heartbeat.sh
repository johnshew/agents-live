#!/usr/bin/env bash
# windows-heartbeat.sh - Keep WSL alive so cron-based Agents Live agents keep running.
# Called by Windows Task Scheduler every 5 minutes.
#
# Writes Agents/data/heartbeat.ok on each successful run so the health check
# and dashboard can confirm the Windows scheduler is working.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
LOG_DIR="$REPO_DIR/Agents/logs"
LOG_FILE="$LOG_DIR/heartbeat.log"
DATA_DIR="$REPO_DIR/Agents/data"
HEARTBEAT_OK="$DATA_DIR/heartbeat.ok"

mkdir -p "$LOG_DIR" "$DATA_DIR"

log() {
    local ts
    ts="$(date -Is)"
    echo "$ts $1" >> "$LOG_FILE"
}

# Battery awareness: skip if on battery and WSL was poked < 10 min ago
on_battery() {
    grep -q "Discharging" /sys/class/power_supply/BAT*/status 2>/dev/null
}

STAMP="/tmp/life-heartbeat-stamp"

if on_battery; then
    last=$(stat -c %Y "$STAMP" 2>/dev/null || echo 0)
    now=$(date +%s)
    if (( now - last < 600 )); then
        exit 0
    fi
fi

# Keep systemd alive so cron jobs fire
systemctl --user status > /dev/null 2>&1
touch "$STAMP"

# Write health beacon
echo "alive $(date '+%Y-%m-%d %H:%M %Z')" > "$HEARTBEAT_OK"

log "heartbeat: WSL alive"