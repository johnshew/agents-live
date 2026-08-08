---
title: Diagnostics
description: Diagnose definitions, convergence, dispatch, and WSL liveness
ms.date: 2026-08-08
ms.topic: troubleshooting
---

# Diagnostics

Start with read-only commands:

```bash
agents-live status --all-repos
agents-live doctor --all-repos
agents-live logs timeline --all
```

Use `agents-live doctor --repair --dry-run` to preview the one convergence diff
and `agents-live doctor --repair` to apply it.

## Definition failures

The loader reports the exact `SKILL.md` and rejected property. Common causes
are an unquoted metadata value, an unknown `agents-live.*` key, a directory and
`name` mismatch, invalid selector or trigger syntax, a path that escapes the
skill, or a 5.x flat definition. Use `agents-live migrate --dry-run` before the
one-shot conversion.

## Collection failures

An unreadable registry, ownership source, or started-state record causes
convergence to abstain. Repair that input rather than deleting runtime
artifacts manually. An unreadable registered repository is different: it has
no desired subscriptions, so its structured owned artifacts are pruned.

## Trigger and watcher drift

`doctor --repair --dry-run` shows install, remove, start, and stop operations.
A changed canonical watch expression changes its fingerprint and restarts only
that watcher. All watchers restart once when moving from the 5.x fingerprint
form to 6.0.

Never inspect runtime log files by hand. Use `agents-live logs` and
`agents-live logs timeline`; they correlate versioned event records and
provider transcripts.

## Dispatch skips

Automatic firings can be skipped because the definition is stopped, a clock
fire is not due, another run of the same agent holds the lock, or the durable
dispatch budget is exhausted. These are successful skip outcomes, not child
failures.

Failure categories include `state_unavailable`, `agent_invalid`, `timeout`,
`cli_crash`, `pre_processor_crash`, `post_processor_crash`,
`empty_output`, `output_parse_error`, and `agent_output_invalid`.

## WSL liveness

There is no public heartbeat command.

```bash
agents-live doctor
agents-live doctor --repair
```

A repair stages a distinct Windows task and requires a fresh beacon before
swapping. If it fails, verify PowerShell interop, the stable uv tool shim,
`wslg.exe`, Task Scheduler policy, and `WSL_DISTRO_NAME` in the interactive
session. The previous working task remains registered after a failed stage.
