---
title: Agents Live Log Diagnostics
description: Log inventory and correlated diagnostic procedures for Agents Live agents
ms.date: 2026-07-19
ms.topic: troubleshooting
---

## Log diagnostics

Diagnose issues in the agents-live pipeline (watchers -> pre-processor
-> agent -> post-processor) by correlating events across log files. Most
issues are races between a long-running agent and a fast watcher/sync
agent writing the same files.

## Log inventory

All logs are **UTC JSONL** and live in the user-level XDG state home
(`$XDG_STATE_HOME/agents-live/`, default `~/.local/state/agents-live/`),
never in the project tree. Each repository gets its own state directory
`repos/<basename>-<hash>/`; the paths below are relative to that
directory unless marked host-level. There is one per-agent log for each
discovered agent definition. Agents Live discovers standard definitions
in `.claude/agents/` and `.github/agents/`; logs remain centralized at
`logs/<name>.log` in the repo's state directory. `agents-live logs`
resolves these locations for you; `--all` unions this repo's logs with
the host-level logs.

### Infrastructure logs

| Log | Type | What it tells you |
|-----|------|-------------------|
| `logs/agents-live.log` | runtime | **Every** agent's lifecycle: `watcher` debounce batches, `activate`, `start`, `done` with `status`, `duration_s`, `trigger`, `changed_files`. The join point for all diagnostics. |
| `~/.local/state/agents-live/logs/health-check.log` | host-level | The built-in check-and-repair loop: per-repo sweep results, smoketest gating, beacon writes. |

### Per-agent logs (domain work)

Each of your agents writes `logs/<name>.log`. Keep a short
catalog of what each one means in your own repo -- during an incident,
"which log is which" should not require reading agent definitions.


### Ancillary sources (not JSONL)

| Source | Use |
|--------|-----|
| `logs/runs/<agent>-<ts>.stdout.txt` | Full raw stdout from every agent run. |
| `logs/runs/<agent>-<ts>.stderr.txt` | Full raw stderr from every agent run. |
| `logs/runs/<agent>-<ts>.transcript.md` | Archived session transcript (copilot-family runtimes). |
| `logs/<agent>-transcript.md` | Live session transcript (overwritten each run). |
| Agent CLI session logs | Full raw transcript kept by the agent CLI itself; location depends on the CLI. |
| `git log --pretty='%h %ai %s' -- <file>` | Committed state history. |
| `crontab -l` | Active cron registrations (ground truth). |
| `ps aux \| grep inotifywait` | Active watcher processes. |

### Agent transcript deep dive

When a pipeline log shows an unexpected agent result, the session
transcript is the definitive source.

**Quick check -- run output files:**

```bash
# Recent runs for a specific agent (in this repo's state directory)
runs_dir=~/.local/state/agents-live/repos/<repo-key>/logs/runs
ls -lt "$runs_dir"/my-agent-* | head -10

# View the stdout of the most recent run
cat "$(ls -t "$runs_dir"/my-agent-*.stdout.txt | head -1)"
```

**Correlating with pipeline logs:**

The `phase: agent` log entry includes `transcript_path` and the agent
CLI's session directory in its `message` field. To go from a log entry
to the full transcript:
1. Find the `phase: agent` entry for your agent and time
2. Extract the session directory from the `message` field
3. Read the CLI's transcript file in that directory

## Diagnostic procedure

1. **Identify the symptom and approximate time.** Convert to UTC.

2. **Get a narrow window from `agents-live.log`:**
   ```bash
  agents-live logs --agent <name> --since <time> --until <time>
   ```
   Look for: `start (trigger=...) -> done (status=ok|error|skipped, duration_s)`.

3. **Find the long-running agent.** `duration_s > 30` is the usual suspect.

4. **Pull the per-agent log** for that window.

5. **Correlate with domain logs.** A pre-processor's own log (e.g. a
   parser log) shows what the pipeline saw at run time.

6. **Cross-check with git.** `git log --pretty='%h %ai %s' -- <file>`.

## Common patterns

- **Stale-read race.** Long agent reads input at t=0, writes output at
  t=200s. User edits during that window. Signature: `duration_s > 60`
  + multiple debounce batches on the same input file.

- **Self-write cascade.** Agent writes back to a watched input file.
  Signature: a second `start` within 1-2s of `done`, often with
  `status: skipped` (pre-processor catches the self-write).

- **Sync loop.** A post-processor propagates state between two watched
  files (e.g. checkbox state between a recommendations file and a
  tracking file). When the agent also writes one of those files, you
  get oscillation.

