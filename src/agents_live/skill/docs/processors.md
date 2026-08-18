---
title: Writing a processor
description: The contract between Agents Live and a pre- or post-processor
ms.date: 2026-08-18
ms.topic: reference
---

# Writing a processor

Selected by `agents-live.schema-version: "2"`. Definitions still on schema
version 1 use the earlier contract, which is removed in 7.0.

A processor is a filter that runs around the model:

```text
pre-processor -> model -> post-processor
```

Any of the three may be absent. A definition with no selector and both
processors is a deterministic pipeline with no model in it.

Input arrives on stdin, the value leaves on stdout, diagnostics go to stderr,
and the exit code is the verdict. That is what every Unix filter does, and a
processor is one. A program that already behaves that way is already a
processor.

## Invocation

The definition names the program and the command line it wants:

```yaml
metadata:
  agents-live.schema-version: "2"
  agents-live.options: '{"account": "string", "dry_run": "bool=false"}'
  agents-live.pre-processor: "scripts/email_audit.py [--account ${account}] ${dry_run}"
  agents-live.post-processor: "scripts/send.py --account ${account}"
```

Three rules govern the command line, and there are no others:

| Form | Expands to |
|---|---|
| `${name}` | The option's value, as one argument, whatever spaces it contains |
| `${name}` where the option is a boolean | `--name` when true, nothing when false |
| `[ ... ]` | The bracketed fragment, or nothing at all if any `${name}` inside it has no value |

An option declared with a default always has a value. An option declared
without one may be absent, and the brackets are how a command line stays valid
when it is:

```text
scripts/email_audit.py [--account ${account}] ${dry_run}
```

With `account` supplied, the program runs with `--account team-inbox`. Without
it, the whole fragment disappears and the program runs with no account flag at
all, which is what an optional flag means.

Referencing an absent option outside brackets is an error, raised before the
program is spawned rather than passed through as a dangling `--account` with
nothing after it.

Agents Live appends nothing of its own, so a strict argument parser never sees
an argument it does not know. A processor that takes no arguments is named with
no arguments.

The two processors write their own command lines, so they may take different
flags, or different spellings of the same value. Nothing has to agree except
the option names the definition itself declared.

**Precedence** for an option's value, lowest to highest: the default in
`agents-live.options`, then the environment, then the flag on `agents-live run`.

The interpreter is chosen by extension: `.py` through `uv run`, `.js` and `.ts`
through `node`, `.ps1` through `pwsh -NoProfile -File`, anything else executed
directly. The working directory is the repository root.

## Input

Stdin carries one JSON object. `AGENTS_LIVE_INPUT` names a file holding the
same bytes, for a program that would rather read a path. The shape is identical
for both roles in every execution mode:

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
  "options": {"account": "team-inbox", "dry_run": true},
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

`text` is always the previous step's output as written. `path` addresses the
same bytes in the store: the step's captured output, or the declared
`agents-live.result-path` when the model published there instead of returning
text. A post-processor can therefore find the model's result without hard-coding
the path the prompt told the model to write to. `value` is populated only where
Agents Live already had a reason to parse, meaning the definition declared
`agents-live.output-schema` or the model published a JSON value into the store.
Otherwise it is null and the post-processor parses `text` itself, or does not.

Rules:

- `contract` is the version. Fields are additive within a version; a removal or
  a meaning change is a new version.
- Every field has a defined empty value. `changed_files` is `[]`,
  `instructions` is `""`, and `options` holds every declared option with `null`
  for any that is absent. A processor never needs a presence check.
- `options` repeats what the flags carried, so a shell program can read the
  envelope with `jq` instead of parsing arguments.
- Empty stdin is legal. Run by hand with nothing piped in, the program behaves
  as if every field held its empty value.

## Output

Whatever the program writes to stdout is the result. Agents Live does not parse
it, validate it, or require it to be JSON.

For a pre-processor it is handed to the model verbatim, under a label, the way
an attachment is. For a post-processor it becomes the run's result.

```python
print(json.dumps({"findings": findings}))
```

A program with output too large or too binary for a pipe, or one whose stdout
is already spoken for, may write the file at `AGENTS_LIVE_OUTPUT` instead. When
it does, its stdout is treated as diagnostics. Most programs never need this.

Two things are shape-checked, and neither is a processor's output:
`agents-live.output-schema` validates what the model returns, and a bound
`$schema` validates what the model writes into the store. Both guard the
untrusted participant. A processor is repository code.

## Control

To end the run early, write the file at `AGENTS_LIVE_CONTROL`:

```python
Path(os.environ["AGENTS_LIVE_CONTROL"]).write_text(json.dumps({"skip": True}))
```

| Field | Type | Meaning |
|---|---|---|
| `skip` | boolean | End the run now, successfully, without running later steps |
| `message` | string | One short line for the run record |

Control never travels on stdout, so no value a processor emits can be mistaken
for an instruction.

## Failure

A non-zero exit fails the run, and stderr becomes the recorded message.
Exceeding the step timeout fails it as well. Processors are not retried; only
the model step is, once on timeout and twice on empty output.

The step timeout is `agents-live.timeout` if declared, otherwise 120 seconds,
applied to each step separately.

## Logging

`AGENTS_LIVE_LOG` names a JSONL file that only this step writes. The host
validates it, stamps identity onto every row, caps it, and merges it into the
agent log when the step exits.

