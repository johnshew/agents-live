---
title: Agents Live commands
description: Command reference for lifecycle, diagnostics, and repository operations
ms.date: 2026-08-20
ms.topic: reference
---

# Agents Live commands

The lifecycle vocabulary is `run`, `start`, and `stop`. Operational commands
inspect, diagnose, migrate, or manage repositories without introducing another
word for lifecycle state.

```bash
agents-live run <name>
agents-live start <name>
agents-live stop <name>
```

`--repo PATH_OR_ALIAS` pins a registered repository. `--json` requests stable
machine-readable output where a command supports it.

## Lifecycle

### `run`

Runs a definition once through the fixed pre-processor, provider, and
post-processor pipeline. A manual run does not change started state.

```bash
agents-live run link-check
agents-live run link-check --changed-files '["docs/index.md"]'
agents-live run link-check -p "Focus on the authentication pages"
agents-live run link-check -o dry-run -o account=team-inbox
```

`-p/--prompt` and `--prompt-file` add instructions for this run only, without
editing the definition. `--prompt-file -` reads them from stdin, and the two
sources are mutually exclusive.

Quoting differs by shell, and getting it wrong is the usual first problem:

```bash
agents-live run link-check -p "Focus on the auth pages; ignore drafts"
agents-live run link-check -p 'Leave $HOME and `backticks` literal'
```

```powershell
agents-live run link-check -p "Focus on the auth pages; ignore drafts"
agents-live run link-check -p 'Leave $HOME and `backticks` literal'
```

In PowerShell a double-quoted string expands `$name` and treats a backtick as
an escape, so single quotes are the safe default for anything containing them.
In Bash the equivalent trap is `$` and backticks inside double quotes. Either
way, `--prompt-file` avoids the question entirely and is the better choice for
more than a sentence.

`-o/--option` passes values to the processors. The presence of `=` is the whole
grammar: `-o dry-run` is a flag and `-o account=team-inbox` carries a value.
Both reach every processor as `AGENTS_LIVE_OPTIONS`, described in
[processors.md](processors.md). Neither instructions nor options are recorded
into an installed trigger, so an ad hoc run cannot change what a schedule does.

`run --json` returns the outcome text, structured value, transcript, usage,
and run ID. A pipeline definition may declare one canonical MCP result with
`agents-live.result-path`. When declared, `structured` is that path's value and
`result_status` is `published` or `not_published`; absence remains nonfatal and
does not change the run status or exit code. Definitions without a result path
retain the existing envelope and do not emit `result_status`.

Installed schedule and watcher artifacts use hidden origin and artifact-marker
flags. They are runtime contracts, not author-facing options.

### `start`

Records one or every valid definition as started on this machine, collects the
complete desired subscription set across registered repositories, and invokes
the one convergence operation.

```bash
agents-live start link-check
agents-live start --all
agents-live start link-check --dry-run
agents-live start link-check --transfer-here
agents-live start link-check --transfer-to hostname/runtime/uuid
```

Dry-run previews the same diff without writing started state, triggers, or
processes.

`--transfer-here` claims an agent for this runtime and starts it here.
`--transfer-to` assigns it to another runtime, named by the full identity
`status --json` reports, and withdraws its triggers from this host. Both
need an ownership registry backend, and the first transfer in a project
declares registry ownership. Use `--transfer-here` to recover an agent
whose owner value cannot be matched to any runtime.

### `stop`

Records a definition as stopped and converges the complete desired set. The
definition remains in the repository.

```bash
agents-live stop link-check
agents-live stop link-check --dry-run
```

## Operations

- `status [name] [--all-repos]` reports definitions and their started or
  stopped state.
- `doctor [--all-repos] [--repair] [--dry-run]` reports runtime health. A
  normal invocation checks and repairs only the selected repository; pass
  `--all-repos` to inspect every registered repository. `--repair` invokes the
  same convergence path as lifecycle changes within that scope.
- `doctor --quick` is the fast agent-facing automatic-maintenance check. It
  always emits JSON. A cached host health record younger than 70 minutes
  returns immediately, including an actionable category and remedy when the
  cached record is degraded. A missing, invalid, or stale record runs automatic
  maintenance once and checks again. The command exits nonzero unless a current
  healthy record exists after that attempt. It checks only the runtime where it
  runs.
