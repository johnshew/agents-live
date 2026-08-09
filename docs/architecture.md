---
title: Agents Live Architecture
description: Current package ownership, runtime flow, state, and architectural invariants
ms.date: 2026-08-09
ms.topic: concept
---

# Agents Live architecture

This document describes the implemented 6.0 architecture. It is normative for
repository work: proposals and historical discussions do not override the
code ownership and invariants recorded here.

Agents Live turns portable Agent Skill definitions into local automation. The
agent port understands definitions and providers. The runtime port understands
hosts, triggers, watchers, and processes. `dispatch.py` is the handoff between
them. State and observability remain independent of both ports.

## Package ownership

```text
agents_live/
  agent/                 definitions, selectors, providers, outcomes
  runtime/               convergence, grammars, process and watch policy
    hosts/               POSIX, native Windows, WSL, memory, host I/O
  state/                 repository registry, ownership, started intent
  obs/                   event schema, administrative events, query, timeline
  pipeline/              run-scoped MCP resources and stdio bridge
  cli/
    commands/            importable command handlers
    scripts/             optional-dependency PEP 723 tools
    lifecycle.py         agent/state/runtime composition
    update_check.py      interactive release notices
  legacy/                5.x recognition and migration, removed in 7.0
  dispatch.py            one firing becomes one run
  paths.py               shared path and repository resolution
  plugins.py             declared extension inspection and convergence
  preflight.py           shared capability and error contract
```

Two root resources are temporary physical-path compatibility exceptions:

- `hidden.py` lets Windows tasks persisted before the host-module move enter
  the owned implementation at `runtime.hosts.hidden`.
- `windows-heartbeat.sh` lets historical WSL heartbeat tasks run once and
  install the canonical distro-scoped task.

Both exceptions expire with the 5.x artifact migration support in 7.0.

## Definitions

A definition is one of:

- `<discovery-root>/<name>/SKILL.md`, a conforming Agent Skill bundle; or
- `<discovery-root>/<name>.md`, the Agents Live flat-file extension.

`Agents/` is always a discovery root. Repositories can add immediate,
repository-relative roots through `agent_directories`. Execution metadata uses
quoted `agents-live.*` values under the standard `metadata` map. The complete
schema is in
[definition-format.md](../src/agents_live/skill/docs/definition-format.md).

Every definition receives `<name>-<path-hash>` as its canonical identifier.
The hash comes from the normalized repository-relative prompt path. A checkout
move therefore preserves identity, while moving a definition changes it.

## Lifecycle

The public lifecycle is three verbs:

- `start` records started intent and converges host automation;
- `stop` removes started intent and converges away its automation; and
- `run` dispatches one invocation immediately.

`status` and `doctor` report those facts. Internal terms such as subscription,
diff, and convergence do not become additional user lifecycle states.

### Collection and convergence

`cli/lifecycle.py` collects the complete desired set:

1. read the registered repositories;
2. load each repository's started identifiers;
3. resolve definitions and optional ownership;
4. translate schedules and watches into runtime subscriptions; and
5. call `runtime.converge()` only when collection is complete.

An unavailable repository or unreadable started-state file aborts convergence.
It is safer to preserve known automation than to interpret missing input as an
instruction to delete it.

The runtime compares desired subscriptions with structured host artifacts.
It installs missing artifacts, repairs drift, restarts changed watchers, and
removes owned artifacts absent from the complete desired set. Preview uses the
same diff without applying it.

### Firing and dispatch

Schedules and watchers produce a `Firing` record containing primitive context:
definition identifier, repository, origin, subscription key, and changed
files. Both paths enter `dispatch.py`.

Dispatch:

1. claims the project/host execution budget and per-definition run lock;
2. loads the definition through the agent port;
3. asks the selected provider for an immutable launch description;
4. runs it through the host `ChildRunner`;
5. applies optional processors and output policy; and
6. records a versioned observability event.

Concurrency and misfire policy are both skip. A firing is not queued behind a
live run, and a missed schedule is not replayed later.

## Host adapters

`runtime.hosts.current()` selects one adapter:

| Host | Scheduling | Watching | Process policy |
|---|---|---|---|
| Linux/POSIX | crontab | filesystem source plus generic watch loop | POSIX process groups and PTY support |
| Native Windows | Task Scheduler | `ReadDirectoryChangesW` source plus generic watch loop | Windows process trees and windowless task actions |
| WSL | POSIX behavior plus Windows-side liveness | POSIX filesystem source | POSIX children; Windows Task Scheduler keeps the distro available |
| Memory | in-memory stores and fake child runner | deterministic fake source | tests and framework smoketest only |

Platform detection and operating-system calls stay under `runtime/hosts/`.
Common code consumes protocols and immutable value records.

## State

Portable definition content stays in the repository. Machine-local facts do
not.

| State | Owner |
|---|---|
| Repository registry and default | host-level state/config |
| Started identifiers | repository-keyed host state |
| Optional ownership assignments | configured ownership backend |
| Trigger and watcher artifacts | native host stores |
| Run locks and budget | repository-keyed host state |
| Events, transcripts, heartbeat beacon | host or repository state directories |

Repository removal is explicit. Moving a repository requires stop, move,
register, remove the old registration, then start. Inert stale records are
reported rather than guessed away.

## Observability

`obs/` owns the versioned event schema and the decoder for current and legacy
records. Agent runs and host administration use the same event writer.
`logs`, `logs timeline`, and the dashboard consume the query layer rather than
hand-parsing files.

Observability is local. Off-host export remains out of scope until a consumer
requires it. Local span correlation and sensitivity metadata are tracked by
issue [#105](https://github.com/johnshew/agents-live/issues/105).

## Extensions

Provider plugins implement the agent provider contract: resolve a selector,
prepare a launch, and normalize raw output. Host adapters implement runtime
protocols and never import `agent/`. Declared plugin distributions are
validated and converged by `plugins.py` before their entry points are used.

## Compatibility boundary

6.0 is a clean definition-format break. The loader rejects retired 5.x fields
and the explicit migrator rewrites supported inputs. Runtime compatibility is
limited to durable artifacts that can outlive an upgrade. `legacy/` recognizes,
adopts, migrates, or safely removes those artifacts. It must not receive new
product behavior and is removed in 7.0.

## Invariants

1. `runtime/` does not import `agent/`, and `agent/` does not import
   `runtime/`.
2. Only immutable records built from primitives cross a port boundary.
3. Host services never enter the agent port.
4. Platform detection and platform APIs stay under `runtime/hosts/`.
5. Started intent, ownership, and a live run are different facts.
6. Convergence receives the complete desired set or does not run.
7. Both firing paths produce the same dispatch inputs.
8. Concurrency and misfire policy are skip.
9. Machine-local state never lives in a repository.
10. Legacy code recognizes old artifacts but gains no new behavior.

The architecture fitness tests in `tests/test_seams.py` enforce the import,
platform, CLI-target, and retired-root-module boundaries. The portable smoke
suite exercises definition discovery, start, firing, observability, stop, and
vendored payload installation against temporary repositories.

## Decision records

- [Runtime and agent seams](decisions/runtime-agent-seams.md)
- [Definition format](decisions/definition-format.md)
- [Native Windows support](windows-support.md)
- [WSL support](wsl-support.md)