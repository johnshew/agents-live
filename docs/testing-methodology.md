---
title: Testing Methodology
description: What this project tests, at which layer, and why each gate exists
ms.date: 2026-08-11
ms.topic: concept
---

This document states the testing policy: what earns a test, which layer must
prove what, and which gate stands between a change and a release. The
mechanics of running each suite live in
[.agents/testing.md](../.agents/testing.md); this file says what the suites
are for and what may not be traded away.

It exists because the policy failed once, visibly, and the failure is worth
keeping in front of whoever reads this next.

## What happened

The 6.0 refactor rewrote 115 source files. It rewrote the test suite in the
same change, from 9,641 lines and 535 tests to 1,255 lines and 53 tests.
Seven releases followed in three days, each one answering a defect a live
deployment found.

Nine of those defects landed in modules whose test class had been deleted:

| Defect | Deleted coverage |
|---|---|
| Plugin convergence and adapter crashes | `TestProjectPlugins`, `TestAdapterRegistry` |
| Ownership resolved once for every repository | `TestOwnershipEnforcement` |
| Missing ownership backend raised the wrong error | `TestOwnershipKernel` |
| Migration turned exact file watches into non-matching globs | `TestMigratePlanning` |
| Health beacon and `doctor --repair` reporting | `TestHealthCheckLoop` |
| Watcher crontab lines exceeding the host limit | `TestCrontabConvergenceBehavior` |
| Timeline crash on a record with no timestamp | `TestTimeline` |

The last one is the clearest. The deleted test wrote a log containing
`{"ts": []}` and a line of plain text, then asserted `logs timeline` exited
zero. Five days later a live host filed the crash it had been holding.

Two conclusions follow, and they are the reason for the rules below.

A suite that changes in the same commit as the code it covers proves only
that the new code agrees with the new tests. Green is trivially reachable by
deleting whatever is red.

A gate that runs the source does not cover the artifact. Six consecutive
dashboard defects were reachable only by starting a packaged dashboard, which
no gate did.

## The layers

Each layer answers a question the one below it cannot.

| Layer | Command form | Answers |
|---|---|---|
| Source | `uv run --with-editable .` | Does the code in this tree behave? |
| Built wheel | `uv run --with <wheel>` | Does the artifact behave once packaged? |
| Installed tool | bare `agents-live` | Does it behave as a consumer runs it? |
| Live host | a real repository | Does it behave against real history and real state? |

A pass at one layer is not evidence about the layer above it. Three separate
releases exist because packaged imports, reload-worker startup, and an
isolated action interpreter each differ from source execution in ways no
editable run can show.

The live layer is the one that finds cost and scale defects. A dashboard that
answers in 60 ms against a fixture answered in 7.8 seconds against a
repository with 21 agents and 50 MB of history, because it read the whole log
directory once per row. No fixture that size would have shown it.

## What earns a test

Add a test when a failure would be high impact and either silent or
combinatorial. Flag matrices, format parsing, and error classification clear
that bar.

Do not add tests against internal function signatures or one-caller helpers.
They freeze implementation details and catch nothing.

The distinction that matters, and the one that was lost: a test that names a
function is not the same as a test that holds a decision. Ownership matching,
what a trigger store does to a table it shares, which declared plugin is safe
to install, and whether a reader steps over a damaged record are decisions.
They survive a refactor because they are statements about behavior, not about
call shapes.

## Rules

These are the ones the failure bought.

**A refactor may not change code and its tests in the same commit.** Move the
tests first, keep them passing, then move the code. When a test cannot move
because the behavior genuinely changed, say which behavior in the commit
message.

**Deleting a test requires naming what still covers the behavior.** "It froze
an implementation detail" is a valid reason to delete. "It no longer compiles"
is not; that is the moment the test is doing its job.

**Every gate runs against the artifact, not only the source.** The release
gates build the wheel and then exercise it. A cross-module assertion in
`tests/test_behaviors.py` requires every `tests/test_*.py` to appear in both
`tools/release.py` and `.github/workflows/test.yml`, so a suite cannot exist
without being run.

**A test may not assert only serialized values at a translation boundary.**
Assert the decision too. The dashboard once reported the correct state and
offered the opposite actions, and the test written for that table checked
only that rows appeared.

**A stub in place of the thing under test must be deliberate.** Ownership
enforcement was reached with `ownership.owns` replaced by a stub, which proved
the caller branched but never that the matcher decided correctly. Where the
decision is the risk, exercise the real one.

**Interactive surfaces need a readiness assertion, not `--help`.** A
successful GET of `/` proves NiceGUI bound a port. The rows are drawn over a
websocket and are absent from that response.

**Mutating checks use temporary projects.** The exception is a deliberate
operational change, recorded as such. A gate that installs host triggers
pointing at a temporary directory leaves the developer's crontab holding
entries for a path that no longer exists, so the dashboard readiness gate
seeds started state through the artifact instead of calling `start`.

## The gates

`tools/release.py` declares the list once; `prepare`, `publish`, the printed
plan, and the publish workflow all run it from there, because restating it in
YAML once shipped a release past a gate the local run kept.

1. `tools/pre-release-audit.py` - export-clean tree, packaging, doc links.
2. `tests/test_smoke.py` - the chain from definition to dispatch to stop.
3. `tests/test_seams.py` - the port contracts and architecture fitness.
4. `tests/test_behaviors.py` - the decisions listed above.
5. `agents-live smoketest` - a real trigger, run, and status loop.
6. `uv build`.
7. `tools/dashboard-readiness.py` - the built wheel serves `/api/agents` with
   the expected row, its state, and its Start and Stop availability, in normal
   and reload-worker modes.

CI runs the same suites on Ubuntu and Windows for every push and pull request,
and the publish workflow cannot publish until both hosts pass.

## Verifying a live deployment

Automated gates cannot cover ownership transfer, host schedulers, or behavior
at real scale. Before a release that touches those, exercise them on a host
and record what was observed.

Capture a baseline first and compare against it at the end:

```bash
agents-live status --all-repos --json > baseline.json
agents-live doctor --all-repos --json >> baseline.json
```

Then exercise each surface and confirm the observability layer saw it:

- Run, Start, and Stop through the dashboard, not only through the CLI. The
  dashboard reaches the CLI through a different path than a terminal does, and
  that path has broken on its own.
- Confirm each action appears in `agents-live logs timeline`, correlated with
  the run it caused. Never read the log files directly; the readers correlate
  across files and transcripts, and hand-parsing has repeatedly produced wrong
  conclusions.
- Confirm `agents-live logs --errors` is empty, or that every entry predates
  the exercise.
- Confirm the final state matches the baseline, and restore anything the
  exercise changed.

For a multi-host deployment, confirm the second runtime resolves the same
registry and reports the same ownership before attempting a transfer. A
runtime whose declared ownership plugin was never converged into its tool
environment fails closed and cannot participate, which is invisible from the
first host until someone tries.

## History

Written after the 6.0.0 to 6.0.6 series, alongside the restoration of the
deleted behavioral coverage and the built-wheel dashboard gate.
