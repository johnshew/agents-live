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
  agents-live.schema-version: "2"
  agents-live.selector: "claude"
  agents-live.mode: "write"
  agents-live.watch: "docs/** debounce 1s"
---
Correct spelling, grammar, and Markdown formatting errors in the selected files.
Preserve their meaning, links, code, and frontmatter. When a `Files changed:`
list is present, process only those files.
```

## Quick start

See [Installation](#installation) for required host tools and platform-specific
instructions.

```bash
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/johnshew/agents-live/releases/latest/download/install.sh | sh
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
post-processors and plugins may bring their own dependencies; Agents Live core
does not require them.

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

Agents Live supports Linux, WSL, and native Windows. macOS is currently
untested. First install and sign in to at least one supported provider CLI:

- [Claude Code](https://code.claude.com/docs/en/setup)
- [GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli/cli-getting-started)

### Linux and WSL

On Debian, Ubuntu, or WSL, install the host tools used by schedules and file
watchers, then run the latest stable installer:

```bash
sudo apt-get update
sudo apt-get install -y cron inotify-tools
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/johnshew/agents-live/releases/latest/download/install.sh | sh

agents-live --repo /path/to/repository init
```

`cron` runs scheduled agents and automatic maintenance. `inotifywait` is only
needed for file or directory watches. The installer links `agents-live` and
`al` from `~/.local/bin` to the stable `current` commands and adds that
directory to supported shell profiles. Open a new shell if the current one did
not already include `~/.local/bin` on `PATH`.

The installer refuses to replace an existing `~/.local/bin/agents-live` or
`~/.local/bin/al` that does not point to this installation. Remove or rename
the conflicting command, then run the installer again.

On WSL, use these Linux instructions inside the distribution. The first
convergence also stages and verifies Windows-side liveness so scheduled work
can wake the distribution without an open terminal.

### Windows

Run PowerShell as your normal user. Install a provider CLI if one is not
already available:

```powershell
winget install Anthropic.ClaudeCode
# Or: winget install GitHub.Copilot
```

Run the latest stable installer, then initialize a repository:

```powershell
irm https://github.com/johnshew/agents-live/releases/latest/download/install.ps1 | iex
agents-live --repo C:\path\to\repository init
```

The installer updates both the current PowerShell process and the persistent
user `PATH`. Native Windows uses Task Scheduler and directory change
notifications, so no separate scheduler or watcher package is required.

### Manage the installation

```bash
agents-live upgrade
agents-live uninstall
```

See the [command reference](src/agents_live/skill/docs/commands.md) for pinning,
rolling back, or removing installed versions. The [diagnostics
guide](src/agents_live/skill/docs/diagnostics.md) covers installation conflicts,
existing repositories, and repair.

## Go further

Definitions live under a registered repository's `Agents/` directory by
default, and Agents Live also searches `.claude/skills/`, `.github/skills/`,
and `.agents/skills/`, claiming a skill there only when it carries
`agents-live.` execution metadata. Set `agent_directories = ["foo"]` in
`.agents-live.toml` to also discover immediate `foo/<name>.md` files and
`foo/<name>/SKILL.md` bundles.
Register another repository with `agents-live init --repo <path>`. Once
registered, `run`, `start`, `stop`, and `status` fall back to the other
registered repositories when a name is not present locally.

Cross-machine assignment is optional. Repository registration and ownership
backend installation leave a project local-only. Run
`agents-live ownership enable` to validate the backend and owners document
before enabling transfers; `agents-live ownership status` reports the mode.

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
