---
title: Definition format
description: Agent Skills layout and Agents Live execution metadata schema
ms.date: 2026-08-08
ms.topic: reference
---

# Definition format

An Agents Live definition uses one of two layouts:

- `<discovery-root>/<name>/SKILL.md` is a conforming Agent Skill. `name` must
  match its directory.
- `<discovery-root>/<name>.md` is the Agents Live flat-file extension. `name`
  must match the filename stem.

`Agents/` is always a discovery root. Add repository-relative roots with
`agent_directories = ["foo"]` in `.agents-live.toml` or the
`[tool.agents-live]` table in `pyproject.toml`. Discovery is immediate, not
recursive. Standard Agent Skills fields remain at the top level. Optional
unattended execution policy uses quoted string values under `metadata` with
the `agents-live.` prefix.

```yaml
---
name: link-check
description: Checks documentation links. Use when reviewing documentation health.
metadata:
  agents-live.schema-version: "1"
  agents-live.selector: "claude/sonnet:high"
  agents-live.schedule: "0 7 * * 1"
  agents-live.watch: "docs/** !node_modules/** debounce 5s"
  agents-live.mode: "plan"
---
```

## Schema version 1

| Key | Encoding | Default and constraint |
|---|---|---|
| `agents-live.schema-version` | string | Required when any `agents-live.` key exists. Must be `"1"`. |
| `agents-live.selector` | selector | Required. `provider[/model][:effort]`; `none` requires a processor. |
| `agents-live.schedule` | schedule or JSON string array | Optional. Five-field cron, including month and weekday names, or one of the eight documented `@` names. |
| `agents-live.watch` | watch expression | Optional. Includes, `!` excludes, then optional `debounce <duration>`. |
| `agents-live.mode` | enum | `plan` by default; `plan`, `write`, or `pipeline`. |
| `agents-live.allow-tools` | JSON string array | Empty by default. Narrows unattended authority; it is not standard `allowed-tools`. |
| `agents-live.mcps` | JSON string array | Empty by default. |
| `agents-live.env` | JSON string map | Empty by default. Do not store secrets. |
| `agents-live.transcript` | `true` or `false` | `true` by default. |
| `agents-live.timeout` | positive decimal integer | Provider or processor timeout in seconds; `120` by default. |
| `agents-live.pre-processor` | relative path | Optional, relative to the skill directory. |
| `agents-live.post-processor` | relative path | Optional, relative to the skill directory. |
| `agents-live.output-schema` | JSON object or relative path | Optional JSON Schema for provider output. |
| `agents-live.output-max-bytes` | positive decimal integer | Output size cap; 10 MiB by default. |
| `agents-live.output-path-roots` | JSON string array | Optional repository-relative path allowlist. |
| `agents-live.output-provenance` | `strict` | Optional strict whole-output JSON requirement. |

Metadata keys and values must be strings, and every metadata value must be
quoted. JSON is compact and uses stable key ordering. Unknown
`agents-live.*` keys and schema versions fail closed. Metadata owned by other
clients is preserved.

`allowed-tools` and `agents-live.allow-tools` are different security
contracts. The standard field pre-approves tools for interactive skill use.
The Agents Live key can only narrow tools during an unattended run.

The loader rejects duplicate keys, tabs, anchors, aliases, merge keys,
explicit tags, byte-order marks, unknown top-level fields, and the retired
5.x names. Use `agents-live migrate` to convert 5.x top-level execution fields
to namespaced metadata. The migrator refuses host assignment, client-specific
fields, and environment values rather than copying a possible secret into
portable metadata.

Each discovered definition receives a canonical identifier of the form
`<name>-<path-hash>`. The hash uses the normalized repository-relative prompt
path, so moving a checkout preserves identity while moving the definition does
not. Commands accept a plain name when it is unique and otherwise require the
canonical identifier.
