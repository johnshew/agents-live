---
title: Architecture
description: Runtime, agent, dispatch, state, and observability seams
ms.date: 2026-10-08
ms.topic: concept-article
---

# Architecture

Agents Live is two independent ports composed by lifecycle collection and
dispatch.

## Runtime port

`runtime/` owns automation on one host and separates four capabilities by
lifetime:

- `TriggerStore` survives reboot.
- `Supervisor` survives the process that spawned detached work.
- `ChangeSource` dies with its holder.
- `ChildRunner` dies with one call.

POSIX and Windows adapters implement those protocols. WSL extends the POSIX
adapter with a staged, verified Windows-side liveness task.

One `converge(desired)` operation renders the complete subscription set,
compares it with structured owned artifacts and watcher process markers, and
repairs drift. `health()` is the read-side operation. Version activation
temporarily withdraws triggers and stops watchers before selecting a runtime;
the selected runtime then uses normal convergence to restore started intent.
An interprocess launch gate coordinates activation, lifecycle convergence, and
dispatch lock acquisition without serializing running agents. Activation refuses
active or unverifiable run locks, preserves started state, and attempts rollback
when the selected runtime cannot converge. Idle watchers receive a cooperative
stop signal before bounded host termination is used.

Clock firings wait up to 60 seconds for the launch gate instead of dropping due
work during maintenance. They retain their arrival time for schedule matching,
then reload started state and the definition under the gate before claiming the
per-agent run lock. A wait that expires records a failed run with category
`runtime_activation_timeout`; it is not a successful or skipped firing. Manual
and watcher launches retain their immediate activation-exclusion behavior.

Maintenance also waits up to 60 seconds for the launch gate. A wait records the
observed holder's operation, run, agent, repository and elapsed wait in queryable
admin attributes. Expiry records one `maintenance_deferred` event, keeps the
previous health beacon, waits 30 seconds and attempts acquisition once more.
A second expiry remains deferred, not a maintenance error or a retry storm.
Maintenance collects desired intent, inventories triggers and watchers once,
renders subscriptions and computes the diff before acquiring the gate. Under
the gate it revalidates a persisted runtime commit epoch and cheap registry and
started-state content tokens, then persists adoption and applies the prepared
operations. A stale plan is discarded and recomputed outside the gate, up to
three times; exhaustion records a clean deferral and preserves the health beacon.
Health, retention and reporting remain outside the gate.

Start/stop convergence retains its full gated path. Maintenance, start/stop,
activation and rollback bracket potentially partial mutations with atomic epoch
updates under the gate; dispatch does not advance the epoch. An odd epoch marks
an unfinished commit, so even a plan collected during a writer's critical section
cannot be applied. After acquiring the gate, maintenance can retire an interrupted
writer's odd marker, but must collect a fresh plan before applying anything.
This detects cooperating Agents Live commits, not external edits to definitions
or native scheduler artifacts. Registry and started-state tokens provide an
additional check, not transactional isolation from outside actors.
This policy does not explain historical firings without their original evidence.

Automatic maintenance is the sole writer of the host-local health record.
`doctor --quick` treats a record as healthy only when it is both fresh and
semantically healthy. A fresh degraded record returns its cached category and
remedy without rerunning expensive diagnostics. A missing, stale, or invalid
record triggers that same maintenance operation once, followed by one more
content and freshness check. The command answers only for the runtime where it
runs; it neither discovers nor probes another runtime.

Persisted subscriptions and resident watchers carry one opaque
`agents-live:v2:` metadata envelope. Its payload contains only the deterministic
subscription ID, scope, target, and an optional clock-or-boot origin. The ID is
derived from the artifact contract version plus the scope, target, trigger kind,
and canonical trigger; it replaces separate identity and drift fingerprints.
The command route supplies the watcher and maintenance roles. Trigger stores and
process supervisors decode the same envelope, so native artifacts remain
self-describing without a second registry. Version 1 marker decoding lives only
under `legacy/` and exists to replace old artifacts during convergence.

Host watcher inventory checks the `internal watch-loop` command role before
using metadata for agent identity; legacy `watch-loop` and `--watch-loop`
routes remain recognizable. Scheduled and manual runs are not watchers, even
with agent metadata. Physical launcher and child rows remain available for
cleanup, while deployment deduplicates them by repository and agent. Excluding
ordinary work from watcher inventory does not bypass activation's active-work
or ownership checks.

Every non-preview maintenance pass records correlated start and terminal admin
events. The terminal event includes its source, subscription ID when scheduled,
exit code, convergence counts, watcher and schedule counts, smoketest verdict,
retention counts, and resulting health status. The same pass rotates
framework-owned repository and host logs into queryable archives and removes
expired run transcripts, pipeline journals, and processor channels. A
process-owned marker protects every active run from retention. The health beacon
remains the current-state record; events are the durable account of how it got
there.

Long-lived watchers compare their loaded package version with the installed
distribution at a bounded idle check. A mismatch is handled only between
dispatches: the old loop stops its change source, launches the same marked
subscription through the current CLI, and exits.

## Agent port

`agent/` owns a runnable unit of work through five pure operations:

