# agents-live

**Take your agents live.** Cron and file-watch dispatch, deterministic
pre/post processing, safety wrappers, and operations for agents:
**the agent thinks, the script acts**. Agent definitions are markdown
files with YAML frontmatter; a runner executes the agent headless,
validates its output (JSON Schema, size caps, path roots, provenance),
and hands it to deterministic handlers.

## Install

```bash
uv tool install agents-live   # or: uv tool install <path-to-wheel>
```

## Quick start

```bash
cd your-repo
agents-live init      # layout + config marker + skill payload; ends with doctor
# copy a starter from .claude/skills/agents-live/templates/ into Agents/
agents-live run my-agent      # execute once
agents-live start my-agent    # activate cron/watcher triggers
agents-live status
agents-live logs
```

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- `crontab` (scheduled agents), `inotifywait` (file watchers; preflight
  is trigger-derived, so cron-only setups don't need inotify)
- An agent CLI for agent-backed definitions: `claude` or `copilot`

`agents-live doctor` reports exactly what this host is missing.

## Documentation

- [SKILL.md](src/agents_live/skill/SKILL.md) — full reference
- [Architecture](src/agents_live/skill/docs/approach.md)
- [Commands](src/agents_live/skill/docs/commands.md)
- [Starter templates](src/agents_live/skill/templates/)
