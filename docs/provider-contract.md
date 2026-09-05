---
title: Why the provider contract is shaped this way
description: The complete provider integration contract, the ownership boundary it preserves, and the migration from the 6.8 protocol
ms.date: 2026-09-05
ms.topic: concept
---

# Why the provider contract is shaped this way

A provider is the only module that may know a CLI's name. This file states
the contract that makes that possible, the boundary it must not cross, and
what a 6.8 provider has to change.

Discussed in [#446](https://github.com/johnshew/agents-live/issues/446).

## The problem being solved

The 6.8 protocol was a name, a model set, an effort set, `prepare`, and
`parse`. That was enough to build a command line and read a final result,
and not enough to integrate a CLI.

Everything else a working integration needs lived outside the provider,
keyed on the provider's name: configuration isolation and run-scoped config
files in dispatch and `agent/unattended.py`, pipeline MCP rendering in the
pipeline runtime, executable probing and installation guidance in doctor,
failure interpretation in the agent port, and turn rendering in
observability. Adding a CLI meant editing six modules that had no interest
in it, and forgetting one of them produced a provider that ran but could not
be diagnosed, isolated, or read.

The contract is now complete in one place: a provider describes its whole
integration, and no other module learns its name.

## What a provider declares

`ProviderBase` in `src/agents_live/agent/providers/base.py` supplies the
neutral behavior. A provider overrides only what differs.

**Identity and CLI metadata.** `name` is the selector. `cli` is a
`ProviderCli` carrying the executable to probe, the arguments that make it
report itself, the help surfaces that document emitted options, and per-host
installation guidance. Doctor pins the executable and then runs the probe
arguments, because a pinned executable is a file and not yet a working CLI.
Executable conformance combines the declared help surfaces, including nested
subcommands such as `codex exec`, without learning the provider name. A provider with
no executable of its own sets `executable` to `None` and is never probed nor
offered an install command. The probe arguments are a tuple, so a CLI whose
version lives behind a subcommand is described rather than special-cased.

**Capabilities.** `ProviderCapabilities` names the definition modes,
models, efforts, and MCP transports the integration supports, and whether
the CLI can be handed an output schema natively. Registration rejects a
capability record that is empty or of the wrong type, so an integration
cannot half-declare what it accepts.

**Semantic validation.** `validate` receives the resolved specification and
returns a message naming what is unsupported, or `None` when the combination
is accepted. The port turns a message into a `DefinitionError`. The defaults cover mode, model,
effort, and transport. A provider adds the rules only it can state, such as
Claude's plan-mode tool restriction or Copilot's pipeline allow-list.
Validation runs in `prepare`, before any process is launched, so an
unsupported safety guarantee fails closed rather than silently degrading.

**Run-scoped artifacts.** `artifacts` returns a tuple of `RunArtifact`
records describing the directories and files the run needs: an isolated
home, a settings file, a policy file, an MCP configuration. Each record
names its relative path, its content, its mode, and the environment
variable that should hold its absolute path. Ordering is significant: a
directory precedes the files inside it.

**MCP input.** `artifacts` receives a `ProviderRuntime` carrying the
selected server descriptors and, in pipeline mode, a `PipelineEndpoint`
with the pipeline server's URL, token, and stdio bridge command. The
runtime also carries the resolved output schema so a provider whose CLI
accepts only a schema file can describe that run-scoped artifact. The provider
renders its own CLI configuration from that input. The pipeline runtime owns
the server; the provider only describes how its CLI is told where the server
is.

**Failure and transcript normalization.** `failure` maps raw output to a
category, and `transcript` maps raw output to a `ProviderTranscript` of
turns and tool calls. Doctor and transcript rendering consume the
normalized form, so neither branches on a provider name.

## Providers stay pure

**Decision.** A provider returns descriptions. Dispatch performs the
effects.

A provider never receives a process, a filesystem manager, or a server
object, and never opens a file. `dispatch._provider_files` creates the run's
provider directory at `0o700` under the existing run scratch directory,
writes each artifact at the mode the provider asked for, binds the declared
environment names to absolute paths, and removes the tree when the run ends,
including when it fails. Containment is checked, not trusted: an artifact
whose resolved path leaves the owned directory raises before anything is
written.

**Why.** This is invariant 2 and invariant 3 of the architecture, which the
6.0 agent port established and this contract must not weaken. Keeping the
provider pure is also what makes it testable without a host: the fake
provider proves environment contribution, materialization, cleanup, nested
probing, mode rejection, and transcript normalization with no CLI installed.

**Rejected: handing the provider the run directory.** It would have been
fewer types. It also would have made every provider a filesystem client,
put permission and cleanup correctness in each plugin rather than once in
dispatch, and given a third-party plugin a writable host path.

## A descriptor, not a matrix of booleans

**Decision.** Capabilities are a record of sets, and anything not
expressible as a set is a `validate` rule the provider writes.

**Why.** The alternative that suggested itself was a flag per behavior:
supports isolated home, supports stdio MCP, supports plan mode, requires
allow-list. Flags of that kind accumulate, correlate, and eventually
contradict each other, and no flag can express a rule like "plan mode only
with these tools." A set answers "is this value supported" completely, and
a method answers everything else without inventing vocabulary.

## Migration from the 6.8 protocol

There is no compatibility shim. A 6.8 provider is registered by a 6.9
runtime only after it satisfies the whole contract, and registration names
the member that is missing.

1. Subclass `providers.ProviderBase` instead of implementing the protocol
   from nothing. The defaults then cover `validate`, `artifacts`,
   `failure`, and `transcript`.
2. Replace the `models` and `efforts` attributes with a
   `ProviderCapabilities` record on `capabilities`, adding the definition
   modes and MCP transports the integration supports.
3. Add a `ProviderCli`, including every help surface that documents an option
   the provider emits. A provider with no executable declares `ProviderCli()`.
4. Move any configuration file the integration needed into `artifacts`, and
   read its path from the environment name the record declares rather than
   writing the file.
5. Move any rule that used to be enforced inside `prepare` by raising into
   `validate`, so it is checked before launch.
6. Override `failure` and `transcript` if the CLI's output shape is not the
   generic JSON one.

`prepare` and `parse` are unchanged.

## Consequences

- A source-directory provider plugin can complete a run without any package
  module changing, which is the property the seam test asserts.
- Removing a provider is deleting a module, not auditing six.
- `agent/unattended.py` and `mcp.mcp_config_runtime` are gone; their
  behavior is now Copilot's and Claude's `artifacts`.
- The pipeline runtime no longer writes CLI configuration, so adding a
  provider does not change it.
