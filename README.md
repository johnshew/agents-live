# agents-live

**Take your agents live.** Turn Claude Code and GitHub Copilot agents into
scheduled and file-triggered local automations, without moving them to another
agent platform.

Your agent stays one Markdown file. Agents Live adds triggers, execution
controls, logs, and automatic repair using standard host tools.

### `markdown-polisher.md`

```markdown
---
description: Polish Markdown documents when they change.
runtime: claude
mode: write
watchPath: docs/
---
Correct spelling, grammar, and Markdown formatting errors in the selected files.
Preserve their meaning, links, code, and frontmatter. When a `Files changed:`
list is present, process only those files.
```

## Quick start

See [Prerequisites](#prerequisites) for required host tools and installation
details.

```bash
uv tool install agents-live
agents-live init
agents-live start ./markdown-polisher.md
```

The watcher sleeps until a file changes, then runs the agent immediately with
the changed paths. Add or edit a Markdown file under `docs/`, then open the
file to see the fixes.

Manage the running agent with `status` and `stop`:

```bash
agents-live status
agents-live stop ./markdown-polisher.md
```

There is no polling interval or clock tick. The agent runs only when the
operating system reports a change in the watched directory.

## Lightweight

There is no listener service, separate application runtime, or database to
deploy and maintain. The core stack is the Claude Code or GitHub Copilot CLI
you already use, `uv`, `crontab` for scheduling and maintenance, and
`inotifywait` for file watches.

Cron-only agents have no persistent process. A file-watch agent uses one small
local watcher. There are no externally reachable ports or databases. Custom
handlers and plugins may bring their own dependencies; Agents Live core does
not require them.

## Safe by default

Execution modes make write access explicit:

1. `plan` is read-only. The agent emits JSON for a validated handler to apply.
2. `pipeline` limits the agent to a schema-checked data channel shared with
   your pre-processors and post-processors.
3. `write` grants full write access as an explicit per-agent choice.

This is tool policy, not a sandbox. Agents still inherit the permissions of
your local account and agent CLI.

The example uses `write` so it can fix documents directly. For tighter
control, use [`plan`](src/agents_live/skill/docs/approach.md#execution-modes)
with a validated handler or
[`pipeline`](src/agents_live/skill/docs/approach.md#execution-modes) with
schema-checked pre-processors and post-processors.

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/getting-started/installation/):
   `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Claude Code: `npm i -g @anthropic-ai/claude-code`
- GitHub Copilot CLI: `npm i -g @github/copilot`
- `crontab` and `inotifywait`: `sudo apt install cron inotify-tools`

Install Claude Code, GitHub Copilot CLI, or both. `crontab` supports
initialization, automatic maintenance, and scheduled agents; `inotifywait` is
only required when agents watch files or directories for changes.

Linux is the primary platform, with Ubuntu on WSL as the reference setup.
Windows support is partial and macOS is untested.

Run `agents-live doctor` to diagnose missing requirements and inspect
configuration. Use `agents-live doctor --repair` to repair supported
configuration issues.

## Go further

Repositories are optional. Initialize one later with `agents-live init --repo`
when you need shared configuration or name-based commands.

See the [command reference](src/agents_live/skill/docs/commands.md) for
repository workflows, health checks and repair, upgrades, dashboards, shell
completion, plugins, ownership, and multi-repository operations. The
[architecture guide](src/agents_live/skill/docs/approach.md) covers runtime,
safety, persistence, and maintenance behavior.

## Documentation

The optional `/agents-live` skill is installed by `agents-live init`, but every
workflow remains an ordinary CLI command.

- [Overview](src/agents_live/skill/docs/overview.md)
- [Starter templates](src/agents_live/skill/templates/)
- [Skill reference](src/agents_live/skill/SKILL.md)
