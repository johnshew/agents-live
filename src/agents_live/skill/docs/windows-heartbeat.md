---
title: Agents Live Windows Heartbeat
description: Keep WSL available for scheduled Agents Live agents with Windows Task Scheduler
ms.date: 2026-07-18
ms.topic: how-to
---

## Windows heartbeat

WSL2 auto-terminates its VM after a short idle period (~8 seconds with no
active connections). When the machine enters Modern Standby, the VM is killed
immediately. The Windows heartbeat prevents idle termination by poking WSL
every 5 minutes from Windows Task Scheduler.

## How it works

One task per WSL distro runs this action every five minutes:

```text
wscript.exe "\\wsl.localhost\<distro>\...\agents_live\run-hidden.vbs"
    "wsl.exe -d <distro> --exec /home/<user>/.local/bin/agents-live heartbeat"
```

The packaged `run-hidden.vbs` wrapper launches wsl.exe with a hidden
window, so the five-minute cadence never flashes a console. The distro
argument selects the host runtime. The uv-managed shim selects the
currently installed agents-live version without pinning a checkout or a
Python-minor-version directory (the wrapper's own path tracks the
installed package through the `\\wsl.localhost` share). There is
deliberately no project binding: one host heartbeat serves every Agents
Live project in that distro.

The command writes `heartbeat.ok` and `heartbeat.log` under
`${XDG_STATE_HOME:-~/.local/state}/agents-live/`. Repository discovery is never
used. Missing state directories are created; an unknown distro or missing
stable CLI shim makes installation fail clearly.

## Health check

`agents-live doctor` verifies the shared beacon is less than 10 minutes old.

It also verifies the current distro's task is enabled, repeats every five
minutes, and invokes the stable shim through the hidden-window wrapper.
Direct `wsl.exe` actions (visible console), checkout, Python-versioned,
project-pinned, and legacy `WSL Heartbeat` actions produce a
re-registration or migration recommendation.

## Diagnosing

```powershell
# 1. Is the task registered?
$distro = "Ubuntu"
$task = "Agents Live Heartbeat ($distro)"
Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue

# 2. Is it enabled?
Get-ScheduledTask -TaskName $task | Select-Object State

# 3. When did it last run?
Get-ScheduledTaskInfo -TaskName $task | Select-Object LastRunTime, LastTaskResult

# 4. Check from WSL side
cat "${XDG_STATE_HOME:-$HOME/.local/state}/agents-live/heartbeat.ok"
tail "${XDG_STATE_HOME:-$HOME/.local/state}/agents-live/heartbeat.log"
```

## Installing the scheduled task

Install agents-live as a uv tool, then register the current distro:

```bash
uv tool install agents-live
agents-live heartbeat install --distro "$WSL_DISTRO_NAME"
```

A bare `uv tool install` does not carry project-declared plugin wheels;
if any project declares them, follow with `agents-live upgrade` (the
hourly `agents-live health-check` pass also converges them).

Registration is idempotent. It replaces a stale canonical action, starts the
new task, and waits for a fresh global beacon. Only after that verification
succeeds does it remove the legacy `WSL Heartbeat` task. If verification fails,
the legacy task remains. Upgraded copies of the temporary
`windows-heartbeat.sh` compatibility wrapper perform this migration
automatically when an old task next invokes them and its old path still
resolves. If a checkout, environment, or Python path was already removed,
`doctor` identifies the stale action and the same install command repairs it.

Editable development may exercise
`uv run --with-editable . agents-live heartbeat`, but an editable checkout must
not be persisted in Task Scheduler. Production registration requires the
executable `~/.local/bin/agents-live` uv shim. Package and Python upgrades can
therefore replace the shim's target without changing the task.

## Removing the scheduled task

```bash
agents-live heartbeat uninstall --distro "$WSL_DISTRO_NAME"
```

This removes only the selected distro's task and the generated beacon/log.
Pass `--retain-state` to keep those files; unrelated files in the state
directory are always preserved.

To remove both host integration and the uv tool, use `agents-live uninstall
[--retain-state]`. The package is removed only after Windows cleanup succeeds.
If `uv tool uninstall agents-live` was run first and left an orphaned task:

```bash
uvx agents-live heartbeat uninstall --distro <name>
```

Tasks never self-delete merely because the Linux shim is temporarily missing.
This supports normal upgrades and repairs; `doctor` reports the orphan.

## Limitations

- Does **not** prevent Modern Standby shutdown. When Windows sleeps, Task
  Scheduler stops and WSL is terminated. On wake, WSL restarts on first
  access and the heartbeat resumes.
