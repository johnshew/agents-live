---
title: Writing a processor
description: The contract between Agents Live and a pre- or post-processor
ms.date: 2026-08-18
ms.topic: reference
---

# Writing a processor

Selected by `agents-live.schema-version: "2"`. Definitions still on schema
version 1 use the earlier contract, which is removed in 7.0.

A processor is a filter. The run is a pipeline:

```text
pre-processor | model | post-processor
```

Any of the three may be absent. A definition with no selector and both
processors is a deterministic pipeline with no model in it.

The contract has three stages, and each one is optional. A program written for
stage one keeps working unchanged when the agent moves to stage three.

| Stage | The program | What it uses |
|---|---|---|
| 1 | A plain filter | stdin, stdout, stderr, exit code |
| 2 | Knows when Agents Live invoked it | Run context in the environment, the log sink, control |
| 3 | Takes part in a validated pipeline | The pipeline MCP and its schemas |

## Stage 1: a plain filter

Nothing here is specific to Agents Live. Read stdin, do the work, write the
value to stdout, put diagnostics on stderr, and exit non-zero if it went wrong.

**A pre-processor** is handed nothing on stdin. What it writes to stdout is
given to the model verbatim, under a label, the way an attachment is.

**A post-processor** is handed the model's answer on stdin: the value extracted
from the reply, not the reply itself, because a provider wraps its answer in
prose and a session footer. What it writes to stdout becomes the run's result.

```python
findings = audit(account)
print(json.dumps({"findings": findings}))
```

Agents Live does not parse, validate, or reshape what a processor writes. JSON
is the usual choice because the next reader is a model or another program, but
a Markdown table is equally valid if that is what the model should see.

The definition says how to run it, using the command line you already type:

```yaml
metadata:
  agents-live.schema-version: "2"
  agents-live.pre-processor: "scripts/email_audit.py [--account ${account}] [${dry_run}]"
  agents-live.post-processor: "scripts/apply.py"
```

The values come from the invocation, and nothing declares them in advance:

```text
agents-live run email-audit -o account=team-inbox -o dry-run
```

That is the whole of stage one. The same program still runs by hand:

```text
uv run scripts/email_audit.py --account team-inbox
uv run scripts/email_audit.py --account team-inbox | uv run scripts/apply.py
```

## Stage 2: knowing you are under Agents Live

Sooner or later the program wants the things only the run knows: which files
changed, what the invocation asked for, where to put structured log records,
and how to say there is nothing to do.

Every one of these is announced by an environment variable, and every one is
absent when the program runs by hand. `AGENTS_LIVE_CONTRACT` is the single
detection rule.

```python
under_agents_live = "AGENTS_LIVE_CONTRACT" in os.environ
```

### Run context

Everything the run knows arrives in the environment. There is no file to open
and no envelope to parse: a scalar is a plain value, and a collection is JSON,
which is the convention `AGENTS_LIVE_CHANGED_FILES` already follows.

| Variable | Form | Holds |
|---|---|---|
| `AGENTS_LIVE_CONTRACT` | scalar | `2` |
| `AGENTS_LIVE_ROLE` | scalar | `pre` or `post` |
| `AGENTS_LIVE_AGENT_NAME` | scalar | The definition's name |
| `AGENTS_LIVE_AGENT_ID` | scalar | The stable identifier used in logs and state |
| `AGENTS_LIVE_RUN_ID` | scalar | This run |
| `AGENTS_LIVE_ORIGIN` | scalar | `clock`, `boot`, `watch`, or `manual` |
| `AGENTS_LIVE_ATTEMPT` | scalar | Which attempt this is |
| `AGENTS_LIVE_REPO_ROOT` | scalar | The repository root, which is also the working directory |
| `AGENTS_LIVE_INSTRUCTIONS` | scalar | What the invocation asked for, or empty |
| `AGENTS_LIVE_CHANGED_FILES` | JSON array | Repository-relative paths, or `[]` |
| `AGENTS_LIVE_OPTIONS` | JSON object | The options this invocation supplied |

So reading one value costs one line, in any language:

```python
options = json.loads(os.environ.get("AGENTS_LIVE_OPTIONS", "{}"))
```

```bash
account=$(jq -r .account <<< "$AGENTS_LIVE_OPTIONS")
```

Values in `AGENTS_LIVE_OPTIONS` carry the same distinction the command line
makes: an option supplied as a bare `-o dry-run` is `true`, one supplied as
`-o account=team-inbox` is the string, and one not supplied at all is absent
rather than null.

