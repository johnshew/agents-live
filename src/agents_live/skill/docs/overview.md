---
title: Agents Live overview
description: Safe local automation for standard Agent Skill definitions
ms.date: 2026-08-08
ms.topic: overview
---

# Agents Live

Agents Live adds scheduled and file-change automation to conforming Agent
Skills. It uses the host's native trigger store, runs provider CLIs locally,
and keeps repository content separate from machine-local started state and
observability.

## Definition

Each runnable definition is either a standard `<name>/SKILL.md` bundle or an
Agents Live flat `<name>.md` document in `Agents/` or a configured
repository-relative discovery directory. Standard Agent Skills properties stay
at the top level; Agents Live policy uses quoted `agents-live.*` metadata.
Flat documents use the same content schema but are an Agents Live extension,
not conforming Agent Skills.

```yaml
---
name: markdown-polisher
description: Polishes Markdown documents after they change.
metadata:
  agents-live.schema-version: "1"
  agents-live.selector: "claude"
  agents-live.mode: "write"
  agents-live.watch: "docs/** debounce 1s"
---

Polish the changed Markdown while preserving its meaning.
```

See [Definition format](definition-format.md) for the complete registry and
strict parsing rules.

## Three lifecycle verbs

```bash
agents-live run markdown-polisher
agents-live start markdown-polisher
agents-live stop markdown-polisher
```

`run` executes once. `start` records that the definition should run
automatically on this host and converges all durable triggers and watchers.
`stop` clears that recorded fact and converges again. Re-running any command is
safe.

Operational commands such as `status`, `doctor`, `logs`, and `repos` inspect or
repair the system without adding another lifecycle vocabulary. The public
heartbeat command is gone. On WSL, convergence stages a distinct liveness task,
waits for a fresh beacon, and only then replaces the prior task.

## Safety model

- `plan` allows read-oriented provider work and is the default.
- `write` enables provider-side writes.
- `pipeline` adds the run-scoped pipeline MCP resource.
- A `none` selector, not a mode, selects a processor-only run and requires at
  least one processor.
- `agents-live.allow-tools` narrows unattended authority. It is distinct from
  the standard Agent Skills `allowed-tools` property.
- Output schemas, path roots, byte caps, and strict provenance are enforced
  before a post-processor receives output.
- Definitions and repository registries never contain machine-local started
  state, process records, logs, credentials, or host assignment.

Malformed definitions fail closed. An unreadable registry or started-state
record causes convergence to abstain rather than prune. An unreadable
registered repository contributes no desired subscriptions, so its owned
artifacts are removed.

## Architecture

The runtime and agent ports do not import each other.

- `runtime/` owns trigger persistence, detached processes, change streams,
  child processes, health, and convergence.
- `agent/` owns pure `load`, `shape`, `prepare`, `interpret`, and `outcome`
  functions around `PRE`, `AGENT`, and `POST` steps.
- `dispatch.py` performs the fixed pipeline and owns retries and run-scoped
  resources.
- `state/` owns repository registration and started facts.
- `obs/` owns the versioned event schema.
- `cli/` composes those seams.

Only immutable records made from primitive values cross a seam. Host behavior
lives behind POSIX and Windows adapters; provider quirks live behind Claude,
Copilot, and deterministic fake-provider plugins.

## Installation

```bash
uv tool install agents-live
agents-live init --repo /path/to/repository
agents-live doctor
```

Python 3.12 or newer is required. POSIX schedules use the user crontab and
watchers use `inotifywait`. Native Windows schedules use Task Scheduler and
watchers use directory change notifications.

For details, see the [command reference](commands.md), [diagnostics
guide](diagnostics.md), and [release process](release-process.md).
