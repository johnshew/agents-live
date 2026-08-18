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

## The definition owns the command line

**Decision.** A processor declares nothing about itself. The definition that
uses it writes the command line, with `${name}` substitution, boolean flags,
and bracketed fragments that vanish when a value is absent.

**Why.** Whoever wires an agent to a script wrote both, so a separate interface
file exists only to be kept in sync. Makefiles, systemd units, and crontab
entries all work this way and nobody finds them mysterious.

**Rejected: a sidecar declaration file.** It duplicated knowledge that already
existed, added a file format, and had to be maintained against a program that
could change independently.

**Rejected: a discovery protocol**, where Agents Live runs the program with a
flag to ask what it accepts. It makes every processor implement an extra entry
point, which is exactly the "rewrite your script" tax the design exists to
remove.

**Cost accepted.** Agents Live cannot check that a flag name is one the program
accepts. The program's own argument parser catches it immediately, and for a
pre-processor that happens before any model call, so the failure is cheap and
clear.

## Optional values are a property of the command line, not the program

**Decision.** `[--account ${account}]` disappears entirely when `account` has
no value.

**Why.** An optional flag is normal, and a substitution that leaves a dangling
`--account` with nothing after it is worse than useless. The bracket keeps the
flag and its value together, which is the unit that is actually optional.

**Rejected: requiring every referenced option to have a default.** It works
only when a default exists, and for a genuinely optional flag the meaningful
state is absence. An audit that sweeps every account when none is named cannot
express that with a default.

**Rejected: shell-style `${account:+--account $account}`.** It is a templating
language, and templating languages grow.

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

## The store is a directory, and the MCP server is the model's door onto it

**Decision.** Shared state is a run-scoped ephemeral directory that processors
read and write with ordinary file operations, in every mode. In pipeline mode
the existing in-process MCP server is started in front of that directory and
gives the model `get` and `put` as its only tools.

**Why.** Processors are already full participants in the store, and that is the
point of pipeline mode rather than an incidental use. In the
`exercise-judgment` agent the pre-processor publishes the recommendations and
state documents as chunked values with a manifest, the model reads them and
publishes a patch, and the post-processor reads that patch and applies it to
the file on disk. Three participants, one store.

What is wrong today is not who uses the store but what it costs to use. That
pre-processor carries `dependencies = ["mcp<2"]`, an `asyncio.run`, a
`sys.path.insert` reaching into a shared `Agents/lib`, a helper module wrapping
session setup, and a bearer token, all to write JSON values. None of that is
domain work, none of it survives outside a dispatch, and every processor in
every language pays it again. A directory costs a `write_text`.

The security story does not change, because the store was never the boundary.
The model is the untrusted participant, so validation against a bound `$schema`
and refusal to overwrite seeded paths stay in the server, applied to the
model's calls. Processors are repository code and were already trusted with the
same values.

**Cost accepted.** A directory cannot refuse a write, so a processor can
overwrite anything, including a value the model just published. It could
already do that through `put`.

**A directory buys two things the server cannot.** The run replays, because the
state is a set of files that can be captured and restored. And the same
processor works standalone, which under the current shape is impossible for a
pipeline processor: its entire job is store access, so with no server running
there is nothing for it to do.

Chunking does not go away. Splitting a large document into numbered values with
a manifest exists because one tool call is a poor way to move a large document
to a model, which is a property of how the model reads rather than of how the
value is stored.

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

`agents-live definition migrate` can raise the version and add an empty options
map, but it cannot rewrite a processor's expectations. The migration note has
to name what needs human review: a post-processor that reads a bare value from
stdin, and a pre-processor that emits a bare `{"skip": true}`.

## What is still undecided

Listed at the end of
[processors.md](../src/agents_live/skill/docs/processors.md). The one with a
live
consumer is whether a non-zero exit always means failure, since an audit or
linter conventionally reports findings that way and would otherwise be read as
a failed run.