Two of these can grow without bound, so `AGENTS_LIVE_INSTRUCTIONS` and
`AGENTS_LIVE_CHANGED_FILES` are capped before the program is spawned, against
the same host limit that bounds the command line.

### Structured logs

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
- **Reserved**: never write `phase: "done"` nor a run-outcome status. Those
  decide whether an agent is healthy. A log row is an observation; the verdict
  is the exit code.
- **Correlation** arrives as `TRACEPARENT`. Ignore it and rows are still
  correlated, because the host stamps the step span.
- **Sensitivity defaults closed.** Rows are local-only unless explicitly marked
  as exportable operational metadata.
- **A malformed row is quarantined, never fatal.** Overflowing the cap
  truncates and records a marker.
- With no sink named, write the same records to stderr.

### Ending the run early

Write `AGENTS_LIVE_CONTROL` to stop before the later steps run:

```python
Path(os.environ["AGENTS_LIVE_CONTROL"]).write_text(json.dumps({"skip": True}))
```

| Field | Type | Meaning |
|---|---|---|
| `skip` | boolean | End the run now, successfully, without running later steps |
| `message` | string | One short line for the run record |

Control has its own file so that nothing a processor writes to stdout can be
mistaken for an instruction. That matters most for a post-processor, whose
stdin is literal model output.

For a scheduled audit that usually finds nothing, skipping is also most of the
cost.

## Stage 3: taking part in the pipeline

Stage 3 is where you stop trusting the model with anything a program could
check instead.

In `mode: pipeline` the model loses every tool except `get` and `put` against a
run-scoped MCP server that exists only for the duration of the run. Four
properties follow, and together they are the reason to be here.

**It sees only what you published.** The model cannot read the repository, so
it cannot wander into a credential, an unrelated document, or a file that
happens to contain instructions aimed at it. A pre-processor decides what goes
in, which means the injection surface is a set you curated rather than whatever
the filesystem holds today.

**It cannot act.** No file writes, no shell, no network tools. Every effect on
the world happens afterwards, in a program you wrote, that you can read and
test.

**Its output is checked before anything acts on it.** A schema bound at
`<path>/$schema` validates every `put` at the moment of writing, so a
malformed or hostile value is refused at the boundary rather than reaching code
that edits files. Because the schema is seeded from the definition and frozen,
the model cannot widen its own contract.

