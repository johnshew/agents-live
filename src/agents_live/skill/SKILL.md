---
name: agents-live
description: >-
  Add safe local schedules and file triggers to existing Claude Code and
  GitHub Copilot agents, then test, activate, inspect, and tear down that
  automation. Supports claude and copilot out of the box; installed plugins
  can register additional adapters (e.g. agency claude, agency copilot).
  Triggers: "make this agent live", "schedule an agent", "watch files with an agent",
  "agents-live create", "agents-live run", "agents-live status",
  "review agent logs", "why did X not pick up", "debug watcher race",
  "trace a pipeline run", "what triggered rebuild".
---

# Agents Live

Agents Live adds safe, local automation to the Claude Code and GitHub Copilot
agents you already use. It does not replace or invent those agents, their
prompts, their tools, their authentication, or their reasoning.

- Each live agent remains one standard agent file under `.claude/agents/` or
  `.github/agents/`, with Agents Live frontmatter defining when and how it
  runs.
- Deterministic pre- and post-processing scripts can prepare input and apply
  output without granting the agent direct write access.
- Agents Live uses the installed agent CLIs, Python/uv, cron, and inotifywait.
  It adds lifecycle management, debounce, locking, logs, recovery, and cost
  tracking around them.
- Agent state is computed from crontab and process lists. Runtime is the
  source of truth. A watcher's durable "should be running" intent is its
  `@reboot` respawn line in the crontab (it survives reboot and is removed by
  a deliberate stop). Ownership is local by default (every agent belongs
  to this host). With a plugin-provided registry backend, the shared state
  file `Agents/data/agent-owners.json` records which host owns each agent.

## Load before acting

| Before doing... | Read first |
|---|---|
| Explaining the system or comparing it to other offerings | [docs/overview.md](docs/overview.md) |
| `create` (building a new agent) | [docs/commands.md](docs/commands.md) section "create" |
| `install` or `doctor` | [docs/commands.md](docs/commands.md) -- or just run the command; output is self-documenting |
| `smoketest` | [docs/commands.md](docs/commands.md) section "smoketest" |
| `release` | [docs/release-process.md](docs/release-process.md) |
| Editing any script | [docs/approach.md](docs/approach.md) (architecture) |
| Understanding services available to agents and handlers (env, MCPs, `Agents/lib/` helpers, pipeline side-channel) | [docs/approach.md](docs/approach.md) |
| Debugging log issues | [docs/diagnostics.md](docs/diagnostics.md) (log inventory, procedures, patterns, query recipes) |
| Debugging cron/watcher lifecycle | [docs/key-learnings.md](docs/key-learnings.md) |
| Debugging WSL/9P issues | [docs/reference/wsl-runbook.md](docs/reference/wsl-runbook.md) |
| Reviewing implementation history | [docs/changelog.md](docs/changelog.md) |

If you change behavior that contradicts approach.md, update it in the same
commit. Stale docs are worse than missing ones.

## Agent directories

`Agents/` is always searched. Project configuration may add repository-relative
discovery roots with `agent_directories = ["foo"]` in `.agents-live.toml` or
the `[tool.agents-live]` table in `pyproject.toml`. Each root is searched one
level deep for `<name>.md` flat definitions and `<name>/SKILL.md` Agent Skill
bundles. Flat definitions use the Agent Skills content schema but are an
Agents Live extension; only the bundle layout conforms to Agent Skills.

Every definition has a canonical `<name>-<path-hash>` identifier derived from
its repository-relative prompt path. A plain name remains valid when unique.
When names repeat, use the canonical identifier shown by `status`. Relative
processor paths resolve from the bundle directory or, for a flat definition,
from the containing discovery root.

## Lifecycle

```
create -> run (test) -> start (activate) -> stop
```

## Commands

All user-invoked lifecycle commands go through `agents-live`. Persisted
cron/watcher entries and internal spawns still invoke the underlying scripts
until the package migration completes; those paths are implementation details,
not the user-facing contract.

