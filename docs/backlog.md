---
title: High-Level Backlog
description: Themes and direction for agents-live, linked to the GitHub issues that carry the detail
ms.date: 2026-07-25
ms.topic: concept
---

Direction at the theme level. Individual work items, bugs, and acceptance
criteria live in GitHub issues (`gh issue list`); this file exists so the
shape of the work stays visible when the issue list does not show it.

A theme without an open issue is direction, not committed work.

## Local observability

Give the existing JSONL event stream span identity, parent-child
correlation, and sensitivity classification, so nested phase timing and
causality are explicit locally. OpenTelemetry, OTLP export, and any
off-box log shipment stay postponed until a consumer needs them; a future
sidecar can translate the local schema instead.
([#105](https://github.com/johnshew/agents-live/issues/105))

The same stream should also answer whether the agents are working, not
just what they did. An agent that fails every run stays invisible today:
status keeps showing a fresh error time, and the health beacon reports
only infrastructure state. Escalating that from existing log data comes
before any richer export.
([#123](https://github.com/johnshew/agents-live/issues/123))

## Safer execution modes in practice

`plan` and `pipeline` are documented as the safe defaults, but the
runnable example in the README uses `write`. Publish complete `plan` and
`pipeline` variants of the same watcher task, including schemas,
handlers, and processors, so the safer modes are as easy to adopt as the
permissive one.
([#116](https://github.com/johnshew/agents-live/issues/116))

## Platform coverage

Linux is the primary platform, with Ubuntu on WSL as the reference setup.
macOS is untested; broadening it is direction rather than committed work,
so file an issue before starting.

A native Windows runtime, replacing cron and inotifywait with Task
Scheduler and Windows change notification behind a small host-runtime
seam, is implemented and covered by CI on `windows-latest`.
[windows-support.md](windows-support.md) is the architecture guide: what
the seam is, why it is functions rather than a protocol object, and what
the spikes contradicted. Whether the Windows half earns its keep in the
long run stays an open product question; the engineering question is
settled.

## Maintaining this file

- Add a theme when work spans several issues or needs a stated direction
  of its own.
- Link each theme to its open issues; do not duplicate their content.
- Rewrite or remove a theme once it ships, in the same change that ships
  it.
