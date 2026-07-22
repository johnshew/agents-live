# agents-live

**Take your agents live.** agents-live adds safe, local automation to
the Claude Code and GitHub Copilot agents you already use - cron and
file-watch dispatch, deterministic pre/post processing, safety
wrappers, and operations. It does not replace your agents: an agent
stays one Markdown file with the same prompt. A few frontmatter fields
say *when* it runs (a cron schedule, a watched directory, or both) and
*how* (the agent CLI, execution mode, optional pre/post scripts):

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

# agents-live

**Take your agents live.** Add cron and file-watch automation to the Claude
Code and GitHub Copilot agents you already use.

An agent stays one Markdown file. A few frontmatter fields say when it runs
and how it may act. No application server, database, or agent registry is
required.

## Quick start

```bash
uv tool install agents-live
agents-live init
agents-live run ./my-agent.md
```

The path is authoritative. It resolves from the current directory and does
not require an initialized repository or registered agent. After the
foreground run looks right, activate its cron or file-watch triggers:

```bash
agents-live start ./my-agent.md
```

Inspect and stop it with the same path:

```bash
agents-live status
agents-live logs
agents-live stop ./my-agent.md
```

## One file

```yaml
---
runtime: claude
mode: plan
watchPath: notes/inbox/
post-processor: file-notes.sh
---
For each new file in notes/inbox/: add frontmatter and tags, fix the
title, decide where it belongs, and emit JSON describing the changes.
```

Drop a note into `notes/inbox/` and the watcher runs the agent. The agent
decides what should happen; the deterministic post-processor you own changes
the files.

Use `schedule` instead of `watchPath` for cron, or declare both:

```yaml
schedule: "0 8 * * *"
```

## Safe by default

Execution modes make write access explicit:

1. `plan` is read-only. The agent emits JSON for a validated handler to apply.
2. `pipeline` limits the agent to a schema-checked data channel shared with
   your pre-processors and post-processors.
3. `write` grants full write access as an explicit per-agent choice.

This is tool policy, not a sandbox. Agents still inherit the permissions of
your local account and agent CLI.

## Lightweight

Cron and `inotifywait` do the triggering. agents-live adds activation,
debounce, concurrency control, structured logs, cost reporting, and automatic
trigger repair.

Cron-only agents have no persistent process. A file-watch agent uses one small
local watcher. There are no externally reachable ports or databases.

## Go further

Repositories are optional. Initialize one later with `agents-live init --repo`
when you need shared configuration or name-based commands.

See the [command reference](src/agents_live/skill/docs/commands.md) for
repository workflows, health checks and repair, upgrades, dashboards, shell
completion, plugins, ownership, and multi-repository operations. The
[architecture guide](src/agents_live/skill/docs/approach.md) covers runtime,
safety, persistence, and maintenance behavior.

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- `crontab` for scheduled agents
- `inotifywait` for file-watch agents
- `claude` or `copilot` for agent-backed definitions

Linux is the primary platform, with Ubuntu on WSL as the reference setup.
Windows support is partial and macOS is untested. `agents-live doctor` reports
what the current host needs.

## Documentation

The optional `/agents-live` skill is installed by `agents-live init`, but every
workflow remains an ordinary CLI command.

- [Overview](src/agents_live/skill/docs/overview.md)
- [Starter templates](src/agents_live/skill/templates/)
- [Skill reference](src/agents_live/skill/SKILL.md)
