---
title: Agents Live Overview
description: Architecture, design principles, and market positioning for agents-live
ms.date: 2026-07-19
ms.topic: overview
---

> What the system is, the design principles behind it, and how it compares
> to other agent-automation offerings. Written 2026-07-11 09:25 PDT from a
> landscape review; refresh the comparison section before citing it externally.
> **This document describes the end state** (single-command package
> published, skill thin, deployments consuming the package). For what
> remains before that state, see the release sequence in
> backlog.md.

---

## 1. What it is

**Agents Live quickly adds safe, local automation to the Claude Code and
GitHub Copilot agents you already use.**

Agents Live does not replace or reinvent your existing agents. Your agent
stays one Markdown file with the same prompt. Add Agents Live fields to
its frontmatter to say *when* the agent runs (a cron schedule, a watched
directory, or both) and *how* it runs (the agent CLI, execution mode, and
optional pre/post scripts). A live agent looks like this:

```yaml
---
runtime: claude            # unattended execution adapter
mode: plan                 # read-only; a handler script does the writing
watchPath: notes/inbox/    # or schedule: "0 8 * * *", or both
post-processor: file-notes.sh
---
For each new file in notes/inbox/: add frontmatter and tags, fix the
title, decide where it belongs in the vault, and emit JSON:
{ "moves": [{ "from": "...", "to": "...", "content": "..." }] }
```

Drop a raw note into `notes/inbox/` and the watcher fires within
seconds; one agent run later the note is cleaned up and filed. The agent
decides, but the only thing that touches your vault is `file-notes.sh`,
a deterministic script you own.

**Why use this?** You keep the Claude Code and GitHub Copilot agents, prompts,
tools, authentication, and CLI workflows you already use. Agents Live adds
local scheduling, file triggers, debounce, locking, cost-tracked logs, and
self-healing, self-optimizing outer loops around them. You do not need to
migrate into a new agent platform, install an always-on desktop app or gateway,
or send your work through a hosted orchestration service. This is less
machinery than OpenClaw, Microsoft Scout, or Claude Routines, and more complete
than the wrap-`claude -p`-in-cron approach that Anthropic's own docs recommend.

**Safe by default.** Write access is a ladder with an explicit final rung:

1. **plan** (the default) - the agent runs read-only and emits JSON; a
   deterministic handler script you own validates and applies the result.
2. **pipeline** - by default, the agent's tool surface narrows to a
   schema-checked `put`/`get` side-channel that your pre/post-processors
   mediate, over a token-protected loopback endpoint that exists only for
   the duration of one run.
3. **write** - full write access: the last option, an explicit per-agent
   opt-in.

**Lightweight.** There is no framework to learn, no APIs to call, and no
daemon to maintain - just things you use every day: git, files, markdown,
cron, and scripts. Cron and inotifywait do the triggering; Agents Live adds
activation, debounce, concurrency, structured logs, and per-run token cost.
If you can read a crontab, you can audit the whole system. What it adds to
your machine:

| What you add | How much |
|---|---|
| Python package | one - `uv tool install agents-live` (plus `uv` itself if needed) |
| Host trigger tools | cron and inotifywait; `doctor` reports anything missing |
| Frontmatter in your agent file | three or four fields |
| Application daemons or gateways | none |
| Persistent processes | one small watcher loop per file-watch agent; none for cron-only agents |
| Externally reachable inbound ports | none |
| Databases | none - plain-text JSONL logs, aged into monthly Parquet archives |

Trying it takes about a minute:

```bash
uv tool install agents-live        # install the Python package
source <(agents-live completions bash) # optional shell completion
agents-live doctor                 # verify cron, inotifywait, and agent CLIs
agents-live run file-notes         # test the agent once, in the foreground
agents-live start file-notes       # activate it unattended
agents-live stop file-notes        # clean up - remove its triggers
```