- `logs` and `logs timeline` query local event records.
- `smoketest` exercises an end-to-end provider path.
- `init [--repo PATH]` initializes or registers a workspace.
- `upgrade` upgrades the tool or its installed skill payload. An installed
  native Windows tool queues runtime replacement until the invoking process
  exits. Installed-tool watchers keep started intent unchanged, finish any
  active dispatch, quiesce at their next idle check, and are restored by
  ordinary convergence from the new runtime. A managed dashboard remains a
  fail-closed blocker and must be stopped first. The command prints an
  operation ID; use `agents-live logs admin` on the next invocation to inspect
  the correlated quiesce, replacement, restoration, and terminal events.
- `migrate [PATHS] [--dry-run] [--bundle]` is the one-shot 5.x converter. It
  rewrites `Agents/<name>.md` frontmatter in place, leaving processors where
  they are; `--bundle` converts to `<name>/SKILL.md` and copies them instead.
  A scan covers `Agents/` and every configured `agent_directories` root.
- `uninstall` removes host integration and the uv-managed tool. If a watcher
  from that installation does not stop within the grace period, the command
  exits nonzero before host cleanup and names the processes to stop. On native
  Windows, a successful command queues final tool removal until its own
  processes exit; `uv tool list` can show the tool briefly afterward.
- `repos list|add|default|remove` manages the repository registry.
- `completions bash|zsh|powershell|--update` emits shell completions.
- `dashboard` opens the local operational UI and prints its clickable loopback
  URL before serving. The default remains port 8231 and reports a conflict
  rather than silently moving. Pass `--port next` to select the first available
  port at or above 8231. `dashboard list` includes each recorded dashboard's
  URL; an explicit numeric `--port` keeps selecting that exact port.

`init` and skill upgrades install a directory-local `.gitignore` under
`.claude/skills/agents-live/`. The managed payload stays out of repository
status while sibling skills remain visible. The tool never changes the Git
index. If an existing repository already tracks the payload, untrack it once:

```bash
git rm -r --cached .claude/skills/agents-live
git add -f .claude/skills/agents-live/.gitignore
```

The former public `heartbeat` command is not part of 6.0. On WSL, liveness is
runtime-owned: convergence registers a staged task under a distinct name,
waits for a fresh beacon, swaps it into place, then removes the prior task.

## Definition creation

Create `Agents/<name>/SKILL.md` and put execution policy under quoted
`agents-live.*` metadata. Copy one of the shipped starter directories when
useful. See [Definition format](definition-format.md).

<!-- BEGIN GENERATED CLI -->
## CLI grammar

The public command surface is generated from the declarative command
spec. `VALUE`, `NAME`, `PATH`, and `ALIAS` are terminal values.

