# agents-live

**Take your agents live.** Add cron and file-watch automation to existing or
new Claude Code and GitHub Copilot agents.

An agent stays one Markdown file. A few frontmatter fields say when it runs
and how it may act. No application server, database, or agent registry is
required.

### `note-filing-agent.md`

```yaml
---
runtime: claude            # unattended execution adapter
mode: plan                 # read-only; a handler script does the writing
watchPath: notes/inbox/    # or schedule: "0 8 * * *", or both
post-processor: file-notes.sh
---
Read ~/AGENTS.md for vault context and filing conventions. Then, for each
new file in notes/inbox/: add frontmatter and tags, fix the title, decide
where it belongs in the vault, and emit JSON:
{ "moves": [{ "from": "...", "to": "...", "content": "..." }] }
```

## Quick start

See [Prerequisites](#prerequisites) for required host tools and installation
details.

```bash
uv tool install agents-live
agents-live init
agents-live run ./note-filing-agent.md
```

Run the agent directly by path. Once the foreground run looks right, activate
its cron or file-watch triggers:

```bash
agents-live start ./note-filing-agent.md
```

Inspect and stop it with the same path:

```bash
agents-live status
agents-live logs
agents-live stop ./note-filing-agent.md
```

Drop a note into `notes/inbox/` and the watcher runs the agent. The agent
decides what should happen; the deterministic post-processor you own changes
the files.

Use `schedule` instead of `watchPath` for cron, or declare both:

```yaml
schedule: "0 8 * * *"
```

## Lightweight

There is no listener service, separate application runtime, or database to
deploy and maintain. The core stack is the Claude Code or GitHub Copilot CLI
you already use, `uv`, `crontab` for scheduling and maintenance, and
`inotifywait` for file watches.

Cron-only agents have no persistent process. A file-watch agent uses one small
local watcher. There are no externally reachable ports or databases. Custom
handlers and plugins may bring their own dependencies; Agents Live core does
not require them.

Run `agents-live doctor` to see which core host tools are missing.

## Safe by default

Execution modes make write access explicit:

1. `plan` is read-only. The agent emits JSON for a validated handler to apply.
2. `pipeline` limits the agent to a schema-checked data channel shared with
   your pre-processors and post-processors.
3. `write` grants full write access as an explicit per-agent choice.

This is tool policy, not a sandbox. Agents still inherit the permissions of
your local account and agent CLI.

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
Windows support is partial and macOS is untested. `agents-live doctor` reports
what the current host needs.

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
