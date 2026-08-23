---
title: Definition format
description: Agent Skills layout and Agents Live execution metadata schema
ms.date: 2026-08-23
ms.topic: reference
---

# Definition format

An Agents Live definition uses one of two layouts:

- `<discovery-root>/<name>/SKILL.md` is a conforming Agent Skill. `name` must
  match its directory.
- `<discovery-root>/<name>.md` is the Agents Live flat-file extension. `name`
  must match the filename stem.

Four standard discovery roots are searched: `Agents/`, `.claude/skills/`,
`.github/skills/`, and `.agents/skills/`. Set `agent_directories = ["foo"]` in
`.agents-live.toml` or the `[tool.agents-live]` table in `pyproject.toml` to
add your own repository-relative roots to that set. Discovery is immediate,
not recursive. Standard Agent Skills fields remain at the top level. Optional
unattended execution policy uses quoted string values under `metadata` with
the `agents-live.` prefix.

The three client skill roots hold skills written for other tools, so Agents
Live only claims a definition there when it carries `agents-live.` execution
metadata. A guidance-only skill in `.claude/skills/` is invisible to
`agents-live`, and a file there that fails to parse is reported as broken only
when it mentions `agents-live.`. Naming one of those roots in
`agent_directories` claims it for the repository, and it is then treated like
any other root. When a name exists in both `Agents/` and a client skill root,
the `Agents/` definition wins so a newly visible skill cannot re-route a
command that already worked.

```yaml
---
name: link-check
description: Checks documentation links. Use when reviewing documentation health.
metadata:
  agents-live.schema-version: "2"
  agents-live.selector: "claude/sonnet:high"
  agents-live.schedule: "0 7 * * 1"
  agents-live.watch: "docs/** !node_modules/** debounce 5s"
  agents-live.mode: "plan"
---
```

## Schema version 2

| Key | Encoding | Default and constraint |
|---|---|---|
| `agents-live.schema-version` | string | Required when any `agents-live.` key exists. `"2"` for the current processor contract, or `"1"` for the earlier one, which is removed in 7.0. |
| `agents-live.selector` | selector | Required. `provider[/model][:effort]`; `none` requires a processor. |
| `agents-live.schedule` | schedule or JSON string array | Optional. Five-field cron, including month and weekday names, or one of the eight documented `@` names. |
| `agents-live.watch` | watch expression | Optional. Includes, `!` excludes, then optional `debounce <duration>`. |
| `agents-live.mode` | enum | `plan` by default; `plan`, `write`, or `pipeline`. |
| `agents-live.result-path` | absolute PipelineMcp path | Optional in `pipeline` mode only. The declared value becomes `structured` in `run --json`; absence is nonfatal. |
| `agents-live.allow-tools` | JSON string array | Empty by default. Narrows unattended authority; it is not standard `allowed-tools`. |
| `agents-live.mcps` | JSON string array | Empty by default. Explicitly opts named servers from `.mcp.json` or `.vscode/mcp.json` into the otherwise isolated provider session. |
| `agents-live.env` | JSON string map | Empty by default. Do not store secrets. |
| `agents-live.transcript` | `true` or `false` | `true` by default. |
| `agents-live.timeout` | positive integer | Provider or processor timeout in seconds; `120` by default. |
| `agents-live.pre-processor` | relative path | Optional, relative to the skill directory. |
| `agents-live.post-processor` | relative path | Optional, relative to the skill directory. |
| `agents-live.output-schema` | JSON object or relative path | Optional JSON Schema for provider output. |
| `agents-live.output-max-bytes` | positive integer | Output size cap; 10 MiB by default. |
| `agents-live.output-path-roots` | JSON string array | Optional repository-relative path allowlist. |
| `agents-live.output-provenance` | `strict` | Optional strict whole-output JSON requirement. |

Metadata keys and values must be strings, and every metadata value must be
quoted. JSON is compact and uses stable key ordering. Metadata owned by other
clients is preserved.

## Forward compatibility

A repository is often synced to a host before the tool on it is upgraded, so
the loader has to say which of the two is behind.

An `agents-live.` key this release does not recognise is ignored. A key is
only added for a capability that is additive, so an older runtime honours the
rest of the definition and runs it. `status --json` reports such keys in
`unknown_metadata`, and `doctor` fails with a diagnostic explaining that each
key may be a typo or may require a newer runtime.

A change to what an existing key means is not additive, and raises the schema
version instead. A definition declaring a version above the one the running
release implements is refused: the run is abandoned and recorded under
`runtime_outdated`, naming the installed version and the upgrade command. The
definition is not malformed, so its trigger stays installed and the agent
resumes once the tool is upgraded.

`allowed-tools` and `agents-live.allow-tools` are different security
contracts. The standard field pre-approves tools for interactive skill use.
The Agents Live key can only narrow tools during an unattended run.

Provider sessions do not implicitly run repository-controlled hooks, workspace
MCP servers, or project extensions. Provider project instructions are also
disabled so the definition body is the unattended instruction source. This
policy is the same for Claude and Copilot and does not depend on whether either
CLI has previously trusted the checkout. A repository MCP server runs only
when its name appears in `agents-live.mcps`; a missing or malformed named
server fails the run before the provider starts. Repository hooks and project
extensions have no unattended opt-in surface.

