---
title: Runtime and provider learnings
description: Constraints established while extracting the 6.0 seams
ms.date: 2026-09-19
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

Foreground child ownership begins before execution. Windows children start
suspended and join a kill-on-close Job Object before resuming; POSIX children
lead an owned process group. Cleanup must reach descendants even after the
original parent exits. Bounded stream readers must report persistence failures
and close their pipes before returning.

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

Diagnostic snapshots may be read during execution without claiming a completed
provider value. Bound diagnostic streams separately from the parsed completion
or declared pipeline result. Retry from prepared immutable inputs, not an earlier
attempt's mutable output. The shared deadline bounds retries and processors;
cleanup has its own bounded, reported duration.

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

## Validate the consumer before publication

A source checkout, built wheel in isolation, and installed tool are different
systems. The installed tool adds launcher replacement, uv receipts,
co-installed plugins, native triggers, long-lived watcher processes, real
logs, and browser state. Those are exactly where release-only defects have
appeared.

Prepare numbered RCs locally, then install the exact wheel and operate it.
Prepare final-version bytes independently; tag only after final acceptance.
Exercise CLI Run, Start, Stop, status, doctor, logs, plugin
convergence, usage and cost capture where the selected provider reports it,
health beacon repair, and dashboard health plus Run, Start, and Stop through a
real browser. Snapshot every registered repository before and after. Any
mismatch invalidates the candidate.

Do not equate process exit zero with successful work. A dispatch may exit zero
after reporting `skipped`; acceptance must retain its run ID and require the
matching successful terminal event. Verify cost through the dashboard's own
row model as a before/after increase, not an absolute historical total or a
duplicate parser. Require both cost windows to increase by the correlated run
cost and reject any intervening run ID. Dashboard health must consume a fresh verdict
from the current smoketest, not a prior pass. Retain process identity before
later probes, then verify that dashboard descendants have exited after cleanup.
A termination request is not proof of termination.

Mock only dependencies outside the decision under test. If production reads a
CLI JSON envelope, qlog JSONL, DuckDB attributes, a Windows helper result, or a
NiceGUI websocket-rendered button, the regression must cross that same
boundary. Several green tests failed because they asserted locally invented
shapes.

Independent review is most valuable before commit and before publication,
focused on bypasses, races, cleanup, stale authorization, and false success.
When review finds a defect, add a discriminating regression, rerun the complete
candidate loop, and do not reuse an earlier acceptance result. Revoke the old
receipt before evaluating any retry precondition.

<!-- glp-update:v1 id=dcce029f-e251-4b88-bb1b-4ad450889f6f -->
<a id="glp-dcce029f-e251-4b88-bb1b-4ad450889f6f"></a>
### GLP Update: Own foreground descendants before execution

- Update-ID: dcce029f-e251-4b88-bb1b-4ad450889f6f
- Recorded-UTC: 2026-09-19
- Kind: correction
- Topics: child-lifetime, diagnostic-retention, timeout
- Workstream: RC pipeline timeout correction
- Target: Keep lifetimes separate; compared source ca9f81829543f21b73b8f23d7de7cb9a026a50ff
- Source-Session: Runtime regression investigation, 2026-09-19
- Evidence-Basis: observed
- Application: applied
- Consolidation: integrated into lifetime and normalization sections on 2026-09-19; closure recorded in the development release process

#### Change
Child-tree lookup at timeout is insufficient when the parent already exited
and descendants retain diagnostic pipes. Establish ownership before execution:
a suspended Windows child joins a kill-on-close Job Object before resuming;
POSIX children lead a dedicated process group. Diagnostic persistence failures
must terminate the owned children and report failure rather than lose a reader
thread silently. Provider completion bounds apply to business values, not the
complete diagnostic event stream.

#### Evidence
`TestRuntimeProcessPolicy.test_child_capture_failure_is_explicit_and_orphaned_pipes_are_closed`
failed with incomplete cleanup before Job Object ownership and passed afterward
on Windows. The paired capture-limit/descendant-timeout test also passed.
`TestAgentPipeline` and `TestTranscriptRetrieval` passed 53 tests with one
platform skip after retry isolation and partial transcript changes.

#### Previous Knowledge
The lifetime and consumer-validation sections required child cleanup but did not
distinguish a live parent from an exited parent whose descendants held pipes.
Completed-value interpretation remains separate from raw in-progress snapshots.

#### Verification and Limits
This is source-test evidence, not installed-provider acceptance. Cross-platform
CI and exact-package/live acceptance remain separate gates. Pruning availability
markers retain no provider content and expire after another retention period.

#### Follow-up
Consolidate into the lifetime and normalization sections; retain this failure
evidence and validate the packaged candidate through the official RC workflow.
<!-- /glp-update:v1 id=dcce029f-e251-4b88-bb1b-4ad450889f6f -->
