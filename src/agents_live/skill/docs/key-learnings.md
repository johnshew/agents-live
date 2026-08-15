---
title: Runtime and provider learnings
description: Constraints established while extracting the 6.0 seams
ms.date: 2026-08-14
ms.topic: concept-article
---

# Runtime and provider learnings

## Collect before pruning

A trigger store is machine-global while definitions are repository-scoped.
Convergence must receive one complete desired set after registry collection,
started-state filtering, and optional assignment. Calling convergence once per
repository lets the last repository prune every earlier one.

Missing started state is not the same as an empty started set. On first use,
installed structured artifacts are adoption evidence. An existing state record
that cannot be read is abstention evidence.

## Mark every owned artifact

Substring matching of command lines is not ownership. Durable triggers carry a
versioned canonical JSON marker encoded for their host store. Detached watcher
argv carry role, key, and fingerprint fields. Enumeration ignores anything it
cannot fully decode.

The fingerprint is embedded in both durable artifact and watcher argv. No
machine-local side index is needed, including at the measured Windows
command-line bound.

## Keep lifetimes separate

Durable triggers, detached watchers, held change streams, and provider children
fail differently and need different cleanup. One broad host service obscures
those obligations. The four runtime protocols make ownership and recovery
explicit.

Run locks are per agent, not per trigger, so a clock and watcher firing cannot
overlap. Dead lock owners are recoverable. The dispatch budget is atomically
updated under an inter-process lock and deliberately fails open if its own
state is unavailable.

## Bound cascading watcher writes

Watchers that write into one another's watched paths form a directed graph.
Before enabling such a system, enumerate every watcher and file, trace each
cycle from an external edit back to quiescence, and identify the deterministic
guard that breaks every re-trigger path. Suitable guards include unchanged
content checks, stable content hashes, monotonic source/output timestamps, and
idempotent writes.

Each cycle needs a bounded termination argument. A healthy path normally does
one meaningful dispatch and at most one skipped re-trigger. Log both guard
passes and skips with the value that made the decision; otherwise a loop can
burn provider requests while appearing idle, or silently suppress a real edit.

Processor-only tests do not exercise the change source, debounce window, or
dispatcher guards. Validate the deterministic guards directly, then run one
live watcher flow to prove that the complete cycle reaches quiescence.

## Normalize at the right boundary

Claude and Copilot emit complete machine-readable values through
provider-specific formats. Output schemas, provenance, size caps, path roots,
and post-processors all consume a completed value. A fake streaming CLI
produced no provider-independent partial contract, so interpretation happens
once after child exit.

Provider quirks belong in provider plugins. Due-time, retries, concurrency,
budget, resources, and child cleanup belong in dispatch. Error classification
and output validation belong in the pure agent port.

## Definitions must be portable

The `Agents/<name>/SKILL.md` layout lets standard skill tooling validate the
bundle. Quoted `agents-live.*` metadata separates unattended execution policy
from standard Agent Skills properties. `allowed-tools` and
`agents-live.allow-tools` remain distinct because interactive preapproval must
not silently grant unattended authority.

The one-shot migrator refuses environment values, host assignment, and
client-specific fields. Refusal is safer than copying a possible secret or
guessing a nonportable meaning.

## Liveness is runtime state

WSL liveness is not a fourth lifecycle verb. A replacement task is staged and
started under a distinct name, then a fresh atomic beacon is verified before
the stable task or any legacy task is replaced. A failed verification leaves
the working task unchanged.
