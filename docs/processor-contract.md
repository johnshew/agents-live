---
title: Why the processor contract is shaped this way
description: The reasoning and the rejected alternatives behind the processor specification
ms.date: 2026-08-18
ms.topic: concept
---

# Why the processor contract is shaped this way

The contract itself is specified in
[processors.md](../src/agents_live/skill/docs/processors.md). This file
holds the reasoning: what each choice buys, what it costs, and what was
considered and rejected. It is a record of judgement, not of chronology.

Discussed in [#373](https://github.com/johnshew/agents-live/issues/373).

## The problem being solved

A processor today is a program only Agents Live can run.

It receives no arguments, so its inputs cannot be expressed on a command line.
Its stdin means different things in different execution modes: outside pipeline
mode a post-processor is handed the model's extracted value, and inside it the
stream is closed, so switching mode starves a processor with no error. Its
stdout is at once payload, log, and control channel, where a top-level `skip`
key cancels the run whether or not the author meant it. Its log records go into
a file shared with the runtime, where nothing separates an observation from a
verdict.

The consequence is that a working script must be rewritten to become a
processor, and once rewritten it can no longer be run by hand. The valuable
part of a processor is domain work, and domain work has a natural command-line
shape.

## A processor is a filter

**Decision.** Stdin carries the input, stdout carries the value, stderr carries
diagnostics, and the exit code is the verdict.

**Why.** That is what `jq`, `sort`, and every other filter does. A program that
already writes JSON to stdout is already a processor, so adoption costs nothing.

**Rejected: moving the result to a file handle.** An earlier draft did this and
justified it with the GitHub Actions migration away from stdout control
commands. The precedent does not transfer. An Actions step is a task whose
stdout is genuinely log output, so its structured results needed a side
channel. A processor is a filter whose stdout is the value. Applying the
Actions shape to the payload charged an adoption tax for nothing and forced the
document into the awkward position of telling authors to stop printing their
own output.

`AGENTS_LIVE_OUTPUT` survives as an override for output too large or too binary
for a pipe. It is not the normal path.

## Control and structured logs never travel on stdout

**Decision.** `skip` is a file, log records are a file, and no reserved word
ever appears in a payload.

**Why.** Here the Actions precedent does apply. Actions needed a
`stop-commands` escape hatch precisely because content flowing through stdout
can impersonate control syntax, and the risk is sharper in this system because
a post-processor's stdin is literal model output. Under the current contract a
model that emits `{"skip": true}` through a pass-through processor cancels a
run.

Same reasoning as the section above, opposite conclusion about which channel
carries what, because a filter's stdout is data while a task's stdout is
narration.

## The invocation reaches a processor through the environment, not its argv

**Decision.** The definition names the program and stops:
`agents-live.pre-processor: "scripts/email_audit.py"`. Agents Live adds no
arguments. What an invocation supplies arrives in `AGENTS_LIVE_OPTIONS`, where
the presence of `=` records the shape: `-o dry-run` is `true`, and
`-o account=team-inbox` is the string. A processor that wants options merges
them into its own argument parsing and takes the ones it recognizes.

**Why.** Three designs were tried against this and each bought its machinery
with someone else's money.

A typed declaration map asked the author to state an option's arity when the
person typing `-o` already knew it from the program's `--help`, and it bought
argument checking Agents Live cannot perform, since it does not know the
program's interface.

A `${name}` substitution template then asked for the same information again in
a second syntax, and charged a parser, a quoting rule, and an absent-value rule
for it.

Appending the options to argv removed both, but broke the property that made
the contract cheap to adopt: Agents Live appended nothing, so a strict argument
parser never met a flag it did not know. It also created a routing problem with
no good answer, because a run has one invocation and two processors, and an
unknown flag reaching the post-processor fails the run after the model call has
been paid for.

The environment breaks nothing. Both processors receive the same set without
either having to tolerate the other's flags, a processor that ignores options
is untouched, and options never compete with the prompt for the argv budget.
The routing question dissolves rather than being answered.

**Consequence: options are a stage 2 feature, not stage 1.** Reading them means
knowing you are under Agents Live, which is what stage 2 is. Stage 1 goes back
to being a filter with nothing in it.

**Rejected: a typed options map**, `${name}` substitution with bracketed
optional fragments, and appending options to argv, for the reasons above. Also
**rejected: a sidecar declaration file**, which duplicated knowledge that
already existed, and **a discovery protocol** where Agents Live asks the
program what it accepts, which makes every processor implement an extra entry
point, the exact "rewrite your script" tax this design exists to remove.

**Defaults belong in the program.** A processor must work when run by hand, so
its argument parser already expresses the default. A second one in the
definition would be reachable only under Agents Live, which is the split brain
the filter contract exists to avoid.

**Costs accepted.** A processor that wants options writes a few lines to
consume them, where appending would have given them for free. And a misspelled
`-o dry-runn` reaches every processor and is recognized by none, which Agents
Live cannot catch because it does not know any program's interface. The
strictness decision is therefore documented as the processor's: ignoring an
unrecognized option is right when its absence is a missing convenience, and
exiting non-zero is right for one like `--dry-run`, where silently not applying
it is the expensive outcome. Putting that choice where the knowledge is beats
guessing centrally.

**Not accepted as a cost: per-processor option values.** The same option cannot
be set differently for the pre- and post-processor. Nobody has wanted that.

## Agents Live does not know what a processor returns

**Decision.** The result is opaque. It is not parsed, not validated, and not
required to be JSON.

**Why.** For a pre-processor the next reader is the model, and handing it the
bytes verbatim is the whole job. Requiring a declared output shape asked the
system to understand something it never has to look at.

Exactly two things are shape-checked, and both predate this contract:
`agents-live.output-schema` for what the model returns, and a bound `$schema`
for what the model writes into the store. Both guard the untrusted participant.
A processor is repository code, and validating trusted code's output against a
schema the same author wrote is ceremony.

## No new sharing mechanism: the pipeline MCP is it

**Decision.** Shared state stays exactly what it is today, the run-scoped
in-process MCP with `put`, `get`, `$schema` binding, and frozen seeded paths.
A processor that needs it connects to it, as `exercise-judgment` already does.

**Rejected: a run-scoped directory of JSON files** that processors would read
and write as ordinary files, with the MCP server in front of it for the model.
It looked like it removed a transport, and it did, but it bought that by
inventing a second mechanism for something the system already has, and it
quietly moved the enforcement point. Today the model physically cannot reach
the values except through the server, because they live in another process's
memory. With files, the only thing keeping the model out is the tool policy
that denies it file access, so a future decision to grant a pipeline model one
read tool would silently bypass schema validation and the seeded-path freeze.
A safety property that holds by construction is worth more than one that holds
by policy.

**Cost accepted.** Stage three still costs a processor an MCP client. In
`exercise-judgment` that is `dependencies = ["mcp<2"]`, an `asyncio.run`, a
`sys.path.insert` into a shared `Agents/lib`, and a session helper, to write
JSON values. That is real, and it is the reason a helper library is an open
question rather than an obvious no.

What the contract does instead is make sure nobody pays that cost before they
need to, which is what the staged shape below is for.

## Adoption is staged, and each stage is optional

**Decision.** Three stages. A plain filter; then a program that notices Agents
Live invoked it; then a participant in the validated pipeline. A program
written for stage one keeps working unchanged at stage three.

**Why.** This is the actual life of a useful script. It starts as something
run by hand, becomes worth automating, and eventually matters enough to want
rigor around what the model may see and say. If each of those transitions
demands a rewrite, the rigor gets skipped, which is the worst outcome
available.

Two design consequences follow, and both are load-bearing:

**Stdin carries data, not metadata.** An envelope on stdin would break the
filter shape at stage one, because a filter's stdin is its input. So the run is
literally `pre | model | post`, and a developer can test it with a pipe.

**The result path is snapshotted onto the post-processor's stdin.** In pipeline
mode the model publishes to `agents-live.result-path` rather than returning
text, and Agents Live pipes that snapshot to the post-processor. A stage one
post-processor therefore survives the move to pipeline mode without knowing it
happened. Without this, moving to stage three would force every post-processor
to be rewritten as an MCP client, which is exactly the cliff the staging exists
to remove.

**Size is never a reason to move a stage.** An earlier draft offered size as a
second motivation for stage three, on the grounds that a prompt is a
command-line argument and a published value is not. That was a mistake. It
asked a developer to accept a security model because of a transport limit, and
the transport limit is the host's to fix. See the next section.

## Size is the host's problem, not the developer's

**Decision.** Agents Live delivers the prompt to the provider by whatever
means that provider supports for large input. A processor writes to stdout and
never reasons about a byte ceiling.

**Why.** The ceiling is real but it is entirely a property of process
spawning. Windows caps a command line at 32767 characters through
`CreateProcessW`, roughly 64 times smaller than a typical Linux `ARG_MAX` of
2 MB, and it surfaces as `OSError [WinError 206] The filename or extension is
too long`. Nothing about that belongs in a contract between a program and its
host.

**The evidence is unusually good, because other harnesses hit this first.**
[github/copilot-cli#3398](https://github.com/github/copilot-cli/issues/3398)
asks for a `--prompt-file` flag and is open and unimplemented. It records two
independent projects arriving at the same wall and solving it two ways:

- One shipped a fail-fast guard telling Windows users to move to WSL, then
  replaced it in the next release by piping the prompt on stdin,
  `subprocess.run(input=prompt)` instead of `-p`. A Windows CI smoke
  round-trips a 60 KB prompt and checks its SHA-256. The guard was deleted as
  unreachable and the documentation reversed from "blocked, use WSL" to "works
  natively on Windows".
- The other stopped passing prompt bodies in argv at all: the prompt stays in a
  file the harness already writes, and argv carries a fixed pointer at it. A
  38 KB prompt became a 554-byte command line, with a regression test that a
  200 KB prompt stays under a fixed bound.

Both conclusions point the same way, and neither asks the program being invoked
to change.

**What each provider supports**, verified from vendor documentation rather than
assumed:

| | Claude Code | Copilot CLI |
|---|---|---|
| Prompt on stdin | Yes, alongside `-p`, documented, capped at 10 MB | Yes, but only when `-p` is omitted; piped input is ignored otherwise |
| Prompt from a file | `--system-prompt-file`, `--append-system-prompt-file` | None; `--prompt-file` is the open request in #3398 |
| Guidance above the cap | Write a file and reference its path in the prompt | Same, by implication |

That asymmetry is why this is a provider-adapter decision rather than a
contract rule. `Launch.input_text` already exists and is unused for the model
step, so the seam is there.

**A pointer needs a grant, which is the part that is easy to miss.** If a
pre-processor hands the model a path instead of content, the model must be
allowed to read it. Both CLIs take `--add-dir`, and the harness that took the
pointer route grants "read access to the prompt file's directory and nothing
wider". Copilot happens to grant the system temporary directory by default,
which is what its `--disallow-temp-dir` flag revokes; Claude does not. Under
`-p` there is nobody to approve a prompt, so an ungranted read simply fails.

**Two smaller findings worth keeping.** Copilot's session semantics differ
between argv and stdin, where stdin-mode `--resume` is resume-only and a first
call needs `--session-id`; that costs nothing today because nothing resumes,
and would bite immediately if something did. And one of those harnesses
classified an oversized argv as a *retryable* crash, which burned its whole
attempt budget rebuilding an identical command. Agents Live avoids that, though
not by design: `retryable` is set only for `timeout` and `empty_output`, so the
overflow lands as `cli_crash` and stops.

**Two findings that belong to other decisions**, surfaced by the same reading
of provider documentation and recorded so they are not rediscovered. The two
CLIs disagree about whether an unattended run may load repository-controlled
hooks and MCP servers, which is
[#375](https://github.com/johnshew/agents-live/issues/375). And Claude can
return a schema-validated value directly, which would retire the JSON
extraction heuristic in plan mode, which is
[#376](https://github.com/johnshew/agents-live/issues/376).

**Cost accepted.** Prompt delivery becomes provider-specific, so a Claude agent
and a Copilot agent have different effective ceilings until #3398 lands or
Copilot's stdin path is exercised here. That asymmetry is invisible to
processors, which is the point.

The adapter work is [#374](https://github.com/johnshew/agents-live/issues/374).

## Run context arrives in the environment, not in a file

**Decision.** Everything the run knows is an environment variable: scalars
plain, collections JSON.

**Why.** The convention already exists. `AGENTS_LIVE_CHANGED_FILES` is a JSON
array in the environment today, and `AGENTS_LIVE_AGENT_NAME` is a plain string.
Extending a convention costs a reader nothing; introducing an envelope file
beside it means two ways to learn the same kind of thing.

It is also cheaper to consume. One value costs one line in any language, with
no file to open and no document to parse for a program that wants a single
field.

**Cost accepted.** The environment is bounded, so `AGENTS_LIVE_INSTRUCTIONS`
and `AGENTS_LIVE_CHANGED_FILES` need caps enforced before spawn, using the same
limit and the same wording as the existing command-line overflow error rather
than a second phrasing for one condition. If either genuinely outgrows the
environment, the host writes a file and passes its path, which is the same
answer as the prompt and for the same reason: transport is the host's job.

## Mode constrains the model, not processors

**Decision.** What a processor receives is identical in every execution mode.

**Why.** Mode-dependent plumbing is the origin of the worst defect in the
current contract, where a post-processor silently reads an empty stdin after a
mode change. Once the store is uniform and the envelope is uniform, mode has
nothing left to say about processors, which is the correct amount.

## Logging: a per-step sink the host stamps

**Decision.** Each step writes its own JSONL file. The host validates, stamps
identity, caps, and merges on exit. A processor may not write `phase: "done"`
nor a run-outcome status.

**Why**, in three pieces of evidence rather than principle:

- Buffered appends to the shared log spliced 11,577 records into each other in
  a live deployment (#290). One writer per file cannot have that failure.
- `obs.query.consecutive_failures` counts a health streak from any record with
  `phase == "done"` and `status == "error"`, so today a log line can degrade an
  agent's health as though the run had failed. An observation must not be able
  to cast a verdict.
- Identity is currently self-asserted, so a processor can author a record
  claiming to be another agent. Stamping on ingest makes that impossible rather
  than discouraged.

A fourth defect belongs to the readers rather than the writers: `status` is
rewritten from `success` to `ok` by the query view but not by the health path,
so one written value means two things depending on who reads it. It should be
closed in the same work.

**Cost accepted.** In-flight tailing needs the sink path to be derivable from
the run id and step, rather than random.

## The log record belongs to #105, the channel belongs here

**Decision.** This contract specifies which file, who writes it, who stamps
identity, what vocabulary is reserved, and what happens to malformed rows.
[#105](https://github.com/johnshew/agents-live/issues/105) specifies the record:
span fields, start and end pairing, sensitivity classification, and the query
projection.

**Why.** #105 is a clean schema change across all canonical writers, and this
contract adds a new canonical writer. Specifying field names in both places
guarantees a collision. Splitting on channel versus record lets either land
first.

One consequence for #105: its writers now include programs in five languages,
which argues for a small required field set and for the host stamping
everything it can.

## A clean break, gated by schema version

**Decision.** `agents-live.schema-version: "2"` selects this contract. No
definition runs both and there is no per-field shim. Contract 1 leaves in 7.0,
on the same removal train as `legacy/`.

**Why.** Per-field compatibility would mean every seam carrying two meanings
indefinitely, which is what the no-shim rule exists to prevent. Two dispatch
paths are themselves a burden, so the removal is dated rather than open-ended.

`agents-live definition migrate` can raise the version, but it cannot rewrite a
processor's expectations. The migration note has to name what needs human
review: a post-processor that reads a bare value from stdin, and a
pre-processor that emits a bare `{"skip": true}`.

## What is still undecided

Listed at the end of
[processors.md](../src/agents_live/skill/docs/processors.md). The one with a
live
consumer is whether a non-zero exit always means failure, since an audit or
linter conventionally reports findings that way and would otherwise be read as
a failed run.