## Smoketests (what's what)

The word "smoketest" refers to **several distinct things**. Don't
confuse them when debugging (and if your deployment adds its own
smoketest agents, catalog them in your per-agent log inventory).

| Name | What it is | Trigger | Where to look |
|------|------------|---------|---------------|
| `agents-live smoketest` | End-to-end system test. Creates the `_smoketest-*` agents below, exercises cron + watcher + debounce + spawn paths, then tears them down. Manual / CI only. | `agents-live smoketest --runtime <runtime>` | `logs/smoketest-framework-result.json` (verdict), stdout |
| `_smoketest-cron` | Synthetic scheduled agent created by the smoketest. | created + run by the smoketest | `logs/_smoketest-cron.log` |
| `_smoketest-watcher` | Synthetic watcher agent created by the smoketest. **Refuses to run inside the VS Code chat sandbox** (cgroup kills the daemon mid-Claude-call). | created + run by the smoketest | `logs/_smoketest-watcher.log` |
| `_smoketest-spawn-child` / `_smoketest-debounce` / `_smoketest-preprocessor` / `_smoketest-pipeline` | Synthetic agents exercising spawn, debounce dispatch, pre/post processors, and pipeline mode. | created + run by the smoketest | `logs/_smoketest-*.log` |

If the system smoketest fails, check `smoketest-framework-result.json`
first -- it carries `verdict`, `failed_step`, and `reason`.

`BUSY` (exit 75) is not a test failure. It means another system smoketest
owns `smoketest-framework.lock` in the repo's state directory; the lock
file contains its PID,
host, agent, model, and start time. Do not delete the lock file: kernel `flock`
ownership, not file presence, determines whether it is held. After an
uncatchable exit, the lock releases automatically and the next run removes
stale `_smoketest-*` resources before setup.

## Traps to avoid

- `tail -N` / `cat` on 200k-line logs overflows context. Always filter
  first with `agents-live logs` / `agents-live logs timeline`.
- **Table display caps columns at 80 chars.** Use `--format jsonl` for
  full values.
- File-change events don't mean content changed -- mtime can bump on an
  identical atomic-write.
- `agents-live.log` has **multiple agents interleaved**. Filter by
  `--agent` or `"agent_name":"<name>"`.
- **Every log entry has an `agent_name` field.**
- **Schema version.** Entries carry `log_schema: <int>` (current: 5).
  If a query reports a type mismatch across log generations, fix the data
  rather than weakening the query. See [commands.md](commands.md) "Schema
  evolution".
- **`--sql` ignores other filter flags.** Include conditions in your SQL
  `WHERE` clause.
- **Warnings are deterministic telemetry.** 85 warnings in 12h = 6 runs
  x ~14 repeated warnings. Group by `COUNT(DISTINCT line)`.
- **Level vs status.** Some handlers report errors as `level: error`,
  others as `status: error`. The `--errors` filter ORs both.
- **`--errors` auto-enriches output** with `error_category` column and
  a Tracebacks section.

## Query recipes

### logs (qlog)

```bash
# Events for one agent in a window
agents-live logs --agent my-agent \
  --since 2026-04-22T13:00 --until 2026-04-22T13:30

# Correlated view across ALL logs
agents-live logs --all --since 2026-04-22T13:02:41 --until 2026-04-22T13:06:10 \
  --columns ts,_src,agent_name,phase,status,duration_s

# Slow runs (agent duration > 30s)
agents-live logs --slow 30 --since 2026-04-22

# All errors across all logs
agents-live logs --errors --all

# Validate live-plus-archive normalized column types
agents-live logs --all --check-schema

# Custom SQL
agents-live logs --all --sql "SELECT agent_name, COUNT(*) FROM log GROUP BY 1 ORDER BY 2 DESC"
```

Filters: `--agent`, `--since`, `--until`, `--phase`, `--status`, `--trigger`,
`--slow SEC`, `--errors`. Output: `--format table|jsonl|csv`.

### logs timeline

```bash
# Last 50 events across all agents
agents-live logs timeline

# Timeline for a specific agent
agents-live logs timeline my-agent --since 2026-05-01T12:00

# Content substring filter
agents-live logs timeline "invoice" --last 30

# All agents in a window
agents-live logs timeline --all --since 2026-05-01T16:00
```

## Key files for reference

Deployment-specific pipeline entry points (pre-processors, parsers,
post-processors) belong in your per-agent log inventory.
