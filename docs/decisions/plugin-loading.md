---
title: Plugin Loading Decision
description: Why declared plugins are source directories loaded dynamically against a protocol instead of installed wheels discovered through entry points
ms.date: 2026-09-04
ms.topic: concept
---

# Dynamically loaded plugins

## Status

Decided for 6.8. Supersedes the installed-wheel plugin model delivered by
[#34](https://github.com/johnshew/agents-live/issues/34).

The declaration keys and their containment rules survive. What changes is what
`path` names and what the runtime does with it.

## Context

A declared plugin extends one of two seams. A provider plugin supplies an
object satisfying the `Provider` protocol in
`src/agents_live/agent/providers/__init__.py`; 6.9 widened that protocol to
the complete integration contract described in
[provider-contract.md](../provider-contract.md). An ownership plugin supplies
a registry backend with four callables. `register()` checks the provider
contract and nothing else. Neither seam reads packaging metadata for any
purpose beyond finding the object.

Until 6.8 a plugin was a built wheel. `.agents-live.toml` named the wheel by
repository-relative path with an optional digest; the runtime installed it into
the environment and discovered it through an `importlib.metadata` entry point.

That model was inherited from `uv tool install --with`, which co-installed
plugins alongside the tool. It brought a cost that the seams never asked for:

- Wheel metadata parsing, digest comparison against installed distribution
  versions, and conflict resolution when two repositories declared the same
  plugin.
- A subprocess compatibility probe with a 120 second timeout, run before any
  runtime replacement.
- Convergence: reconciling one mutable environment's installed plugin set
  against the union of every registered repository's declarations. Because
  convergence reinstalled the tool, it could change the runtime version as a
  side effect, which required pinning the primary requirement to the running
  version to prevent.
- A plugin change could only take effect by rewriting the environment, which is
  the operation the generation model exists to avoid.

The decisive observation is that the declaration format already forbids
everything installation buys. `validated_plugins` in `src/agents_live/paths.py`
rejects an absolute path and any path that resolves outside the declaring
repository. A plugin must be a file inside the repository that declares it.
There is no name-and-version form, so a plugin can never come from a package
index. The runtime was paying the full cost of packaged distribution to load a
file whose exact path it had already been given.

## Decision

`path` names a Python module or package directory inside the declaring
repository. The runtime imports it directly and hands the exposed objects to
the seam. Nothing is installed.

Declaration, unchanged except for what `path` may name:

```toml
[plugins.example]
path = "Agents/plugins/example"
sha256 = "..."
```

Loading uses the recipe published in the CPython `importlib` documentation:
`spec_from_file_location`, `module_from_spec`, registration in `sys.modules`,
then `exec_module`.

A loaded module declares its seam by exposing well known attributes:
`PROVIDER` or `PROVIDERS` for the provider seam, `OWNERSHIP_REGISTRY` for the
ownership seam. A module exposing neither is an error, reported by name.

Three constraints follow from the mechanism and are not negotiable.

**Module names are namespaced.** The CPython recipe registers the module in
`sys.modules`, and pytest documents the collision this invites for
`conftest.py` files that are not inside a package: a second file with the same
name is silently ignored or silently wins. Two registered repositories may each
declare `plugin.py`. Plugin modules are therefore registered under a key
derived from the registry entry, not from the filename. Skipping `sys.modules`
registration instead is not an option, because dataclasses, pickling, and
`typing.get_type_hints` resolve through it.

**A package directory is a first class case.** `spec_from_file_location`
accepts `submodule_search_locations`, so a plugin can be a directory with
`__init__.py` and siblings that import each other relatively. A single file
rule would push an author back toward wheels the moment a plugin outgrew one
file.

**Dependencies are declared and verified, never installed.** A plugin may
carry a PEP 723 header. The runtime reads it, checks each named distribution
is present, and reports precisely which one is missing. It installs nothing.
The contract is that a plugin runs inside the Agents Live runtime and may use
only that runtime's own dependencies.

The digest, the repository containment rule, and the protocol validation in
`register()` all carry over unchanged. Integrity is checked against the file
or directory rather than against a wheel.

## Grounding

The pattern is not novel, and the two closest systems in the Python ecosystem
disagree in exactly the place this decision has to choose.

**pytest** is the nearest match and supports both mechanisms deliberately. It
discovers distributable plugins through the `pytest11` entry point, and it
loads `conftest.py` files directly from the filesystem as local per-directory
plugins. Its own documentation gives the argument: the local mechanism makes
it easy to share behaviour "without the need to create external plugins using
the entry point packaging metadata technique". pytest also allows
`pytest_plugins = [...]` to bless any importable module, noting that "any
module can be blessed as a plugin, including internal application modules".
pytest is also the source of the collision warning above.

**Home Assistant** validates the fuller version. A custom integration is a
directory under `custom_components/` carrying a `manifest.json`, and the
manifest has a `requirements` list of pip specifiers. Home Assistant installs
those at runtime into a `deps` directory. It also ships `--skip-pip` and
`--skip-pip-packages` to opt out, and it advises custom integrations to
declare only requirements the core does not already have. Those escape hatches
and that advice are the evidence for the one place this decision deviates:
declaring requirements is proven and useful, and installing them is the part
that generates operational complexity. Agents Live declares and verifies but
does not install.

**The Python Packaging User Guide** lists three plugin discovery mechanisms:
naming convention, namespace packages, and package metadata. All three exist
to solve discovery across installed distributions. Agents Live does not have a
discovery problem, because the repository configuration supplies the exact
path. Adopting a discovery mechanism to solve a problem the design does not
have was the original error.

**CPython** publishes the direct-load recipe with an explicit caution that it
approximates an import statement and that alternatives should be considered
first. The alternatives it names, modifying `sys.path` or `runpy.run_path`,
are both worse here. `sys.path` manipulation reintroduces cross-plugin
shadowing, and `runpy` yields a namespace dictionary rather than a module
object with a resolvable name.

## Alternatives rejected

**Keep installing wheels into each generation.** Works, and is what 6.7 does.
Rejected because it makes registering a repository rewrite or rebuild an
environment, which contradicts the immutability the generation model exists to
provide, and because it retains every cost listed under Context to serve a
declaration form that cannot reference a package index.

**Install plugins once into a shared directory and add it to each generation's
`sys.path` with a `.pth` file.** Verified working, including entry point
discovery, distribution metadata, and a plugin's own dependencies resolved by
`uv pip install --target`. Rejected because it keeps an installation step and
its resolution failure modes while adding two new hazards: the shared
directory is not resolved against the generation's own packages, so a shared
dependency silently resolves by `sys.path` order, and a plugin that depends on
`agents-live` places a second distribution metadata directory on `sys.path`.

**Content-address generations by runtime version and plugin set**, so a plugin
change produces a new generation name such as `6.8.0+p<hash>`. Rejected as
unnecessary once plugins stopped living inside the generation. It would also
have split the generation name from the package version, which the validation
step currently relies on being equal.

**Install a plugin's declared dependencies, as Home Assistant does.**
Rejected for now. It reintroduces resolution, a writable shared location, and
the version conflicts that the `--skip-pip` escape hatches exist to work
around. The hook is cheap to add later: generation population already accepts
extra requirements.

## Consequences

The plugin set becomes dynamic and global. A registered plugin is picked up by
every installed generation, including ones installed before it was declared,
because loading happens at runtime from a path rather than at build time into
an environment. Registering a repository records a path; it does not rebuild
anything.

Generations stay pure runtime versions. Generation build stops knowing about
plugins.

`plugins.py` loses wheel identity parsing, the union conflict logic, installed
state comparison, the compatibility probe subprocess, and convergence.
`agent/providers/__init__.py` loses entry point discovery and becomes a plain
registry. `state/ownership.py` resolves its backend from a slot the loader
sets, keeping its existing flat sibling fallback.

Plugin loading lives in `plugins.py` because it is above both seams:
`agent/` may not import `agents_live.runtime`, and that layering is enforced
by a fitness test.

A plugin author no longer builds a wheel. For an existing plugin the migration
is to expose the seam attributes from the package `__init__.py` and repoint
`path` at the package directory. The build step and its output become dead
weight.

This is a breaking configuration change: a `.agents-live.toml` declaring a
`.whl` path stops validating. It is not a breaking CLI or definition-format
change, and no agent definition file is affected. It ships in 6.8 with a
changelog migration note.

## History

Supersedes the wheel and entry point model from
[#34](https://github.com/johnshew/agents-live/issues/34), whose reasoning stays
findable in that issue and in the `plugins.py` history before 6.8.
