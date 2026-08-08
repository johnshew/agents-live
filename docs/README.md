---
title: Agents Live Repository Documentation
description: Index of design documents and the high-level backlog for the agents-live repository
ms.date: 2026-07-25
ms.topic: overview
---

Repository-facing documentation: design documents that record why the
framework works the way it does, and the high-level backlog that records
where it is going.

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

- [backlog.md](backlog.md) - high-level backlog: themes and direction,
  linked to the issues that carry the detail.
- [project-status.md](project-status.md) - current platform maturity,
  prioritized stabilization work, and recommended implementation sequence.
- [windows-support.md](windows-support.md) - architecture guide to running
  the runtime natively on Windows instead of through WSL.
- [refactoring-runtime-and-agent-seams.md](refactoring-runtime-and-agent-seams.md) -
  proposal to reduce the package to two ports, a host runtime manager and
  an agent execution seam, with platform and provider plugins.
- [target-architecture.md](target-architecture.md) - reader's guide to the
  end state that proposal describes: the major pieces, where state lives,
  and the lifecycle of one agent from registration to firing.
- [frontmatter-convergence.md](frontmatter-convergence.md) - decision to
  make definitions conforming Agent Skills, with execution policy under
  namespaced `metadata` keys.

Design documents are added here as they are written, one file per topic,
named after the topic.

## Conventions

- Start every file with frontmatter carrying `title`, `description`,
  `ms.date`, and `ms.topic`; update `ms.date` on a material change.
- A design document states the problem, the options considered, the
  decision, and its consequences. Record the decision that was made, not
  a plan for making one.
- Keep detail in GitHub issues. Link to an issue rather than restating
  its acceptance criteria here.
- The export-clean rule applies: no personal information, secrets,
  hostnames, or machine-specific paths.
