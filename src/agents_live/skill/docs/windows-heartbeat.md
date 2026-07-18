---
title: Agents Live Windows Heartbeat
description: Keep WSL available for scheduled Agents Live agents with Windows Task Scheduler
ms.date: 2026-07-15
ms.topic: how-to
---

## Windows heartbeat

WSL2 auto-terminates its VM after a short idle period (~8 seconds with no
active connections). When the machine enters Modern Standby, the VM is killed
immediately. The Windows heartbeat prevents idle termination by poking WSL
every 5 minutes from Windows Task Scheduler.

## How it works

1. **Windows Task Scheduler** runs `run-hidden.vbs` every 5 minutes.
2. `run-hidden.vbs` silently launches `wsl.exe -- bash .../windows-heartbeat.sh`.
3. The script touches systemd to keep WSL alive and writes `Agents/data/heartbeat.ok`.

## Health check

The agents-live health check (hourly cron) verifies `heartbeat.ok` is
less than 10 minutes old. If stale or missing, it logs a warning.

Run `agents-live doctor` for an out-of-band check. It verifies both recent
end-to-end execution through `heartbeat.ok` and, when Windows PowerShell
interop is available, the registered task's enabled state, script paths, and
five-minute repetition interval. This catches stale paths even while a marker
from an earlier successful run is still fresh.

## Diagnosing

```powershell
# 1. Is the task registered?
Get-ScheduledTask -TaskName "WSL Heartbeat" -ErrorAction SilentlyContinue

# 2. Is it enabled?
Get-ScheduledTask -TaskName "WSL Heartbeat" | Select-Object State

# 3. When did it last run?
Get-ScheduledTaskInfo -TaskName "WSL Heartbeat" | Select-Object LastRunTime, LastTaskResult

# 4. Check from WSL side
cat ~/repos/your-repo/Agents/data/heartbeat.ok
stat ~/repos/your-repo/Agents/data/heartbeat.ok
tail ~/repos/your-repo/Agents/logs/heartbeat.log
```

## Installing the scheduled task

Run in an **elevated PowerShell** (Admin):

In a packaged install, point the task at the scripts inside the
installed package — never at a repo checkout, whose copies can move or
be deleted — and pass the repo root as the script's argument (the
walk-up default only resolves the repo in the flat checkout). Find the
packaged copies with:

```bash
find ~/.local/share/uv/tools/agents-live -name windows-heartbeat.sh -o -name run-hidden.vbs
```

```powershell
# Adjust distro name, user, repo, and Python version for your machine
# (the site-packages segment embeds the Python minor version — re-run
# the find above and re-register after interpreter upgrades):
$vbsPath = "\\wsl.localhost\Ubuntu\home\you\.local\share\uv\tools\agents-live\lib\python3.13\site-packages\agents_live\run-hidden.vbs"
$shPath  = "/home/you/.local/share/uv/tools/agents-live/lib/python3.13/site-packages/agents_live/windows-heartbeat.sh"
$repo    = "/home/you/repos/your-repo"

$action  = New-ScheduledTaskAction -Execute "wscript.exe" `
    -Argument "`"$vbsPath`" `"wsl.exe -d Ubuntu -- bash $shPath $repo`""

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName "WSL Heartbeat" `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description "Keep WSL alive for Agents Live agents" `
    -RunLevel Limited
```

Omitting `-RepetitionDuration` repeats indefinitely. Do **not** pass
`([TimeSpan]::MaxValue)` -- Task Scheduler rejects the resulting
`P99999999DT23H59M59S` XML with "value which is incorrectly formatted or out
of range".

## Removing the scheduled task

```powershell
Unregister-ScheduledTask -TaskName "WSL Heartbeat" -Confirm:$false
```

## Limitations

- Does **not** prevent Modern Standby shutdown. When Windows sleeps, Task
  Scheduler stops and WSL is terminated. On wake, WSL restarts on first
  access and the heartbeat resumes.
- Battery-aware: if on battery, the script skips if WSL was poked in the
  last 10 minutes (reduces wake-ups).
