---
title: Windows task for WSL liveness
description: How a Windows-side task keeps Agents Live scheduled work available in WSL
ms.date: 2026-08-14
ms.topic: concept-article
---

# Windows task for WSL liveness

WSL can stop when no Windows-side process keeps the distribution available.
Agents Live therefore treats liveness as runtime-owned durable state, not as a
user lifecycle command.

On the first real convergence, the WSL host adapter:

1. registers a Task Scheduler entry under a distinct staged name;
2. starts it immediately;
3. waits for a fresh, atomically written liveness beacon;
4. registers and starts the stable distro-scoped task;
5. removes the staged task; and
6. removes a legacy task only after the verified replacement exists.

If staging or verification fails, the working task is left unchanged and
convergence reports unhealthy. Run `agents-live doctor --repair` after fixing
the reported Task Scheduler, interop, launcher, or stable-shim problem.

The action invokes the stable uv tool shim as:

```text
agents-live internal liveness
```

The internal command refreshes the machine-local beacon. It is not a public
lifecycle verb. `agents-live uninstall` removes the distro task and, unless
state retention was requested, its local beacon and log.

## Diagnostics

```bash
agents-live doctor
agents-live doctor --repair
```

Healthy means the POSIX trigger store is readable, the distro-scoped Windows
task has the expected windowless action and five-minute interval, and its
beacon is no more than ten minutes old. A cron or systemd session may not carry
`WSL_DISTRO_NAME`; a fresh beacon remains the read-side liveness fact in that
case. See [diagnostics.md](diagnostics.md#wsl-liveness) for restart recovery,
shell initialization, and 9P failure diagnosis.
