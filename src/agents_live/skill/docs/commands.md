---
title: Agents Live commands
description: Command reference for lifecycle, diagnostics, and repository operations
ms.date: 2026-08-30
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
The provider session does not run implicit repository hooks, workspace MCP
servers, or project extensions, and provider project instructions are disabled
even when the provider has previously trusted the checkout. Only servers
explicitly listed in `agents-live.mcps` are added. See
[Definition format](definition-format.md) for provider authentication effects.

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
more than a sentence. It does not bypass a provider transport limit: Copilot
still requires the resolved prompt in `-p`, so an oversized native Windows
prompt fails before spawn with its measured size. Claude receives the resolved
prompt on stdin and does not share that command-line limit.

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

### `context`

Prints the resolved command line, working directory, and environment for one
run step without executing it.

```bash
agents-live context link-check --role pre
agents-live context link-check --role agent --json
agents-live context link-check --changed-files '["docs/index.md"]' \
  -p "Focus on authentication" -o dry-run
```

The command accepts the same invocation instructions, changed files, and
processor options as `run`. It names the step's control, log, and output paths
but does not create them. A post-processor preview cannot know its future stdin,
so `stdin_available` is false. Pipeline contexts are available only while the
run-scoped MCP server exists and are rejected by this read-only preview.

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
require registry ownership to be explicitly enabled first. Use
`agents-live ownership enable` after installing the ownership backend. A
refused transfer in local mode does not change project configuration, registry
assignments, repository registration, or host triggers. Use `--transfer-here`
to recover an agent whose owner value cannot be matched to any runtime.
The JSON status row includes `owner`, `is_owner`, and `ownership_available` so
automation can distinguish remote assignment from missing local runtime state.

### `ownership`

Cross-machine assignment is optional and local-only by default. Installing an
ownership backend or registering a repository does not enable it.

```bash
agents-live ownership status
agents-live ownership enable
```

`ownership status` reports `local`, `registry`, or `registry-unavailable`.
`ownership enable` validates project configuration, the installed backend, and
any existing owners document before writing `ownership = "registry"`. If
validation fails, project configuration is unchanged. An absent owners document
is initialized safely by the backend on the first assignment. Repositories that
already declare registry ownership remain enabled.

### `stop`

Records a definition as stopped and converges the complete desired set. The
definition remains in the repository.

```bash
agents-live stop link-check
agents-live stop link-check --dry-run
```

### Cross-repository resolution

`run`, `start`, `stop`, and `status` look in the selected repository first.
When the name is not there, they search the registered repositories in alias
order and use the single repository that answers, printing which one it was.
When several answer, `run`, `start`, and `stop` stop and list the qualified
`<alias>/<identifier>` choices instead of guessing; `status` only reports, so
it lists every match. Pinning a repository with `--repo`, or invoking from a
persisted trigger, keeps the search local. `run --json` names the repository
that ran the agent.

## Operations

- `--version` reports the installed version and its `release`, `bake`, or
  `unknown` channel. Bake artifacts also report their commit when encoded in
  the package version.
- `status [name] [--all-repos]` reports definitions and their started or
  stopped state, preceded by the same runtime identity. Its JSON envelope
  includes this information in the additive `runtime` object.
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
- `logs` and `logs timeline` query local event records. Default log columns
  include `run_id` and `has_transcript`. `logs transcript RUN_ID` renders one
  provider-neutral conversation; `--agent NAME --last N` selects recent runs,
  `--summary` bounds prompt and final text, `--json` returns normalized data,
  and `--raw` prints one private provider envelope for deep diagnosis.
- `lock PATH [--timeout SECONDS] -- COMMAND [ARGS...]` runs one command while
  holding the same cross-platform advisory lock used by Agents Live. It opens
  the lock file in append mode so another contender cannot replace the locked
  inode. A zero timeout fails immediately with exit code 75; a positive timeout
  waits for that many seconds. Use this wrapper from handlers, processors, and
  plugins instead of importing `fcntl`, which is POSIX-only and provides no
  exclusion when an ImportError fallback silently continues on Windows.
