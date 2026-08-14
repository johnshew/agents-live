# agents-live

[![PyPI version](https://img.shields.io/pypi/v/agents-live)](https://pypi.org/project/agents-live/)
[![Python 3.12 or later](https://img.shields.io/badge/python-3.12%2B-blue)](https://pypi.org/project/agents-live/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Take your agents live.** Turn Claude Code and GitHub Copilot agents into
scheduled and file-triggered local automations, without moving them to another
agent platform.

Definitions can be conforming Agent Skill directories or flat Markdown files
in configured repository directories. Agents Live reads their namespaced
execution metadata, adds local triggers, and repairs drift using standard host
tools.

### `Agents/markdown-polisher/SKILL.md`

```markdown
---
name: markdown-polisher
description: Polish Markdown documents when they change.
metadata:
  agents-live.schema-version: "1"
  agents-live.selector: "claude"
  agents-live.mode: "write"
  agents-live.watch: "docs/** debounce 1s"
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
agents-live start markdown-polisher
```

The watcher sleeps until a file changes, then runs the agent immediately with
the changed paths. Add or edit a Markdown file under `docs/`, then open the
file to see the fixes.

Manage the running agent with `status` and `stop`:

```bash
agents-live status
agents-live stop markdown-polisher
```

There is no polling interval or clock tick. The agent runs only when the
operating system reports a change in the watched directory.

## Lightweight

There is no listener service, separate application runtime, or database to
deploy and maintain. The core stack is the Claude Code or GitHub Copilot CLI
you already use, `uv`, and your host scheduler and file-watch facility.

Cron-only agents have no persistent process. A file-watch agent uses one small
local watcher. There are no externally reachable ports or databases. Custom
post-processors and plugins may bring their own dependencies; Agents Live core does
not require them.

## Safe by default

Execution modes make write access explicit:

1. `plan` is read-only. The agent emits JSON for a validated post-processor to apply.
2. `pipeline` limits the agent to a schema-checked data channel shared with
   your pre-processors and post-processors.
3. `write` grants full write access as an explicit per-agent choice.

This is tool policy, not a sandbox. Agents still inherit the permissions of
your local account and agent CLI.

The example uses `write` so it can fix documents directly. For tighter
control, use the complete [plan and pipeline Markdown-polisher
examples](src/agents_live/skill/docs/markdown-polisher.md). They apply the
same correction task through validated, deterministic write boundaries.

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
needed when definitions watch files or directories. On WSL, the first
convergence stages and verifies Windows-side liveness before replacing an
existing task, so scheduled runs do not require an open session.

On Windows:

```powershell
winget install --id=astral-sh.uv -e
uv tool install agents-live
agents-live init
```

Windows uses Task Scheduler and a built-in watcher, so there is nothing more to
install.

The installed `.claude/skills/agents-live/` payload is tool-managed and carries
a directory-local `.gitignore`; project-authored sibling skills are unaffected.
For an existing repository that already tracks the payload, run
`git rm -r --cached .claude/skills/agents-live`, then
`git add -f .claude/skills/agents-live/.gitignore`. Agents Live never changes the
Git index itself.

Note that macOS is untested.

Run `agents-live doctor` to diagnose missing requirements and inspect
configuration. Use `agents-live doctor --repair` to repair supported
configuration issues.

## Go further

Definitions live under a registered repository's `Agents/` directory by
default. Set `agent_directories = ["foo"]` in `.agents-live.toml` to also
discover immediate `foo/<name>.md` files and `foo/<name>/SKILL.md` bundles.
Register another repository with `agents-live init --repo <path>`.

See the [command reference](src/agents_live/skill/docs/commands.md) for
repository workflows, health checks and repair, upgrades, dashboards, shell
completion, plugins, ownership, and multi-repository operations. The
[architecture guide](src/agents_live/skill/docs/approach.md) covers runtime,
safety, persistence, and maintenance behavior.

## Documentation

Every workflow is an ordinary CLI command.

- [Overview](src/agents_live/skill/docs/overview.md)
- [Starter templates](src/agents_live/skill/templates/)
- [Definition format](src/agents_live/skill/docs/definition-format.md)
- [Skill reference](src/agents_live/skill/SKILL.md)
- [Changelog](src/agents_live/skill/docs/changelog.md)

Design documents and the high-level backlog for the project itself live in
[docs/](docs/); they are not installed with the skill.

## Contributing

Bug reports and pull requests are welcome in
[Issues](https://github.com/johnshew/agents-live/issues).

## License

[MIT](LICENSE)
