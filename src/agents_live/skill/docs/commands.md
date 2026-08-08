---
title: Agents Live commands
description: Command reference for lifecycle, diagnostics, and repository operations
ms.date: 2026-08-08
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
```

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
```

Dry-run previews the same diff without writing started state, triggers, or
processes.

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
- `doctor [--all-repos] [--repair] [--dry-run]` reports runtime health.
  `--repair` invokes the same convergence path as lifecycle changes.
- `logs` and `logs timeline` query local event records.
- `smoketest` exercises an end-to-end provider path.
- `init [--repo PATH]` initializes or registers a workspace.
- `upgrade` upgrades the tool or its installed skill payload.
- `migrate [PATHS] [--dry-run]` is the one-shot 5.x flat-definition converter.
- `uninstall` removes host integration.
- `repos list|add|default|remove` manages the repository registry.
- `completions bash|zsh|powershell|--update` emits shell completions.
- `dashboard` opens the local operational UI.

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
run          ::= "run" ( NAME | "--name" NAME ) [ "--changed-files" VALUE ] [ "--scheduled" ] [ "--boot" ] [ "--quiet" ]
start        ::= "start" ( NAME | "--name" NAME | "--all" ) [ ( "--dry-run" | "-n" ) ]
stop         ::= "stop" ( NAME | "--name" NAME ) [ ( "--dry-run" | "-n" ) ]
status       ::= "status" [ NAME ] [ "--all-repos" ]
logs         ::= "logs" ( logs_query | "timeline" timeline_args )
logs_query   ::= [ NAME ] [ "--log" VALUE ] [ "--all" ] [ "--agent" VALUE ] [ "--since" VALUE ] [ "--until" VALUE ] [ "--phase" VALUE ] [ "--status" VALUE ] [ "--trigger" VALUE ] [ "--slow" VALUE ] [ "--errors" ] [ ( "-n" | "--limit" | "--tail" ) VALUE ] [ "--columns" VALUE ] [ "--order-by" VALUE ] [ "--desc" ] [ "--asc" ] [ "--sql" VALUE ] [ "--format" ( "table" | "jsonl" | "csv" ) ] [ "--check-schema" ]
timeline_args ::= [ FILTER ] [ "--all" ] [ "--since" VALUE ] [ "--last" VALUE ] [ "--logs" VALUE ]
smoketest    ::= "smoketest" [ "--runtime" VALUE ] [ "--model" VALUE ]
doctor       ::= "doctor" [ "--all-repos" ] [ "--repair" ] [ "--dry-run" ]
init         ::= "init" [ "--repo" VALUE ]
upgrade      ::= "upgrade" [ "--runtime-only" ] [ "--skills-only" ] [ "--from" VALUE ]
migrate      ::= "migrate" [ PATHS ] [ "--dry-run" ]
uninstall    ::= "uninstall" [ "--distro" VALUE ] [ "--retain-state" ]
repos        ::= "repos" ( "list" | "add" PATH | "default" REPO | "remove" REPO )
completions  ::= "completions" ( "bash" | "zsh" | "powershell" | "--update" )
dashboard    ::= "dashboard" ( dashboard_query | "list" | "stop" stop_args )
dashboard_query ::= [ "--native" ] [ "--open" ] [ "--dev" ] [ "--port" VALUE ] [ "--all-repos" ]
stop_args ::= [ "--port" VALUE ] [ "--all" ]
```

## CLI command and flag table

| command | dispatch | root | probes | JSON | all repos | name sugar | flags | summary |
|---|---|---|---|---|---|---|---|---|
| run | in-process | required |  | yes |  | yes | --name, --changed-files, --scheduled, --boot, --quiet | Execute an agent once. |
| start | in-process | required | schedule, watch | yes |  | yes | --name, --all, --dry-run, -n | Start automatic runs for an agent. |
| stop | in-process | required | schedule | yes |  | yes | --name, --dry-run, -n | Stop automatic runs and keep the definition. |
| status | in-process | registry |  | yes | yes |  | --all-repos | List agents and runtime state. |
| logs | subprocess | registry |  | yes |  |  | --log, --all, --agent, --since, --until, --phase, --status, --trigger, --slow, --errors, -n, --limit, --tail, --columns, --order-by, --desc, --asc, --sql, --format, --check-schema | Query logs and correlated event timelines. |
| logs timeline | subprocess | registry |  | yes |  |  | --all, --since, --last, --logs | Show a correlated event timeline. |
| smoketest | in-process | required | schedule, watch | yes |  |  | --runtime, --model | Run end-to-end validation. |
| doctor | in-process | markerless |  | yes | yes |  | --all-repos, --repair, --dry-run | Check environment and installation readiness. |
| init | in-process | none |  | yes |  |  | --repo | Initialize the global or repository workspace. |
| upgrade | in-process | none |  | yes |  |  | --runtime-only, --skills-only, --from | Upgrade runtime and project skill payloads. |
| migrate | in-process | required |  |  |  |  | --dry-run | Convert 5.x flat definitions. |
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
