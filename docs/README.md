---
title: Agents Live Repository Documentation
description: Index of current architecture, decisions, platform guides, and backlog
ms.date: 2026-08-29
ms.topic: overview
---

Repository-facing documentation records what is implemented, why its durable
decisions were made, and what remains.

User-facing documentation ships with the package and lives in
[src/agents_live/skill/docs/](../src/agents_live/skill/docs/). Nothing in
this directory is installed by `agents-live init` or `upgrade`.

## Where documentation lives

| Content | Location |
|---|---|
| How to use the released tool | [src/agents_live/skill/docs/](../src/agents_live/skill/docs/) |
| How to work on this repository | [AGENTS.md](../AGENTS.md) and [.agents/](../.agents/) |
| Design rationale and direction | this directory |
| Tracked work items | GitHub issues (`gh issue list`) |

## Contents

- [architecture.md](architecture.md) - normative current package ownership,
  runtime flow, state, compatibility boundary, and invariants.
- [testing-methodology.md](testing-methodology.md) - what earns a test, which
  layer proves what, and why each release gate exists.
- [windows-support.md](windows-support.md) - native Windows host architecture.
- [wsl-support.md](wsl-support.md) - POSIX runtime composition and
  Windows-side distro liveness under WSL.
- [decisions/runtime-agent-seams.md](decisions/runtime-agent-seams.md) - why
  host automation and agent execution are separate ports.
- [decisions/definition-format.md](decisions/definition-format.md) - why
  definitions use Agent Skills and namespaced execution metadata.
- [decisions/no-python-api.md](decisions/no-python-api.md) - why processors
  use the JSON CLI and child process contract instead of importing the
  package, and what handlers should call instead.
- [decisions/deployment-generations.md](decisions/deployment-generations.md) -
  why installation writes immutable version directories, selects one with a
  `current` directory link, and how each partial failure recovers.
- [processor-contract.md](processor-contract.md) - why the processor contract
  is shaped the way it is, and which alternatives were rejected. The contract
  itself is specified in the skill payload.
- [dashboard/dashboard-product-requirements.md](dashboard/dashboard-product-requirements.md) -
  durable dashboard product direction and experience contract.
- [dashboard/dashboard-requirements.md](dashboard/dashboard-requirements.md) -
  prioritized, testable dashboard requirements.
- [dashboard/dashboard-implementation.md](dashboard/dashboard-implementation.md) -
  current dashboard delivery sequence and release boundaries.
- [dashboard/dashboard-validation.md](dashboard/dashboard-validation.md) -
  dashboard acceptance evidence and release gates.
- [backlog.md](backlog.md) - high-level themes linked to GitHub issues.

Design documents are added here as they are written, one file per topic,
named after the topic.

## Conventions

- Start every file with frontmatter carrying `title`, `description`,
  `ms.date`, and `ms.topic`; update `ms.date` on a material change.
- `architecture.md` states current implementation, not aspirations.
- A decision record states context, decision, alternatives, and consequences.
- Retire a superseded document rather than leaving it beside the record that
  replaced it, and name it in that record's `History` section so its
  reasoning stays findable by path in git history.
- Keep detail in GitHub issues. Link to an issue rather than restating
  its acceptance criteria here.
- The export-clean rule applies: no personal information, secrets,
  hostnames, or machine-specific paths.