```ebnf
invocation   ::= "agents-live" pre_command* ( command post_command* | help_word )
help_word    ::= "-h" | "--help" | "help" [ COMMAND | "--all" ] | "--version" | ""
pre_command  ::= "--json" | "--repo" ( PATH | ALIAS )
post_command ::= "--json" | "-h" | "--help" | "help"
command      ::= run | start | stop | status | logs | smoketest | doctor | init | upgrade | migrate | uninstall | repos | completions | dashboard
run          ::= "run" ( NAME | "--name" NAME ) [ "--changed-files" VALUE ] [ ( "-p" | "--prompt" ) VALUE ] [ "--prompt-file" VALUE ] [ ( "-o" | "--option" ) VALUE ] [ "--quiet" ]
start        ::= "start" ( NAME | "--name" NAME | "--all" ) [ ( "--dry-run" | "-n" ) ] [ "--transfer-here" ] [ "--transfer-to" VALUE ]
stop         ::= "stop" ( NAME | "--name" NAME ) [ ( "--dry-run" | "-n" ) ]
status       ::= "status" [ NAME ] [ "--all-repos" ]
logs         ::= "logs" ( logs_query | "timeline" timeline_args )
logs_query   ::= [ NAME ] [ "--log" VALUE ] [ "--all" ] [ "--agent" VALUE ] [ "--since" VALUE ] [ "--until" VALUE ] [ "--phase" VALUE ] [ "--status" VALUE ] [ "--trigger" VALUE ] [ "--slow" VALUE ] [ "--errors" ] [ ( "-n" | "--limit" | "--tail" ) VALUE ] [ "--columns" VALUE ] [ "--order-by" VALUE ] [ "--desc" ] [ "--asc" ] [ "--sql" VALUE ] [ "--format" ( "table" | "jsonl" | "csv" ) ] [ "--check-schema" ]
timeline_args ::= [ FILTER ] [ "--all" ] [ "--since" VALUE ] [ "--last" VALUE ] [ "--logs" VALUE ]
smoketest    ::= "smoketest" [ "--runtime" VALUE ] [ "--model" VALUE ]
doctor       ::= "doctor" [ "--all-repos" ] [ "--repair" ] [ "--dry-run" ] [ "--quick" ]
init         ::= "init" [ "--repo" VALUE ]
upgrade      ::= "upgrade" [ "--from" VALUE ]
migrate      ::= "migrate" [ PATHS ] [ "--dry-run" ] [ "--bundle" ]
uninstall    ::= "uninstall" [ "--distro" VALUE ] [ "--retain-state" ]
repos        ::= "repos" ( "list" | "add" PATH | "default" REPO | "remove" REPO )
completions  ::= "completions" ( "bash" | "zsh" | "powershell" | "--update" )
dashboard    ::= "dashboard" ( dashboard_query | "list" | "stop" stop_args )
dashboard_query ::= [ PROJECT ] [ "--native" ] [ "--open" ] [ "--dev" ] [ "--port" VALUE ] [ "--all-repos" ]
stop_args ::= [ "--port" VALUE ] [ "--all" ]
```

## CLI command and flag table

| command | dispatch | root | probes | JSON | all repos | name sugar | flags | summary |
|---|---|---|---|---|---|---|---|---|
| run | in-process | required |  | yes |  | yes | --name, --changed-files, -p, --prompt, --prompt-file, -o, --option, --quiet | Execute an agent once. |
| start | in-process | required | schedule, watch | yes |  | yes | --name, --all, --dry-run, -n, --transfer-here, --transfer-to | Start automatic runs for an agent. |
| stop | in-process | required | schedule | yes |  | yes | --name, --dry-run, -n | Stop automatic runs and keep the definition. |
| status | in-process | registry |  | yes | yes |  | --all-repos | List agents and whether each is started. |
| logs | subprocess | registry |  | yes |  |  | --log, --all, --agent, --since, --until, --phase, --status, --trigger, --slow, --errors, -n, --limit, --tail, --columns, --order-by, --desc, --asc, --sql, --format, --check-schema | Query logs and correlated event timelines. |
| logs timeline | subprocess | registry |  | yes |  |  | --all, --since, --last, --logs | Show a correlated event timeline. |
| smoketest | in-process | required | schedule, watch | yes |  |  | --runtime, --model | Run end-to-end validation. |
| doctor | in-process | markerless |  | yes | yes |  | --all-repos, --repair, --dry-run, --quick | Check environment and installation readiness. |
| init | in-process | none |  | yes |  |  | --repo | Initialize the global or repository workspace. |
| upgrade | in-process | none |  | yes |  |  | --from | Upgrade runtime and project skill payloads. |
| migrate | in-process | required |  |  |  |  | --dry-run, --bundle | Convert 5.x flat definitions. |
| uninstall | in-process | none |  |  |  |  | --distro, --retain-state | Remove host integrations and the uv tool. |
| repos | in-process | none |  | yes |  |  |  | Manage registered repositories. |
| repos list | in-process | none |  |  |  |  |  | List registered repositories. |
| repos add | in-process | none |  |  |  |  |  | Register a repository. |
| repos default | in-process | none |  |  |  |  |  | Set the fallback repository. |
| repos remove | in-process | none |  |  |  |  |  | Remove a registered repository. |
| completions | in-process | none |  |  |  |  | --update | Generate shell completion scripts. |
| dashboard | subprocess | registry |  |  | yes |  | --native, --open, --dev, --port, --all-repos | Open the interactive control panel. |
| dashboard list | subprocess | none |  |  |  |  |  | List dashboards this host is running. |
| dashboard stop | subprocess | none |  |  |  |  | --port, --all | Stop a dashboard this host is running. |
<!-- END GENERATED CLI -->