- `smoketest` exercises an end-to-end provider path.
- `init [--repo PATH]` initializes or registers a workspace.
- `upgrade` upgrades the tool or its installed skill payload. An installed
  self-managed runtime resolves and verifies the official latest stable release,
  stages it beside the active generation, and switches the `current` link;
  `--from <wheel>` uses the same generation builder for candidate acceptance.
  The full PEP 440 version names the directory, including a local commit suffix
  for a bake, so repeated builds on one release line do not collide. Before
  sealing, the builder validates all registered repositories and installs their
  declared plugin wheels into the candidate. Version directories are retained.
  After `current` moves, the selected version runs host maintenance to converge
  native triggers and still-started watchers; running processes may finish on
  the prior generation without blocking activation. A uv-managed runtime
  retains the existing uv upgrade behavior during migration.
  An installed
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
  A scan covers every effective discovery root except unclaimed standard
  client skill roots. A repository can opt a client root into migration by
  naming it in `agent_directories`.
- `uninstall` removes host integration and whichever installation owns the
  running command. A self-managed install removes its generation root and
  installer-added PATH exposure; a uv-managed install asks uv to remove its
  tool. If a watcher
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
command      ::= context | run | start | stop | status | logs | smoketest | doctor | lock | init | upgrade | migrate | uninstall | repos | ownership | completions | dashboard
context      ::= "context" ( NAME | "--name" NAME ) [ "--role" ( "pre" | "agent" | "post" ) ] [ "--changed-files" VALUE ] [ ( "-p" | "--prompt" ) VALUE ] [ "--prompt-file" VALUE ] [ ( "-o" | "--option" ) VALUE ]
run          ::= "run" ( NAME | "--name" NAME ) [ "--changed-files" VALUE ] [ ( "-p" | "--prompt" ) VALUE ] [ "--prompt-file" VALUE ] [ ( "-o" | "--option" ) VALUE ] [ "--quiet" ]
start        ::= "start" ( NAME | "--name" NAME | "--all" ) [ ( "--dry-run" | "-n" ) ] [ "--transfer-here" ] [ "--transfer-to" VALUE ]
stop         ::= "stop" ( NAME | "--name" NAME ) [ ( "--dry-run" | "-n" ) ]
status       ::= "status" [ NAME ] [ "--all-repos" ]
logs         ::= "logs" ( logs_query | "transcript" transcript_args | "timeline" timeline_args )
logs_query   ::= [ NAME ] [ "--log" VALUE ] [ "--all" ] [ "--agent" VALUE ] [ "--since" VALUE ] [ "--until" VALUE ] [ "--phase" VALUE ] [ "--status" VALUE ] [ "--trigger" VALUE ] [ "--slow" VALUE ] [ "--errors" ] [ ( "-n" | "--limit" | "--tail" ) VALUE ] [ "--columns" VALUE ] [ "--order-by" VALUE ] [ "--desc" ] [ "--asc" ] [ "--sql" VALUE ] [ "--format" ( "table" | "jsonl" | "csv" ) ] [ "--check-schema" ]
transcript_args ::= [ RUN_ID ] [ "--agent" VALUE ] [ "--last" VALUE ] [ "--since" VALUE ] [ "--errors" ] [ "--summary" ] [ "--raw" ]
timeline_args ::= [ FILTER ] [ "--all" ] [ "--since" VALUE ] [ "--last" VALUE ] [ "--logs" VALUE ]
smoketest    ::= "smoketest" [ "--runtime" VALUE ] [ "--model" VALUE ]
doctor       ::= "doctor" [ "--all-repos" ] [ "--repair" ] [ "--dry-run" ] [ "--quick" ]
lock         ::= "lock" PATH COMMAND [ "--timeout" VALUE ]
init         ::= "init" [ "--repo" VALUE ]
upgrade      ::= "upgrade" [ "--from" VALUE ]
migrate      ::= "migrate" [ PATHS ] [ "--dry-run" ] [ "--bundle" ]
uninstall    ::= "uninstall" [ "--distro" VALUE ] [ "--retain-state" ]
repos        ::= "repos" ( "list" | "add" PATH | "default" [ REPO ] [ "--clear" ] | "remove" REPO )
ownership    ::= "ownership" ( "status" | "enable" )
completions  ::= "completions" ( "bash" | "zsh" | "powershell" | "--update" )
dashboard    ::= "dashboard" ( dashboard_query | "list" | "stop" stop_args )
dashboard_query ::= [ PROJECT ] [ "--native" ] [ "--open" ] [ "--dev" ] [ "--port" VALUE ] [ "--all-repos" ]
stop_args ::= [ "--port" VALUE ] [ "--all" ]
```

## CLI command and flag table

| command | dispatch | root | probes | JSON | all repos | name sugar | flags | summary |
|---|---|---|---|---|---|---|---|---|
| context | in-process | registry |  | yes |  | yes | --name, --role, --changed-files, -p, --prompt, --prompt-file, -o, --option | Inspect what one run step would receive. |
| run | in-process | required |  | yes |  | yes | --name, --changed-files, -p, --prompt, --prompt-file, -o, --option, --quiet | Execute an agent once. |
| start | in-process | required | schedule, watch | yes |  | yes | --name, --all, --dry-run, -n, --transfer-here, --transfer-to | Start automatic runs for an agent. |
| stop | in-process | required | schedule | yes |  | yes | --name, --dry-run, -n | Stop automatic runs and keep the definition. |
| status | in-process | registry |  | yes | yes |  | --all-repos | List agents and whether each is started. |
| logs | subprocess | registry |  | yes |  |  | --log, --all, --agent, --since, --until, --phase, --status, --trigger, --slow, --errors, -n, --limit, --tail, --columns, --order-by, --desc, --asc, --sql, --format, --check-schema | Query logs and correlated event timelines. |
| logs transcript | subprocess | registry |  | yes |  |  | --agent, --last, --since, --errors, --summary, --raw | Read a normalized run conversation. |
| logs timeline | subprocess | registry |  | yes |  |  | --all, --since, --last, --logs | Show a correlated event timeline. |
| smoketest | in-process | required | schedule, watch | yes |  |  | --runtime, --model | Run end-to-end validation. |
| doctor | in-process | markerless |  | yes | yes |  | --all-repos, --repair, --dry-run, --quick | Check environment and installation readiness. |
| lock | in-process | none |  |  |  |  | --timeout | Run a command while holding a cross-platform file lock. |
| init | in-process | none |  | yes |  |  | --repo | Initialize the global or repository workspace. |
| upgrade | in-process | none |  | yes |  |  | --from | Upgrade runtime and project skill payloads. |
| migrate | in-process | required |  |  |  |  | --dry-run, --bundle | Convert 5.x flat definitions. |
| uninstall | in-process | none |  |  |  |  | --distro, --retain-state | Remove host integrations and the uv tool. |
| repos | in-process | none |  | yes |  |  |  | Manage registered repositories. |
| repos list | in-process | none |  |  |  |  |  | List registered repositories. |
| repos add | in-process | none |  |  |  |  |  | Register a repository. |
| repos default | in-process | none |  |  |  |  | --clear | Set or clear the fallback repository. |
| repos remove | in-process | none |  |  |  |  |  | Remove a registered repository. |
| ownership | in-process | required |  | yes |  |  |  | Manage optional cross-machine ownership. |
| ownership status | in-process | required |  | yes |  |  |  | Report local, registry enabled, or unavailable state. |
| ownership enable | in-process | required |  | yes |  |  |  | Explicitly enable registry ownership for this project. |
| completions | in-process | none |  |  |  |  | --update | Generate shell completion scripts. |
| dashboard | subprocess | registry |  |  | yes |  | --native, --open, --dev, --port, --all-repos | Open the interactive control panel. |
| dashboard list | subprocess | none |  |  |  |  |  | List dashboards this host is running. |
| dashboard stop | subprocess | none |  |  |  |  | --port, --all | Stop a dashboard this host is running. |
<!-- END GENERATED CLI -->
