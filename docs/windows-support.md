---
title: Native Windows Support
description: Current native Windows host architecture, security model, and operational boundaries
ms.date: 2026-08-17
ms.topic: concept
---

# Native Windows support

Agents Live runs natively on Windows without WSL. The common runtime sees the
same trigger, change-source, supervisor, and child-runner protocols used on
POSIX. Windows-specific behavior stays under `runtime/hosts/`.

This document covers native Windows. WSL uses the POSIX runtime plus a
Windows-side liveness task and is documented in
[wsl-support.md](wsl-support.md).

## Code ownership

| Responsibility | Module |
|---|---|
| Host composition | `runtime/hosts/windows.py` |
| Task Scheduler store and command-line rules | `runtime/hosts/task_scheduler.py` |
| Filesystem change source | `runtime/hosts/windows_watch.py` |
| Process enumeration and tree termination | `runtime/hosts/processes.py` |
| Windowless task child | `runtime/hosts/hidden.py` |
| Shared host utilities | `runtime/hosts/system.py` |

`runtime/hosts/posix.py` supplies common subscription translation where the
meaning is platform-independent. Windows overrides the native stores and
process behavior, not lifecycle policy.

## Scheduling

One owned Task Scheduler artifact represents one runtime subscription. All
tasks live under `\AgentsLive\` and carry a structured marker containing
scope, subscription identity, fingerprint, and process role.

Calendar schedules use exact native triggers when Task Scheduler can express
them. Other supported cron expressions use a bounded coarse repetition plus a
dueness check. Dueness atomically claims the minute, so a coarse trigger cannot
run the same logical firing twice. `@reboot` maps to a logon trigger because
tasks run in the developer's interactive session.

Task actions pin the installed CLI path and an explicit repository. They do
not depend on the Task Scheduler service's PATH or working directory.

Task Scheduler stores one argument string rather than an argv vector.
`task_scheduler.py` owns quoting and verifies that every generated string
round-trips through Windows command-line parsing before registration.

## Windowless actions

A console executable launched by Task Scheduler with an interactive token can
open a visible window on every firing. Agents Live therefore uses the
`pythonw` interpreter beside the installed runtime and executes:

```text
pythonw -P -m agents_live.runtime.hosts.hidden <command> <arguments>
```

The hidden module starts the real child with `CREATE_NO_WINDOW`, preserving
normal child streams without drawing a console.

Tasks created before this module moved may contain `agents_live.hidden`.
Read-back recognizes both paths, while new tasks use the owned host module.
The package-root entry point remains through 6.x and is removed in 7.0 after
convergence has replaced old artifacts.

## File watching

The Windows source uses `ReadDirectoryChangesW` directly. It emits primitive
path changes into the generic watch loop, where common include, exclude, and
debounce policy applies.

Each watched definition has one resident watcher process and one reboot/logon
artifact that restores it. The structured marker and process argv carry its
subscription identity and fingerprint, so convergence can detect drift without
a separate watcher index.

Buffer overflow degrades to one bounded rescan rather than silently losing the
fact that something changed.

## Processes

Windows has no POSIX process groups or PTYs. The host process layer therefore:

- creates and tracks process trees using Windows process identity;
- terminates descendants before the parent when a run times out or stops;
- captures child text explicitly as UTF-8 unless a native tool documents a
  different encoding; and
- refuses shell shim files when a native executable is required.

Task Scheduler output is decoded using the console code page it actually
writes. PowerShell invocations explicitly request UTF-8 output.

## Security model

Tasks use the developer's interactive token and limited run level. This keeps
the same credentials, filesystem access, and provider CLI login as an
interactive invocation without storing a password in Task Scheduler.

The consequence is intentional: no signed-in session means no scheduled run.
Running while logged off would require stored credentials or S4U and is a
different security posture.

All owned tasks live in one folder, generated names and arguments are read back
before replacement or deletion, and unreadable artifacts fail closed. An
artifact Agents Live cannot prove it owns is left untouched.

The dashboard binds to loopback only. Remote exposure requires a separately
designed authentication and transport boundary.

## Ownership and paths

Native Windows ownership uses the same repository and optional assignment
model as other hosts. Repository identity is based on normalized paths and
generated host identity, not display names.

Persisted actions use absolute executable and repository paths. Common code
uses `pathlib`; Windows command-line comparison uses `PureWindowsPath` so
results do not depend on which platform runs a test.

## Validation

Windows CI runs the portable smoke and seam suites. Pure Task Scheduler
translation and quoting tests run on every platform; operating-system mutation
is additionally exercised on Windows.

Before release, validate both editable source and the built wheel:

```powershell
uv run --with-editable . --script tests/test_smoke.py
uv run --with-editable . --script tests/test_seams.py
uv run --with-editable . agents-live smoketest
uv build
uvx --from .\dist\agents_live-<version>-py3-none-any.whl agents-live smoketest
```

Use temporary repositories for mutating tests. A passing source invocation
does not prove the wheel or installed uv tool works.

Platform defects should be fixed in the Windows host adapter or shared protocol
that owns the behavior, not as call-site branches in common code.
