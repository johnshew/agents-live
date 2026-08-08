---
title: Target Architecture - How the Pieces Fit
description: Reader's guide to the proposed end state of agents-live - the major components, what each owns, where state lives, and the lifecycle of one agent from registration to firing
ms.date: 2026-08-08
ms.topic: concept
---

This document describes the proposed system as if it were finished. It
is the companion to
[refactoring-runtime-and-agent-seams.md](refactoring-runtime-and-agent-seams.md),
which carries the argument: why each choice was made, what was rejected,
what is still undecided, and how to get there phase by phase. Read that
one to challenge a decision. Read this one to understand what is being
built.

Two cautions. The end state is **not yet decided**, so nothing here is a
commitment. And one question inside it is deliberately left to
implementation; it is named under
[What is settled, and what is not](#what-is-settled-and-what-is-not)
rather than papered over.

## The system in one paragraph

An **agent definition** is a markdown file in a repository whose
frontmatter says what work to do, how to run it, and when it should run.
Agents Live turns that last part into real automation on one machine: it
registers a trigger with the operating system's own scheduler, and when
that trigger fires it runs the agent through a provider CLI and records
what happened. The whole system is two ports with a handoff between
them. One port knows about hosts and never learns what an agent is. The
other knows about agents and never touches a process or a trigger.

## What changes in the frontmatter

Twenty-five fields are parsed today, not counting `name`. Almost all of
them are untouched, because this refactor is about who reads a field,
not about what an author writes.

| Today | End state | What changes |
|---|---|---|
| `handler` | Retired | An alias for `post-processor`, which the no-shims rule does not allow to persist. The only unconditional removal. |
| `watchPath`, `watchIgnore`, `debounce` | `watch`, one string | Three fields that every consumer has to re-join become one comparable, hashable key: `watch: "docs/** !node_modules/** debounce 5s"`. |
| `runtime`, `model` | `runtime`, one selector | `runtime: claude/sonnet:low` replaces two fields and adds a reasoning-effort level without needing a third. |
| `schedule` | Same field, same language | One grammar parses it and each host renders it, closing the gap where `@daily` and `JAN-DEC` validate on POSIX and then fail during Windows registration. |
| `owner` | Same field | Still a seed read the first time an agent is started, but now by an optional plugin that a default install never loads. |
| `timeout` | Same field | Still the agent's own value. `prepare` resolves it onto `Launch` and `dispatch` enforces it, so neither side owns both halves. |
| The other 16 | Unchanged | `mode`, `allow-tools`, `mcps`, `env`, `transcript`, both processors, the four `output-*` fields, `description`, `tools`, `user-invocable`, `disable-model-invocation`, `argument-hint`. None of them ever leaves the agent seam. |

So the count goes 25 today to 21 in 6.0. `handler` retires and both
collapses land in the same breaking release, because there is no reason
to spend two breaks where one will do.

**No new fields are added.** The two behaviors this design pins down,
concurrency policy and misfire policy, are both fixed in the runtime core
rather than exposed to authors, because neither is a decision an author
has the information to make.

What the fields *mean* changes more than how they are spelled, and that
is covered in the [worked example](#a-worked-example).

### The watch grammar

```ebnf
watch       = patterns , [ sp , debounce ] ;

patterns    = pattern , { sp , pattern } ;
pattern     = include | exclude ;
include     = glob ;
exclude     = "!" , glob ;

debounce    = "debounce" , sp , duration ;
duration    = number , unit ;
unit        = "ms" | "s" | "m" ;

number      = digit , { digit } ;
glob        = ? repo-relative path or glob; quoted if it contains spaces ? ;
sp          = " " , { " " } ;
```

`debounce` is deliberately not a pattern. It is a single optional
trailing term on the whole expression, so there is no position where it
could appear to attach to a preceding glob and no way to write two of
them. That matches the implementation, where one watcher per agent holds
one `DebounceWindow` with one delay.

Six rules travel with the grammar because EBNF cannot carry them:

1. **One `watch` expression per agent, producing one watch
   subscription.** Several includes give the watcher several roots, the
   way `watchPath` takes a list today, but they feed one loop with one
   window. This is what makes debounce global by construction rather
   than by rule.
2. **At least one include.** An expression of only excludes watches
   nothing, which is an error rather than a silent no-op.
3. **At most one `debounce`, trailing.** A second occurrence is an
   error, not last-wins. Omitted means the current default.
4. **Precedence is not positional.** A path fires when it matches at
   least one include and no exclude. This is worth stating because
   readers will assume gitignore's last-match-wins, where a later
   pattern can re-include.
5. **Canonical form is sorted includes, then sorted excludes, then the
   normalized duration.** The fingerprint then depends on meaning rather
   than spelling, so reordering an expression does not restart a
   watcher. Rule 4 is what makes sorting safe.
6. **Author excludes sit on a built-in floor.** Dotfiles, `__pycache__`,
   `_index_.md`, and the tool's own JSONL logs are always ignored
   regardless of the expression, which is existing behavior and stays
   implicit.

```yaml
watch: "docs/**"
watch: "docs/** src/**/*.py debounce 2s"
watch: "docs/** !node_modules/** !**/*.tmp debounce 500ms"
```

One thing this collapse is not: a rename. Today's `watchIgnore` is not
glob-matched at all. An entry matches an exact basename, or a directory
prefix when it ends in `/`. Globs are strictly more capable, so the old
form maps into the new one mechanically while the reverse does not.

### The selector grammar

```ebnf
selector    = provider , [ "/" , model ] , [ ":" , effort ] ;
provider    = "default" | "none" | "local" | name ;
model       = name | "default" ;
effort      = "low" | "medium" | "high" | "xhigh" | "max" ;
name        = alnum , { alnum | "-" | "_" | "." } ;
```

Provider is required; model and effort are optional and independent, so
`claude`, `claude/opus`, `claude:high`, and `claude/opus:high` are all
valid. A provider declares which models and effort levels it honors, and
the core rejects a selector no installed provider can serve with a
message listing what is installed. The effort levels are taken verbatim
from Claude Code's `effort` field rather than invented. Canonical form is
the parsed selector re-rendered, so `claude` and `claude/default` hash
identically.

Examples: `default:high`, `claude/opus:high`, `copilot`,
`local/llama3.1`, `none`.

### Retiring the old fields

The break is clean. 6.0 does not understand `watchPath`, `watchIgnore`,
`debounce`, `model`, or `handler`, and no dual-form parsing period is
offered. Coexistence was considered and declined: it buys a delay at the
cost of two accepted spellings, a mixing rule, and a removal condition
somebody has to enforce later, which is exactly the arrangement `handler`
already demonstrates nobody enforces.

A clean break still has to fail well, and the failure has a specific
shape here because **a durable trigger written by 5.x survives the
upgrade and keeps firing into the 6.0 binary.** The stale definition is
the thing that stops working, not the trigger.

**Refuse the retired names specifically; keep tolerating unknown ones.**
The format is deliberately open to other clients' extension fields, so
erroring on every unrecognized key is not available. 6.0 therefore
carries a short list of *retired* names and refuses those by name. This
is a diagnostic, not a shim: a shim makes the old input produce the old
behavior, while this makes the old input produce a legible failure.
Silently ignoring them is the one unacceptable option, because the agent
would load, look healthy, and quietly never watch anything.

**Compute the replacement from the file's own values.** The translation
is mechanical, so the error can show the exact line to paste rather than
a generic template:

```
Agents/link-check.md: 'watchPath', 'watchIgnore', and 'debounce' were
replaced in 6.0 by a single 'watch' expression.

  Replace:  watchPath: docs
            watchIgnore: ["node_modules/"]
            debounce: 5
  With:     watch: "docs/** !node_modules/** debounce 5s"
```

**Report every offending file at once.** A malformed definition aborts
convergence for its repository rather than masquerading as a deletion,
so one stale file blocks that repository until it is fixed. Listing them
all turns migration into one pass instead of a whack-a-mole loop.

**Surface it in four places**, because each catches a different person:

| Where | What it does |
|---|---|
| `upgrade` | Scans registered repositories and prints the migration list while the operator is still at the terminal |
| `doctor` | Lists every offending file with its computed replacement |
| `start` | Refuses, naming the file and the fix |
| `status` | Renders the agent as started but not loadable |

That last row is the one worth designing for. After an upgrade the old
trigger is still installed and still firing, so an agent can be started
and broken at the same time. `status` has to say so rather than reporting
it as healthy. It is not a third recorded state: started state still says
started, and the definition simply cannot be read.

**A migrator is the escape hatch, and it is not a shim.** Since the
translation is mechanical, a one-shot command can rewrite definitions in
place as an explicit operator action. The loader never accepts the old
form; a separate tool converts files. `migrate.py` already establishes
that shape for persisted trigger entries.

**The retired-name list has its own expiry.** It ships for one major
cycle and is deleted in 7.0. Without that, the diagnostic becomes its own
museum, which is the failure it exists to prevent.

## The pieces

| Piece | Owns | Must never |
|---|---|---|
| `runtime/` | Triggers, watches, processes, liveness, convergence | Know what an agent is |
| `agent/` | Definition loading, provider preparation, output normalization | Register a trigger or create a process |
| `dispatch` | The handoff: one firing becomes one run | Contain host or provider specifics |
| `state/` | The repository registry and started state | Hold runtime artifacts |
| `obs/` | The event schema, query, and timeline | Belong to either port |
| `cli/` | Commands and lifecycle composition | Contain execution logic |

```mermaid
graph TD
    CLI[cli/ - commands and lifecycle composition] --> RT[runtime/ - port]
    CLI --> AG[agent/ - port]
    CLI --> ST[state/ - registry and started state]
    CLI --> DISP[dispatch - execution handoff]
    CLI --> OBS[obs/ - events, timeline, query]
    RT --> HOSTS[hosts: posix, wsl, windows]
    AG --> PROV[providers: claude, copilot, fake, api]
    DISP -->|consumes firing context from| RT
    DISP -->|resolves target, calls prepare and finish on| AG
    DISP --> ST
    DISP --> OBS
    RT --> OBS
    AG --> OBS
```

Arrows point from depender to dependee. The two ports never import each
other, and only immutable value records built from primitives cross
between them.

### `runtime/` - automation on this host

The runtime port answers one question: what automation exists on this
machine, and does it match what should exist. It exposes two functions
and four protocols.

`converge(subscriptions)` is the whole write surface. It is handed the
complete set of things that should be running, compares that against
what it finds, and makes reality match. It is idempotent, so calling it
twice reports nothing to do the second time, and the remedy for a
partial failure is to call it again. `health()` is the read surface, and
liveness is a field on it rather than a command.

The protocols are separated by **lifetime**, and they form a ladder:

| Protocol | Survives | Used by |
|---|---|---|
| `TriggerStore` | Reboot | Convergence |
| `Supervisor` | The spawning process, but not a reboot | Convergence |
| `ChangeSource` | Nothing; dies with its holder | The watch loop |
| `ChildRunner` | Nothing; dies with the call | Dispatch |

- **`TriggerStore`** is the operating system's own scheduler: a crontab
  on POSIX, Task Scheduler on Windows. One OS artifact per subscription.
- **`Supervisor`** launches detached processes and later finds them
  again: `spawn_detached`, `owned(role=...)`, `alive`, `terminate`.
- **`ChangeSource`** reports raw filesystem paths. No policy, no
  debounce.
- **`ChildRunner`** runs one child to completion and returns what it
  produced. It is the only one dispatch touches.

The ladder is worth reading as a design statement rather than a
taxonomy, because it explains the watcher. A watcher is the only thing
in the system at the second rung: it outlives the process that started
it but not a reboot. That is precisely why a watch subscription needs
**two** pieces of actual state, a durable `@reboot` respawn artifact and
a live process, while a schedule needs only one. The oddity that
otherwise needs a paragraph of explanation is just a consequence of
where the watcher sits.

Splitting supervision from execution also keeps the fakes honest.
A dispatch test needs a recording `ChildRunner` and has no business
implementing `owned()`; a convergence test needs a scripted `Supervisor`
and never runs a child. One protocol serving both is how a fake drifts
into being a mock.

`ProcessRef` stays a single value type across both, carrying pid,
creation time, image name, and a role. Value records may cross freely;
it is service objects that must not merge. The role matters no matter
how the protocols divide, because the OS process table is shared, so a
supervision sweep must still be able to prove it is not looking at an
in-flight provider child.

A host adapter (`posix`, `wsl`, `windows`) supplies those four plus a
liveness report and a set of host facts: identity, state location, lock
acquisition, executable pinning, the child environment floor. Everything
generic lives above them in the runtime core: both trigger grammars,
debounce, the fire-rate breaker, duplicate suppression, the dueness gate,
subscription-key derivation, and the pure diff that turns desired plus
actual into a list of operations.

The single most important property: **the runtime never sees an agent.**
A subscription carries a target string like `agent:link-check`, never an
object and never a callable. This is not fastidiousness. On every
supported platform, the process that registers a trigger is dead by the
time that trigger fires, sometimes days later. Nothing but bytes and an
argv can cross that gap, so the seam is built for what actually survives.

**What changes.** Today this is nine modules totalling 3,544 lines, and
the job of "make the host match what should be installed" is
reimplemented in four more: `activate`, `stop`, `health_check`, and
`doctor`. One `converge` replaces all four partial implementations,
POSIX and Windows go behind one adapter contract instead of two parallel
stores, and `heartbeat` stops being a public command by folding into WSL
liveness.

### `agent/` - a runnable unit of work

A run is a short pipeline, not a single call. Up to three child processes
execute in a fixed order, each optional, plus one optional run-scoped
resource. The port describes each piece; it runs none of them.

```python
class Step(StrEnum):
    PRE   = "pre"     # pre-processor script; may end the run with skip
    AGENT = "agent"   # provider CLI; absent when runtime is none
    POST  = "post"    # post-processor script


def load(agent_id: str, *, root: Path) -> AgentSpec: ...
def shape(spec: AgentSpec) -> RunShape: ...
def prepare(spec: AgentSpec, step: Step, ctx: StepContext) -> Launch: ...
def interpret(spec: AgentSpec, step: Step, launch: Launch,
              raw: RawOutput) -> StepResult: ...
def outcome(spec: AgentSpec,
            results: Mapping[Step, StepResult]) -> Outcome: ...
```

`RunShape` is four booleans: which of the three steps exist, and whether
the run needs the pipeline MCP resource. It is a pure function of the
definition, which is what makes the six valid pipeline shapes a table
test rather than a narrative. `StepResult` carries `skip`, `text`, and
`retryable`. Every one of these functions is pure, so the whole port is
exercisable with no subprocess and no CLI installed.

Only the `AGENT` step involves a provider. `PRE` and `POST` launches are
built from a file extension and an environment dict, which is why they
look identical at the runtime seam and why a provider plugin never learns
that processors exist.

| Step | Selected by | Failure category | Provider |
|---|---|---|---|
| `PRE` | `pre-processor` | `pre_processor_crash` | No |
| `AGENT` | `runtime` other than `none` | `cli_crash`, `timeout`, `empty_output`, `output_parse_error`, `agent_output_invalid` | Yes |
| `POST` | `post-processor` | `post_processor_crash` | No |

A **provider plugin** is small, and is meant to stay that way:

```python
class Provider(Protocol):
    name: str
    def prepare(self, spec: ResolvedSpec, request: Request) -> Launch: ...
    def parse(self, raw: RawOutput) -> Completion: ...
```

`Launch` describes a subprocess (argv, env, temp config files, the
resolved timeout, whether a pty is needed) or, for a future
direct-to-API provider, a call. `Completion` is what a provider could
read out of its own output. Everything provider-independent lives once
in the port: size caps, JSON extraction and repair, schema validation,
path-root enforcement, provenance, and a closed error taxonomy. A
provider never classifies an error, which is what keeps that taxonomy
closed.

The agent port never creates a process. It describes one, and hands the
description back.

### Three narrowings, three consumers

The definition is progressively reduced on its way outward, and each
reduction has a different reason.

| Type | Who receives it | Contents |
|---|---|---|
| `AgentSpec` | The agent port only | Everything parsed, all 25 fields |
| `Subscription` | The host runtime | `key`, `scope`, `target`, `kind`, `trigger`: five primitives |
| `ResolvedSpec` | A provider plugin | Prompt, mode, allow-tools, mcps, env overlay, resolved model and effort |

```
load(agent_id)                -> AgentSpec       everything
  lifecycle expands triggers  -> Subscription    the host's view
  prepare(spec, AGENT, ctx)   -> ResolvedSpec    -> Provider.prepare -> Launch
  prepare(spec, PRE|POST, ctx)                   -> Launch, no provider
```

`Subscription` is the reduced runtime specification, and it deliberately
does not carry "spec" in its name. It is not a projection of the agent at
all; it is a statement about the host, which never learns that an agent
exists and sees only a target string that wants a trigger. Naming it
after the agent would re-couple the two.

`ResolvedSpec` narrows for a different reason: the provider seam is a
published plugin boundary, so anything crossing it becomes a
compatibility surface. Excluding the trigger fields, `owner`, the output
contract, and the processors keeps roughly half the definition out of a
third party's reach.

**What changes.** Today `headless.py` is 2,528 lines holding frontmatter
parsing, path discovery, MCP resolution, argv construction, subprocess
execution, output normalization, safe-output enforcement, logging, and
handler invocation in one place, with the pipeline itself orchestrated
separately in `run.py`. That splits three ways: definition and result
handling stay in the port, provider quirks move into plugins, and process
execution and sequencing leave the port entirely. `agent_adapters` is
already the shape the provider plugins take.

### Why the port is called several times instead of once

A single `run(spec, request) -> Outcome` is the obvious alternative, and
it fails on the first requirement: the agent port is not allowed to own a
process. Combining would mean either creating children inside the seam,
putting platform code back into it, or receiving a `ChildRunner` by
injection, which invariant 3 forbids.

Splitting also keeps every function pure and therefore table-testable
with no CLI installed. Today's equivalent path is only reachable through
`smoketest` with real credentials, and that is a direct consequence of
the interleaving.

A middle design was considered and rejected: having the port return the
whole pipeline as one data structure, with declared bindings between
steps and a rule vocabulary for what to do after each. It was rejected
for inventing a general-purpose orchestrator to serve a fixed
four-position sequence. The only thing it bought was avoiding a second
call across the seam, and it paid for that with two closed languages, a
state object threaded through the port, and six new types. Asking the
port again is cheaper than a vocabulary that lets you avoid asking.

The calls are named and made at fixed points, so nothing stateful crosses
the seam. That is the distinction that matters, not the call count.

### Is `Launch` reusable?

Within the `AGENT` step, yes, and it has to be. Today's code already
works this way: `command` and `env` are built once, before the retry
loop, and every attempt re-runs the same command.

There are two retry budgets, and the second settles the question. A
timeout is detected by the runner. An **empty output** is detected only
after parsing, and then the same command runs again. So the decision to
retry depends on the classification `interpret` produces, which is why
the loop lives in dispatch and the judgement lives in the port.

Across steps, no. The `AGENT` step's launch cannot even be built until
`PRE` has finished, because the pre-processor's stdout is interpolated
into the prompt. That is precisely why dispatch calls `prepare` for each
step at the moment it needs it rather than collecting launches up front.

Across dispatches, no. A launch is built from one specific request, and a
watch firing's changed-file list differs every time.

**Why `interpret` takes `launch` at all:** raw output cannot be read
without knowing how it was produced. Whether a pty was used, whether TUI
noise was filtered, which provider ran, and where the transcript went all
change how the bytes should be read.

### `dispatch` - the handoff

The only module that knows both halves exist. It is small on purpose,
roughly one page, and it is where the four questions that must be asked
at firing time live:

1. **Is this still wanted?** Re-read started state for this subscription
   key and fail closed. This is what covers the window between a change
   and the next convergence.
2. **Is it actually due?** Clock firings only. Boot, watch, and manual
   firings are due by definition.
3. **Is one already running?** If so, record the firing and drop it.
4. **What does the agent need?** Translate the firing context into a
   `Request`.

Then it runs the pipeline. The sequence is fixed, so this is
straight-line code with three conditionals rather than an engine:

```python
def dispatch(firing):
    spec = agent.load(firing.agent_id, root=firing.root)
    shape = agent.shape(spec)
    results = {}

    with pipeline_mcp() if shape.needs_mcp else nullcontext() as mcp:
        if shape.has_pre:
            raw = run_child(agent.prepare(spec, PRE, ctx(mcp)))
            results[PRE] = agent.interpret(spec, PRE, raw)
            if results[PRE].skip:
                return agent.outcome(spec, results)

        if shape.has_agent:
            launch = agent.prepare(spec, AGENT, ctx(mcp, pre=results.get(PRE)))
            while True:                       # both retry budgets live here
                raw = run_child(launch)       # the same launch every attempt
                results[AGENT] = agent.interpret(spec, AGENT, launch, raw)
                if not results[AGENT].retryable:
                    break

        if shape.has_post:
            raw = run_child(agent.prepare(spec, POST, ctx(mcp, agent=results.get(AGENT))))
            results[POST] = agent.interpret(spec, POST, raw)

    return agent.outcome(spec, results)
```

`run_child` is `ChildRunner.run_child`. The runtime sees one operation
and never learns which step it is or what the argv means, which is what
keeps every command line inside the agent port.

Dispatch owns process creation, the retry loop, streaming, cleanup, and
the timeout each `Launch` carries. It owns no judgement: whether a run
should be retried, whether a pre-processor asked to skip, and how a
post-processor receives its input are all answered by `interpret` and
`prepare`.

The `with` block is the whole answer to MCP lifetime. The pipeline MCP
server is run-scoped rather than step-scoped, since all three steps
connect to the same instance, so it belongs to the one component that
owns the run.

**What changes.** New as a module, but not as behavior. `run.py` already
runs an ownership gate, a dueness claim, an origin derivation from
`--scheduled`, `--boot`, and changed files, and a legacy-task repair,
all as a prologue to execution. Those move into one named place with one
test surface.

### `state/` - what should happen here

Two facts, both machine-local, both outside every repository:

- **The repository registry.** Which repositories this installation
  looks in. Answering "where do I look" is the precondition for
  everything else.
- **Started state.** For each `(repository, agent)` pair, started or
  stopped. `start` writes it, `stop` clears it.

Started state is the fact that makes the whole design work, and it does
not exist today. Frontmatter says how an agent *would* run; it never
says that it *is* started here. Without a recorded intent, a system that
goal-seeks on frontmatter alone would reinstall every trigger the user
had deliberately stopped. With it, a trigger someone deleted behind the
tool's back becomes repairable drift instead of a silent stop.

An **optional assignment policy** also lives here, for installations that
opt into multi-machine ownership. In a default install it is absent and
every local agent is simply yours.

### Started state is a collection input, not just an output

Convergence reads started state on every pass, which makes it subject to
the same safety question as every other input: *would treating this as
empty destroy working automation?* For started state the answer is yes,
emphatically, because empty means "nothing runs here" and the response
is to prune every trigger on the machine.

So it takes its place as the fourth row of the collection table:

| Input | Meaning | Absent or unreadable means |
|---|---|---|
| Repository registry | Where to look | Abstain |
| Optional assignment policy | Permission to run here | Abstain |
| Definitions in a repository | What could run | Prune that repository's triggers |
| Started state | What should run here | Adopt if never initialized, abstain if unreadable |

The last row needs its two absences kept apart, and that distinction is
the whole of it:

- **Never initialized.** No store, no marker. Convergence **adopts**:
  every trigger this installation can identify as its own becomes a
  started record, and the marker is written. On a fresh machine there is
  nothing installed, so the adopted set is empty and this is
  indistinguishable from a normal first run.
- **Present but unreadable.** Permission denied, corrupt, caught
  mid-write. **Abstain**, exactly as with the registry. The condition is
  transient and guessing at it destroys working automation for a fact
  that could not be confirmed.

Adoption is not a migration step that runs once. Started state can go
missing at any point in a machine's life: a cleared state directory, a
restored home directory, a new user profile, a failed disk. Making
adoption an ordinary property of convergence means the recovery path and
the fresh-install path are the same code, which is what stops it from
rotting unnoticed.

It also cannot live only in `upgrade`. New code arrives through package
upgrades without any interactive command running, and the maintenance
trigger fires on a schedule regardless, so the rule has to sit where
every path passes through.

One consequence, accepted deliberately: adoption can only claim triggers
the installation can identify as its own, which is what the structured
marker is for. An artifact it cannot identify is left alone rather than
adopted, so the failure mode is a surviving orphan rather than a deleted
agent. That is the right direction to fail.

**What changes.** `repos.py`, `ownership.py`, and `paths.py` are already
close to this. The addition is started state, which does not exist
today. The subtraction is that ownership becomes explicitly optional
rather than a concept the dispatch and lifecycle paths each consult.

### `obs/` - what happened

The JSONL event schema, the query interface, and the timeline. Both
ports write to it and neither owns it. Keeping it separate is what lets
a runtime test assert on emitted events without importing an agent, and
what lets a field incident be replayed later against fakes.

**What changes.** Least of any piece. `qlog`, `timeline`, and `adminlog`
already do this work. Naming them as one owner is what stops both ports
from free-writing their own JSONL shapes.

### `cli/` - the three verbs

The user's whole vocabulary is `start`, `stop`, and `run`.

- `start` means make this happen automatically here.
- `stop` means stop making it happen.
- `run` means do it once, now.

`status` and `doctor` report in those same words. Nothing the user sees
says plan, diff, subscription, or convergence. Those are mechanism, and
mechanism stays invisible.

**What changes.** The command surface barely moves: `start`, `stop`, and
`run` are already the published verbs, and `cli_spec`'s declarative
approach survives intact. What changes is that `status`, `completions`,
and `dashboard` stop reaching into scheduling and execution internals,
`activate.py` is renamed to match the verb it serves, help text stops
saying "Activate" and "Deactivate", and two flags retire:
`--prune-orphans`, because convergence removes anything absent from the
desired set, and `--transfer-to`, because it cannot finish the move it
names.

## Where state lives

Nothing machine-local is ever written inside a repository. Repositories
sync between machines and export to archives; machine state must not
travel with them.

| Fact | Location | Survives reboot | Survives a repo move |
|---|---|---|---|
| Which repositories to look in | `$XDG_CONFIG_HOME/agents-live/config.toml` | Yes | Yes |
| Started or stopped, per (repo, agent) | `$XDG_STATE_HOME/agents-live/repos/<name>-<hash8>/` | Yes | No, and deliberately: see [stage 9](#stage-9-moving-the-repository) |
| The definition itself | `<repo>/Agents/<name>.md` | Yes | Yes, it travels with the repo |
| The OS trigger | crontab or Task Scheduler | Yes | n/a |
| A running watcher process | The process table | No, the `@reboot` trigger restores it | n/a |
| Run logs and outcomes | `$XDG_STATE_HOME/agents-live/repos/<name>-<hash8>/logs/` | Yes | No, old logs are abandoned |
| Host health beacon | `$XDG_STATE_HOME/agents-live/health.ok` | Yes | n/a |
| Dispatch rate budget | One file per project per host | Emptied, which is the safe direction | n/a |

The per-repo directory name is the repository's basename plus a hash of
its resolved absolute path, which is what keeps two repositories with
the same name distinct. It is also why moving a repository is an
explicit act rather than something the tool infers.

## A worked example

A repository at `/src/handbook` contains one agent,
`Agents/link-check.md`:

```markdown
---
name: link-check
description: Verify that documentation links resolve.
runtime: claude/sonnet
mode: write
schedule: "0 7 * * 1"
watch: "docs/** !node_modules/** debounce 5s"
timeout: 300
---

Check every relative link under docs/. For each one that does not
resolve, open a GitHub issue naming the file and the line.
```

It should run every Monday at 07:00, and again whenever anything under
`docs/` changes and then settles for five seconds.

> In 5.x this was five fields rather than two: `runtime: claude` with
> `model: sonnet`, and `watchPath: docs` with
> `watchIgnore: ["node_modules/"]` and `debounce: 5`. Collapsing them is
> the breaking change in 6.0. The lifecycle below is identical either
> way.

**What changes for the author.** Two lines of this file, and the
field-level differences are tabled under
[What changes in the frontmatter](#what-changes-in-the-frontmatter). What
changes underneath them matters more:

- **Who reads it.** Today `headless.py` parses the definition and also
  runs it, so frontmatter knowledge leaks into everything that calls it.
  In the end state only the agent port loads the file, and lifecycle
  orchestration reads nothing from it but the trigger fields.
- **What it means.** Today an installed trigger is the only record that
  an agent is automated here, which is why the repair loop can delete an
  orphan but never restore one. Frontmatter now says how an agent *would*
  run; started state says whether it *does*.
- **Where it is authoritative.** The definition is inert. Copying this
  repository to a second machine automates nothing there until somebody
  runs `start` on that machine, which is the point stage 1 makes.

### Stage 1: the machine learns where to look

```
$ cd /src/handbook
$ agents-live init
```

This writes `/src/handbook` into the registry and creates the per-repo
state directory. That is all it does.

**Nothing is automated yet.** No trigger exists, because no agent has
been started. Registration answers "where do I look", which is a
different question from "what should run here". Keeping those separate
is what lets you register a repository on ten machines and run its
agents on one.

### Stage 2: starting the agent

```
$ agents-live start --name link-check
```

Five things happen, in this order:

```mermaid
sequenceDiagram
    participant U as user
    participant C as cli
    participant S as state/
    participant R as runtime/
    participant H as host adapter

    U->>C: start --name link-check
    C->>S: record (handbook, link-check) = started
    C->>S: read registry, read started state
    C->>C: load definitions, expand to subscriptions
    C->>R: converge(subscriptions)
    R->>H: check prerequisites and liveness
    R->>H: list installed triggers, list owned processes
    R->>R: diff(desired, actual) -> operations
    R->>H: install trigger, install respawn, spawn watcher
    R-->>C: Converged(done=[...], failed=[], health=...)
    C-->>U: link-check is started. Next run Monday 07:00.
```

**1. Record the intent.** Started state gets `(handbook, link-check) =
started`. This is written first, so that even if everything after it
fails, running the command again finishes the job.

**2. Collect the complete desired set.** Not just this agent. The CLI
reads the registry, walks every registered repository, loads every
definition, keeps the ones marked started, and expands each one's
trigger declarations. If the registry cannot be read, it stops here and
converges nothing, because a desired set that cannot be bounded is not a
desired set.

**3. Expand to subscriptions.** `link-check` is one agent but two
subscriptions, because it declares two triggers:

| Key | Scope | Target | Kind | Trigger |
|---|---|---|---|---|
| (derived) | `repo:/src/handbook` | `agent:link-check` | `schedule` | `0 7 * * 1` |
| (derived) | `repo:/src/handbook` | `agent:link-check` | `watch` | `docs`, ignore, debounce 5 |

The runtime adds a third of its own, scoped to this installation rather
than to any repository, targeting `runtime` instead of an agent. That is
the check-and-repair loop, and it is an ordinary subscription rather than
a special case.

**4. Converge.** The runtime checks host prerequisites first, then reads
what is actually installed, computes a pure diff, and executes the
resulting operations. Schematically, the crontab now holds three lines:
the Monday schedule, an `@reboot` line that respawns the watcher, and the
maintenance line.

**5. Spawn the watcher.** A watch subscription has a second piece of
state the trigger store cannot see: a live process. The `@reboot` line
carries a structured marker holding the subscription key and a
fingerprint of the watch expression, and the watcher is spawned with that
same canonical expression in its own command line. Both halves of
"actual" therefore describe themselves, and convergence recovers them by
reading the artifact and matching `owned(role="watcher")` against it.

Nothing is written to a side index, which is the point. A separate record
would have to be written after the spawn, opening a window where a
process exists and its record does not, and that window is what forces a
crash-recovery rule. Making the process its own record closes it by
construction. Whether the command-line length bound on Windows permits
this is the one thing phase 2 measures before committing; if it does not,
the fallback is a runtime-owned index and the crash-ordering rule that
comes with it.

### Stage 3: what exists now

```
Registry        ~/.config/agents-live/config.toml   -> /src/handbook
Started state   ~/.local/state/agents-live/repos/handbook-a1b2c3d4/
                  link-check = started
Definition      /src/handbook/Agents/link-check.md   (unchanged, in git)
crontab         0 7 * * 1   cd /src/handbook && agents-live run ...
                @reboot     cd /src/handbook && agents-live watch ...
                */N * * * * agents-live internal maintain
Process table   one watcher process, watching /src/handbook/docs
```

Note what is *not* in the repository: nothing. The definition is the only
file in `/src/handbook` that Agents Live cares about, and it was written
by the author, not by the tool.

### Stage 4: a scheduled firing

Monday, 07:00. The process that installed the trigger exited days ago.
Cron creates a brand new process.

```mermaid
sequenceDiagram
    participant K as cron
    participant D as dispatch
    participant S as state/
    participant A as agent/
    participant P as provider
    participant PH as ChildRunner
    participant O as obs/

    K->>D: exec argv (agent id, origin=clock)
    D->>S: still started?
    D->>D: due this minute? already running?
    D->>A: load(link-check)
    A-->>D: AgentSpec
    D->>A: shape(spec)
    A-->>D: RunShape(pre=no, agent=yes, post=no, mcp=no)
    D->>A: prepare(spec, AGENT, ctx)
    A->>P: prepare(ResolvedSpec, request)
    P-->>A: Launch (argv, env, timeout 300)
    A-->>D: Launch
    D->>PH: run_child(launch)
    PH-->>D: ChildResult
    D->>A: interpret(spec, AGENT, launch, raw)
    A->>P: parse(raw)
    P-->>A: Completion
    A-->>D: StepResult(retryable=false)
    D->>A: outcome(spec, results)
    A-->>D: Outcome
    D->>O: record firing and outcome
```

Three checks happen before any work: still started, actually due, not
already running. All three are cheap, and all three fail closed. Only
then does the run begin.

`link-check` is the simplest of the six shapes, one step and no
resource. `shape()` says so, and dispatch skips straight past the two
conditionals it does not need.

Notice where the timeout comes from. `300` is a fact in the definition,
so the agent port resolves it and puts it on `Launch`. Dispatch enforces
it, because dispatch is the only side holding the child. Neither one owns
both halves.

### The six pipeline shapes

Three optional steps give six valid combinations, all decided by the
definition alone. Dispatch runs the same code for every one of them.

| `runtime` | `pre-processor` | `post-processor` | What runs |
|---|---|---|---|
| a provider | no | no | Agent only; its output is logged |
| a provider | no | yes | Agent, then post-processor fed the agent's output |
| a provider | yes | no | Pre-processor, its output appended to the prompt, then the agent |
| a provider | yes | yes | All three |
| `none` | yes | yes | Pre-processor piped straight to post-processor, no model |
| `none` | yes or no | either one present | Whichever script is declared |

`runtime: none` with neither processor is the one invalid combination,
and it is rejected when the definition loads. The shipped `handler-only`
template is the fifth row: a scheduled automation with no model in it at
all, which is why the selector grammar has to accept `none`.

**`mode: pipeline` changes the wiring, not the shape.** Normally the
post-processor receives the agent's stdout on stdin. In pipeline mode the
agent publishes structured output to the pipeline MCP store and the
post-processor fetches it with `get()`, so the agent's stdout is
narration and non-JSON there is expected rather than an error. That is a
different `ctx` passed to `prepare(spec, POST, ...)`, decided inside the
agent port where the `mode` field lives. Dispatch is unaware of the
distinction.

The MCP server itself is the run-scoped resource in dispatch's `with`
block. All three steps connect to one instance, reaching it through the
environment, which each provider adapter turns into its own flag.

### Stage 5: a watch firing

Someone saves `docs/setup.md`.

```mermaid
sequenceDiagram
    participant F as filesystem
    participant CS as ChangeSource
    participant W as watchloop
    participant D as dispatch

    F->>CS: raw path changed
    CS->>W: "docs/setup.md"
    W->>W: apply ignore rules
    W->>W: debounce 5s, coalesce further edits
    W->>W: rate breaker, duplicate suppression
    W->>D: firing context (origin=watch, changed files)
```

From `dispatch` onward this is the **same path** as stage 4, minus the
dueness gate. That is the point of the design: a watch firing and a
scheduled firing differ only in how they traveled and what origin they
carry. One runs in-process inside the watcher; the other arrives through
an argv in a fresh process.

The host adapter's job here is small and dumb: report raw paths. Ignore
rules, debounce, the breaker, and duplicate suppression are all generic,
which means they are testable with no host present at all.

### Stage 6: drift, and why it is repairable

Someone edits the crontab by hand and deletes the Monday line.

Nothing notices immediately. Then the maintenance subscription fires,
which runs a convergence over everything started on this installation.
Desired contains the schedule; actual does not; the diff produces one
install operation; the line comes back.

This is only possible because started state exists. A system converging
from frontmatter alone could not tell the difference between "someone
deleted this trigger" and "someone ran `stop`", so repairing the first
would silently undo the second. That is exactly why the current
check-and-repair loop can remove an orphaned trigger but can never
restore a missing one.

The same mechanism handles a subtler case. If the author edits
`watchPath`, the running watcher is still watching the old directory,
because it read its configuration at spawn. Convergence compares a
fingerprint of the desired watch expression against the one the running
watcher carries, and on a mismatch stops it and starts a new one.
Without that, editing a watch expression would take effect only at the
next reboot.

### Stage 7: stopping

```
$ agents-live stop --name link-check
```

Started state is cleared, then the same convergence runs. `link-check`
now contributes no subscriptions, so both of its artifacts are absent
from the desired set, and absence is what removal means: the crontab
lines go, the watcher is terminated.

Orphan pruning is not a separate feature here. An agent whose file was
deleted, an agent that was stopped, and an agent that was never started
all look identical to the diff, because all three are simply not in the
list. The definition file itself is never touched.

### Stage 8: the repository goes away

The volume holding `/src/handbook` is unmounted.

The next convergence reads the registry fine, but cannot read the
repository, so that repository contributes no subscriptions and its
triggers are pruned. This is deliberate, and it is the opposite of the
rule applied to the registry. A trigger for an unreachable repository
does not preserve working automation; it preserves a run that fails every
Monday, plus log noise, plus a `status` that claims the agent is fine.

Pruning is safe because the intent is not in the repository. Started
state is machine-local, so when the volume comes back the next
convergence rebuilds both subscriptions from frontmatter times started
state. The accepted cost is stated plainly: a firing due inside that
window is lost, because misfire policy is skip.

The registry is different and keeps the opposite rule. If the registry
itself cannot be read, convergence does not run at all, because there the
agents would run correctly and their triggers would be deleted for a fact
that merely could not be confirmed.

### Stage 9: moving the repository

Moving a directory does nothing on its own, and it should not.
Registration says where to look, started state says what runs here, and
neither is derivable from a path. So a move is an explicit sequence:

```
agents-live stop --name link-check      # in the old location
mv /src/handbook /work/handbook
cd /work/handbook && agents-live init   # register the new path
agents-live repos remove /src/handbook
agents-live start --name link-check
```

**If you forget and simply move it**, the half that misbehaves cleans
itself up and the half that is merely stale does not:

| Left behind | What happens to it |
|---|---|
| Crontab lines for the old path | Pruned at the next convergence |
| The running watcher | Terminated in the same pass |
| Registry entry for the old path | Survives, nothing removes it |
| Started state under the old path's hash | Survives, nothing removes it |

That is stage 8's rule doing its job. The repository is unreadable, so it
contributes no subscriptions, so its triggers go. What remains costs disk
and confusion, nothing else.

The leftovers are reported rather than removed, because "unreadable" is
ambiguous between moved, deleted, and temporarily unmounted, and stage 8
commits to the unmount case being recoverable. Auto-deregistering would
break exactly that: a drive unmounted at boot would deregister its
repository, and nothing would bring it back. So `doctor` reports it in
the user's vocabulary:

> Repository `/src/handbook` is registered but cannot be read. One agent
> is recorded as started there (`link-check`), and its triggers have been
> removed. If you moved the repository, register the new location and
> start it there. If you deleted it, run `repos remove /src/handbook`.

That message does three jobs: it says what already happened
automatically, it distinguishes the two causes, and it names the exact
command. Nothing new is needed to act on it, since `repos remove` exists.

One hazard worth knowing, because it behaves differently on the two
paths: on Linux, inotify watches follow the inode. A same-filesystem move
leaves the watcher running against a directory that no longer matches its
recorded root, so it can fire runs whose `cd` fails. A cross-filesystem
move is a copy and a delete, so the watch simply dies. Convergence
terminates it either way, but the window before that pass is not the
same.

## Rules that hold everywhere

These are the invariants worth checking any change against. Most are
enforceable as tests.

1. **`runtime/` does not import `agent/`, and `agent/` does not import
   `runtime/`.** Only `dispatch` and the lifecycle commands touch both.
2. **Only immutable value records built from primitives cross a seam.** A
   firing carries an agent id, never an agent. A launch description
   carries argv, never a process.
3. **No host service object enters the agent port.** The agent port
   describes a process; it never holds one.
4. **`sys.platform`, `os.name`, and WSL detection appear only under
   `runtime/hosts/`.** Everywhere else is host-agnostic.
5. **One word, one meaning.** An agent is started or stopped. "Running"
   is reserved for a run in flight, which the concurrency rule needs it
   to mean.
6. **The user's vocabulary is three verbs.** No CLI output, help text, or
   error message names a plan, a diff, a subscription, or a convergence
   pass.
7. **Convergence is idempotent and total.** It is always handed the
   complete desired set. If the set cannot be bounded, it is not called.
8. **Both firing paths produce the same dispatch inputs.** In-process and
   cross-process differ only in transport and origin.
9. **Concurrency policy is skip, and misfire policy is skip.** A firing
   arriving on top of a live run is dropped. A missed firing is not
   replayed.
10. **Machine state never lives in a repository.**

## What is settled, and what is not

Two of the three questions this document originally opened were settled
on 2026-08-08. The argument for each is in
[the proposal](refactoring-runtime-and-agent-seams.md#open-decisions).

**Settled: the grammars land as a clean break.** The watch and selector
collapses ship with the `handler` retirement in one major version bump
from 5.5.2. No compatibility period and no dual-form parsing. The retired
names are refused by name with the replacement computed from the file's
own values, per
[Retiring the old fields](#retiring-the-old-fields). Coexistence was
considered and declined: it buys a delay at the price of two accepted
spellings, a mixing rule, and a removal condition somebody has to
enforce later.

**Settled: moving a repository does nothing on its own.** A move is
`stop`, `mv`, register, `repos remove`, `start`, and forgetting is safe
but leaves two inert records that `doctor` reports rather than deletes.
See [stage 9](#stage-9-moving-the-repository).

**Open, deliberately: does a scheduled firing need a full event
envelope?** Today's argv already carries agent id, origin, and changed
files. Whether it needs a versioned envelope with a subscription key and
timestamp is a question the prototype answers better than a document can,
so it is settled during phase 5 rather than before it.

One design detail inside the picture is settled as a target rather than
as a fact: the watcher fingerprint lives on the durable artifact and in
the watcher's own command line, not in a runtime-owned index. That makes
it the same mechanism as the marker exhaustive pruning already needs, and
it removes the write-ordering window in stage 2 instead of writing a rule
to survive it. Phase 2 confirms it by measurement, since a watch
expression at the Windows command-line length bound is what could defeat
it, and keeps an index as the named fallback.