1. `load` a restricted `Agents/<name>/SKILL.md`;
2. `shape` it into one of six pre/provider/post combinations;
3. `prepare` one `PRE`, `AGENT`, or `POST` launch;
4. `interpret` primitive child output; and
5. `outcome` from collected step results.

Provider plugins contain Claude and Copilot argv and output quirks. The fake
provider and fake CLI exercise the same path deterministically. Providers
receive a narrow immutable projection, not trigger or repository state.
Unattended launches do not implicitly run repository-controlled hooks,
workspace MCP servers, or project extensions, and provider project
instructions are disabled. Claude uses isolated non-bare print mode with
empty setting sources, disabled hooks and slash commands, strict MCP
configuration, and explicit instruction, memory, connector, and IDE opt-outs,
preserving native authentication and managed-policy precedence. Copilot uses a fresh
run-scoped configuration home and explicit prompt-mode opt-out environment
values. Only MCP servers named by `agents-live.mcps` are added to a
non-pipeline session.

## Dispatch

`dispatch.py` is the handoff. A firing contains only repository, agent,
origin, subscription key, and changed files. Dispatch checks started state for
automatic origins, applies due-time, per-agent concurrency, and durable budget
gates, then runs straight-line pre/provider/post sequencing. It owns retries,
PTY selection, run-scoped pipeline resources, timeout enforcement, and cleanup.

Output is normalized after child exit. The provider formats and validation
contract require complete values, so schema version 1 has no incremental
provider parsing hook.

Pipeline definitions may declare ordered fenced `put` blocks in their body.
Dispatch seeds those values into the run-scoped MCP before the first phase,
and seeded paths remain frozen for the run. Copilot-family pipeline launches
make only the `pipeline` server and Copilot's inert `task_complete` control
available to the model; project, built-in, shell, and write tools remain
unavailable.

Retries share one overall deadline, preserving immutable prepared inputs but
resetting mutable attempt output. Expired in-flight puts cannot enter the next
attempt. Per-run timeout overrides only lower the budget; `/run/deadline` and
processor environment expose its absolute deadline. Opt-in failure postprocessing
reserves a bounded allowance inside that budget and sees the final attempt's
partial store and explicit outcome/result-presence markers. Diagnostic retention
is bounded separately from completed values; pipeline telemetry overflow retains
prefix/tail evidence rather than rejecting an independently bounded result.

## State and observability

Repository registration says where to collect. Machine-local started state says
which definitions should be automated here. Missing started state adopts
identifiable installed subscriptions exactly once. Present but unreadable state
causes collection to abstain before any mutation.

An optional assignment plugin may filter collected work. Repository
registration and plugin installation do not activate it. Projects are
local-only until `agents-live ownership enable` validates the backend and
project configuration, then writes the explicit registry declaration. Transfer
operations never create that declaration as a side effect. Local mode makes no
assignment decision.

`obs/` creates versioned immutable event records. The dispatch envelope and
event envelope are deliberately separate.

## Safety invariants

Handlers, processors, and plugins that share a mutable resource can serialize
their critical section with `agents-live lock PATH -- COMMAND`. This public CLI
wrapper uses `LockFileEx` on Windows and `flock` on POSIX, opens the lock file
without truncating it, and avoids coupling consumer code to package internals.
Bare `fcntl` is not a portable fallback: it is unavailable on Windows, where a
handler that continues after `ImportError` holds no lock at all.

- Runtime and agent ports never import each other.
- Only immutable primitive records cross seams.
- Unknown `agents-live.*` metadata is additive and reported by `status` and
  `doctor`. Unsupported schema versions, providers, trigger syntax, and
  retired fields fail closed.
- Global pruning occurs only after complete registry and started-state
  collection.
- An unreadable registry abstains. A registered repository that cannot be
  read, and a started definition that no longer parses, keep the artifacts
  they already own until they resolve again or are stopped.
- Concurrency and misfire policy are fixed to skip.
- Definitions, artifacts, started state, and events have one owner each.
- Release readiness is a state machine, not a checklist. Preparation creates a
  local tagged artifact; installed-candidate acceptance exercises that exact
  artifact against real host state; only a commit/tag/wheel-bound acceptance
  receipt authorizes publication.
- Operational acceptance must use the self-managed stable launcher and real CLI,
  serialization, browser, plugin, scheduler, logs, and health boundaries. A
  source import, mocked envelope, or HTTP bind alone cannot certify release
  behavior.
- Dashboard actions are successful only when the child reports semantic
  success and its exact run ID reaches a successful terminal event. Exit zero
  and a fresh unrelated log record are insufficient.
- Cost acceptance requires exact before/after increases in both dashboard cost
  windows and rejects intervening run IDs. Dashboard health requires the
  current smoketest action and a fresh passing verdict. Cleanup retains process
  identity before later probes and requires confirmed process-tree exit.
  Calling a parser or a termination API is not evidence that the consumer
  surface worked.
- Candidate probes restore the exact all-repository baseline. Failure or
  interruption leaves no durable test intent, dashboard process, or stale
  acceptance authorization. A retry revokes prior authorization before it
  checks whether the candidate is still publishable.