**What must be correct is computed, not asserted.** See
[Ask for decisions, not content](#ask-for-decisions-not-content).

Taken together the model becomes a pure function from curated input to a
checked value, sandwiched between two deterministic programs. That is what
makes an unattended agent that edits real files reviewable, testable without a
model, and safe to leave running.

That is the only reason to be here. In particular, do not move to stage 3
because your material got large: see [Size](#size), which is the host's problem
rather than yours.

A processor joins the pipeline by connecting to the same server:

```text
PIPELINE_MCP_URL      the loopback URL, present only in pipeline mode
PIPELINE_MCP_TOKEN    the bearer token for it
```

### Publishing inputs

A pre-processor publishes what the model is allowed to see:

```python
await session.call_tool("put", {"path": "/input/recommendations", "value": text})
```

A large document is usually split into numbered values with a manifest, because
a provider caps how much tool output it will hand the model at once. Copilot
CLI's limit is 20 KiB by default, which is why published documents are
typically chunked at a few thousand characters rather than at any size that
feels tidy:

```text
/input/recommendations/manifest   {"chunks": ["/input/recommendations/0", ...]}
/input/recommendations/0          the first 4000 characters
```

The manifest exists because the model cannot list the store. It can only fetch
a path it has been told about.

### Constraining the output

Bind a schema by putting it at `<path>/$schema`, and the server validates every
`put` to that path against it, using JSON Schema Draft 2020-12.

Seed it from the definition body rather than from a processor, with a fenced
`put` block:

````markdown
```put /output/judgment/$schema
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["patch"]
}
```
````

Seeded paths are written before the first step and are frozen for the rest of
the run. That is what stops the model from publishing a permissive schema of
its own and then validating itself against it.

### Ask for decisions, not content

The schema says what shape the answer takes. This says how much of the answer
to ask for, and it is the difference between an agent you can trust and one you
have to re-read every morning.

Have the model return the judgement and keep the document. Identifiers,
orderings, verdicts, and short rationales, not a rewritten file:

```json
{
  "action": "update",
  "patch": {
    "omit": {"main": ["Bicep Curl"]},
    "reorder": {"main": ["Farmer Carry", "Hip Abd (Push)"]}
  },
  "summary": "Removed Bicep Curl; reordered main."
}
```

The post-processor holds the real file, applies the patch, and writes it. Four
things come from that.

**Rules the model cannot break, because it never touches them.** A completed
item cannot be silently dropped if removal requests are checked against the
file. Sections the model was not asked about pass through untouched rather than
surviving a rewrite intact by luck.

**Derived values stay derived.** Anything computable, a budget, a total, a
safety check, is recomputed by the post-processor from the final state. A model
asserting a number it did not compute is a defect waiting for a quiet morning.

**A claim check becomes possible.** With decisions in hand you can compare what
the model said it did against what the patch actually does, and record the
difference.

**No-change is cheap and explicit.** A verdict of "the plan is sound" is two
lines, and it means the same thing every time.

A large document goes in and a few hundred bytes of decisions come out, which
is pleasant but not the point. Ask for decisions because they are checkable,
not because they are small.

### Reading the result

Declare where the run's result lives:

```yaml
agents-live.result-path: "/output/judgment"
```

Agents Live snapshots that path when the model finishes and pipes it to the
post-processor's stdin. **A stage 1 post-processor therefore keeps working
after the move to pipeline mode**, because it still reads its input from stdin.
A post-processor that wants more than the result can `get` any other path
itself.

### Keeping the command line

Stage 3 does not cost you stage 1. Publish to the MCP when it is there and to
stdout when it is not:

```python
if os.environ.get("PIPELINE_MCP_URL"):
    publish(values)                 # put each path
else:
    print(json.dumps(values))       # still a filter
```

The program remains runnable by hand, testable without a model, and pipeable
into the post-processor.

## Reference

### The command line

Two rules govern the template, and there are no others:

| Form | Expands to |
|---|---|
| `${name}` | The option, as exactly one argument |
| `[ ... ]` | The bracketed fragment, or nothing at all if any `${name}` inside it was not supplied |

What a `${name}` becomes is decided by how the option was supplied, not by
anything the definition declares:

| Supplied as | `${name}` becomes |
|---|---|
| `-o account=team-inbox` | `team-inbox`, whatever spaces it contains |
| `-o dry-run` | `--dry-run` |
| `-o account=` | One empty argument |
| Not supplied | Nothing, and the fragment around it drops |

The presence of `=` is the whole distinction. A bare `-o dry-run` is for a
program that takes `--dry-run` with no argument; `-o account=team-inbox` is for
one that takes a value. The name is used verbatim, so `-o dry-run` produces
`--dry-run` and `-o dry_run` produces `--dry_run`. Spell the option the way the
program spells it.

Brackets are how a command line stays valid when an option is absent: with
`account` supplied the program runs with `--account team-inbox`, and without it
the flag and its value disappear together.

Two mistakes are caught before the program is spawned. A `${name}` outside
brackets that was not supplied is an error rather than a dangling `--account`
with nothing after it. And an option supplied that the template never mentions
is reported, because it is almost always a misspelled name that would otherwise
drop its fragment in silence.

**Defaults belong in the program, not in the definition.** A processor has to
work when you run it by hand, so `--account` falling back to the team inbox is
the argument parser's job. The definition says what this agent passes, not what
the program means. A scheduled or watched run passes whatever was recorded with
its trigger.

Agents Live appends nothing of its own, so a strict argument parser never sees
an argument it does not know. The two processors write their own command lines,
so they may take different flags, or different spellings of the same value.
Substitution builds an argument list directly and never a shell string, so a
value containing spaces, quotes, or a semicolon is one argument and can inject
nothing.

The interpreter is chosen by extension: `.py` through `uv run`, `.js` and `.ts`
through `node`, `.ps1` through `pwsh -NoProfile -File`, anything else executed
directly. The working directory is the repository root.

### Streams

| Stream | Pre-processor | Post-processor |
|---|---|---|
| stdin | Nothing | The value extracted from the model's reply, or the snapshot of `result-path` in pipeline mode |
| stdout | Given to the model verbatim | Becomes the run's result |
| stderr | Diagnostics; the recorded message on failure | Same |

A program with output too large or too binary for a pipe may write the file at
`AGENTS_LIVE_OUTPUT` instead, in which case its stdout is treated as
diagnostics. Most programs never need this.

### Size

Write what you have. Agents Live is responsible for getting it to the model,
and a processor should never choose a design because of a byte ceiling.

The ceiling is real but it belongs to process spawning, not to this contract.
Windows caps a command line at 32767 characters, roughly 64 times smaller than
a typical Linux `ARG_MAX`, so a prompt passed as an argument is the one handoff
with a hard limit. Agents Live delivers the prompt by whatever route the
provider supports for large input, which for Claude Code is stdin, capped at
10 MB, and for Copilot CLI is stdin when `-p` is omitted.

Everything else already streams: stdin, stdout, and the log sink are pipes or
files with no practical ceiling. Environment values are bounded, so
`AGENTS_LIVE_INSTRUCTIONS` and `AGENTS_LIVE_CHANGED_FILES` are capped before
spawn, and anything that outgrows the environment is handed over as a file path
instead.

**If you do hand the model a path**, which is reasonable when the material is
already a file on disk, two things have to be true. Write it outside the
repository, because a file created inside a watched tree can re-trigger the
watcher that started the run. And make sure the model is allowed to read it:
the model is confined to directories the run granted, so a path in the system
temporary directory is readable under Copilot by default and not under Claude.
Declare the directory in the definition rather than hoping. Under a
non-interactive run there is nobody to approve a read, so an ungranted path
fails rather than prompts.

The model's own output is bounded separately by
`agents-live.output-max-bytes`, 10 MB by default, and what lands in the run
record is truncated because a log line is not the artifact.

Moving prompt delivery off the command line is tracked in
[#374](https://github.com/johnshew/agents-live/issues/374).

### Environment

Run context is listed under stage 2 above. The rest of what Agents Live sets:

| Variable | Present when | Holds |
|---|---|---|
| `AGENTS_LIVE_OUTPUT` | Under Agents Live | Where to write a result too large for stdout |
| `AGENTS_LIVE_CONTROL` | Under Agents Live | Where to write `skip` |
| `AGENTS_LIVE_LOG` | Under Agents Live | The JSONL sink for this step |
| `TRACEPARENT` | Under Agents Live | The step span, as a parent |
| `PIPELINE_MCP_URL`, `PIPELINE_MCP_TOKEN` | `mode: pipeline` | The run-scoped pipeline MCP |

Anything in `agents-live.env` is added as well, and the `AGENTS_LIVE_`
variables are written last, so a definition cannot override them.

Everything a program needs is therefore either an argument or an environment
variable, which is what makes a step reproducible by hand: `agents-live context
<agent> --role pre` prints the assignments and the command line for one step,
ready to paste into a shell.

### Failure

A non-zero exit fails the run, and stderr becomes the recorded message.
Exceeding the step timeout fails it as well. Processors are not retried; only
the model step is, once on timeout and twice on empty output.

The step timeout is `agents-live.timeout` if declared, otherwise 120 seconds,
applied to each step separately.

### What execution mode changes

Mode decides what the model may do. It does not change how a processor is
invoked or what it reads and writes.

|  | `plan`, `write` | `pipeline` |
|---|---|---|
| Model tools | Provider tools per policy | `get` and `put` only |
| Model reads the repository | Yes, per policy | No |
| Model output | Returned as text, optionally schema-checked | Published to a path, schema-checked on write |
| Post-processor stdin | The model's output | The snapshot of `result-path` |

In `plan` mode the same rigor is available without the MCP:
`agents-live.output-schema` validates the value extracted from the model's
reply, and `agents-live.output-path-roots` rejects any `path` in it that
escapes the directories you named. The model proposes; your post-processor
disposes.

Both are tool policy and deterministic mediation, not an operating system
sandbox. Processors run with the local account's permissions, and what else a
provider may load from the repository during a run is being settled in
[#375](https://github.com/johnshew/agents-live/issues/375).

## Open decisions

Tracked in [#373](https://github.com/johnshew/agents-live/issues/373), which
also records the reasoning behind everything above.

- Whether a non-zero exit always means failure. A program whose job is to
  report findings, such as an audit or a linter, conventionally exits non-zero
  when it finds something.
- Whether a helper library ships to remove the MCP client boilerplate at stage
  three, and if so whether it is a published package or a copy in the skill.
- Whether `agents-live.result-path` should be available outside pipeline mode.