```json
{"ts": "2026-08-18T09:12:04.331Z", "level": "info", "message": "swept 42 threads", "phase": "collect", "duration_s": 4.1}
```

- **Yours to write**: `ts`, `level`, `message`, `phase`, `duration_s`, the cost
  columns `cost_usd`, `credits`, and `premium_requests`, and any field of your
  own. A processor that calls a paid API has a real reason to report spend.
- **Stamped by the host**, and not yours to set: `run_id`, `agent_name`,
  `agent_id`, `step`, and span identity.
- **Reserved**: a processor may not write `phase: "done"` nor a run-outcome
  status. Those decide whether an agent is healthy. A log row is an
  observation; the verdict is the exit code.
- **Correlation** arrives as `TRACEPARENT`. Ignore it and your rows are still
  correlated, because the host stamps the step span.
- **Sensitivity defaults closed.** Rows are local-only unless explicitly marked
  as exportable operational metadata.
- **A malformed row is quarantined, never fatal.** Overflowing the cap
  truncates and records a marker.
- **Standalone**, with no sink named, the same records go to stderr.

## Shared state

`AGENTS_LIVE_STORE` names a run-scoped directory of JSON values. A path
addresses a file, so `/in/messages` is `$AGENTS_LIVE_STORE/in/messages.json`.
Read and write it with ordinary file operations, in every mode.

The store is how the three participants pass work along. The usual pipeline
shape is that a pre-processor publishes inputs, the model reads them and
publishes a result, and a post-processor reads that result and applies it.

It is ephemeral: created for one run, removed when the run ends. `--keep-store`
preserves it for debugging. Values the definition seeds through fenced `put`
blocks are written before the first step and are read-only thereafter, which is
where an output schema belongs so the model cannot rebind the schema it is
validated against.

A value the model will read is often split into numbered chunks with a
manifest, because one tool call is a poor way to move a large document. That is
an authoring choice about how the model reads, not about how the value is
stored.

## Execution mode

Mode decides what the model may do. It does not change what a processor
receives, or how a processor reaches the store.

|  | `plan`, `write` | `pipeline` |
|---|---|---|
| Model tools | Provider tools per policy | Store tools only |
| Store exposed to the model | No | Yes, as `get` and `put` |
| Seeded paths | Not applicable | Frozen against the model |
| What a processor receives | Envelope, store, log sink | Identical |

In pipeline mode the model becomes a third participant in the store. Agents
Live starts an ephemeral in-process MCP server in front of the same directory
and gives the model `get` and `put` as its only tools, validating writes
against a bound `$schema` and refusing writes to seeded paths. The server
exists so the model's access can be constrained and mediated, not because the
store is remote: processors keep reading and writing the directory directly.

A post-processor does not need to know which of those happened. Its envelope
names the previous step's result path either way.

## Environment

| Variable | Present when | Holds |
|---|---|---|
| `AGENTS_LIVE_CONTRACT` | Running under Agents Live | The contract version, and the signal that the rest of this table exists |
| `AGENTS_LIVE_INPUT` | Always, under Agents Live | The envelope, as a file |
| `AGENTS_LIVE_OUTPUT` | Always, under Agents Live | Where to write a result too large for stdout |
| `AGENTS_LIVE_CONTROL` | Always, under Agents Live | Where to write `skip` |
| `AGENTS_LIVE_LOG` | Always, under Agents Live | The JSONL sink for this step |
| `AGENTS_LIVE_STORE` | Always, under Agents Live | The run-scoped store directory |
| `TRACEPARENT` | Always, under Agents Live | The step span, as a parent |

`AGENTS_LIVE_CONTRACT` is the single detection rule. Note what is absent: agent
name, origin, changed files, and options are all in the envelope. The
environment announces capabilities, the envelope carries data, and that is why
an envelope replays and produces an identical run.

Anything in `agents-live.env` is added as well, and the `AGENTS_LIVE_`
variables are written last, so a definition cannot override them.

## Running one by hand

```text
agents-live envelope email-agent --role pre > pre.json
uv run scripts/email_audit.py --account team-inbox < pre.json
```

Nothing about the program requires Agents Live to be running. Add
`--keep-store` to a real run and the store directory survives for inspection
alongside the envelope and the step's log.

## The optional helper

Reading the envelope, defaulting its fields, writing control, and appending log
records is a small amount of boilerplate:

```python
# /// script
# dependencies = ["agents-live-processor>=1"]
# ///
from agents_live_processor import context

ctx = context()                        # standalone-safe; every field defaulted

if not ctx.options.dry_run:
    send(to=ctx.options.account)

ctx.log("swept", threads=42)
ctx.store.put("/out/summary", data)
ctx.emit({"threads": 42})
```

Nothing imports `agents_live`, so no internal module path is frozen. The raw
contract is small enough to use inline in any language.

## Open decisions

Tracked in [#373](https://github.com/johnshew/agents-live/issues/373), which
also records the reasoning behind everything above.

- Whether a non-zero exit always means failure. A program whose job is to
  report findings, such as an audit or a linter, conventionally exits non-zero
  when it finds something.
- Whether the helper is a published package, as shown, or a copy written into
  the skill.
- Option types beyond `bool` and `string`.
- Whether a processor may declare the capabilities it requires, so an
  incompatible definition fails at load rather than mid-run.
