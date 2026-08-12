---
title: No Python API for Processors Decision
description: Why agents-live exposes a CLI and environment contract instead of an importable Python surface, and what handlers should use instead
ms.date: 2026-08-11
ms.topic: concept
---

# No Python API for processors

## Status

Accepted for 6.0. Emission side deferred to
[#105](https://github.com/johnshew/agents-live/issues/105).

## Context

Pre- and post-processors run as child processes of `dispatch`. Because a
processor written in Python can import the installed package, several
handlers did exactly that: `agents_live.paths` for locations and
`agents_live.legacy.headless` for `load_agent_config`, `list_agents`,
`list_active_agent_names`, `list_spawned_definitions`, and `EventLog`.

Nothing in the package declared those imports supported. `agents_live/__init__.py`
exports only `__version__`, so every internal move silently broke consumer
code. During the 6.0 seam refactor
([#256](https://github.com/johnshew/agents-live/issues/256)) that happened
twice in one session: once when definition bundling changed, and again when
`headless` moved under `legacy/`. `legacy/` is removed in 7.0, so those
imports break again on that release.

The question raised was whether to answer this with a supported
`agents_live.api` facade over functions the suite already covers.

## Decision

**No public Python API.** The supported surfaces for consumer code are:

- the **JSON CLI** for introspection: `agents-live status --json`,
  `agents-live logs --json`, `agents-live doctor --json`,
  `agents-live run --json`; and
- the **child process contract** for execution: environment variables in,
  stdin in, stdout/stderr/exit code out, invocation cwd at the repository
  root.

Both are already documented, tested, and language-agnostic. A Python facade
would be a second way to do the same thing, a second surface to keep stable,
and a promise to freeze module paths indefinitely.

The contract is expected to grow; the importable surface is not.

| Surface | Grows |
|---|---|
| Correlation context passed in the child environment | yes |
| A documented record schema a child may emit | yes |
| Python module paths a processor imports | no |

### Why the contract, not an SDK

**Processors are not only Python.** `agent/port.py` dispatches `.py` through
`uv run`, `.js` and `.ts` through `node`, `.ps1` through `pwsh`, `.sh` and
anything else directly. A Python SDK serves one of five languages; an
environment variable naming an append-only file serves all of them in three
lines of any language.

**Progressive emission wants a file, not a call.** Detailed logging as work
progresses means appending and flushing as you go. Durability comes from the
file either way, and a file lets a future `logs` command tail a run in flight
without changing the writer.

**Spans need context passed in, not functions exposed.** A span needs a
parent id, a run id, and a clock. Those arrive as environment values; the
child emits records. Nothing needs importing.

**The schema is already the interface one level up.** #105 justifies its local
event schema by noting that a future sidecar can translate it to OpenTelemetry
without coupling writers to a vendor. The same reasoning applies to processors.

**Prior art agrees.** GitHub Actions began with stdout control commands
(`::set-output name=x::value`) and migrated to environment file handles
(`GITHUB_OUTPUT`, `GITHUB_ENV`, `GITHUB_STEP_SUMMARY`). The scar tissue is the
argument: the stdout era required a randomly generated `stop-commands` token
because content flowing through stdout could impersonate control commands.
That risk is sharper here, because a post-processor's stdin is literal model
output. `@actions/core` is a convenience over the file contract, not the
contract itself.

**Stdout is already load-bearing here.** PRE stdout is parsed for a `{"skip":
true}` object, and POST stdout becomes the run's outcome text. A diagnostic
line written there corrupts a control channel, so multiplexing observability
onto stdout is not available.

### The emission gap this leaves

5.x handlers wrote structured entries with `headless.EventLog`. 6.0 gave
nothing back, so processors can record only through exit code, stderr, and
stdout. `c8968ba` narrowed that by recording each step's diagnostics and the
run's output text on the run event, bounded by `_RECORDED_MAX_CHARS`, so a
scheduled run's output no longer vanishes into `--quiet`.

A structured channel is deliberately **not** in 6.0, because it would commit
to a record schema before #105 settles field names. Shipping a schema in a
major release and changing it afterwards is worse than shipping none. The
intended shape is an environment-provided append-only JSONL handle with
correlation context, per-step isolation, size bounds, and redaction rules,
designed once under #105.

`Event.attributes` is the tempting field and the wrong one: `obs/query.normalize`
drops it, so anything written there is stored and invisible.

## What to use instead

If a handler imports `agents_live`, replace the import with a CLI call.

`agents-live status --json` returns `{"ok": ..., "agents": [row, ...]}`;
`agents-live logs --json` returns `{"ok": ..., "operation": "logs",
"records": [record, ...]}`.

| 5.x import | Replacement |
|---|---|
| `headless.list_agents()` | `agents-live status --json` - one row per definition |
| `headless.list_active_agent_names()` | `agents-live status --json`, `state` field (`started`, `stopped`, `unloadable`) |
| `headless.list_spawned_definitions()` | `agents-live status --json`, derived from `execution.schedules` and `execution.watch` |
| `headless.load_agent_config(name)` | `agents-live status --json`, `execution` object: `selector`, `provider`, `model`, `mode`, `schedules`, `watch`, `mcps`, `pre_processor`, `post_processor` |
| `headless.EventLog` (reading) | `agents-live logs --json` - `ts`, `agent_name`, `phase`, `status`, `message`, `trigger`, `duration_s` |
| `headless.EventLog` (writing) | no replacement in 6.0; write to stdout or stderr and let dispatch record it. Structured emission is #105 |
| `headless.AgentsLiveError` | `agents-live status --json`, `loadable` plus `error` per row; or a non-zero CLI exit status |
| hand-rolled spawn | `agents-live run --name <identifier> --json` - it both spawns and records |
| health beacon inspection | `agents-live doctor --json` |
| `paths.repo_state_dir`, `paths.host_logs_dir`, `paths.health_beacon_path` | still present and unmoved, but not a promised surface. Prefer the CLI; log rotation, the main reason to want a log directory, becomes built-in under [#259](https://github.com/johnshew/agents-live/issues/259) |

Notes on the two that are not mechanical substitutions:

- `list_active_agent_names()` meant "scheduled or watched on this host" - a
  single notion 6.0 deliberately split into started intent and installed
  artifacts. A dashboard usually wants both, shown separately. That is a
  porting decision, not a rename.
- `agent_spawn`-style handlers reimplemented dispatch. Calling
  `agents-live run` is simpler than what they replace, and it records the run.

### The contract a processor can rely on

- **Environment in:** `AGENTS_LIVE_AGENT_NAME`; `AGENTS_LIVE_CHANGED_FILES`
  as a JSON array when the firing carried changed files; plus any `env`
  declared in the definition and any MCP resource values.
- **Working directory:** the repository root.
- **Stdin:** the upstream step's text for a post-processor in non-pipeline
  mode.
- **Stdout:** for PRE, an optional JSON object whose `skip` field short-circuits
  the run; for POST, the run's outcome text.
- **Stderr:** diagnostics, recorded on the step result.
- **Exit code:** non-zero fails the step with `pre_processor_crash` or
  `post_processor_crash`.

## Alternatives rejected

- **`agents_live.api` facade.** Every proposed member except definition
  policy and locations was already answered by `status --json`, `logs --json`,
  `doctor --json`, or `run`. The two real gaps were fields missing from
  existing JSON output, not missing functions; execution policy was added to
  the status row instead.
- **Blessing `legacy.headless` as public.** It is removed in 7.0 and must not
  receive new behavior.
- **Stdout control commands for observability.** Disqualified by the
  injection risk from model-authored text and by stdout already carrying
  control meaning.
- **A structured emission channel in 6.0.** Rejected on timing: it would
  freeze a schema that #105 has not settled.

## Consequences

- Consumer code is decoupled from package layout, so internal moves stop
  breaking handlers.
- 6.0 ships with less emission capability for handlers than 5.x offered. This
  is a stated regression, not an oversight, and the changelog and migration
  guidance say so.
- Introspection features must be added to the JSON CLI rather than to a
  library, which keeps one tested surface.
- A future helper library remains possible, but only as emission-only
  convenience over a schema that stays independently usable, so a processor
  writing raw JSONL never depends on it.
