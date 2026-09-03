---
title: Writing a processor
description: The contract between Agents Live and a pre- or post-processor
ms.date: 2026-08-30
ms.topic: reference
---

# Writing a processor

Selected by `agents-live.schema-version: "2"`. Definitions still on schema
version 1 use the earlier contract, which is removed in 7.0; see
[Moving from version 1](#moving-from-version-1).

A processor is a filter. The run is a pipeline:

```text
pre-processor | model | post-processor
```

Any of the three may be absent. A definition with no selector and both
processors is a deterministic pipeline with no model in it.

Processors come in three classes. They are kinds, not steps: most programs stay
class 0 forever, and nothing has to pass through class 1 to reach class 2. The
number is how much Agents Live is in the program.

| Class | The program | What it uses |
|---|---|---|
| 0 | Knows nothing about Agents Live | stdin, stdout, stderr, exit code |
| 1 | Agents Live aware | Run context and options in the environment, the log sink, control |
| 2 | Pipeline aware | The pipeline MCP and its schemas |

A class 0 program keeps working unchanged when the agent moves to class 2.

## Class 0: a plain filter

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

The definition names the program, and nothing else:

```yaml
metadata:
  agents-live.schema-version: "2"
  agents-live.pre-processor: "scripts/email_audit.py"
  agents-live.post-processor: "scripts/apply.py"
```

Agents Live adds no arguments of its own, so a strict argument parser never
meets a flag it does not know. The other half of that bargain is that a class 0
processor runs with no arguments at all, so anything it needs has to be its
default. That is the whole of class 0, and the same program still runs by hand:

```text
uv run scripts/email_audit.py --account team-inbox
uv run scripts/email_audit.py --account team-inbox | uv run scripts/apply.py
```

To reproduce what Agents Live would hand one step, inspect it without starting
the processor or provider:

```bash
agents-live context email-audit --role pre
agents-live context email-audit --role post --json
```

The preview names the same command line, working directory, environment, and
ephemeral channel paths as a run, but it does not materialize those paths. A
post-processor's future stdin is unavailable. Pipeline mode is rejected because
its MCP endpoint and credentials exist only while a run is active.

## Class 1: Agents Live aware

A class 0 program is configured entirely by its own defaults, because it
receives no arguments. Class 1 is where that stops being enough, and the
program wants what only this run knows: which files changed, what the
invocation asked for or passed as an option, where to put structured log
records, and how to say there is nothing to do.

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

Values in `AGENTS_LIVE_OPTIONS` record how each option was supplied: a bare
`-o dry-run` is `true`, `-o account=team-inbox` is the string, and an option
not supplied at all is absent rather than null. See
[Taking options from the invocation](#taking-options-from-the-invocation).

`AGENTS_LIVE_INSTRUCTIONS` and `AGENTS_LIVE_OPTIONS` are bounded where they are
supplied, so an oversized invocation fails rather than arriving trimmed. What a
processor reads is therefore exactly what the model was given, never a shorter
version of it.

**A change set too large to pass fails the run** before anything is spawned,
naming the count and the limit. Trimming the list would let a processor loop
over it and skip work it was never told about, with nothing downstream able to
tell. If an agent hits this, the watch pattern is usually wider than the work,
and the alternative is a processor that scans the repository itself rather than
taking a list.

### Taking options from the invocation

An invocation can pass values without anyone editing the definition:

```text
agents-live run email-audit -o account=team-inbox -o dry-run
```

Nothing declares these in advance. The presence of `=` is the whole
distinction, and it is the only thing Agents Live needs to know:

| Supplied as | `AGENTS_LIVE_OPTIONS` holds |
|---|---|
| `-o dry-run` | `true` |
| `-o account=team-inbox` | `"team-inbox"` |
| `-o account=` | `""` |
| Not supplied | The key is absent |

Names are used verbatim, so write the option the way the program spells it.

They arrive in the environment and nowhere else, which is what lets both
processors receive the same set without either of them having to tolerate the
other's flags. Merge them into your own arguments and a program takes exactly
the options it already knows about:

```python
def option_argv():
    argv = []
    for name, value in json.loads(os.environ.get("AGENTS_LIVE_OPTIONS", "{}")).items():
        argv.append(f"--{name}")
        if value is not True:
            argv.append(value)
    return argv

args, unrecognized = parser.parse_known_args(sys.argv[1:] + option_argv())
```

The program now behaves the same under Agents Live as it does by hand, and an
option it does not implement is simply not its business.

**Whether an unrecognized option is fatal is your call, and worth making
deliberately.** Agents Live cannot check a name against a program's interface,
so a misspelled `-o dry-runn` reaches every processor and is recognized by
none. For an option whose absence is merely a missing convenience, ignoring it
is right. For one like `--dry-run`, where silently not applying it is the
expensive outcome, exit non-zero on anything left in `unrecognized`.

### Structured logs

`AGENTS_LIVE_LOG` names a file for this step's structured records, and only
this step writes it. Append one JSON object per line. Agents Live does not
validate it, stamp it, or rewrite it, so the records are exactly as good as the
program that wrote them.

```json
{"ts": "2026-08-18T09:12:04.331Z", "level": "info", "message": "swept 42 threads", "phase": "collect", "duration_s": 4.1}
```

Conventional field names, worth using so a later reader recognizes them: `ts`,
`level`, `message`, `phase`, `duration_s`, and the cost columns `cost_usd`,
`credits`, and `premium_requests`. A processor that calls a paid API has a real
reason to report spend. Add fields of your own freely.

To correlate a row, copy `AGENTS_LIVE_RUN_ID` and `AGENTS_LIVE_AGENT_ID` into
it. Nothing stamps them for you.

Two things not to write. **Never `phase: "done"`, and never a run-outcome
status**, because those are how an agent's health is judged: a log row is an
observation, and the verdict is the exit code. And nothing secret, because the
file is as readable as anything else in the state directory.

With no sink named, write the same records to stderr.

How these records surface in `agents-live logs` is settled separately, in
[#105](https://github.com/johnshew/agents-live/issues/105).

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

## Class 2: pipeline aware

Class 2 is where you stop trusting the model with anything a program could
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

That is the only reason to be here. In particular, do not become class 2
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
post-processor's stdin. **A class 0 post-processor therefore keeps working
after the move to pipeline mode**, because it still reads its input from stdin.
A post-processor that wants more than the result can `get` any other path
itself.

### Keeping the command line

Class 2 does not cost you class 0. Publish to the MCP when it is there and to
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

The definition names the program:

```yaml
agents-live.pre-processor: "scripts/email_audit.py"
```

There is no template, no substitution, and nothing appended. Every value a run
supplies arrives in the environment instead, so the arguments a processor sees
are exactly the ones its own author wrote, and a strict parser never meets a
flag it does not know. What an invocation can pass is under
[Taking options from the invocation](#taking-options-from-the-invocation).

**Defaults belong in the program, not in the definition.** A processor has to
work when you run it by hand, so `--account` falling back to the team inbox is
the argument parser's job. The definition says which program to run, not what
the program means.

The interpreter is chosen by extension: `.py` through `uv run`, `.js` and `.ts`
through `node`, `.ps1` through `pwsh -NoProfile -File`, anything else executed
directly. The working directory is the repository root.

### Streams

| Stream | Pre-processor | Post-processor |
|---|---|---|
| stdin | Nothing | The value extracted from the model's reply, or the snapshot of `result-path` in pipeline mode |
| stdout | Given to the model verbatim | Becomes the run's result |
| stderr | Diagnostics; the recorded message on failure | Same |

A program with output too large or too awkward for a pipe may write the file at
`AGENTS_LIVE_OUTPUT` instead, in which case its stdout is treated as
diagnostics. It is read as UTF-8, so it is a value rather than a blob. Most
programs never need this.

### Size

Write what you have. Agents Live is responsible for getting it to the model,
and a processor should never choose a design because of a byte ceiling.

The ceiling is real but it belongs to process spawning, not to this contract.
Windows caps a command line at 32767 characters, roughly 64 times smaller than
a typical Linux `ARG_MAX`, so a prompt passed as an argument is the one handoff
with a hard limit. Agents Live delivers the prompt by whatever route the
provider supports for large input. Claude Code takes it on stdin, capped at
10 MB, which is what a run uses. Copilot CLI offers no documented stdin or
`--prompt-file` non-interactive contract. Omitting `-p` under the unattended
adapter flags enters its interactive alternate-screen UI rather than returning
the requested JSON stream. Its prompt therefore remains an argument and is
still bounded by the host; Agents Live detects that overflow before spawning
the child and reports the prompt size. The provider gap is tracked in
[#374](https://github.com/johnshew/agents-live/issues/374).

Everything else already streams: stdin, stdout, and the log sink are pipes or
files with no practical ceiling. Environment values are bounded, so an
invocation carrying more instructions, options, or changed paths than one
environment value holds fails before anything is spawned rather than arriving
incomplete.

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

### Environment

Run context is listed under class 1 above. The rest of what Agents Live sets:

| Variable | Present when | Holds |
|---|---|---|
| `AGENTS_LIVE_OUTPUT` | Under Agents Live | Where to write a result too large for stdout |
| `AGENTS_LIVE_CONTROL` | Under Agents Live | Where to write `skip` |
| `AGENTS_LIVE_LOG` | Under Agents Live | The JSONL sink for this step |
| `PIPELINE_MCP_URL`, `PIPELINE_MCP_TOKEN` | `mode: pipeline` | The run-scoped pipeline MCP |

Anything in `agents-live.env` is added as well, and the `AGENTS_LIVE_`
variables are written last, so a definition cannot override them.

Everything a program needs is therefore either an argument or an environment
variable, which is what keeps a step reproducible by hand: set the few
variables it reads and run the program.

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
`agents-live.output-schema` asks Claude to produce a schema-validated value
and then validates that value again before continuing. Copilot has no
supported schema-output option, so its final answer is parsed for JSON and
validated locally. Definitions without a schema also use local extraction
when another output policy needs a JSON value. In every case,
`agents-live.output-path-roots` rejects any `path` that escapes the directories
you named. The model proposes; your post-processor disposes.

Both are tool policy and deterministic mediation, not an operating system
sandbox. Processors run with the local account's permissions, and what else a
provider may load from the repository during a run is being settled in
[#375](https://github.com/johnshew/agents-live/issues/375).

### Moving from version 1

Most of a version 1 processor is already a version 2 processor. Streams, exit
codes, the working directory, the timeout, and the pipeline MCP are unchanged,
a definition still names its processors the same way, and a processor invoked
with no options still receives no arguments.

Two things need editing.

**Skip moved off stdout.** Under version 1 a pre-processor ended the run by
printing `{"skip": true}`. Under version 2 stdout is never parsed, so that line
becomes prompt text and the run continues instead. Write `AGENTS_LIVE_CONTROL`
instead. This is the one change that fails quietly rather than loudly, so it is
worth searching for before you raise the version.

**`AGENTS_LIVE_LOG_FILE` became `AGENTS_LIVE_LOG`**, and it names a different
file. Version 1 pointed at the agent's own log, which every step appended to
directly, so concurrent writers could splice each other's records. Version 2
gives each step a file of its own. Nothing else changes: you still write the
rows, and nothing validates or stamps them. Stop writing `phase: "done"` or a
run status, which are reserved, and copy `AGENTS_LIVE_RUN_ID` into any row you
want correlated.

Two more differences are unlikely to matter but are real.
`AGENTS_LIVE_CHANGED_FILES` is always set, as `[]` when nothing changed, where
version 1 omitted it; reading it with a default works under both, but testing
for its presence to detect a watch run does not, and `AGENTS_LIVE_ORIGIN` says
it properly. And in pipeline mode a post-processor now receives the
`result-path` snapshot on stdin where version 1 gave it nothing, which changes
nothing for a program that ignores stdin.

Nothing runs both contracts. `agents-live.schema-version` selects one.

## Open decisions

Tracked in [#373](https://github.com/johnshew/agents-live/issues/373), which
also records the reasoning behind everything above.

- Whether a non-zero exit always means failure. A program whose job is to
  report findings, such as an audit or a linter, conventionally exits non-zero
  when it finds something.
- Whether a helper library ships to remove the MCP client boilerplate at class
  2, and if so whether it is a published package or a copy in the skill.
- Whether `agents-live.result-path` should be available outside pipeline mode.
