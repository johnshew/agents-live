---
title: Processor contract, second design
description: A first-principles contract for pre- and post-processors that keeps them ordinary command-line programs
ms.date: 2026-08-18
ms.topic: concept
---

# Processor contract, second design

## Status

Proposed. The contract that ships today is described in
[processors.md](../src/agents_live/skill/docs/processors.md); this document
designs its replacement and does not describe current behavior.

## The problem with the current contract

Today a processor is a program that only Agents Live can run. It receives no
arguments, so its inputs cannot be expressed on a command line. Its stdin
means different things in different execution modes. Its stdout is
simultaneously a payload, a log, and a control channel, where a top-level
`skip` key cancels the run whether or not that was the author's intent.

The consequence is that a useful script has to be rewritten to become a
processor, and once rewritten it can no longer be run by hand. That is
backwards. The valuable part of a processor is domain work: sweeping a
mailbox, formatting Markdown, filing a report. Domain work has a natural
command-line shape, and it should keep it.

## Design principles

**1. A processor is an ordinary program.** It has flags, a `--help`, and an
exit code. It is useful without Agents Live and testable without Agents Live.
The system adapts to the program, not the other way round.

**2. Agents Live only ever passes flags the definition declared.** Nothing is
injected onto the command line that the author did not ask for, so a strict
argument parser never sees an argument it does not know.

**3. Everything else arrives as one document.** Context that has no natural
flag, such as changed files, run identity, upstream output, and invocation
instructions, arrives as a single JSON envelope on stdin. One shape, in every
mode, for both roles.

**4. Capabilities are optional and announced.** The side channel, the log
sink, and the envelope itself are each announced by an environment variable. A
processor that finds none of them still runs. This is what makes one file
usable in three settings: by hand, in a pipeline, and under a scheduler.

**5. Control is explicit, never inferred.** A processor cancels a run by
saying so under a reserved key. No payload can accidentally mean "skip".

**6. Nothing has to be imported.** The contract is bytes on a stream and names
in the environment, so `.py`, `.js`, `.ts`, `.ps1`, and `.sh` are equal. A
convenience helper exists, is optional, and is copied into the skill rather
than imported from the installed package.

## The contract

### Declaring the program

```yaml
metadata:
  agents-live.schema-version: "2"
  agents-live.pre-processor: "scripts/sweep_email.py"
  agents-live.post-processor: "scripts/send.py"
  agents-live.options: '{"dry_run": "bool=false", "account": "string"}'
```

`agents-live.options` declares the flags this agent accepts, as a JSON string
map of name to type with an optional default. Types are `bool`, `string`,
`int`, and `enum[a|b|c]`.

### Invocation

Agents Live runs the program with its declared flags and nothing else:

```text
uv run scripts/sweep_email.py --dry-run --account john@foo.net
```

Name mapping is mechanical: `dry_run` becomes `--dry-run`, a `bool` is present
or absent, everything else takes one value. A declared option that was not
supplied and has a default is passed with its default, so the program sees the
same command line whether the value came from the invocation or the
definition.

A definition that declares no options passes no arguments, which is what makes
adopting an existing zero-argument script a no-op.

The interpreter is still chosen by extension, and the working directory is
still the repository root.

### The envelope

