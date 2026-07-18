---
name: agents-live
description: >-
  Add safe local schedules and file triggers to existing Claude Code and
  GitHub Copilot agents, then test, activate, inspect, and tear down that
  automation. Supports claude and copilot, plus plugin-registered adapters
  (this deployment: agency claude, agency copilot).
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
  a deliberate teardown). The only shared state file is
  `Agents/data/agent-owners.json`, which records which host owns each agent.

## Load before acting

| Before doing... | Read first |
|---|---|
| Explaining the system or comparing it to other offerings | [docs/overview.md](docs/overview.md) |
| `create` (building a new agent) | [docs/commands.md](docs/commands.md) section "create" |
| `install` or `prereqs` | [docs/commands.md](docs/commands.md) -- or just run the script; output is self-documenting |
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

Canonical agent files live in the native agent directories:
`.claude/agents/<name>.md` (the default - Claude Code, Copilot
CLI, and VS Code all discover it) or `.github/agents/<name>.agent.md`
(adds github.com cloud-agent exposure; pin `target: vscode` on
write/pipeline agents there). A native-directory file WITHOUT
`schedule`/`watchPath` is a plain interactive agent: discovery, `status`,
`start --all`, and orphan pruning skip it. Additional agent directories
(`Agents/`, plus `agent_directories` from the project config - root
`.agents-live.toml` or `[tool.agents-live]` in `pyproject.toml`) still
work; ephemeral `_` fixtures MUST stay in `Agents/` (interactive
surfaces would list them from native dirs). Names must be unique across
all locations.

Native agent directories hold no executables: agents there reference
pre/post-processors by repo-relative path (e.g.
`Agents/handlers/x.py`); bare names are rejected. In additional agent
directories, bare names still resolve relative to the agent's own
directory. Logs are always centralized in `Agents/logs/`. Agents in
`.claude/agents/` are visible to Claude Code as subagents - give each a
description ending "Never delegate to this agent." plus
`disable-model-invocation: true` (the doctor lints this).

## Lifecycle

```
create -> run (test) -> start (activate) -> stop -> teardown
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
| `stop <name>` / `teardown <name>` | `agents-live stop <name>` |
| `status [name] [--json]` | `agents-live status [name] [--json]` |
| `dashboard` | `agents-live dashboard --dev` |
| `logs [name]` | `agents-live logs [name] [--errors] [--all] [--limit 50]` |
| `logs query` | `agents-live logs [--agent name] [--errors] [--all] [--since T] [--slow N]` |
| `logs timeline [name]` | `agents-live logs timeline [name] [--all] [--since T]` |
| `smoketest` | `agents-live smoketest` |
| `doctor` / `prereqs` | `agents-live doctor` (plus judgment checks per [docs/commands.md](docs/commands.md)) |
| `install` | Install required tools *(see [docs/commands.md](docs/commands.md))* |
| `release` | Audit, assemble, publish *(publisher-side; see [docs/release-process.md](docs/release-process.md))* |

**Smoketest and commands that touch cron/inotifywait require `requestUnsandboxedExecution: true`.**

**Bootstrap: if `uv` is missing (every command above needs it), install it first with `curl -LsSf https://astral.sh/uv/install.sh | sh`.**

## Prompt Frontmatter

Each agent file's YAML frontmatter is the source of truth for Agents Live
configuration.

| Field | Default | Description |
|-------|---------|-------------|
| `runtime` | *(required)* | `claude`, `copilot`, `none`, or a plugin-registered adapter (this deployment: `agency claude`, `agency copilot`) |
| `mode` | `plan` | `plan` (read-only), `pipeline` (mediated `put`/`get`), or `write` (explicit direct authority) |
| `model` | *(agent default)* | Optional model override |
| `pre-processor` | *(none)* | Deterministic script that runs before the agent |
| `post-processor` | *(log-only)* | Deterministic script that runs after the agent |
| `env` | *(none)* | Map of env vars passed to the agent process |
| `mcps` | *(none)* | List of MCP server specs |
| `owner` | *(registry)* | Machine ownership: `"*"` (any host) or short hostname. Seeds `Agents/data/agent-owners.json` on first activation; if unset, only a targeted `start <name>` claims (never `start --all`) |
| `schedule` | -- | Cron expression (at least one of schedule/watchPath required) |
| `watchPath` | -- | Repo-relative directory or list of directories |

Both `schedule` and `watchPath` can be set (type `multi`). Each trigger
fires independently. On `start`/`stop`, both are activated/deactivated.

## Pre-processor pipeline

```
pre-processor -> agent -> post-processor
```

- Pre-processor stdout is appended to the agent prompt as `pre-processor="<output>"`.
- Output `{"skip": true}` to skip the agent call (status `skipped`).
- With `runtime: none`, pre-processor output pipes directly to post-processor (deterministic pipeline).
- Watchers ignore `.*`, `__pycache__/`, and `Agents/logs/` to prevent loops.
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

The MCP supports `put(path, value)` and `get(path)` —
a path-addressed key/value store. Schema metadata is supported via put and get on `<path>/$schema` with Draft 2020-12 JSON-Schema
validation on `put` content.

## Guardrails

- **Do not create tests for agents, processors, or prompts.** Use real agent
  output and the smoketest. Focused framework tests are allowed under the
  high-impact silent/combinatorial rule in `.agents/testing.md`.
- **Do NOT use `git checkout`, `git reset`, or `git stash`** on tracked
  files -- other agents may have uncommitted work.
- Agency agents require a one-time interactive auth before unattended use.
