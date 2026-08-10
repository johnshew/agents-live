---
title: Definition Format Decision
description: Why Agents Live uses Agent Skills with namespaced execution metadata
ms.date: 2026-08-09
ms.topic: concept
---

# Definition format

## Status

Accepted and implemented for 6.0.

## Context

Earlier definitions mixed native client fields, unattended execution policy,
and host-local assignment in one top-level YAML mapping. The result looked
portable but was not a conforming Agent Skill, overloaded names such as
`model`, and exposed behavior to YAML parser differences.

Repositories also need a compact form for many small runnable definitions
without requiring one directory per file.

## Decision

The canonical definition is a conforming `<name>/SKILL.md` Agent Skill.
Standard fields remain at the top level. Agents Live execution policy uses
quoted string values under the standard `metadata` extension map with an
`agents-live.` prefix.

Every discovery root may also contain `<name>.md`. This is an explicit Agents
Live extension using the same body and metadata contract, not a conforming
Agent Skill bundle. It exists because a repository with many small runnable
definitions should not need one directory each, and because it is the form a
5.x definition migrates into without its processors being relocated.

Every definition receives a path-derived canonical identifier. Started state,
ownership, credentials, and resolved environment values remain outside the
definition.

The normative field registry is
[definition-format.md](../../src/agents_live/skill/docs/definition-format.md).

## Why namespaced metadata

Agent Skills defines `metadata` as the extension point. Unknown prefixed
top-level fields are still nonconforming. Separate `agents-live.*` keys remain
inspectable and diffable, unlike one opaque JSON configuration value.

Metadata values are strings. Domain values use documented grammars, while
arrays and mappings use canonical JSON encoded inside a quoted YAML string.
This avoids parser-dependent implicit booleans, numbers, dates, aliases, and
duplicate-key behavior.

The standard `allowed-tools` field and `agents-live.allow-tools` remain
different security contracts. The former can pre-approve interactive skill
tools. The latter can only narrow authority for unattended execution.

## Discovery decision

Agents Live repository registration and client skill discovery are separate.
Definitions under `Agents/` or configured roots do not automatically become
interactive slash commands. A developer may explicitly expose a bundle using
the client's discovery mechanism.

This prevents an unattended definition that commits, publishes, or performs
maintenance from entering an interactive model's automatic skill inventory
merely because the repository is registered for automation.

## Parsing profile

The loader accepts a deliberately restricted profile:

- UTF-8 without a byte-order mark;
- exact frontmatter delimiters;
- unique string mapping keys and no tabs;
- no anchors, aliases, merge keys, explicit tags, or complex keys;
- specification-defined top-level fields only;
- string metadata keys and quoted string values; and
- known `agents-live.*` keys under a supported schema version.

Unknown metadata owned by another client is preserved. Unknown Agents Live
keys and schema versions fail closed.

## Alternatives rejected

**Prefixed top-level fields.** A prefix avoids collision but does not create a
standard extension point.

**One nested Agents Live mapping.** Agent Skills metadata values must be
strings, so a nested mapping is nonconforming.

**One JSON metadata blob.** It conforms but makes small changes opaque and
prevents generic metadata inspection.

**A sidecar file.** It splits one movable definition across artifacts and
creates consistency and discovery rules.

**Dual old/new parsing.** It creates two accepted spellings and an indefinite
removal condition. 6.0 instead provides an explicit migrator and targeted
diagnostics for retired fields.

## Consequences

Definitions are portable to Agent Skill clients while unattended execution
retains a versioned owner. The cost is explicit string encoding, a breaking
5.x migration, and a separate flat-file extension for compact repositories.
The retired-field diagnostics and migration support expire in 7.0.

## History

`docs/frontmatter-convergence.md` preceded this record and is not maintained.
It holds the Agent Skills conformance analysis and the field-by-field
convergence table behind the decision above.

```bash
git log --oneline -- docs/frontmatter-convergence.md
git show <deletion-commit>^:docs/frontmatter-convergence.md
```

Implemented in [#256](https://github.com/johnshew/agents-live/pull/256).