Stdin carries one JSON object. The same shape reaches a pre-processor and a
post-processor, in every execution mode:

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
    "root": "/home/dev/work/notes",
    "changed_files": ["docs/guide.md"]
  },
  "options": {"dry_run": true, "account": "john@foo.net"},
  "instructions": "Focus on the authentication changes",
  "upstream": null
}
```

For a post-processor, `upstream` describes what the previous step produced:

```json
{
  "upstream": {
    "step": "agent",
    "text": "Here is the plan...",
    "value": {"files": [{"path": "docs/guide.md", "content": "..."}]}
  }
}
```

`value` is the structured value if one was extracted, `null` otherwise.
`step` is `agent` or `pre`, naming what actually produced it.

Rules that make this safe to depend on:

- **`contract` is the version.** A processor may assert it. New fields are
  additive within a version; a removal or a meaning change is a new version.
- **Every field has a defined empty value.** `changed_files` is `[]`, not
  absent. `instructions` is `""`. `options` is `{}`. A processor never needs a
  presence check.
- **`options` repeats the flags.** The same values, in whichever form suits
  the language. A Python program parses flags; a shell program reads the
  envelope with `jq`. Neither is second-class.
- **Stdin may be empty.** Run by hand with nothing piped in, the program sees
  no envelope and must behave as if every field held its empty value.

The envelope is also written to the path in `AGENTS_LIVE_ENVELOPE`, which
gives a program that cannot read stdin a second route, and gives a developer a
file to replay.

### The result

Stdout carries the program's output. Two forms are accepted, and the
difference is decided by one rule: control is honoured only when the reserved
key is present.

**Plain form.** Anything that is not an object containing `agents_live` is the
result itself, whether that is JSON or text:

```python
print(json.dumps({"threads": 42}))
```

This keeps a trivial program trivial, and it means no payload can accidentally
carry a control word.

**Control form.** An object containing `agents_live` separates control from
payload:

```python
print(json.dumps({
    "agents_live": {"skip": True, "message": "no unread mail"},
    "result": None,
}))
```

Control fields in contract 2:

| Field | Type | Meaning |
|---|---|---|
| `skip` | bool | End the run now, successfully, without invoking later steps |
| `message` | string | One short line for the run record and `agents-live logs` |

`result` holds the payload. For a pre-processor it becomes the model's
context; for a post-processor it becomes the run's result.

Failure is the exit code, not a field. A non-zero exit fails the run and
stderr becomes the recorded message.

### Capabilities

Each capability is announced. Absence is normal, never an error.

| Variable | Present when | The program may |
|---|---|---|
| `AGENTS_LIVE_CONTRACT` | Running under Agents Live | Assume the envelope and the rest of this table |
| `AGENTS_LIVE_ENVELOPE` | Always, under Agents Live | Read the envelope from a file instead of stdin |
| `AGENTS_LIVE_LOG` | Always, under Agents Live | Append JSONL records that `agents-live logs` reads |
| `AGENTS_LIVE_STORE_URL`, `AGENTS_LIVE_STORE_TOKEN` | The run has a side channel | `get` and `put` values shared across steps |

The single detection rule is `AGENTS_LIVE_CONTRACT`. A program that checks it
can log to stderr when standalone and to the sink when not, without knowing
anything else about the host.

Note what is absent from this table: agent name, agent id, origin, changed
files, and options are all in the envelope, not the environment. The
environment announces capabilities; the envelope carries data. Keeping those
separate is why the envelope can be replayed from a file and produce an
identical run.

## Adopting an existing program

This is the case the design is for. Given a script that already works:

```text
uv run sweep_email.py --dry-run --account john@foo.net
```

Adoption is one metadata line:

```yaml
agents-live.options: '{"dry_run": "bool=false", "account": "string"}'
```

The program is now a pre-processor and its own command line is unchanged. It
gains three things when it wants them, and nothing breaks if it never does:

- it can read the envelope for changed files, run identity, and invocation
  instructions;
- it can append structured records to the log sink; and
- it can publish to the side channel for a later step.

Progress output is the one habit that must change, because stdout is the
payload. Diagnostics move to stderr, which is where they belong in a
command-line program anyway.

## The optional helper

Reading an envelope, defaulting its fields, and speaking to the side channel
is about forty lines that nobody should write twice. `agents-live create`
copies `agents_live_processor.py` into the skill's `scripts/` directory. The
skill owns that file. Nothing imports the installed package, so no internal
module path is ever frozen, and the accepted no-Python-API decision holds.

```python
from agents_live_processor import context

ctx = context()                      # works standalone; every field defaulted

if not ctx.options.dry_run:
    send(to=ctx.options.account)

ctx.log("swept", threads=42)         # JSONL sink, or stderr when standalone
ctx.store.put("/out/summary", data)  # raises a named error if unavailable
ctx.emit({"threads": 42})            # writes the result form to stdout
```

`context()` merges the parsed flags and the envelope, so `ctx.options` is
populated in both settings. Equivalent helpers for other languages are worth
adding only when a real processor asks for one; the raw contract is small
enough to implement inline in any of them.

## Developing against it

The envelope is a file, so the whole run is reproducible without the system:

```text
agents-live run email-agent --save-envelope pre.json --dry-run-dispatch
uv run scripts/sweep_email.py --dry-run --account john@foo.net < pre.json
```

That is the practical payoff of principle 1. A processor can be debugged in a
normal editor, against a captured input, with a normal debugger, without a
model, a scheduler, or a watcher anywhere in the loop.

## What this changes

| | Contract 1 | Contract 2 |
|---|---|---|
| Arguments | None, ever | Declared flags, and only those |
| Pre-processor stdin | Closed | Envelope |
| Post-processor stdin | Extracted value outside pipeline mode, nothing inside it | Envelope, identically in every mode |
| Changed files | `AGENTS_LIVE_CHANGED_FILES` in the environment | `repository.changed_files` in the envelope |
| Invocation instructions | Not available to a processor at all | `instructions` in the envelope |
| Skip | Top-level `skip` key, collides with payload | Reserved `agents_live.skip` |
| Result | Whole stdout | Whole stdout, or `result` in the control form |
| Side channel | `PIPELINE_MCP_URL` in pipeline mode only | Announced when present, in any mode |
| Standalone use | Not possible | The default case |

## Migration

Clean break, gated by the definition schema version. `agents-live.schema-version: "1"`
keeps contract 1 until the next major release; `"2"` selects this contract.
No definition runs both, and there is no per-field compatibility shim.

`agents-live definition migrate` can do the mechanical part: raise the schema
version and add an empty options map. It cannot rewrite a processor's stdin
expectations, so the migration note has to name the two changes that need
human review, which are a post-processor that reads a bare value from stdin,
and a pre-processor that emits a bare `{"skip": true}`.

## Open questions

- Should `int` and `enum` exist in the first version of the option types, or
  only `bool` and `string` until something needs more?
- Should `--save-envelope` be a flag on `run`, or its own `agents-live envelope`
  command that produces one without dispatching?
- Does the side channel keep its MCP shape in contract 2, or become a plain
  HTTP resource for processors, with MCP reserved for the model? A processor
  is not a model and does not need tool semantics to read a value.
- Should a processor be able to declare which capabilities it requires, so a
  definition that cannot supply one fails at load rather than at run?
