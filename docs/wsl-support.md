---
title: WSL Support
description: Current WSL host architecture, Windows-side liveness, migration, and limitations
ms.date: 2026-08-14
ms.topic: concept
---

# WSL support

WSL uses the POSIX scheduler, watcher, and process implementation inside the
distribution. It adds one host responsibility: a Windows-side scheduled task
keeps the distribution available so cron automation continues to run.

## Runtime composition

`WslHost` extends `PosixHost`. It therefore uses:

- crontab for schedule and watcher-reboot artifacts;
- the POSIX filesystem source and generic watch loop;
- POSIX child process and PTY behavior; and
- the same convergence and dispatch paths as Linux.

Before convergence, `WslHost.prepare()` asks `wsl_liveness` to verify the
Windows-side task and a recent beacon. Host health combines POSIX health with
that beacon.

## Liveness task

The canonical task is scoped to the WSL distribution and repeats every five
minutes. Its action uses WSL's windowless launcher and the stable uv tool shim:

```text
wslg.exe -d <distribution> -- ~/.local/bin/agents-live internal liveness
```

The internal command pokes the user systemd manager when available and writes
an atomic beacon under the host state directory. It does not collect projects
or run agents.

Installation is staged:

1. register a distinct staged task;
2. start it;
3. wait for a fresh beacon;
4. register and start the stable task;
5. remove the staged task; and
6. remove the historical task only after the replacement is verified.

A failed stage leaves the prior working task intact.

## Historical task migration

Some 5.x installations persisted an action pointing directly to
`agents_live/windows-heartbeat.sh`. That exact package-root path must remain
available through 6.x. The wrapper now runs `internal liveness`, then asks the
CLI to install the canonical distro task. Once migration succeeds, the
historical task is removed.

The wrapper is not canonical WSL implementation and must not gain features.
It is deleted with the rest of the 5.x artifact compatibility surface in 7.0.

## Diagnostics and repair

Use:

```bash
agents-live doctor
agents-live doctor --repair
```

Healthy WSL liveness means the POSIX trigger store is readable, the
distro-scoped Windows task has the expected windowless action and interval,
and the beacon is no more than ten minutes old.

An ssh, cron, or systemd session may not carry `WSL_DISTRO_NAME`. A fresh
beacon remains authoritative for read-only health in that context. Installing
or replacing a task requires a known distribution name and working Windows
PowerShell interop.

## Boundaries

- WSL is not native Windows. Agent processes and handlers run inside Linux.
- Windows-side Task Scheduler owns only distro liveness, not agent schedules.
- The stable CLI path must exist inside the distribution.
- Cross-distribution installation is refused because verification observes
  the current distribution's filesystem.
- A moved or renamed distribution requires task repair from inside that
  distribution.

The user-facing references are
[windows-heartbeat.md](../src/agents_live/skill/docs/windows-heartbeat.md) for
the liveness mechanism and
[diagnostics.md](../src/agents_live/skill/docs/diagnostics.md#wsl-liveness) for
repair, shell initialization, and 9P troubleshooting.