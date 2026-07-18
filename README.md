# agents-live

**Take your agents live.** agents-live adds safe, local automation to
the Claude Code and GitHub Copilot agents you already use — cron and
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

Drop a raw note into `notes/inbox/` and the watcher fires within
seconds; one agent run later the note is cleaned up and filed. **The
agent thinks, a script acts**: the agent decides, but the only thing
that touches your files is a deterministic handler you own.

## Safe by default

Write access is a ladder with an explicit final rung:

1. **plan** (the default) — the agent runs read-only and emits JSON; a
   runner validates it (JSON Schema, size caps, path roots, provenance)
   and hands it to your deterministic handler.
2. **pipeline** — the agent's tool surface narrows to a schema-checked
   `put`/`get` side-channel that your pre/post-processors mediate, over
   a token-protected loopback endpoint that exists only for the
   duration of one run.
3. **write** — full write access: the last option, an explicit
   per-agent opt-in.

This ladder is tool policy, not a sandbox — agents inherit your local
account's privileges unless you configure stricter CLI or OS isolation.

## Lightweight

No framework to learn, no APIs to call, no daemon to maintain — just
things you use every day: git, files, markdown, cron, and scripts.
Cron and inotifywait do the triggering; agents-live adds activation,
debounce, concurrency, structured logs, and per-run token cost. If you
can read a crontab, you can audit the whole system. No application
daemons or gateways, no externally reachable inbound ports, no
databases (plain-text JSONL logs, aged into monthly Parquet archives).
The only persistent processes are one small watcher loop per
file-watch agent; cron-only agents need none.

## Install

```bash
uv tool install agents-live   # or: uv tool install <path-to-wheel>
```

On interactive terminal invocations, agents-live checks PyPI for a newer
stable release when its shared cached result is missing or 24 hours old. The
result is stored under
`$XDG_CACHE_HOME/agents-live/` (normally `~/.cache/agents-live/`). The refresh
runs in the background; network and cache failures never affect the command.
No check runs for scheduled/internal, quiet, JSON, piped, or redirected
commands. This request sends only ordinary package-index request metadata; it
does not include project or agent data. View the cached result with `agents-live
doctor`, and install an available release with `uv tool upgrade agents-live`.
Agents Live never updates itself.

After upgrading, run `agents-live --repo <project> init` to refresh the
optional installed skill payload; `doctor` reports a package and payload
version mismatch.

## Quick start

```bash
cd your-repo
agents-live doctor    # verify cron, inotifywait, and agent CLIs
agents-live init      # layout + config marker + skill payload
# copy a starter from .claude/skills/agents-live/templates/ into Agents/
agents-live run my-agent        # test once, in the foreground
agents-live start my-agent      # activate cron/watcher triggers
agents-live status
agents-live logs
agents-live teardown my-agent   # deactivate — remove its triggers
```

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- `crontab` (scheduled agents), `inotifywait` (file watchers; preflight
  is trigger-derived, so cron-only setups don't need inotify)
- An agent CLI for agent-backed definitions: `claude` or `copilot`

Linux-first: Ubuntu on WSL is the reference platform, Windows support
is partial, and macOS is untested. `agents-live doctor` reports
exactly what this host is missing.

## Documentation

The `/agents-live` skill is optional conversational support for the
CLI: `agents-live init` installs it for Claude Code, every flow it
drives is an ordinary `agents-live` command, and the CLI is fully
usable without it.

- [Overview](src/agents_live/skill/docs/overview.md)
- [Architecture](src/agents_live/skill/docs/approach.md)
- [Commands](src/agents_live/skill/docs/commands.md)
- [Starter templates](src/agents_live/skill/templates/)
- [SKILL.md](src/agents_live/skill/SKILL.md) — full reference
