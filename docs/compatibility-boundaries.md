---
title: Compatibility Boundaries and the 7.0 Retirement
description: What each release line removes, and why the 6.8 and 7.0 boundaries are separate
ms.date: 2026-09-04
ms.topic: concept
---

# Compatibility boundaries

Agents Live carries compatibility debt in two independent places. They are
retired on separate release boundaries because they have nothing to do with
each other, and coupling them would make each harder to validate.

This file states which boundary owns what. It does not restate acceptance
criteria; those live in GitHub issues.

## 6.8: the extension and installation boundary

6.8 removes two mechanisms that exist because of how the tool used to be
installed, not because of what it does.

**Installed-wheel plugins.** A declared plugin becomes a source directory
loaded dynamically against a protocol. See
[decisions/plugin-loading.md](decisions/plugin-loading.md) for the decision,
the precedent it rests on, and the alternatives rejected.

This is the only breaking change in 6.8. A `.agents-live.toml` declaring a
`.whl` path stops validating and must name a module or package directory
instead. No CLI argument, command, or agent definition field changes.

**`uv tool install` as an ownership channel.** uv remains the environment
builder: `uv venv` and `uv pip install` populate every generation. What is
retired is uv *owning* an installation and being the thing `agents-live
upgrade` drives.

That ownership is the root of a large amount of machinery, all of which
descends from one property: `uv tool install` rewrites an environment in
place. Windows holds a mandatory lock on a running image, so an in-place
rewrite can remove an environment's packages and then fail on its launcher.
Everything built to survive that (the deferred Windows upgrade handoff, the
quiesce and restore protocol, launcher timestamp comparison, held-environment
refusal, and receipt-driven plugin convergence) has no analogue under
generations, where a new version is built beside the running one and selected
by moving a directory link.

This is not a breaking change for supported installations. The public
bootstrap already migrates a legacy uv tool installation, and a command run
from a package-manager environment refuses self-upgrade with guidance pointing
at the bootstrap. The new `generations` command exposes selection, deliberate
inactive-version removal, and policy-driven collection. Retirement is tracked under
[#334](https://github.com/johnshew/agents-live/issues/334).

## 7.0: the definition-format boundary

7.0 removes the 5.x compatibility surface. This is committed direction already
recorded in the tree in five places, and it predates the 6.8 work:

- `agent/definition.py` - the earlier processor contract leaves in 7.0.
- `skill/docs/definition-format.md` - `agents-live.schema-version: "1"` is
  removed in 7.0.
- `skill/docs/processors.md` - the version 1 processor contract is removed
  in 7.0.
- `plugins.py` - the retired 5.x entry point group diagnostic expires with the
  rest of 5.x support in 7.0.
- `hidden.py` - the legacy Windows task entry point is removed in 7.0.
- `AGENTS.md` and the architecture fitness test - `legacy/` is removed in 7.0.

7.0 is therefore the release where a definition that has not migrated stops
working. Nothing about deployment or plugin loading belongs to it.

Tracked by [#434](https://github.com/johnshew/agents-live/issues/434).

## Why the boundaries are separate

The two are independently validatable and affect different populations. The
6.8 changes affect whoever declares a plugin and whoever installed through uv;
both have a mechanical migration and neither touches an agent definition. The
7.0 change affects anyone still carrying a 5.x definition or processor
contract, and no amount of deployment work makes that migration easier.

Putting them together would mean a release where a failure could be a new
loader, a new installation owner, or a withdrawn definition contract, with no
way to bisect between them.

## Maintaining this file

Rewrite a section in the same change that ships its boundary. When a boundary
is fully retired, remove the section rather than leaving it as history; the
changelog and git history hold the record.