Claude runs in `--bare` mode, so an Anthropic-hosted account must supply
`ANTHROPIC_API_KEY`; subscription credentials are not read. Bedrock, Google
Cloud's Agent Platform, and Microsoft Foundry retain their provider credential
mechanisms. Copilot uses a fresh run-scoped configuration home. Secure
credential-store login continues to work; on a host without a credential
store, supply `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, or `GITHUB_TOKEN` because
credentials stored below the normal Copilot configuration home are not read.

The loader rejects duplicate keys, tabs, anchors, aliases, merge keys,
explicit tags, byte-order marks, unknown top-level fields, and the retired
5.x names.

## Pipeline seed values

A `pipeline` definition can pre-populate the run-scoped side channel with
fenced `put` blocks in its body:

````markdown
```put /output/result/$schema
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["summary"]
}
```
````

The path must start with `/`, and the fence body must be one JSON value.
Agents Live parses the blocks in document order before any processor or
provider runs. Seeded paths are read-only to the agent, so an output schema or
referenced schema document cannot be replaced by the value it validates.

Use a pre-processor for values that depend on files, changed paths, or current
host state. In `mode: pipeline`, pre-processors and post-processors receive
`PIPELINE_MCP_URL` and `PIPELINE_MCP_TOKEN` and can use the MCP SDK to call
`put` and `get`. Outside pipeline mode those variables are not set.

[processors.md](processors.md) documents the whole processor contract: how each
file is launched, what arrives in the environment and on stdin, and what
changes with execution mode.

## Migrating from 5.x

`agents-live migrate` rewrites the frontmatter of `Agents/<name>.md` in place
and changes nothing else. The definition keeps its path, and processors keep
theirs; only the reference spelling changes, from repository-relative
`Agents/handlers/x.py` to skill-relative `handlers/x.py`.

Prefer that. It is the smallest change that makes a 5.x definition valid, and
it is the safest: a 5.x processor commonly derives the repository root from
its own location, so moving it silently changes what `__file__` means. Sharing
one `Agents/handlers/` or `Agents/lib/` directory across definitions keeps
working, because nothing is copied.

`--bundle` converts to `Agents/<name>/SKILL.md` instead, copying each
processor into the bundle's `scripts/` directory. Choose it per definition,
when you want a self-contained skill that can be moved or published on its
own, and check any processor that computes paths from `__file__`. A shared
helper referenced by several definitions is copied into each one, so the
copies can then drift.

Both forms are first-class. A flat `<name>.md` and a `<name>/SKILL.md` bundle
are discovered in every effective discovery root, and both use the same
metadata contract. The bundle is the conforming Agent
Skill; the flat file is the Agents Live extension.

Migration preserves an exact file watch as an exact pattern. A path ending in
`/` or `\\`, or one that names an existing directory, becomes a recursive
`/**` pattern. Explicit globs are preserved. A path that does not exist yet and
has no trailing directory separator stays exact rather than being guessed to
be a directory.

`migrate` scans every effective discovery root except unclaimed standard
client skill roots. A repository can opt a client root into migration by
naming it in `agent_directories`. A file that already carries `agents-live.`
metadata is skipped, so a scan reports only what is still to do and a
converted repository reports nothing.

Three 5.x fields have no portable equivalent, so the conversion stops rather
than guessing:

| Retired field | Where it goes |
|---|---|
| `owner` | Host assignment is machine-local, not repository content. Delete the field, then run `agents-live start --name <name>` on the host that should own the definition. |
| `env` | Values may be secrets. Delete the field and supply the values from the host environment of the run. |
| `runtime` carrying arguments | A selector is `provider[/model][:effort]` and cannot hold a space. Set `agents-live.selector` to the provider name the runtime plugin registers under 6.0. |

Every refusal names the file it came from. Run `agents-live migrate --dry-run`
first to see the whole set before changing anything.

### Upgrading a machine that is already running agents

The upgrade has two halves, the tool and the definitions, and a host spends
some time holding one without the other. Neither order loses work.

A 5.x runtime holding converted definitions abstains. Its orphan sweep
compares the running set against discovered *file names*, and an in-place
conversion does not move or rename anything, so nothing looks deleted. Behind
that, 5.x already refuses to prune a definition whose file exists but does not
parse. Its trigger convergence reports such a definition as missing rather
than rewriting it. Every scheduled run fails with `no runtime: declared` and
the health beacon degrades, which is loud and reversible; the triggers stay.

A 6.0 runtime holding unconverted definitions abstains too. It reports each
one as unloadable, keeps its installed trigger, and does not initialise
started state from a repository whose definitions did not all load, which
preserves the one chance it has to adopt the 5.x triggers. After
`agents-live migrate`, those triggers are matched to their new canonical
identifiers and the agents that were started stay started.

So choose the order for what else has to move. A repository that declares
plugins in `.agents-live.toml` is easiest converted first: 6.0 refuses a
plugin still registered under the retired `agents_live.agents` group, so
converting first lets one `agents-live upgrade` install the runtime and the
ported plugin together, where upgrading first needs a second run after the
definitions arrive.

Confirm either way with `agents-live status`, and start anything that is not
started.

From 6.0 onward the definitions can lead safely on their own. A definition
declaring a schema version the installed release does not implement is refused
with an upgrade remedy, recorded as `runtime_outdated`, and its trigger is
kept.

Each discovered definition receives a canonical identifier of the form
`<name>-<path-hash>`. The hash uses the normalized repository-relative prompt
path, so moving a checkout preserves identity while moving the definition does
not. Commands accept a plain name when it is unique and otherwise require the
canonical identifier.
