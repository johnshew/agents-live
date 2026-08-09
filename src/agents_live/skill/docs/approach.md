---
title: Architecture
description: Runtime, agent, dispatch, state, and observability seams
ms.date: 2026-08-08
ms.topic: concept-article
---

# Architecture

Agents Live is two independent ports composed by lifecycle collection and
dispatch.

## Runtime port

`runtime/` owns automation on one host and separates four capabilities by
lifetime:

- `TriggerStore` survives reboot.
- `Supervisor` survives the process that spawned detached work.
- `ChangeSource` dies with its holder.
- `ChildRunner` dies with one call.

POSIX and Windows adapters implement those protocols. WSL extends the POSIX
adapter with a staged, verified Windows-side liveness task.

One `converge(desired)` operation renders the complete subscription set,
compares it with structured owned artifacts and watcher process markers, and
repairs drift. There is no held plan and no second mutation path. `health()` is
the read-side operation.

## Agent port

`agent/` owns a runnable unit of work through five pure operations:

1. `load` a restricted `Agents/<name>/SKILL.md`;
2. `shape` it into one of six pre/provider/post combinations;
3. `prepare` one `PRE`, `AGENT`, or `POST` launch;
4. `interpret` primitive child output; and
5. `outcome` from collected step results.

Provider plugins contain Claude and Copilot argv and output quirks. The fake
provider and fake CLI exercise the same path deterministically. Providers
receive a narrow immutable projection, not trigger or repository state.

## Dispatch

`dispatch.py` is the handoff. A firing contains only repository, agent,
origin, subscription key, and changed files. Dispatch checks started state for
automatic origins, applies due-time, per-agent concurrency, and durable budget
gates, then runs straight-line pre/provider/post sequencing. It owns retries,
PTY selection, run-scoped pipeline resources, timeout enforcement, and cleanup.

Output is normalized after child exit. The provider formats and validation
contract require complete values, so schema version 1 has no incremental
provider parsing hook.

## State and observability

Repository registration says where to collect. Machine-local started state says
which definitions should be automated here. Missing started state adopts
identifiable installed subscriptions exactly once. Present but unreadable state
causes collection to abstain before any mutation.

An optional assignment plugin may filter collected work. Local mode makes no
assignment decision.

`obs/` creates versioned immutable event records. The dispatch envelope and
event envelope are deliberately separate.

## Safety invariants

- Runtime and agent ports never import each other.
- Only immutable primitive records cross seams.
- Unknown metadata, versions, providers, trigger syntax, and retired fields
  fail closed.
- Global pruning occurs only after complete registry and started-state
  collection.
- An unreadable registry abstains. A registered repository that cannot be
  read, and a started definition that no longer parses, keep the artifacts
  they already own until they resolve again or are stopped.
- Concurrency and misfire policy are fixed to skip.
- Definitions, artifacts, started state, and events have one owner each.
