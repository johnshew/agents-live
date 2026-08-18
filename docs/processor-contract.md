---
title: Processor contract, second design
description: A first-principles contract for pre- and post-processors that keeps them ordinary command-line programs
ms.date: 2026-08-18
ms.topic: concept
---

# Processor contract, second design

## Status

Proposed, and discussed in
[#373](https://github.com/johnshew/agents-live/issues/373). The contract that
ships today is described in
[processors.md](../src/agents_live/skill/docs/processors.md); this document
designs its replacement and does not describe current behavior.

## The problem with the current contract

A processor today is a program only Agents Live can run.

It receives no arguments, so its inputs cannot be expressed on a command line.
Its stdin means different things in different execution modes: outside pipeline
mode a post-processor is handed the agent's extracted value, and inside it the
stream is closed. Its stdout is at once a payload, a log, and a control
channel, where a top-level `skip` key cancels the run whether or not the author
meant it. Its log records go into a file shared with the runtime, where nothing
distinguishes an observation from a verdict.

So a useful script has to be rewritten to become a processor, and once
rewritten it can no longer be run by hand. That is backwards. The valuable part
of a processor is domain work: sweeping a mailbox, formatting Markdown, filing
a report. Domain work has a natural command-line shape and should keep it.

## Design principles

**1. A processor is an ordinary program.** It has flags, a `--help`, and an
exit code. It prints progress to stdout like any other program. It is useful
and testable without Agents Live.

**2. One channel, one purpose.** Nothing is multiplexed. Payload, control,
observation, shared state, and human narration each have their own route, so no
content can impersonate a control word.

**3. The definition owns the command line.** A processor declares nothing about
itself. The definition that uses it says how to invoke it, the way a Makefile,
a systemd unit, or a crontab entry does. Nothing has to be discovered,
introspected, or kept in sync in a second file.

**4. Capabilities are announced and optional.** Each capability is named by an
environment variable. A program that finds none of them still runs. This is
what makes one file work by hand, in a pipeline, and under a scheduler.

**5. Execution mode is a policy about the model, not about processors.** What a
processor receives is identical in every mode. Mode decides what the model may
do.

**6. Nothing has to be imported.** The contract is files, streams, and
environment names, so `.py`, `.js`, `.ts`, `.ps1`, and `.sh` are equal.

## The five channels

| Channel | Direction | Carried by |
|---|---|---|
| Input | in | Stdin, and the same bytes at `AGENTS_LIVE_INPUT` |
| Result | out | The file at `AGENTS_LIVE_OUTPUT` |
| Control | out | The file at `AGENTS_LIVE_CONTROL` |
| Observation | out | The JSONL sink at `AGENTS_LIVE_LOG` |
| Shared state | both | The directory at `AGENTS_LIVE_STORE` |

Stdout and stderr are left to the program. Stdout is human narration, captured
and bounded by the host and shown in `agents-live logs timeline`. Stderr is
diagnostics, and on failure it becomes the recorded message.

This is the GitHub Actions lesson applied deliberately. Actions began with
stdout control commands and migrated to environment file handles, and the
`stop-commands` escape hatch it still carries is the evidence for why. The risk
is sharper here, because a post-processor's input is literal model output.

## Invocation

The definition names the program and the command line it wants:

```yaml
metadata:
  agents-live.schema-version: "2"
  agents-live.options: '{"account": "string", "dry_run": "bool=false"}'
  agents-live.pre-processor: "scripts/sweep_email.py --account ${account} ${dry_run}"
  agents-live.post-processor: "scripts/send.py --account ${account}"
```

`agents-live.options` is the run's option surface, which is
[#372](https://github.com/johnshew/agents-live/issues/372). Two rules govern
the command line:

- `${name}` substitutes that option's value as a single argument, whatever
  spaces it contains.
- A boolean substitutes `--name` when true and nothing when false, so
  `${dry_run}` becomes `--dry-run` or disappears.

Referencing an option that has no value and no default is an error before
dispatch, which keeps a dangling `--account` with nothing after it from ever
reaching the program.

That is the whole grammar. It is not a templating language and it will not grow
into one. A processor that takes no arguments is named with no arguments, which
is what makes adopting an existing zero-argument script a no-op.

The two processors may take different flags, or different names for the same
value, because each writes its own command line. Nothing has to agree except
the option names the definition itself declared.

Agents Live passes exactly what the definition wrote and never appends anything
of its own, so a strict argument parser never sees an argument it does not
know.

Precedence for an option's value, lowest to highest: the default in
`agents-live.options`, then the environment, then the flag on `agents-live
run`. The most explicit and most local wins, which is the case that has to work
while debugging.

The interpreter is still chosen by extension, and the working directory is
still the repository root.

### What this gives up

Agents Live cannot check that `--account` is a flag the script actually
accepts, because it does not know the script's interface. A wrong name is
caught by the script's own argument parser, which exits immediately with a
clear message and, for a pre-processor, before any model call. That is a good
enough failure, and it costs one less file format than the alternative.

## Input: the envelope

Stdin carries one JSON object, and `AGENTS_LIVE_INPUT` names a file holding the
same bytes. The shape is identical for both roles in every execution mode:

```json
{
  "contract": 2,
  "role": "pre",
  "run": {
    "id": "8f1c2a...",
    "agent": "email-agent",
    "agent_id": "email-agent-8a6923e6b7",
    "origin": "watch",
    "attempt": 1
  },
  "repository": {
    "root": "/work/notes",
    "changed_files": ["docs/guide.md"]
  },
  "options": {"dry_run": true, "account": "team-inbox"},
  "instructions": "Focus on the authentication changes",
  "upstream": null
}
```

For a post-processor, `upstream` names what the previous step produced:

```json
{
  "upstream": {
    "step": "agent",
    "path": "/run/agent/result",
    "text": "Here is the plan...",
    "value": null
  }
}
```

`text` is always populated and is the previous step's output as written.
`path` addresses the same bytes in the store, for output too large to inline.
`value` is populated only when Agents Live already had a reason to parse, which
means the definition declared `agents-live.output-schema` for the model, or the
model wrote a value into the store in pipeline mode. Otherwise it is null and
the post-processor parses `text` itself, or does not, as it prefers. `step`
names what actually produced it, `agent` or `pre`.

Rules that make the envelope safe to depend on:

- **`contract` is the version.** Fields are additive within a version; a
  removal or a meaning change is a new version.
- **Every field has a defined empty value.** `changed_files` is `[]`,
  `instructions` is `""`, `options` is `{}`. A processor never needs a presence
  check.
- **`options` repeats the flags.** A Python program parses flags, a shell
  program reads the envelope with `jq`, and neither is second-class.
- **Empty input is legal.** Run by hand with nothing piped in, the program must
  behave as if every field held its empty value.

`instructions` is where
[#366](https://github.com/johnshew/agents-live/issues/366) becomes visible to a
processor, which is impossible under contract 1.

## Output: result and control

The result is whatever the program writes to the file at
`AGENTS_LIVE_OUTPUT`. **Agents Live does not parse it, validate it, or require
it to be JSON.** It carries it.

For a pre-processor it is handed to the model verbatim, under a label, the way
an attachment is. For a post-processor it becomes the run's result. Neither
case needs Agents Live to know the shape, so nothing declares one.

```python
Path(os.environ["AGENTS_LIVE_OUTPUT"]).write_text(json.dumps({"threads": 42}))
```

A JSON document is the usual choice because the next reader is a model or
another program, but a Markdown table is equally valid if that is what the
model should see.

There are exactly two places where a shape is checked, and both predate this
contract: `agents-live.output-schema` validates what the **model** returns,
and a bound `$schema` validates what the **model** writes into the store in
pipeline mode. Both guard the untrusted participant. A processor is trusted
repository code, so its output is not a validation boundary.

When `AGENTS_LIVE_OUTPUT` is absent, which is the standalone case, the result
goes to stdout, because that is what a person running the program by hand
wants. Under dispatch the variable is always set, so stdout is never a payload
and a stray `print()` cannot corrupt anything.

Control is a separate file at `AGENTS_LIVE_CONTROL`:

```python
Path(os.environ["AGENTS_LIVE_CONTROL"]).write_text(json.dumps({"skip": True}))
```

| Field | Type | Meaning |
|---|---|---|
| `skip` | boolean | End the run now, successfully, without running later steps |
| `message` | string | One short line for the run record |

Because control has its own channel there is no reserved key in the payload,
and no value a processor emits can be mistaken for an instruction.

Failure is the exit code, not a field. A non-zero exit fails the run.

## Shared state: the store

The store is a run-scoped directory of JSON values, named by
`AGENTS_LIVE_STORE`. A path addresses a file, so `/in/messages` is
`$AGENTS_LIVE_STORE/in/messages.json`. Processors read and write it with
ordinary file operations, in every mode.

It is ephemeral. The directory is created for one run and removed when the run
ends, so nothing leaks into a later run and there is no retention policy to
own. `--keep-store` preserves it for debugging.

Values the definition seeds through fenced `put` blocks are written before the
first step and are read-only for the rest of the run.

### What pipeline mode changes

In `mode: pipeline`, and only there, Agents Live also starts the ephemeral
in-process MCP server in front of the same directory and gives the model `get`
and `put` as its only tools. The server keeps doing what it does today:
validating writes against a bound `$schema`, refusing writes to seeded paths,
and journaling every call.

|  | `plan`, `write` | `pipeline` |
|---|---|---|
| Model tools | Provider tools per policy | Store tools only |
| Store exposed to the model | No | Yes, schema-validated, seeded paths frozen |
| What a processor receives | Envelope, store, log sink | Identical |

The trust boundary is the model, not the processors. Processors are repository
code and are the mediators the pipeline design already relies on, so they touch
the directory directly. The model is untrusted and reaches the same values
through the server, which is where enforcement belongs. The Copilot stdio
bridge stays a model-side concern and never appears in a processor's world.

## Observation: logging

Logging is a first-class channel and has to interoperate with what
`agents-live logs` already reads. This contract owns the **channel**; the
record shape is owned by
[#105](https://github.com/johnshew/agents-live/issues/105), which is a clean
schema change across all canonical writers.

**A per-step sink.** `AGENTS_LIVE_LOG` names a file that only this child
writes, derived from the run id and the step. The host validates, stamps, caps,
and merges it into the agent log when the step exits.

Three reasons, each of them a defect in contract 1:

- **No concurrent appenders.** Buffered writes to the shared log spliced 11,577
  records into each other in a live deployment (#290). A file with one writer
  cannot have that failure.
- **Identity cannot be forged.** The host stamps `run_id`, `agent_name`,
  `agent_id`, `step`, and span identity on ingest, overwriting whatever the
  child wrote. Today a processor can author a record claiming to be another
  agent.
- **It is an artifact.** The sink replays with the envelope, and
  `logs --follow` can read it while the step is still running, because the path
  is derived rather than random.

**Reserved vocabulary.** A processor may not write `phase: "done"` nor any
run-outcome status. Those words are how the health path decides an agent is
failing, and an observation must never be able to cast a verdict. Verdicts are
the exit code. A processor's rows carry `level` for severity and may name their
own `phase` for a stage inside the step.

**A minimal row.**

```json
{"ts": "2026-08-18T09:12:04.331Z", "level": "info", "message": "swept 42 threads", "phase": "collect", "duration_s": 4.1}
```

Child-owned fields are `ts`, `level`, `message`, `phase`, `duration_s`, the
cost columns `cost_usd`, `credits`, and `premium_requests`, and anything else
the program wants to add. A processor that calls a paid API has a real reason
to report spend, and those columns already exist in the normalized view.

**Correlation passed in.** `TRACEPARENT` names the step span as the parent. A
processor that ignores it still produces correlated rows, because the host
stamps the step span on ingest.

**Sensitivity defaults closed.** A processor's records are local-only unless a
row is explicitly marked as exportable operational metadata. #105 requires that
content-bearing fields not become exportable merely because a sidecar exists,
so the default has to be the conservative one.

**Malformed rows are quarantined, never fatal.** A bad line is counted and
reported by the schema check and the step still succeeds. Overflowing the
per-step cap truncates and records a marker row. Failing a run because it
logged too much is the wrong trade.

**Standalone falls back to stderr**, where `jq` reads it and a person can skim
it. Under dispatch it goes to the file, so it never pollutes the stderr that
becomes the failure message.

## Capabilities

| Variable | Present when | The program may |
|---|---|---|
| `AGENTS_LIVE_CONTRACT` | Running under Agents Live | Assume the rest of this table |
| `AGENTS_LIVE_INPUT` | Always, under Agents Live | Read the envelope from a file rather than stdin |
| `AGENTS_LIVE_OUTPUT` | Always, under Agents Live | Write the result where stdout cannot corrupt it |
| `AGENTS_LIVE_CONTROL` | Always, under Agents Live | Skip the run |
| `AGENTS_LIVE_LOG` | Always, under Agents Live | Emit structured records |
| `AGENTS_LIVE_STORE` | Always, under Agents Live | Share values with other steps |
| `TRACEPARENT` | Always, under Agents Live | Parent its spans to the step |

`AGENTS_LIVE_CONTRACT` is the single detection rule. Note what is absent: agent
name, origin, changed files, and options are all in the envelope. The
environment announces capabilities; the envelope carries data. That separation
is why an envelope replays and produces an identical run.

## Adopting an existing program

Given a script that already works:

```text
uv run sweep_email.py --dry-run --account team-inbox
```

Adoption is one line in the definition, holding the command line you already
type. The program does not change, and neither does anything it already does
with stdout, because stdout is still just printing.

```yaml
agents-live.pre-processor: "scripts/sweep_email.py --account ${account} ${dry_run}"
```

It gains four things when it wants them, and nothing breaks if it never does:
the envelope, the log sink, the store, and the ability to skip the run.

## Developing against it

```text
agents-live envelope email-agent --role pre > pre.json
uv run scripts/sweep_email.py --dry-run --account team-inbox < pre.json
```

A processor is debugged in a normal editor, against a captured input, with a
normal debugger, without a model, a scheduler, or a watcher anywhere in the
loop. Add `--keep-store` to a real run and the store directory survives for
inspection alongside the envelope and the per-step log.

## The optional helper

Reading an envelope, defaulting its fields, writing the result and control
files, and appending log records is about sixty lines that nobody should write
twice.

```python
# /// script
# dependencies = ["agents-live-processor>=1"]
# ///
from agents_live_processor import context

ctx = context()                        # standalone-safe; every field defaulted

if not ctx.options.dry_run:
    send(to=ctx.options.account)

ctx.log("swept", threads=42)           # sink under dispatch, stderr standalone
ctx.store.put("/out/summary", data)
ctx.emit({"threads": 42})              # result file, or stdout standalone
```

Publishing it as its own small package, rather than vendoring a copy into each
skill, keeps one version of the logic and lets a PEP 723 header resolve it on
both paths. Nothing imports `agents_live`, so no internal module path is frozen
and the accepted no-Python-API decision holds.

The alternative, a copy written into the skill by `agents-live create`, avoids
a second release artifact at the cost of drift: a fix never reaches skills
already created. This is an open decision.

Helpers for other languages are worth adding when a real processor asks for
one. The raw contract is small enough to implement inline in any of them.

## What this changes

| | Contract 1 | Contract 2 |
|---|---|---|
| Arguments | None, ever | The command line the definition wrote |
| Input | Closed for pre, mode-dependent for post | The envelope, identically everywhere |
| Result | Whole stdout | The result file |
| Control | Top-level `skip` key in the payload | Its own file |
| Stdout | Payload, log, and control at once | Human narration |
| Log sink | Shared file, forgeable identity | Per-step file, host-stamped |
| Changed files | `AGENTS_LIVE_CHANGED_FILES` | `repository.changed_files` |
| Invocation instructions | Unavailable to a processor | `instructions` |
| Shared state | MCP over HTTP, pipeline only | A directory, every mode |
| Mode | Changes what a processor receives | Changes only what the model may do |
| Standalone use | Not possible | The default case |

## Migration

Clean break, gated by the definition schema version.
`agents-live.schema-version: "1"` keeps contract 1; `"2"` selects this one. No
definition runs both and there is no per-field shim. Two live dispatch paths
are themselves a burden, so contract 1 leaves in 7.0, on the same removal train
as `legacy/`.

`agents-live definition migrate` raises the version and adds an empty options
map. It cannot rewrite a processor's expectations, so the migration note names
what needs human review: a post-processor that reads a bare value from stdin, a
pre-processor that emits a bare `{"skip": true}`, and any processor that prints
its payload rather than writing it.

## Sequencing against #105

#105 changes the log record across all canonical writers. This contract adds a
new canonical writer, so proceeding independently would collide. The split is
deliberate: this contract owns the channel, its isolation, and its ownership
rules, and #105 owns the record, its span fields, and its sensitivity
projection.

Three defects in the current logging path should be fixed as part of that work,
whichever lands first:

- a processor record carrying `phase: "done"` and `status: "error"` is counted
  as a run failure by the health path;
- `status` is rewritten from `success` to `ok` by the query view but not by the
  health reader, so one written value means two different things depending on
  which reader sees it; and
- every writer appends to one shared file.

## Open questions

- Option types in the first version: `bool` and `string` only, or `int` and
  `enum` as well.
- The helper: a published package, or a copy written into the skill.
- Whether a processor may declare required capabilities, so an incompatible
  definition fails at load rather than mid-run.
- Whether `agents-live envelope` is its own command, as shown, or a flag on
  `run`.
- Whether a non-zero exit always means failure. A program whose job is to
  report findings, such as an audit or a linter, conventionally exits non-zero
  when it finds something, and that is data rather than a fault.
- Whether a program that never writes the result file, but prints one JSON
  value to stdout, should have that taken as its result. It would make
  adoption require no code change at all, at the cost of making stdout
  half-payload again.