| Pattern | Command |
|---------|---------|
| `create <description>` | Create a standard agent definition with Agents Live fields *(agent-led; generates files -- see [docs/commands.md](docs/commands.md))* |
| `run <name>` | `agents-live run <name>` |
| `start <name>` | `agents-live start <name>` |
| `start --all [--dry-run]` | `agents-live start --all [--dry-run]` |
| `stop <name>` | `agents-live stop <name>` |
| `status [name] [--json]` | `agents-live status [name] [--json]` |
| `status --all-repos` | `agents-live status --all-repos` *(read-only, repo-qualified)* |
| `repos` | `agents-live repos list|default|remove` |
| `dashboard` | `agents-live dashboard --dev` |
| `dashboard --all-repos` | `agents-live dashboard --all-repos` *(read-only)* |
| `logs [name]` | `agents-live logs [name] [--errors] [--all] [--limit 50]` |
| `logs query` | `agents-live logs [--agent name] [--errors] [--all] [--since T] [--slow N]` |
| `logs timeline [name]` | `agents-live logs timeline [name] [--all] [--since T]` (bare defaults to all agents, last 50 events) |
| `smoketest` | `agents-live smoketest` |
| `doctor` | `agents-live doctor` (plus judgment checks per [docs/commands.md](docs/commands.md)) |
| `doctor --all-repos` | `agents-live doctor --all-repos` |
| `repair` | `agents-live doctor --repair [--dry-run]` |
| `heartbeat` | `agents-live heartbeat install --distro <name>` (WSL host keep-alive; `init` registers it already) |
| `uninstall` | `agents-live uninstall [--retain-state]` |
| `install` | Install required tools *(see [docs/commands.md](docs/commands.md))* |
| `release` | Preview, prepare, inspect, then publish with `tools/release.py` *(publisher-side; see [docs/release-process.md](docs/release-process.md))* |

**Smoketest and commands that touch cron/inotifywait require `requestUnsandboxedExecution: true`.**

**Bootstrap: if `uv` is missing (every command above needs it), install it first with `curl -LsSf https://astral.sh/uv/install.sh | sh`.**

## Definition metadata

Standard Agent Skills properties remain top-level. Agents Live execution
policy is stored as quoted string values under `metadata` with the
`agents-live.` prefix. Read [docs/definition-format.md](docs/definition-format.md)
for the complete schema.

## Pre-processor pipeline

```
pre-processor -> agent -> post-processor
```

- Pre-processor stdout is appended to the agent prompt as `pre-processor="<output>"`.
- Output `{"skip": true}` to skip the agent call (status `skipped`).
- With `runtime: none`, pre-processor output pipes directly to post-processor (deterministic pipeline).
- Watchers ignore `.*` and `__pycache__/` to prevent loops; logs live
  outside the project tree, so log writes cannot re-trigger watchers.
- In `mode: pipeline`, the pre-processor, agent, and post-processor can `put` and `get` against the PipelineMcp side-channel (see below).

## Pipeline mode (`mode: pipeline`)

Agents Live starts `PipelineMcp`, a bearer-token-protected HTTP MCP server on
a random loopback port, for the duration of a `mode: pipeline` run and injects
the appropriate MCP config into the agent.

By default, builtin MCPs and tools are dropped and the tool allow-list narrows
to the `pipeline` server's tools. An explicit `allow-tools` setting can only
narrow that set further. This is a tool-policy and
mediated-output boundary, not OS-level isolation. The agent can affect the
world only through `put` and `get`, which deterministic pre/post-processors
mediate. The side-channel is ephemeral and scoped to one agent run.

The MCP supports `put(path, value)` and `get(path)` -
a path-addressed key/value store. Schema metadata is supported via put and get on `<path>/$schema` with Draft 2020-12 JSON-Schema
validation on `put` content.

## Guardrails

- **Do not create tests for agents, processors, or prompts.** Use real agent
  output and the smoketest. Focused framework tests are allowed under the
  high-impact silent/combinatorial rule in `.agents/testing.md`.
- **Do NOT use `git checkout`, `git reset`, or `git stash`** on tracked
  files -- other agents may have uncommitted work.
- Agency agents require a one-time interactive auth before unattended use.