On interactive terminal invocations, agents-live checks PyPI for a newer
stable release when its shared cached result is missing or one hour old. The
result is stored under
`$XDG_CACHE_HOME/agents-live/` (normally `~/.cache/agents-live/`). For ordinary
commands, the refresh runs in the background and is skipped for
scheduled/internal, quiet, JSON, piped, or redirected invocations. Network and
cache failures never affect the command. This request sends only ordinary
package-index request metadata; it does not include project or agent data.
`agents-live doctor` is the exception: it always performs a fresh check and
updates the cache. Checks never install updates in the background. Run
`agents-live upgrade` to reinstall the uv-managed runtime at the latest stable
release without dropping co-installed requirements, converge project-declared
plugins, and then refresh managed skill payloads using the newly installed CLI.

Repositories used from outside their working tree can be registered by path
under `$XDG_CONFIG_HOME/agents-live/config.toml` (normally
`~/.config/agents-live/config.toml`). Explicit selection and local project
markers take precedence over the optional default. `status --all-repos`,
`doctor --all-repos`, and `dashboard --all-repos` provide repo-qualified,
read-only views; lifecycle actions remain scoped to one selected repository.

Bare `agents-live upgrade` works outside a project and refreshes the current
initialized project plus every available registered repository. An explicit
`--repo PATH|ALIAS` limits payload refresh to that project. Use `--runtime-only`
or `--skills-only` to run one half of the workflow. Unavailable registered
repositories are reported without blocking other refreshes. `init` remains the
first-time project setup command.

Projects that need plugin-provided adapters or registry ownership declare a
committed wheel under `[plugins]` in `.agents-live.toml` or
`[tool.agents-live.plugins]` in `pyproject.toml`. The wheel path is
repository-relative, and an optional SHA-256 pins its contents. `init`, `start`,
and `upgrade` converge declarations into the host-global uv tool environment;
`doctor` verifies each distribution and its agents-live entry points.

No setup step: the first `run` or `start` inside a git repository records
the project root by writing a minimal `.agents-live.toml` marker (local
mode, all defaults). `agents-live init` is optional - run it to install
the conversational `/agents-live` skill (itself optional support for
the CLI), seed the agent directories, or declare more complex
(multi-host) configuration. `stop` pauses an agent without
removing it, so the full lifecycle is
`create -> run -> start -> stop`. `stop` removes triggers while preserving
the agent definition. An optional
local dashboard adds run/pause/activate controls, ownership visibility, and
trailing 24-hour and seven-day cost per agent - convenient, never required;
everything it does is also a CLI command.

**Honest limits.** The runtime is light: just cron, inotify, and `uv`. But the
implementation is not tiny, at 15K+ lines of code and docs that support
debounce layers, cascade protection, log archiving, multi-host ownership, and
integrated smoketests. It is Linux-first: Ubuntu on WSL is the reference, Windows
support is partial, and macOS is untested. `uv` is a hard dependency
(PEP 723 scripts, no shared venv). And agents inherit your local
account's privileges unless you configure stricter CLI or OS isolation
- the plan/pipeline/write ladder is tool policy, not a sandbox.

## 2. Core design principles

The design follows the Linux philosophy: simple, easy-to-understand
primitives - cron, inotify, one file per agent, scripts - that compose into
sophisticated behavior.

The core is deterministic event processing
augmented with insights from your existing agent: triggers and handler scripts
are predictable machinery; the agent supplies flexibility and judgment in the
middle (safely, in read-only plan mode by default).

Indelible logs record
every run, and self-healing, self-optimizing outer loops keep the system
stable and continuously improving.

1. **No new execution platform.** The moving parts are cron, inotifywait, `uv`, and
   the agent CLIs you already have. There is no persistent gateway, queue,
   externally reachable listener, or application daemon. Pipeline mode binds
   a token-protected loopback MCP endpoint only for the duration of one run.
   The only persistent processes are one inotify watcher loop per file-watch
   agent.
2. **Host trigger state is the source of truth.** Agent state is computed on demand
   from the crontab and the process list. There is no manifest to drift.
   The only shared state file records which host owns each agent.
