---
title: Frontmatter convergence
description: Decision to make Agents Live definitions conforming Agent Skills with namespaced execution metadata
ms.date: 2026-08-08
ms.topic: concept
---

# Frontmatter convergence

## Decision

Agents Live definitions will become conforming
[Agent Skills](https://agentskills.io/specification). A definition is a
`<skill-name>/SKILL.md` directory whose `name` and `description` have the
standard meanings. Agents Live adds execution policy under the standard
`metadata` map, using keys prefixed with `agents-live.`.

This chooses the layout-level position left open in
[refactoring-runtime-and-agent-seams.md](refactoring-runtime-and-agent-seams.md).
It supersedes flat agent files with top-level Agents Live fields. It is a
breaking format change, not a compatibility layer.

The boundary is:

- Agent Skills defines the portable instruction bundle.
- Agents Live defines optional unattended execution of that bundle.
- Host assignment, activation state, credentials, and secrets remain outside
  the definition.
- Agent-tasks task frontmatter remains a separate compatibility surface. Task
  files can share a task vocabulary without making task fields part of
  `SKILL.md`.

## Context

Agents Live began by adding `schedule`, `watchPath`, provider, processor, and
output-policy fields to native Claude Code and GitHub Copilot agent files. That
made existing agent files runnable without introducing another definition
format, but it also combined three contracts in one unqualified mapping:

1. fields understood by an interactive client;
2. fields controlling unattended execution; and
3. fields describing host-local activation and ownership.

The runtime and agent seams proposal established the direction in commit
`b454d11`: Agent Skills is the definition standard and Agents Live is an
execution extension. Review commit `03a6639` made that layering explicit.
Commit `889248d` then corrected the tempting but unsafe mapping from
`allow-tools` to the standard `allowed-tools` field.

Related repository research reached the same division from another direction.
Reusable procedures should use the open `SKILL.md` format rather than a private
synonym. Automating one of those procedures is an additional, usually local,
choice. Separately, task files should retain agent-tasks-compatible core field
names because that buys direct tool compatibility. These conclusions concern
two different document types and do not imply one shared frontmatter schema.

## Target shape

A definition has the standard directory layout:

```text
weekly-delivery-report/
  SKILL.md
  scripts/
    publish.py
  references/
    report-contract.md
  assets/
```

Its frontmatter follows this shape:

```yaml
---
name: weekly-delivery-report
description: Produces a weekly delivery report from repository evidence. Use when reviewing delivery status, risks, or commitments.
compatibility: Requires git and Python 3.12 or later.
metadata:
  agents-live.schema-version: "1"
  agents-live.schedule: "0 8 * * 1"
  agents-live.selector: "claude/opus:high"
  agents-live.mode: "plan"
  agents-live.allow-tools: '["Read","Grep"]'
  agents-live.post-processor: "scripts/publish.py"
---
```

The example fixes the structural decision and illustrates the recommended
encoding. The complete field registry still needs to state every key, default,
grammar, and validation rule before implementation. In particular:

- `description` says what the skill does and when a client should load it. It
  is not a scheduler label or a place for "Never delegate" policy.
- `agents-live.schema-version` is required whenever any `agents-live.` key is
  present.
- `agents-live.selector` replaces the overloaded execution meaning of
  `runtime` and folds provider, model, and effort into one grammar.
- schedules, watch expressions, and selectors use documented domain-specific
  string grammars.
- lists and mappings use canonical JSON encoded inside a YAML string.
- scripts are relative to the skill root and normally live under `scripts/`.
- secret values are never stored in metadata. A definition may name a required
  environment key, but credential resolution is host-local.

The standard `allowed-tools` field and `agents-live.allow-tools` remain
different:

- `allowed-tools` tells a conforming skill client which tools are pre-approved
  during ordinary skill use.
- `agents-live.allow-tools` narrows the tools available to an unattended run
  and can never grant authority.

Renaming the latter to the former would merge two security contracts and could
broaden authority in clients that know nothing about Agents Live.

## Why `metadata`

`metadata` is not only a descriptive label. It is the extension mechanism the
Agent Skills specification defines: a mapping from string keys to string
values for properties outside the standard. The `agents-live.` prefix gives
those properties an owner inside the shared extension map.

This has four material advantages:

1. The file remains a valid Agent Skill. The reference validator accepts only
   `name`, `description`, `license`, `compatibility`, `metadata`, and the
   experimental `allowed-tools` at the top level.
2. Generic clients can read `name` and `description` without understanding or
   accidentally interpreting execution policy.
3. Extension ownership is explicit. Agents Live can reject unknown
   `agents-live.` keys while preserving metadata owned by other clients.
4. The string-only boundary forces a stable wire representation instead of
   exposing language-specific YAML object types to plugins and runtimes.

The main cost is readability. A nested `metadata` block adds indentation, and
structured settings need an encoding. That cost is real, but it is bounded by
a schema version, short domain grammars, and canonical JSON for genuinely
structured values.

### Why not prefixed top-level fields

This is valid YAML:

```yaml
agents-live.schedule: "0 8 * * 1"
```

It is not valid Agent Skills frontmatter at the top level. The reference
validator rejects unknown top-level fields, including prefixed ones. A prefix
avoids a name collision but does not create an extension point.

This is not a theoretical rejection. Claude Code accepts many top-level fields
of its own during a local session, but the strict paths, meaning claude.ai
skill uploads, the Skills API, and `package_skill.py`, refuse anything outside
the six specification fields with a hard error rather than ignoring it:

```text
Unexpected key(s) in SKILL.md frontmatter: argument-hint. Allowed properties
are: allowed-tools, compatibility, description, license, metadata, name
```

That message rejects one of Anthropic's own top-level extensions. An
`agents-live.` prefix would fare no better, because the check is an allowlist
rather than a collision test.

Top-level prefixed fields would therefore preserve the current implementation
convenience by giving up the interoperability this migration is intended to
buy. Some clients reject such files, some ignore the fields, and others parse
then rewrite only recognized properties. Passing through one permissive YAML
library would not establish conformance.

There is also no useful syntactic advantage. Dots are ordinary characters in
YAML mapping keys, so `agents-live.schedule` parses in either location. Some
configuration libraries interpret dots as object paths after YAML parsing,
which is another reason to access metadata as an exact-key mapping rather than
through a generic configuration-path API.

### Why not one nested Agents Live object

The Agent Skills `metadata` values must be strings. This is not conforming:

```yaml
metadata:
  agents-live:
    schedule: "0 8 * * 1"
```

The value of `agents-live` is a mapping, not a string. A single
`agents-live.config` JSON object would conform, but it would make every small
change replace one opaque blob and would prevent generic metadata inspection.
Separate `agents-live.*` keys are easier to diagnose, diff, and evolve.

### Why not a sidecar

A sidecar such as `agents-live.json` would keep `SKILL.md` free of execution
metadata and would give structured values native JSON types. It would also
split one movable definition across two artifacts, introduce consistency and
discovery rules, and make a generic skill directory incomplete unless copied
with tool-specific files.

Portable execution intent therefore stays in `SKILL.md` metadata. Mutable
host facts do not: whether the skill is started here, which host owns it, last
run state, credentials, and resolved environment values belong in Agents Live
state or host configuration. A sidecar should be reconsidered only if the
standard removes or materially restricts its metadata extension point.

## Effect on other clients

The encoding question is settled by the specification. The separate question is
what a conforming definition does when another client actually reads it, and
the answer differs by client.

**Claude Code treats `metadata` as inert.** Its frontmatter reference describes
the field as a free-form map for the author's own key-value data, read by the
author's own tooling, states that Claude Code does not act on its contents, and
drops a value that is not a map. It is more permissive than the specification,
since it does not require string values, so a string-only Agents Live block
satisfies both. Its one caution, not to reuse frontmatter field names as
metadata keys, is already met by the `agents-live.` prefix.

**Copilot CLI documents `name`, `description`, `license`, and `allowed-tools`**
and says nothing about other frontmatter. Inert by omission is weaker evidence
than inert by promise, so Copilot is the client that needs a loading fixture
rather than an assumption.

**The strict Claude paths accept `metadata` and reject unknown top-level
fields**, as shown under
[Why not prefixed top-level fields](#why-not-prefixed-top-level-fields).

### The residual risk is discovery, not parsing

No client studied here breaks on the metadata block. What a client does break
on is being handed an unattended definition as if it were an interactive skill.
When a definition sits in a directory a client searches, three things follow:

- its `name` and `description` enter the session skill listing, so it competes
  for the listing's token budget and can push real skills into truncation;
- the model may invoke it on its own, which is the wrong outcome for a
  definition whose body commits, pushes, or publishes; and
- the only in-file suppression, `disable-model-invocation: true`, is a Claude
  Code top-level extension, so adding it is exactly what the strict paths
  reject.

There is no encoding that resolves that tension, which is why it is resolved by
placement instead. See
[Discovery stays with Agents Live](#discovery-stays-with-agents-live).

### Top-level names Agents Live must not adopt

Claude Code's own extensions occupy top-level names that overlap the Agents
Live vocabulary, including `model`, `effort`, `allowed-tools`,
`disallowed-tools`, `paths`, `hooks`, `context`, `agent`, `arguments`,
`argument-hint`, `disable-model-invocation`, and `user-invocable`.

`paths` deserves particular care. In Claude Code it is a set of globs limiting
when a skill activates automatically. It reads like the watch expression in
[target-architecture.md](target-architecture.md#the-watch-grammar) and means
something entirely different: an activation hint for an interactive session,
not a filesystem trigger for an unattended run. `model` and `effort` are the
two halves that `agents-live.selector` folds into one grammar. Keeping all of
them inside `metadata` is what stops a reader, or a future migration tool, from
mapping one onto the other.

## Discovery stays with Agents Live

Agents Live already has its own registration and discovery mechanism, and this
change does not touch it. `agents-live init` records a repository path in the
user-level registry; convergence reads that registry, walks every registered
repository, loads every definition, and keeps the ones marked started.
Registration answers "where do I look" and started state answers "what runs
here", the separation described in
[target-architecture.md](target-architecture.md#stage-1-the-machine-learns-where-to-look).

Client discovery is a different mechanism with different directories:
`.claude/skills/`, `.github/skills/`, `.agents/skills/`, and their per-user
equivalents. A definition in a registered repository's `Agents/` directory is
in none of them. It is therefore invisible to an interactive client by default
and still runnable by `agents-live run`, which is the correct polarity: a
definition written to run unattended should not become a slash command in
someone's session merely because it was committed.

Opting in remains available and remains explicit:

- Claude Code follows a symlink at `.claude/skills/<name>` to a skill directory
  elsewhere on disk, and loads the skill once even when the target is reachable
  from several locations.
- Copilot CLI takes `copilot skill add <FILE | URL | DIRECTORY>` or
  `/skills add`, and searches the vendor-neutral `.agents/skills` and
  `~/.agents/skills` locations.

**Agents Live never writes into a client discovery directory.** It registers
repositories and installs host triggers. Making a definition visible to an
interactive client is the developer's decision, taken with that client's own
tools.

Two consequences follow for implementation:

- `agents-live run` and the definition loader address the skill directory
  rather than `SKILL.md` directly, because `scripts/`, `references/`, and
  `assets/` resolve relative to the skill root.
- The specification requires `name` to match the parent directory name but says
  nothing about where that directory lives, so a definition in a repository
  outside every client's search path is fully conforming. Discovery is client
  policy, not part of the format.

## Portable parsing profile

YAML libraries do not agree on every YAML feature. Python's PyYAML, JavaScript
libraries, Go's `yaml.v3`, StrictYAML, and Rust deserializers differ on YAML
1.1 versus 1.2 implicit typing, duplicate keys, merge keys, timestamps, custom
tags, and alias handling. Frontmatter extractors also differ on byte-order
marks and whether `---` must occupy a line by itself.

Agents Live should accept a deliberately small profile:

- UTF-8 text without a byte-order mark;
- an opening `---` on the first line and a closing `---` on its own line;
- one top-level mapping with unique string keys;
- no tabs for indentation;
- no anchors, aliases, merge keys, explicit tags, or complex mapping keys;
- standard fields with the types required by Agent Skills;
- `metadata` as a mapping whose keys and values are already strings;
- every metadata value quoted, including numbers, booleans, null-like words,
  dates, cron expressions, and JSON;
- JSON arrays and objects serialized compactly with stable key ordering when a
  value needs structure.

Quoting is required because an unquoted `true`, `30`, `null`, or ISO-looking
date can become a boolean, integer, null, or date in one parser and remain a
string in another. The parser must reject the wrong type rather than convert it
with `str()`: coercion can turn Python `True` into `True`, where another
implementation would produce `true`, and can silently erase the distinction
between a source string and an implicitly typed scalar.

Duplicate keys must also fail. PyYAML's default loader can accept a duplicate
and retain the last value while another implementation rejects it. A schedule
or security policy must not depend on which parser read the file.

Canonical JSON is preferable to YAML-in-YAML for structured values because its
string form has consistent booleans, nulls, arrays, and objects across common
languages. Domain values that need comparison or hashing must additionally
have a parser and canonical renderer. Consumers compare the parsed canonical
form, not the author's whitespace.

## Parser architecture

Agents Live currently loads frontmatter with `yaml.safe_load` and then reads
top-level fields throughout one large normalization function. It accepts
several string-or-list fields, nested `env` and `output-schema` mappings, and
coerces many values with `str()`, `int()`, or `bool()`. That behavior is useful
for the old format but is too permissive for a cross-language contract.

The migration should introduce one definition loader with four explicit
stages:

1. Extract frontmatter using exact delimiter lines and decode UTF-8.
2. Parse the restricted YAML profile while detecting duplicate keys and
   rejecting unsupported YAML constructs.
3. Validate standard Agent Skills fields and the directory-name invariant.
4. Decode recognized `agents-live.*` strings into a typed `AgentsLiveConfig`,
   rejecting unknown keys or unsupported schema versions.

The resulting model has two parts: standard `SkillProperties`, which remains
useful without Agents Live, and optional typed execution configuration. Runtime
and provider code receives the typed configuration and never reparses YAML or
JSON.

Unknown metadata follows ownership:

- unknown top-level fields fail Agent Skills validation;
- unknown `agents-live.*` keys fail with a version-aware diagnostic;
- metadata keys owned by other clients are preserved or ignored;
- unknown future schema versions fail closed before scheduling or execution.

The Agent Skills reference library is a conformance oracle and test fixture,
not necessarily a production parser. Its own documentation calls it a
demonstration library, and its current parser normalizes metadata values to
strings. Agents Live must validate source types before normalization so an
invalid file cannot become valid through implementation-specific coercion.

## Migration and validation

The format change ships in a major release with no runtime compatibility shim:

1. finalize and document the complete `agents-live.*` field registry;
2. add the restricted loader and tests in isolation;
3. migrate shipped templates and fixtures to `<skill>/SKILL.md` directories;
4. move processors into each skill's `scripts/` directory where practical;
5. add `skills-ref validate` as an architecture fitness test;
6. add cross-parser fixtures for quoted scalars, JSON values, CRLF, duplicate
   keys, aliases, merge keys, and unknown fields;
7. confirm on real clients that a migrated definition loads without warnings,
   since Copilot CLI's tolerance of `metadata` is undocumented rather than
   promised;
8. provide a migration command or one-release diagnostic that reads an old
   definition and prints the new form without running it;
9. remove old-format parsing in the breaking release.

The migration tool must never copy secret values into metadata. It should also
surface semantic conflicts instead of guessing, especially `allow-tools`
versus `allowed-tools`, native-client invocation fields, host ownership, and
processor paths.

## Consequences

The definition becomes portable across clients that implement Agent Skills,
while Agents Live retains a clear and versioned execution contract. The cost is
a directory migration, a breaking release, and explicit string encodings for
extension values.

The change also removes an accidental promise: a native Claude Code or GitHub
Copilot agent file and an Agent Skill are not interchangeable merely because
both are Markdown with YAML frontmatter. Client-specific invocation fields
such as `disable-model-invocation`, `user-invocable`, and `argument-hint` are
not Agent Skills fields. They must not remain at the top level of the shared
definition. Where equivalent behavior is still required, the relevant client
adapter or host configuration owns it.