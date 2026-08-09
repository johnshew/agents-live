---
title: Runtime and Agent Seams Decision
description: Why Agents Live separates host automation from agent execution
ms.date: 2026-08-09
ms.topic: concept
---

# Runtime and agent seams

## Status

Accepted and implemented for 6.0.

## Context

The earlier package mixed definition parsing, provider selection, scheduling,
watching, process management, ownership, and logging in large command modules.
Platform branches appeared at call sites, tests patched implementation details,
and a change to one concern regularly disturbed another.

The system needs two independent kinds of variation:

- host automation varies across POSIX, native Windows, WSL, and tests; and
- agent execution varies across provider CLIs and future provider plugins.

Neither side needs to know the other's implementation.

## Decision

Use two ports with one explicit handoff:

- `runtime/` owns host subscriptions, convergence, watches, process policy,
  liveness, and host adapters;
- `agent/` owns definitions, selectors, provider preparation, and output
  normalization; and
- `dispatch.py` turns primitive firing context into one completed run.

Keep `state/`, `obs/`, `pipeline/`, and `cli/` separate because each has a
different lifetime and owner. Lifecycle composition gathers a complete desired
set before invoking runtime convergence.

Host adapters use protocols and immutable value records rather than one large
runtime facade. Provider plugins return launch descriptions and never receive
host process objects.

## Related decisions

- Started intent is durable host state, not a definition field.
- Ownership is optional assignment policy outside the runtime port.
- Convergence is one idempotent total operation, not public plan/apply verbs.
- A firing carries primitives rather than an agent or subscription object.
- Watch policy is generic; host adapters supply change sources.
- Concurrency and misfire policy are fixed to skip.
- Process output is normalized after child exit; no provider-independent
  streaming hook was justified.
- A durable project/host budget bounds dispatch fan-out and fails open when
  its bookkeeping cannot be read.

## Alternatives rejected

**One runtime facade.** It grouped unrelated platform and process concerns and
made common code depend on methods most callers did not need.

**Platform branches in commands.** This duplicated policy and let behavior
drift between operating systems.

**Asyncio as the organizing abstraction.** The durable boundaries are native
stores and subprocesses, not cooperative in-process tasks. Asyncio would not
remove those boundaries.

**Public plan and apply operations.** They expose mechanism as user vocabulary.
Preview is a mode of the same convergence calculation.

**A runtime-owned artifact index.** Structured native artifacts carry their
identity and fingerprint, avoiding an additional write-ordering problem.

## Consequences

Platform defects are fixed in host adapters, provider quirks in providers, and
cross-port behavior in dispatch. Tests can run the real orchestration against
memory hosts and fake providers. The cost is more small modules and explicit
value translation at boundaries.

The architecture is guarded by import and platform-detection fitness tests.
`legacy/` remains a temporary exception for artifacts created by 5.x and is
removed in 7.0.

## History

Two working documents preceded this record and are not maintained. Read them
from git history when you need the option analysis behind a conclusion above
rather than the conclusion itself.

- `docs/refactoring-runtime-and-agent-seams.md` holds the original problem
  statement with its line-count evidence, the ordered goals and their
  measurable proxies, the rejected migration sequences, and the review rounds
  that produced these decisions. It was refined across 23 commits, from
  `b454d11` to its deletion.
- `docs/target-architecture.md` holds the reader's guide to the end state:
  what each component owns, and the lifecycle of one agent from registration
  to firing.

Both are recoverable by path. `git log` lists every commit that touched one,
including the commit that deleted it, and the parent of that commit holds the
final text:

```bash
git log --oneline -- docs/refactoring-runtime-and-agent-seams.md
git show <deletion-commit>^:docs/refactoring-runtime-and-agent-seams.md
```

Implemented in [#256](https://github.com/johnshew/agents-live/pull/256).