3. **One file per agent; git is the deployment mechanism.** Config is
   frontmatter, the prompt is the body. Committing an agent file distributes
   it; `git rm` decommissions it everywhere (orphan pruning on the hourly
   health check tears down cron entries and watchers whose file is gone).
4. **The agent thinks, a script acts.** Default mode is `plan` (read-only):
   the agent emits JSON, a deterministic handler you wrote validates and
   applies it. Prefer declaring that JSON contract as a schema in the agent
   doc itself, versioned alongside the prompt. `pipeline` mode goes further:
   it narrows the agent's entire
   tool surface to a schema-checked `put`/`get` side-channel that the
   pre/post-processors mediate. Full write access is the last option and
   requires an explicit per-agent opt-in.
5. **Everything is logged and queryable.** JSONL per agent plus a system log,
   with phase, status, duration, model, token counts, and cost on every
   agent run. Logs age into monthly Parquet archives and are queried
   through one query tool (`qlog.py`) and a correlated timeline view.
   Logs live in the user-level XDG state home, not the project tree:
   repos sync between machines, so machine-local logs in the tree were an
   export hazard, and user-level state lets the tool log and repair before
   any project is resolved.
6. **Fail gracefully, self-heal, and self-optimize.** A built-in
   check-and-repair loop (`agents-live health-check`, self-installed on
   `@reboot` + hourly crontab entries) sweeps every registered repository:
   it restarts dead watchers, prunes orphans, converges persisted crontab
   entries, enforces multi-host ownership, and stamps a host health beacon.
   A fire-rate circuit breaker stops accidental runaway watcher cascades.
   Automated outer loops continually monitor overall system behavior (using the
   indelible logs): an hourly diagnosis agent turns log errors into
   root-caused issues, and a weekly determinism audit reads per-agent cost
   and error rates to move work out of agents and into scripts - steadily
   more deterministic behavior and lower token usage.
7. **Agent-CLI agnostic.** `claude` and `copilot` are supported peers
   (plus private EntraID-authenticated variants in this deployment);
   per-CLI quirks (headless flags, JSON envelopes, usage parsing) are
   isolated in one module.

### 2.1 Recent highlights

A few of the capabilities the system has accumulated. The full curated
capability-evolution history lives in the [changelog](changelog.md),
alongside the complete implementation record.

* You can define an agent once in `.claude/agents/` or `.github/agents/`
  and use it both interactively and on cron or file-watch triggers.
* You can decommission an agent fleet-wide with `git rm`; orphan pruning
  removes its cron entries and watcher processes on each host's health pass.
* You can let an agent exchange schema-checked data through token-protected
  `put`/`get` tools without granting general write access (pipeline mode).
* You can preview single-agent or batch activation, including ownership
  changes, before anything mutates crontab, watchers, or the fleet registry.
* Your host is protected from machine-speed file-change loops by a watcher
  circuit breaker that stops and logs excessive dispatch rates.
* A weekly determinism audit compares per-agent cost and error history to
  move work out of agents and into scripts - steadily more deterministic
  behavior and lower token usage.

## 3. How it compares (July 2026 landscape)

| System | Shape | Triggers | Runs locally? | Weight / posture |
|---|---|---|---|---|
| **Agents Live** (this) | CLI package + thin skill over cron/inotify | cron, file watch, both | Yes, your files | No persistent gateway; loopback-only pipeline endpoint; plan-mode default |
| **Microsoft Scout** | Always-on desktop "Autopilot" for M365 (Build 2026; reportedly built on OpenClaw) | Deadlines, approvals, user routines | Desktop app, M365-centric | Closed, enterprise-gated (Frontier, Intune, Entra); not a dev trigger tool |
| **OpenClaw** (ex-Clawdbot) | Node gateway daemon + SQLite + chat integrations | cron, heartbeat, hooks; no file watcher | Yes, via daemon | Heavy; Feb-Mar 2026 security crisis (widely reported exposed-instance scans, extensive CVE list, malicious ClawHub skills) |
| **Claude Routines** | Anthropic cloud scheduler | cron, HTTP, GitHub events | No local filesystem | Cloud; 1h min interval, run caps |
| **Claude Desktop scheduler** | Desktop app scheduler | schedule only | Yes | Needs the app running; no file watch |
| **Claude Code /loop + cron tools** | In-session scheduling | intervals, cron | Yes | Session-scoped, 7-day expiry, dies with the terminal |
| **GitHub Agentic Workflows / claude-code-action** | Actions-compiled agent workflows | cron, issues, PRs, comments | GitHub runners | Cloud/repo-bound, good guardrails |
| **Multica / agent-deck / claudectl / 5dive** | Platforms, session managers, orchestrators | webhooks, cron, chat | Mixed | Server/daemon-shaped; none is the small local tool |

