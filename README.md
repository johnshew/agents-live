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
export PATH="${XDG_DATA_HOME:-$HOME/.local/share}/agents-live/current/bin:$PATH"
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

The release installers fetch an authenticated wheel from GitHub, install
[`uv`](https://docs.astral.sh/uv/getting-started/installation/) if it is
missing, and activate the new version only after validation succeeds. Python
3.12 or newer is required; uv obtains a compatible interpreter when the host
does not have one.

### Linux and WSL

On Debian, Ubuntu, or WSL, install the host tools used by schedules and file
watchers, then run the latest stable installer:

```bash
sudo apt-get update
sudo apt-get install -y cron inotify-tools
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/johnshew/agents-live/releases/latest/download/install.sh | sh

export PATH="${XDG_DATA_HOME:-$HOME/.local/share}/agents-live/current/bin:$PATH"
agents-live --repo /path/to/repository init
agents-live --repo /path/to/repository doctor
```

`cron` runs scheduled agents and automatic maintenance. `inotifywait` is only
needed for file or directory watches. The installer adds the stable command
directory to supported shell profiles; the `export` above makes it available
to the current shell immediately.

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

Download and run the latest stable installer, then initialize a repository
through the absolute stable command so the current shell does not depend on a
refreshed `PATH`:

```powershell
$installer = Join-Path $env:TEMP "agents-live-install.ps1"
Invoke-WebRequest `
  "https://github.com/johnshew/agents-live/releases/latest/download/install.ps1" `
  -OutFile $installer
& $installer

$agentsLive = Join-Path $env:LOCALAPPDATA "agents-live\current\Scripts\agents-live.exe"
& $agentsLive --repo C:\path\to\repository init
& $agentsLive --repo C:\path\to\repository doctor
```

The installer updates the user `PATH`; open a new terminal before relying on a
bare `agents-live` command. `init` prints the generated PowerShell completion
script path and the exact line to add to `$PROFILE`. Native Windows uses Task
Scheduler and directory change notifications, so no separate scheduler or
watcher package is required.

### Install an exact version

Every installer asset attached to a release is stamped with that release's
exact version. Download it from the version-specific release path to pin an
installation without passing a separate argument. Prereleases are never
selected through the `latest` path, and reinstalling the same version is safe.

On Linux or WSL:

```bash
version="6.8.0"
curl --proto '=https' --tlsv1.2 -LsSf \
  "https://github.com/johnshew/agents-live/releases/download/v${version}/install.sh" \
  | sh
```

On Windows:

```powershell
$version = "6.8.0"
$tag = [Uri]::EscapeDataString("v$version")
$installer = Join-Path $env:TEMP "agents-live-install-$version.ps1"
Invoke-WebRequest `
  "https://github.com/johnshew/agents-live/releases/download/$tag/install.ps1" `
  -OutFile $installer
& $installer
```

Both installers authenticate the selected release through GitHub metadata and
verify its asset size and SHA-256 digest. Network, proxy, TLS, provenance, and
checksum failures stop without a package-index or stale-cache fallback.

### Upgrade, roll back, and remove

Agents Live retains immutable versions side by side and selects one through the
stable `current` path. Source plugins remain in their declaring repositories
and load directly into the selected runtime; installation does not copy or
install them into a generation.

```bash
agents-live upgrade
agents-live generations list
agents-live generations activate VERSION
agents-live uninstall
```

Selecting a generation converges native triggers and still-started watchers
through that version. Work already running may finish on the immutable version
where it began. Use `generations remove VERSION` to discard an inactive
candidate, and `generations collect` to retain one rollback generation while
removing older versions that no process is using.

An existing legacy `uv tool` installation is removed only after the verified
generation is active. The package also installs `al` as an exact shorthand for
`agents-live`. If an unrelated `al` executable already exists, remove or rename
it before installation rather than forcing replacement.

### Existing repositories and diagnostics

The installed `.claude/skills/agents-live/` payload is tool-managed and carries
a directory-local `.gitignore`; project-authored sibling skills are unaffected.
For an existing repository that already tracks the payload, run
`git rm -r --cached .claude/skills/agents-live`, then
`git add -f .claude/skills/agents-live/.gitignore`. Agents Live never changes the
Git index itself.

Run `agents-live doctor` to diagnose missing requirements and inspect
configuration. Use `agents-live doctor --repair` to repair supported
configuration issues. Automatic maintenance rotates framework logs and removes
retained transcripts and processor output after 30 days by default. Set
`retention_days` to a positive integer in `.agents-live.toml` (or
`[tool.agents-live]`) to change that repository policy.

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
