---
title: Agents Live Architecture
description: Architecture and runtime contracts for agents-live triggered agents
ms.date: 2026-07-19
ms.topic: concept
---

## Agents Live architecture

> Add cron and file-watch automation to your existing agents with `/agents-live`.
> Supports `claude` and `copilot` out of the box; installed plugins can
> register additional adapters (e.g. `agency claude`, `agency copilot`).  
> **Platform: Linux (Ubuntu on WSL)** | Last updated: July 2026

---

## 1. What It Looks Like

Agents Live does not replace or invent agents. It adds local execution fields
to the standard Claude Code and GitHub Copilot agent definitions you already
use. Claude Code exposes `/agents-live` as a slash command; Copilot and Agency
use natural conversation via AGENTS.md. For example:

```
/agents-live create "AI News Digest" that every 3 days uses claude
in plan mode with a handler that writes output to "AI and Prompts/<YYYY.MM.DD>-AI News"
```

Then manage it:

```
/agents-live run ai-news-digest
/agents-live start ai-news-digest
/agents-live status
/agents-live logs ai-news-digest
/agents-live stop ai-news-digest
```

Another example:

```
/agents-live create "recurring git fetch" that runs every 20 minutes and does a bash git fetch using the preprocessor. No agent.
```

### Commands

| Command | What it does |
|---------|-------------|
| `create <desc>` | Parse description, create prompt with frontmatter + handler |
| `run <name>` | Execute once, show output |
| `start <name>` | Activate cron or watcher (auto-detects type) |
| `stop <name>` | Deactivate, keep config |
| `status` | List all agents and state |
| `logs <name>` | Show recent output |
| `doctor` | Check environment readiness |
| `smoketest` | End-to-end validation |

### Lifecycle

```
create → run (test) → start (activate) → stop
```

### Example: status output

```
NAME              TYPE     SCHEDULE      AGENT            MODE  STATE
ai-news-digest    cron     0 0 */3 * *   claude           plan  active
exercise-review   watcher  Exercise/     copilot          plan  active (pid 12345)
weekly-summary    cron     0 8 * * 0     agency copilot   plan  stopped
```

---

## 2. How It Works

### Architecture

```
 .claude/agents/                     # canonical standard agent definitions
└── ai-news-digest.md                # frontmatter carries agents-live triggers

Agents/                              # git-tracked content only
├── handlers/                        # deterministic pre/post-processors
│   ├── write-files.sh               # generic: JSON files[] → disk
│   └── ms-todo-to-md.sh             # agent-specific: To Do JSON → .md files
└── data/
    └── agent-owners.json            # git-synced shared multi-host ownership state

~/.local/state/agents-live/          # machine-local runtime state (XDG state home)
├── logs/                            # host-level logs (health-check.log)
├── health.ok                        # host health beacon
├── heartbeat.ok                     # WSL keep-alive beacon
└── repos/<name>-<hash>/             # per-repo state (watch hashes, smoketest lock)
    └── logs/                        # JSONL log files (one per agent + system log)
        ├── agents-live.log          # system-wide lifecycle events
        ├── todo-sync.log            # per-agent execution log
        ├── runs/                    # per-run stdout/stderr/transcript artifacts
        └── archive/                 # monthly Parquet archives
            └── 2026-05.parquet      # unified monthly archive, all agents

.claude/skills/agents-live/          # installed skill payload (docs and
├── SKILL.md                         # templates; the executable runtime is
├── VERSION                          # the installed agents-live package,
├── docs/                            # not scripts in the tree)
│   └── approach.md
└── templates/
```

The runtime lives in the installed `agents_live` package: `cli.py`
(spec-driven command dispatcher), `paths.py` (root resolution: explicit ->
env -> marker -> default), `repos.py` (XDG registry + isolated read-only
aggregation), `headless.py` (shared helpers: env, flags, frontmatter
parsing), `activate.py` (start cron or watcher, auto-detects type),
`run.py` (execute an agent once), `qlog.py` (query live and archived
logs), `stop.py` (stop + remove an agent), `status.py` (list agents and
state, `--json` for structured output), and `smoketest.py` (end-to-end
validation).

