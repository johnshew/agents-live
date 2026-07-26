# agents-live

[![PyPI version](https://img.shields.io/pypi/v/agents-live)](https://pypi.org/project/agents-live/)
[![Python 3.12 or later](https://img.shields.io/badge/python-3.12%2B-blue)](https://pypi.org/project/agents-live/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

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

See [Installation](#installation) for required host tools and installation
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
you already use, `uv`, and your host scheduler and file-watch facility.

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

## Installation

Install Claude Code, GitHub Copilot CLI, or both:

```bash
npm i -g @anthropic-ai/claude-code
npm i -g @github/copilot
```

Then install [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
and Agents Live.

On Debian or Ubuntu:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo apt install cron inotify-tools
uv tool install agents-live
agents-live init
```

`cron` runs scheduled agents and automatic maintenance; `inotifywait` is only
needed when agents watch files or directories. On WSL, `agents-live init`
installs a heartbeat that keeps the distro running, so scheduled agents fire
without an open session.

On Windows:

```powershell
winget install --id=astral-sh.uv -e
uv tool install agents-live
agents-live init
```

Windows uses Task Scheduler and a built-in watcher, so there is nothing more to
install.

Note that macOS is untested.

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
- [Changelog](src/agents_live/skill/docs/changelog.md)

Design documents and the high-level backlog for the project itself live in
[docs/](docs/); they are not installed with the skill.

## Contributing

Bug reports and pull requests are welcome in
[Issues](https://github.com/johnshew/agents-live/issues).

## License

[MIT](LICENSE)