The decisive gap: nothing else takes the agents you already have and makes
them live. The exact feature - firing your existing headless agent CLIs
from cron and local file watches without an application gateway - was
requested as claude-code issue
[#28229](https://github.com/anthropics/claude-code/issues/28229)
and closed as **not planned** (April 2026); Anthropic's
own docs still recommend wrapping `claude -p` in cron yourself, and no
native file watcher exists. Alternatives that do event-triggering well are
cloud-bound, heavyweight daemon platforms, or session-scoped - and each
means building agents for its platform, rather than letting the agents you
already run do more.

The individual ingredients - schedules, locking, structured logs, budgets,
read-only modes, mediated outputs - are not unique; GitHub Agentic
Workflows and OpenClaw each have substantial versions. The defensible
combination is **local filesystem events + normal Unix scheduling +
multiple existing agent CLIs + deterministic pre/post stages, with no
application gateway**. GitHub Agentic Workflows is the strongest
architectural comparator - it has the same deterministic-setup,
constrained-agent, mediated-output shape - but its boundary is GitHub and
hosted runners, not your local filesystem.

**Security positioning.** The contrast with OpenClaw is significant:
after the February-March 2026 exposure reports, many organizations
advised against running it on a normal machine. By contrast, a tool
whose entire attack surface is just your crontab, a watcher process,
and deterministic scripts you control is a much more tractable
alternative. Each run also wraps the agent in a constraint envelope:
plan mode makes it read-only, pipeline mode narrows its tool surface to
a mediated side-channel, and write access is granted only per agent. These are
tool-policy and mediated-output boundaries, not OS-level isolation. Process,
filesystem, and network
sandboxing would provide additional protections. Agents inherit the
local account's privileges unless stricter CLI or OS isolation is
configured.

Key sources: [Scout coverage](https://petri.com/microsoft-scout-autonomous-ai-agent-enterprise-security/),
[OpenClaw security crisis](https://conscia.com/blog/the-openclaw-security-crisis/),
[Anthropic scheduling docs](https://code.claude.com/docs/en/scheduled-tasks),
[GitHub Agentic Workflows](https://github.github.com/gh-aw/).

## 4. Distribution

Two artifacts, one source of truth:

1. **One `agents-live` package**, developed at
   `github.com/johnshew/agents-live` and installed with `uv tool`:
   the CLI (`run`, `start`, `status`, `logs`, `smoketest`, `init`,
   `doctor`, `migrate`, ...) plus an importable handler SDK for live agents (event
   logging, pipeline put/get client, config access, guarded file writer)
   in the same package. `init` installs the vendored skill and seeds the
   agent directories; `doctor` (read-only) verifies the install and
   environment.
2. **The skill as a thin, optional layer over the CLI**, vendored in
   the package: SKILL.md keeps the conversational surface (make
   existing agents live, debug runs, trace pipelines) and drives
   `agents-live <cmd>`. Installed as a Claude Code skill by `init`;
   the same doc serves Copilot via AGENTS.md. The skill is optional
   support for the CLI - everything it does is an ordinary
   `agents-live` command, and the CLI is fully usable without it.

The public repo is the source of truth for all framework code. Private
deployments (this one included) consume released versions and extend
through plugin entry points (private agent adapters, multi-host
ownership); agents and handlers stay in the consuming repo, while
machine-local logs live in the user-level state home.
Remaining sequencing lives in the release-sequence section of
backlog.md.
