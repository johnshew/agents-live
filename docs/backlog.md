---
title: High-Level Backlog
description: Themes and direction for agents-live, linked to the GitHub issues that carry the detail
ms.date: 2026-08-09
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

## Observability a processor can contribute to

A processor has three channels and all three are lossy: an exit code, stderr
that surfaces only on failure, and stdout that 6.0 now records but bounds. 5.x
let a handler write structured entries directly into the log; 6.0 withdrew that
without replacing it, so the capability regressed.

The replacement should not be a Python API. Processors are `.py`, `.js`, `.ts`,
`.ps1`, or `.sh`, so a Python surface serves one of five and couples user code
to module paths, which is exactly what broke when 6.0 moved them. The
contract should be an environment handle naming an append-only JSONL file,
with correlation context passed in.

GitHub Actions is the closest precedent and already made this migration, from
stdout workflow commands to environment-file handles. Its `stop-commands`
escape hatch remains as evidence of why: content flowing through stdout can
impersonate control syntax. That risk is sharper here, because a
post-processor's input is model output.

For correlation, W3C Trace Context is the standard and OpenTelemetry defines
the carrier as string key-value pairs, so `traceparent` in the environment is a
conforming adaptation. Adopting it verbatim makes a future exporter a
translation rather than a remapping.

[#105](https://github.com/johnshew/agents-live/issues/105) carries this,
together with span identity, per-step isolation and size caps, and the
sensitivity metadata that a redaction rule needs.

## Confidence in the test suite

Every defect found in the week of 2026-07-27 shipped through a green
suite, and so did the three found in the 6.0 release review against a
completely different suite. That is a measured fact, and it points at
structure rather than at missing coverage: nothing required that the
user-visible claim be asserted anywhere
([#184](https://github.com/johnshew/agents-live/issues/184), which now
absorbs the policy question first raised as #180).

The slices done so far point at one shape worth repeating. An invariant
that states a rule about the whole package - no subprocess capture may
rely on the platform locale - costs less than the mock tests it replaces,
cannot drift as the package grows, and runs on hosts where the defect it
guards cannot be reproduced. An assertion about a literal in one file is
the anti-pattern: it breaks on unrelated edits and proves nothing. A test
is not finished until the fix has been removed and the test watched to
fail.

The corollary, learned the hard way: a Windows-only test that flakes is
worse than no test, because an untrustworthy signal invites ignoring the
suite.

The 6.0 architecture work retired the 527-test mock-heavy suite and
replaced it with a small portable one over memory hosts and fake
providers. That resolved the ratio #184 was filed about, but not the
question behind it. The first release review found three defects the new
suite could not see, because it verified structure where behaviour was
what mattered: convergence removing artifacts it could not account for, a
diagnostic command that failed at import, and a migrator that refused most
real 5.x definitions. Structural invariants are necessary and cheap; they
are not sufficient. What #184 still has to settle is which behaviours are
owed an executing test, now that there is no large mock population to
argue about.

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

A native Windows runtime, replacing cron and POSIX file notification with Task
Scheduler and `ReadDirectoryChangesW` behind the host protocols, is implemented
and covered by CI on `windows-latest`. [windows-support.md](windows-support.md)
records the current native architecture. [wsl-support.md](wsl-support.md)
records the separate WSL composition and Windows-side liveness responsibility.
The seams are settled; installation readiness remains tracked work.

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
