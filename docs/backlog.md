---
title: High-Level Backlog
description: Themes and direction for agents-live, linked to the GitHub issues that carry the detail
ms.date: 2026-07-30
ms.topic: concept
---

Direction at the theme level. Individual work items, bugs, and acceptance
criteria live in GitHub issues (`gh issue list`); this file exists so the
shape of the work stays visible when the issue list does not show it.

A theme without an open issue is direction, not committed work.

## Local observability

Give the existing JSONL event stream span identity, parent-child
correlation, and sensitivity classification, so nested phase timing and
causality are explicit locally. OpenTelemetry, OTLP export, and any
off-box log shipment stay postponed until a consumer needs them; a future
sidecar can translate the local schema instead.
([#105](https://github.com/johnshew/agents-live/issues/105))

The same stream should also answer whether the agents are working, not
just what they did. An agent that fails every run stays invisible today:
status keeps showing a fresh error time, and the health beacon reports
only infrastructure state. Escalating that from existing log data comes
before any richer export.
([#123](https://github.com/johnshew/agents-live/issues/123))

## Host changes that cannot half-finish

Several commands mutate host state that they may not be able to finish
mutating. `init` registers a repository before plugin convergence can
fail ([#226](https://github.com/johnshew/agents-live/issues/226));
`upgrade` lets uv remove a plugin before discovering that a running
process locks the launcher it has to replace
([#231](https://github.com/johnshew/agents-live/issues/231)). Both leave
the host in a state the operator did not ask for and cannot easily read.

`uninstall` already sets the precedent. It detects the processes running
from the tool environment before touching anything, refuses outright
while one survives, and hands the last step to a helper that waits
outside the environment being removed. The direction is to hold every
host-mutating command to that shape: know the blockers before the first
write, and leave the prior state intact when you cannot proceed.

Whether an upgrade should go further and restart the watchers it makes
stale is a separate policy question, still open
([#204](https://github.com/johnshew/agents-live/issues/204)).

## Confidence in the test suite

Every defect found in the week of 2026-07-27 shipped through a green
suite. That is a measured fact, and it points at structure rather than
at missing coverage: the mock-driven population of the suite cannot
execute the paths that keep breaking
([#184](https://github.com/johnshew/agents-live/issues/184)), and the
policy never required that anything assert the user-visible claim
([#180](https://github.com/johnshew/agents-live/issues/180)).

These are complements, not duplicates: one rebalances what the suite
executes, the other changes what counts as coverage before a fix is
called done.

The slices done so far point at one shape worth repeating. An invariant
that states a rule about the whole package - no subprocess capture may
rely on the platform locale - costs less than the mock tests it replaces,
cannot drift as the package grows, and runs on hosts where the defect it
guards cannot be reproduced. That last property matters most on Windows,
where the platform receiving the most change is the one CI sees least.
An assertion about a literal in one file is the anti-pattern: it breaks
on unrelated edits and proves nothing. A test is not finished until the
fix has been removed and the test watched to fail.

The corollary, learned the hard way: a Windows-only test that flakes is
worse than no test, because on that platform it is the only signal and
an untrustworthy signal invites ignoring the suite.

What these slices have not done is change the balance #184 was filed
about. The suite has grown from 45 classes and 420 tests to 58 and 527
at a constant ~1.45 patch calls per test, so the mock-driven population
is keeping pace rather than shrinking. The decision #184 poses - convert
those classes, or state plainly that their job is import and signature
breakage rather than behaviour - is still open, and incremental slices
will not make it for us.

## Safer execution modes in practice

`plan` and `pipeline` are documented as the safe defaults, but the
runnable example in the README uses `write`. Publish complete `plan` and
`pipeline` variants of the same watcher task, including schemas,
handlers, and processors, so the safer modes are as easy to adopt as the
permissive one.
([#116](https://github.com/johnshew/agents-live/issues/116))

## Platform coverage

Linux is the primary platform, with Ubuntu on WSL as the reference setup.
macOS is untested; broadening it is direction rather than committed work,
so file an issue before starting.

A native Windows runtime, replacing cron and inotifywait with Task
Scheduler and Windows change notification behind a small host-runtime
seam, is implemented and covered by CI on `windows-latest`.
[windows-support.md](windows-support.md) is the architecture guide: what
the seam is, why it is functions rather than a protocol object, and what
the spikes contradicted. Whether the Windows half earns its keep in the
long run stays an open product question; the seam itself is settled.

The direction for keeping it settled is that a Windows defect is fixed at
the seam, not at the call site. Three rounds of that have now landed:
child-output decoding became a host-runtime member instead of a habit
repeated in every module, the crontab became a trigger store beside Task
Scheduler instead of mechanics inside `headless` that forced the dispatch
point to branch per operation, and reading a command line back moved
beside the enumeration that produced it. All three removed code.

Two tests for any future platform fix follow from that. Does it leave
common code with one more special case or one fewer? And if it needs a
new mode or flag to compensate for behaviour elsewhere, is that other
behaviour the actual defect? The second test retired a hidden
`smoketest --cleanup-only` mode: the residue it cleaned up was harmless,
and what was really wrong was that the maintenance sweep tried to adopt
an ephemeral fixture. Fixtures now belong to the run that creates them,
named once as `headless.is_ephemeral` and honoured everywhere.

What is not settled is process and file lifecycle on that platform.
Windows will not delete or replace a running executable, and the defects
that follow from it keep arriving through the seam rather than in it:
locked launchers during upgrade, deferred self-removal during uninstall,
and detached processes that outlive the run that started them. Those are
tracked under the theme above rather than here.

Installation and first-run readiness on native Windows is the remaining
gap before the platform is releasable from an installed artifact
([#243](https://github.com/johnshew/agents-live/issues/243),
[#244](https://github.com/johnshew/agents-live/issues/244)).

## Maintaining this file

- Add a theme when work spans several issues or needs a stated direction
  of its own.
- Link each theme to its open issues; do not duplicate their content.
- Rewrite or remove a theme once it ships, in the same change that ships
  it.