Each process still operates on one immutable repository root. The user-level
XDG registry only selects that root. Multi-repository status, doctor, and
dashboard collection invokes the existing per-repository collectors in isolated
child processes, qualifies identities, and preserves partial failures.
Mutations never aggregate, and persisted trigger/spawn invocations always pin a
normalized absolute root. Additional `agent_directories` are repository-relative
and cannot escape the selected root, including through symlinks.

### Prompt frontmatter (source of truth)

Each standard agent file keeps its prompt body and adds YAML frontmatter that
defines **when** and **how** Agents Live runs it. This is the authoritative
configuration. There is no separate manifest or Agents Live agent format.

```yaml
---
runtime: copilot               # required unattended execution adapter
mode: plan                     # plan (read-only) or write (default: plan)
model: claude-haiku-4.5        # optional model override
handler: ms-todo-to-md.sh      # handler script name, or omit for log-only
env:                           # optional env vars for the agent process
  EXAMPLE_KEY: example-value
mcps:                          # MCP servers (agency agents only)
  - softeria
schedule: "0 * * * *"          # cron expression (scheduled agent)
# watchPath: Exercise/         # directory to monitor (watch-triggered agent)
---
```

#### Frontmatter fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `runtime` | yes | *(none)* | Unattended execution adapter: `claude`, `copilot`, `agency claude`, `agency copilot`, `none`. A leftover `agent:` key fails loudly. Validated against the adapter registry at parse time |
| `description` | no | *(none)* | Ecosystem-standard (C1): one-line purpose; surfaced in `status --json`; powers VS Code auto-delegation |
| `tools` | no | *(none)* | Ecosystem-standard (C1): fine-grained tool list for interactive surfaces; parsed and passed through (runner enforcement mapping lands with C3) |
| `user-invocable` | no | *(absent)* | Ecosystem-standard (C1): interactive-picker visibility; pass-through |
| `disable-model-invocation` | no | *(absent)* | Ecosystem-standard (C1): opt out of auto-delegation; pass-through |
| `argument-hint` | no | *(none)* | Ecosystem-standard (C1): interactive argument hint; pass-through |
| `mode` | no | `plan` | `plan` (read-only), `pipeline` (mediated `put`/`get`), or `write` (explicit direct authority) |
| `model` | no | *(agent default)* | Optional model override passed to the agent CLI |
| `allow-tools` | no | *(none)* | Optional mode-compatible tool allow-list. It may narrow plan or pipeline tools, but cannot grant direct mutation outside `write` mode |
| `handler` | no | *(log-only)* | Post-processor script name (bare name resolves beside the agent directory's handlers; path with `/` is repo-relative) |
| `pre-processor` | no | *(none)* | Pre-processor script; runs before the agent and can replace/skip the prompt |
| `post-processor` | no | *(none)* | Alias for `handler` (takes precedence when both are set) |
| `env` | no | *(none)* | Map of env vars passed to the agent process |
| `mcps` | no | *(none)* | List of MCP server names (agency agents only) |
| `schedule` | one of | - | Cron expression (makes the agent scheduled) |
| `watchPath` | these | - | Repo-relative path(s) to monitor (string or list; makes the agent watch-triggered) |
| `watchIgnore` | no | *(none)* | Glob patterns to exclude from watcher events (string or list) |
| `debounce` | no | *(none)* | Seconds of quiet before watcher dispatch (Layer 2 debounce, timed in-process) |
| `timeout` | no | `120` | Max seconds for agent execution |
| `transcript` | no | `true` | Capture full session transcript (copilot: `--share`). Set `false` to disable for noisy/stable agents |
| `output-schema` | no | *(none)* | Safe-output (§3.9): JSON Schema the agent output must satisfy - inline mapping, or a `.json` file reference beside the agent. Failure = `agent_output_invalid`; the post-processor never runs |
| `output-max-bytes` | no | `1048576` | Safe-output: cap on raw agent stdout bytes (always enforced; this key overrides the default) |
| `output-path-roots` | no | *(none)* | Safe-output: repo-relative roots; every `path` field in the agent's JSON must resolve under one of them (the `write-files.sh` pattern) |
| `output-provenance` | no | *(none)* | Safe-output: `strict` requires the whole stdout to be a single unrepaired JSON document (extraction record `source=stdout, repaired=false, candidates=1`); default remains accept-and-act |

The three opt-in safe-output keys validate stdout and are rejected on
`mode: pipeline` agents (pipeline output flows through the PipelineMcp
store, not stdout). Every agent run also logs an `extraction record`
event (source, repaired, candidate count, output digest - never the
candidate text) for provenance forensics.

**Defaults:** `runtime` is required. If `mode` is omitted, it defaults to
`plan`. An agent with `schedule` is scheduled; an agent with `watchPath` is
watch-triggered.

### Querying agent state: `status`

There is no manifest file. Agents Live configuration lives in the standard
agent file's frontmatter, and runtime state is computed on demand from crontab
and process lists.

```bash
agents-live status
agents-live status --json
agents-live status --json <name>
```

Infrastructure scripts (`activate.py`, `run.py`, `status.py`, `stop.py`,
`qlog.py`, `smoketest.py`, `headless.py`) declare their dependencies (PyYAML)
inline via PEP 723 `# /// script` blocks and are invoked through `uv run`.
This keeps the skill portable: `uv` resolves the environment on demand, no
shared `.venv` or system `pip install` is required. Handlers use the same
pattern: `build_handler_command()` in headless.py injects base packages
via `uv run --with`, and handlers needing extras declare them inline
via PEP 723 `# /// script` blocks.

The JSON output includes all frontmatter fields plus computed state:

```json
{
  "agents": [
    {
      "name": "todo-sync",
      "type": "cron",
      "runtime": "agency copilot",
      "mode": "plan",
      "promptPath": ".claude/agents/todo-sync.md",
      "state": "stopped",
      "handler": "Agents/handlers/ms-todo-to-md.sh",
      "schedule": "0 * * * *",
      "mcps": ["softeria"]
    }
  ]
}
```

| Field | Source |
|---|---|
| name | Stem of the file in `.claude/agents/` or `.github/agents/` |
| runtime, mode, model, handler, mcps, env | Prompt frontmatter |
| type, schedule, watchPath | Frontmatter (`schedule:` → cron, `watchPath:` → watcher) |
| promptPath | Discovered standard agent definition |
| state | `crontab -l` for cron, process list inspection for watchers |

Watcher runs ignore changes under hidden `.*` directories and `__pycache__/`
so repo-root watchers do not loop on Git metadata. Runtime log writes land in
the user-level state home, outside the watched tree, so they cannot
re-trigger watchers.

### How running agents are identified (and orphan pruning)

Because there is no manifest, the host's cron and process state are the source of truth for
what is running. An agent is identified by its **name** through two exact-match
contracts:

- **Cron**: the installed line invokes `run.py --name <name>`. Matching is an
  adjacent-token `--name <name>` pair (via `shlex.split`), so names never
  collide with substrings or other arguments.
- **Watcher**: the process runs `activate.py watch-loop <name>` in a flat
  checkout or `agents-live internal watch-loop <name>` when packaged.
  Matching uses the adjacent `watch-loop <name>` action/name pair.

`headless.py` exposes both the per-name lookup (`cron_line_matches`,
watcher cmdline matching) and the reverse, enumerate-all lookup
(`_list_active_cron_agent_names`, `_list_active_watcher_agent_names`,
`list_active_agent_names`). The reverse lookup is what makes a deleted agent
file a complete decommission:

- `activate.py --prune-orphans` enumerates everything live on the host and
  tears down (cron + watcher + desired-state entries) any name with no backing
  standard agent file. It is also run at the start of `activate.py --all`, so
  a reconcile both activates defined agents and removes deleted ones.
- The built-in health-check loop (`agents-live health-check`, hourly and on
  boot) calls `--prune-orphans` in its per-repo sweep before
  reconciling. Removing an agent with `git rm` therefore decommissions it:
  every host self-cleans on its next health check, with no manual
  per-host stop. Cron entries (which survive reboots) and watcher processes
  (which do not) are both handled.
- The health-check sweep also runs `migrate` at the start of every pass
  (idempotent convergence): a persisted cron entry that survives reboot can
  still point at a moved or deleted script and fail silently, since `run.py` -
  the logger - is what the entry can no longer reach. Convergence repairs
  that class in-band; stale paths that survive it degrade the beacon, and
  `doctor`'s beacon-freshness check is the out-of-band detector for the case
  where the health check's own entry is the broken one.

A host that has not yet pulled the deletion still has the file, so it does not
prune - correct, because the agent is still defined there. Once the deletion
syncs in, the file is gone and the orphan is pruned. The mechanism is
idempotent: an agent whose file still exists is never touched.

### Execution modes

The agent is invoked headless (`-p`) and its output goes one of three ways:

| Mode | How | When |
|------|-----|------|
| **Plan (`mode: plan`)** | Agent runs read-only by default; output is logged or validated and applied by a deterministic script | Default and preferred |
| **Pipeline (`mode: pipeline`)** | Agent defaults to schema-checked `put`/`get` tools mediated by pre/post-processors | Structured interaction without general write access by default |
| **Write (`mode: write`)** | Agent receives direct write authority | Last, explicit opt-in |
| **Handler-only (`runtime: none`)** | No agent; a deterministic script runs directly | Mechanical work that needs no model judgment |

**Handler-only** (`runtime: none`) skips the LLM entirely and runs the handler
script directly with no stdin. Use this when the work is purely mechanical
(e.g. indexing files, syncing state) and doesn't need reasoning. Works with
both `schedule:` (cron) and `watchPath:` (watcher) triggers. A `handler:` field
is required when `runtime: none`.

**Plan + handler** is preferred: the agent thinks, the script acts. Prompts
include a JSON output section; handlers process the JSON. The generic handler
(`write-files.sh`) creates files from JSON. Write agent-specific handlers for
custom processing (e.g. `ms-todo-to-md.sh` transforms To Do API data into
Obsidian notes).

**Pipeline mode** (`mode: pipeline`) replaces stdin/stdout/files as the
agent↔handler channel with an in-process MCP server. `run.py` brings up
`PipelineMcp` (HTTP, localhost, bearer-token-gated) for the run, writes a
per-agent MCP config to a tempdir, and injects it via
`--mcp-config` (claude) or `--additional-mcp-config @<file>` (copilot, via
`pipeline_mcp_stdio_bridge.py` because copilot `-p` does not auto-connect
HTTP MCPs). The agent's tool allow-list is narrowed to the `pipeline`
server's tools; `allow-tools` can only narrow that set further. For claude
this relies on headless `-p` auto-denying every
tool not in `--allowedTools` (builtins still exist but every call is
denied); do NOT add `--tools ""` to strip them -- on claude CLI >= 2.1.201
any `--tools` value also strips MCP tools, cutting the agent off from the
pipeline server entirely. For copilot builtins are dropped via
`--deny-tool` flags. Every tool call is logged
JSONL with `component: "pipeline-mcp"`, and shutdown emits one
`op: "final-state"` line with call counts.

Current surface area: `put(path, value)` and
`get(path)` - a path-addressed key/value store. Liveness uses
the `/ping` path (put then get) rather than a dedicated ping tool.
`$schema` binding rules and Draft 2020-12 JSON-Schema validation are
already implemented; future phases can extend that schema support further.
See `pipeline_runtime.py` in the installed `agents_live` package for the
lifecycle. Coverage: `smoketest.py` step `[10/13]`.

### Smoketest lifecycle isolation

The system smoketest uses fixed `_smoketest-*` fixture names, so one
host-local `flock` covers the complete setup/run/stop lifecycle. Only the
lock owner may clean or create those resources. Cleanup is idempotent and
covers agent definitions, handlers, cron entries, watcher processes, in-flight
smoketest `run.py` process trees, and
result files. Host cleanup commands have hard timeouts, and process termination
uses start-time identities so PID reuse cannot target an unrelated process.
The verdict fails only when post-cleanup
verification finds a resource that actually survived (residue); a cleanup
command that errors or times out on the way is logged as a diagnostic and does
not fail an otherwise-passing run. Cleanup runs before setup to recover an
unclean prior exit and in a
signal-shielded `finally` block for every current exit. The lock file is kept
on disk because deleting an advisory-lock inode creates a race; ownership is
the held file descriptor, while the file content is diagnostic metadata only.

### Plugin architecture

Plugins are wheels committed within the project and declared under `[plugins]`
in `.agents-live.toml` or `[tool.agents-live.plugins]` in `pyproject.toml`.
The declaration maps a distribution name to a repository-relative wheel path
and an optional SHA-256 integrity pin. The wheel filename and metadata carry
the version.

Plugins extend the kernel through Python entry points:

- `agents_live.agents` registers unattended runtime adapters.
- `agents_live.ownership`, entry point name `registry`, supplies multi-host
  ownership.

The uv tool environment is host-global. `init` and non-dry-run `start`
converge the selected project; `upgrade` unions declarations from all
registered projects and preserves requirements already recorded in uv's tool
receipt. `doctor` only lints installed distributions and resolves their entry
points. `repos add` is bookkeeping and reports pending plugins without
installing them.

No separate consent prompt is required. Activating a repository already grants
its committed handlers and agent prompts execution authority on that host, so
a committed wheel adds no new trust boundary. The optional SHA-256 protects the
declared artifact's integrity.

### Agent support

1. **Your existing agent**: The Markdown prompt remains a normal Claude Code or
  GitHub Copilot agent. Agents Live reads its `runtime` field only to select
  which installed CLI runs it unattended (`claude`, `copilot`,
  `agency copilot`, etc.).

2. **Agent as skill runner**: Copilot reads SKILL.md via AGENTS.md and invokes
   the same scripts conversationally - no slash command needed.

### Agent-specific behavior

| | Claude | Copilot | Agency Claude | Agency Copilot |
|--|--------|---------|---------------|----------------|
| Headless capture | `$(...)` works | Needs `script -qc` (writes to /dev/tty) | `$(...)` works | `$(...)` works (agency handles tty internally) |
| System prompt | `--append-system-prompt` | Not available | `--append-system-prompt` | Not available |
| Output format flag | `--output-format json` | N/A | `--output-format json` | N/A |
| JSON output format | JSON envelope with `result` + `usage` | Raw JSON | JSON envelope (same as claude) | Often markdown-fenced (` ```json `) |
| JSON reliability | High | Medium (needs checklist prompt) | High | High |
| Auth | API key | API key | API key + EntraID | EntraID (one-time interactive) |
| MCP disable flags | N/A | `--disable-mcp-server`, `--no-default-mcps` | N/A | `--disable-mcp-server`, `--no-default-mcps` |

### Token usage logging

Every agent run logs token usage on the `phase: agent` log entry. The source
of usage data varies by agent:

| Agent | Usage source | Fields logged |
|-------|-------------|---------------|
| `claude` | `--output-format json` response body | `model`, `tokens_in`, `tokens_out`, `tokens_cached`, `cost_usd` |
| `agency claude` | Same as claude (JSON envelope) | `model`, `tokens_in`, `tokens_out`, `tokens_cached`, `cost_usd` |
| `copilot` | stderr summary line | `tokens_in`, `tokens_out`, `tokens_cached`, `premium_requests` |
| `agency copilot` | stderr summary line | `tokens_in`, `tokens_out`, `tokens_cached`, `premium_requests` |

**Copilot/agency copilot stderr formats** (both supported by `parse_usage_stats()`):

```
# New format (agency v2026.4.9+, copilot CLI):
Requests  3 Premium (15s)
Tokens    ↑ 55.8k • ↓ 805 • 27.8k (cached)

# Old format (agency <v2026.4.9):
Total usage est:        3 Premium requests
claude-opus-4.6          162.3k in, 971 out, 0 cached
```

**Claude JSON envelope** (parsed by `parse_claude_json_output()`):

```json
{
  "result": "<agent output text>",
  "total_cost_usd": 0.0308,
  "usage": {"input_tokens": 3, "output_tokens": 174, "cache_read_input_tokens": 12380},
  "modelUsage": {"claude-sonnet-4-6": {"inputTokens": 3, "outputTokens": 174}}
}
```

The `result` field is unwrapped to become the agent output; usage fields are
extracted and logged alongside the output.

### Smoketest

`/agents-live smoketest` validates the full chain end-to-end:
create → frontmatter → status → agent CLI → JSON output → handler → file write →
watcher detect → agent CLI → stop

Supports all agents via `--agent`. Stops and cleans up on failure.

---

## 3. Prerequisites

| Tool | Install | Purpose |
|------|---------|---------|
| `python3` (≥ 3.12) | `sudo apt install python3` | All scripts are Python |
| `uv` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | Python package/environment manager |
| `node` / `npm` | `nvm install --lts` | Installing agent CLIs and MCP servers |
| Claude Code | `npm i -g @anthropic-ai/claude-code` | Agent CLI for `claude` / `agency claude` |
| Copilot CLI | `npm i -g @github/copilot` | Agent CLI for `copilot` / `agency copilot` |
| `crontab` | `sudo apt install cron` | Scheduled agents |
| `jq` | `sudo apt install jq` | JSON parsing in handlers |
| `inotifywait` | `sudo apt install inotify-tools` | File watcher |

### MCP server resolution (source of truth + override)

`.vscode/mcp.json` is the **single source of truth** for MCP server
definitions; agents-live reads it directly (`mcp_config_loader.py`).
Most servers are **disabled by default** in the master.

An agent's `mcps:` frontmatter list is resolved **against the master**, not
against whatever the workspace would auto-load. `headless.py resolve_mcp`
reads each requested server's definition from `.vscode/mcp.json`
**regardless of its `disabled` flag** and emits an explicit `--mcp` flag
(or `--additional-mcp-config` for copilot npx-stdio servers).
`_build_agent_command` pairs this with `--disable-mcp-server` for every
*other* workspace server. Net effect:

- An agent can enable a master-disabled server just by listing it in `mcps:`.
  The command-line flags override the `.mcp.json` / `agency.toml` /
  `agency.toml enabled=false` state.
- The agent only sees the servers its definition explicitly declared - no
  surprise inheritance from the workspace config.
- Agents Live runs therefore **ignore** the generated `agency.toml`
  enable/disable state; that file only governs *interactive* agency
  (where the user toggles servers in the TUI). VS Code reads the master
  directly.

So: keep the master mostly-disabled for a lean interactive baseline, and
let each agent opt in to exactly the servers it needs.

### MCP authentication

Agents using MCP servers (e.g. `@softeria/ms-365-mcp-server`) require a
one-time device code login before headless use:

```bash
MS365_MCP_TOKEN_CACHE_PATH="$HOME/.config/ms365-mcp/.token-cache.json" \
MS365_MCP_SELECTED_ACCOUNT_PATH="$HOME/.config/ms365-mcp/.selected-account.json" \
npx -y @softeria/ms-365-mcp-server --login
```

Follow the browser prompt to authenticate. Token cache is stored at
`~/.config/ms365-mcp/.token-cache.json`.

Use `agents-live doctor` to verify all are installed.

---

## 4. Logging

All logs use JSONL format (one JSON object per line) and live in the
user-level XDG state home, never in the project tree: this repo's logs are
under `~/.local/state/agents-live/repos/<name>-<hash>/logs/`, and host-level
logs (the built-in health-check loop) under
`~/.local/state/agents-live/logs/`. Repos sync between machines, so
machine-local logs in the tree were a safety and export hazard; keeping
state at the user level also lets infrastructure commands log before any
repo is resolved, and the tool work with no initialized project.

### Log files

| File | Purpose |
|------|---------|
| `logs/<name>.log` | Per-agent execution log - start, agent output, handler output, done |
| `logs/agents-live.log` | System-wide summary - every agent activation, completion, error |

### JSONL schema

Each line is a JSON object with at minimum `ts` (ISO-8601 UTC) and `phase`:

```json
{"ts":"2026-04-08T13:05:01Z","phase":"start","trigger":"cron","runtime":"agency copilot","mode":"plan","handler":"ms-todo-to-md.sh"}
{"ts":"2026-04-08T13:05:15Z","phase":"agent","status":"ok","output":"...","model":"claude-sonnet-4-6","tokens_in":"12.4k","tokens_out":"174","tokens_cached":"12.4k","cost_usd":"0.0308"}
{"ts":"2026-04-08T13:05:16Z","phase":"handler","status":"ok","message":"wrote 1 file, 9 unchanged"}
{"ts":"2026-04-08T13:05:16Z","phase":"done","status":"ok","duration_s":15.0}
```

The `phase: agent` entry includes optional usage fields when available:
`model`, `tokens_in`, `tokens_out`, `tokens_cached`, `premium_requests`
(copilot), `cost_usd` (claude). See "Token usage logging" above.

The system log (`agents-live.log`) adds an `agent_name` field and records
activations, completions, and errors:

```json
{"ts":"2026-04-08T13:05:01Z","agent_name":"todo-sync","phase":"start","trigger":"cron"}
{"ts":"2026-04-08T13:05:16Z","agent_name":"todo-sync","phase":"done","status":"ok","duration_s":15.0}
{"ts":"2026-04-08T13:10:01Z","agent_name":"todo-sync","phase":"done","status":"error","message":"agent exited with status 2","duration_s":2.1}
```

### Querying logs

**`logs`** (qlog.py under the hood) is the primary query tool across both live
JSONL and archived Parquet files.

```bash
# All errors across all logs (live + archive)
agents-live logs --errors --all

# Events for one agent in a time window
agents-live logs --agent exercise-state-update --since 2026-04-22T13:00 --until 2026-04-22T13:30

# Slow runs (duration > 30s)
agents-live logs --slow 30 --since 2026-04-22

# Custom SQL against the `log` view
agents-live logs --all --sql "SELECT agent_name, COUNT(*) FROM log WHERE status='error' GROUP BY 1 ORDER BY 2 DESC"

# Output formats: table (default), jsonl, csv
agents-live logs --errors --all --format csv
```

Filters: `--agent`, `--since`, `--until`, `--phase`, `--status`, `--trigger`,
`--slow SEC`, `--errors`. All optional, AND'd together.

**`logs timeline`** (timeline.py) produces a human-readable merged timeline
across all log files with phase icons (WATCH/START/PREP/AGENT/POST/DONE/FAIL/SKIP).

```bash
# Last 50 events across all agents
agents-live logs timeline

# Timeline for a specific agent
agents-live logs timeline exercise-state-update --since 2026-05-01T12:00

# Content substring filter (matches agent name, message, or any field)
agents-live logs timeline LMCO --last 30

# All agents in a window
agents-live logs timeline --all --since 2026-05-01T16:00
```

**Quick agent view** (live logs only, no archive):

```bash
# Table view (positional name resolves to <name>.log in the repo's state-home logs directory)
agents-live logs todo-pull --limit 20
```

### Log rotation (pattern)

A weekly `log-rotate` agent (cron `0 3 * * 0`) with a handler-only
pre-processor:

1. Reads each `.log` file in the repo's state-home logs directory
2. Moves rows older than 7 days into unified monthly Parquet files at
  `archive/YYYY-MM.parquet` under that directory (ZSTD compressed)
3. Rewrites the live `.log` with only the last 7 days

Monthly Parquet files are rewritten each rotation (union existing archive
+ new aged-out rows -> fresh file). `qlog.py` reads both live JSONL and
archived Parquet transparently -- no flag needed.

**Archive layout:**

```
~/.local/state/agents-live/repos/<name>-<hash>/logs/archive/
  2026-04.parquet
  2026-05.parquet
  2026-06.parquet
  ...
```

Each row carries `_src` provenance. `qlog.py` normalizes numeric query
columns across the text-typed archive and live JSONL and validates the
contract with `--all --check-schema`.

### Agent-removal behavior

The internal removal helper preserves logs by default. Use
`--delete-logs` to also remove the agent's log file.

---

## Related documents

| Document | Content |
|----------|---------|
| [overview.md](overview.md) | What the system is, design principles, landscape comparison, distribution plan |
| [key-learnings.md](key-learnings.md) | Implementation details, gotchas, and patterns discovered during development |
| [diagnostics.md](diagnostics.md) | Log inventory, diagnostic procedures, common patterns, query recipes |
| [changelog.md](changelog.md) | Reverse-chronological log of infrastructure changes |
| [commands.md](commands.md) | Command reference, CLI flags, cron expressions, script architecture, log schema |
| [reference/copilot-cli-headless-guide.md](reference/copilot-cli-headless-guide.md) | Practical guide to running Copilot CLI headless |
| [reference/wsl-runbook.md](reference/wsl-runbook.md) | WSL operational runbook: debugging, restarting, verifying |
| [reference/cascade-modeling.md](reference/cascade-modeling.md) | Methodology for proving watcher cascades terminate |
| [reference/session-transcript-capture.md](reference/session-transcript-capture.md) | Research on session transcript capture |
