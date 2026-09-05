---
title: Agents Live overview
description: Safe local automation for standard Agent Skill definitions
ms.date: 2026-09-03
ms.topic: overview
---

# Agents Live

Agents Live adds scheduled and file-change automation to conforming Agent
Skills. It uses the host's native trigger store, runs provider CLIs locally,
and keeps repository content separate from machine-local started state and
observability.

## Definition

Each runnable definition is either a standard `<name>/SKILL.md` bundle or an
Agents Live flat `<name>.md` document in one of the standard discovery roots
(`Agents/`, `.claude/skills/`, `.github/skills/`, `.agents/skills/`) or a
repository-relative root you configure. Standard Agent Skills properties stay
at the top level; Agents Live policy uses quoted `agents-live.*` metadata.
Flat documents use the same content schema but are an Agents Live extension,
not conforming Agent Skills.

```yaml
---
name: markdown-polisher
description: Polishes Markdown documents after they change.
metadata:
  agents-live.schema-version: "2"
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

Cross-machine assignment is optional. Repository registration and ownership
backend installation leave a project local-only. Run
`agents-live ownership enable` to validate the backend and owners document
before enabling transfers; `agents-live ownership status` reports the mode.

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

The quick-start Markdown polisher uses `write` mode for a minimal first run.
The complete [plan and pipeline variants](markdown-polisher.md) perform the
same task with schema validation and deterministic processors that enforce the
changed-file set before writing.

Malformed definitions fail closed. An unreadable registry or started-state
record causes convergence to abstain rather than prune. Two narrower failures
hold rather than withdraw what is already installed: a registered repository
that cannot be read, and a started definition that no longer parses. Both are
reported by `status` and `doctor`, and `stop` still withdraws either one.

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

Install at least one supported provider CLI through its current official
installer, then use the verified release bootstrap. It selects the latest
stable release by default, authenticates release bytes before activation, and
does not resolve Agents Live through a configured Python index:

```bash
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/johnshew/agents-live/releases/latest/download/install.sh | sh
agents-live init --repo /path/to/repository
agents-live doctor
```

On native Windows, run the release `install.ps1` from PowerShell. Every
published installer asset is stamped with its release version, so a script
downloaded from a version-specific release path needs no version argument.
The `latest` path selects the latest stable release. Both scripts install uv
when needed, retire an uncontested legacy uv tool installation after activating
the generation, and print the generated command under `current` that works
immediately.
`doctor` validates the active generation, ownership record, and stable commands;
`upgrade` uses the same authenticated generation path, and `uninstall` removes
the owned installation.

For a published Windows bake, download and run the bootstrap with the complete
commit-qualified version. URI-encode the tag in the direct GitHub URL; the
downloaded script already carries the original version:

```powershell
$version = "<complete-commit-qualified-version>"
$tag = [Uri]::EscapeDataString("v$version")
$installer = Join-Path $env:TEMP "agents-live-install-$version.ps1"
Invoke-WebRequest `
  "https://github.com/johnshew/agents-live/releases/download/$tag/install.ps1" `
  -OutFile $installer
& $installer

$agentsLiveBin = Join-Path $env:LOCALAPPDATA "agents-live\current\Scripts"
& (Join-Path $agentsLiveBin "agents-live.exe") --version
```

GitHub encodes a PEP 440 local-version `+` as `%2B`. An existing PowerShell or
activated virtual environment may retain a PATH snapshot from before
installation. Open a new terminal, or prepend `$agentsLiveBin` to `$env:Path`
for that process, when the absolute `current` command is healthy but bare
`agents-live` is missing or resolves an older version.

The installation root is a local version store: complete PEP 440 versions are
retained side by side and `current` selects one. Commit-suffixed bake versions
therefore coexist on the same release line. Source plugins remain in their
declaring repositories and load directly into the selected runtime; they are
not installed into a generation. When a generation is selected, its own command
converges native triggers and still-started watchers; work already running may
finish on the immutable version where it began. Use `agents-live generations list` to inspect the store,
`generations activate VERSION` to roll back, `generations remove VERSION` to
discard an inactive candidate, and `generations collect` to retain the active
version plus one rollback while removing older unheld versions.

The package also installs `al` as an exact shorthand for `agents-live`, so
`al status` and `agents-live status` are interchangeable. uv refuses the
installation if an unrelated `al` executable already exists; remove or rename
that executable before retrying rather than using `--force`.

Python 3.12 or newer is required. POSIX schedules use the user crontab and
watchers use `inotifywait`. Native Windows schedules use Task Scheduler and
watchers use directory change notifications.

Automatic maintenance rotates framework logs and removes retained transcripts
and processor output after 30 days by default. Set `retention_days` to a
positive integer in `.agents-live.toml` (or `[tool.agents-live]`) to change the
repository policy. Rotated records remain available through `agents-live logs`
until their retention boundary.

On native Windows, install a provider CLI through WinGet, then run the verified
PowerShell bootstrap. Use the installed tool's absolute path until future
shells receive its PATH update:

```powershell
winget install Anthropic.ClaudeCode
# Or: winget install GitHub.Copilot
$installer = Join-Path $env:TEMP "agents-live-install.ps1"
Invoke-WebRequest `
  "https://github.com/johnshew/agents-live/releases/latest/download/install.ps1" `
  -OutFile $installer
& $installer
$agentsLive = Join-Path $env:LOCALAPPDATA "agents-live\current\Scripts\agents-live.exe"
& $agentsLive --repo C:\path\to\repository init
```

`init` prints the generated PowerShell completion path and the exact line to
add to `$PROFILE`; Agents Live does not edit shell profiles itself.

The installed `.claude/skills/agents-live/` payload is tool-managed and carries
a directory-local `.gitignore`; project-authored sibling skills are unaffected.
For an existing repository that already tracks the payload, run
`git rm -r --cached .claude/skills/agents-live`, then
`git add -f .claude/skills/agents-live/.gitignore`. Agents Live never changes the
Git index itself.

For details, see the [command reference](commands.md) and [diagnostics
guide](diagnostics.md).
