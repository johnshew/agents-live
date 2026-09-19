---
title: High-Level Backlog
description: Themes and direction for agents-live, linked to the GitHub issues that carry the detail
ms.date: 2026-09-19
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

Health escalation and retention are delivered foundations. The remaining
observability direction is the local schema work above, not the closed health
work in [#123](https://github.com/johnshew/agents-live/issues/123).

## Observability a processor can contribute to

A processor now has separate output, log, and control channels. Build on that
delivered contract for correlation and sensitivity metadata; the old 6.0
channel gap is not a pending replacement project.

The extension should not be a Python API. Processors are `.py`, `.js`, `.ts`,
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
fail. The historical policy issue is closed; the current requirements live in
[testing-methodology.md](testing-methodology.md).

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
are not sufficient. This remains an engineering requirement for each change,
not unfinished scope in the closed #184.

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

Platform-enablement issues #243 and #244 are closed. Current Windows bootstrap
and installed recovery acceptance belong to the active release cycle
([#517](https://github.com/johnshew/agents-live/issues/517),
[#522](https://github.com/johnshew/agents-live/issues/522)).

## Installation and deployment reliability

The first priority cluster is the installation and upgrade path itself. The
project already accepts the generation-based deployment model as the durable
shape for the runtime: immutable version directories, one stable `current`
directory link, and ownership-aware health checks. Public bootstrap installs
authenticated release bytes into that model on Windows and POSIX without a
custom launcher or package-index fallback for Agents Live.

The shared construction path and uv ownership retirement have landed. Remaining
work is numbered candidate identity, installed acceptance, stable finalization,
and recoverable publication without reusing failed artifact identities.

The direction is that the bootstrap does transport and nothing else: verify
bytes, stage a throwaway environment, and let the package build the real
generation through the one code path every other install uses.

Current work is tracked by [#511](https://github.com/johnshew/agents-live/issues/511).
The earlier #334 and #395 are delivered foundations.
[compatibility-boundaries.md](compatibility-boundaries.md) records what the
uv retirement does and does not break.

## Rapid local release candidates

Make local build and activation a short, recoverable loop, distinct from full
qualification and stable publication. Reuse input-qualified evidence and resume
failed checks without repeating unrelated successful work. Preserve immutable
packages, service-state restoration and truthful pending-qualification reporting.
The prioritized design and measurement criteria live in
[#535](https://github.com/johnshew/agents-live/issues/535), linked to the
[release process](development-release-process.md). This is planned work, not
permission to bypass the current preparation or publication gates.

## Extension seams

A declared plugin extends one of two duck-typed protocols. Until 6.8 it
reached them as an installed wheel discovered through entry points, which is
the shape `uv tool install --with` made convenient rather than the shape the
seams need. The declaration format has always required a repository-relative
path inside the declaring repository, so a plugin could never come from a
package index; the runtime was paying for packaged distribution to load a file
whose path it already had.

6.8 makes a plugin a source directory loaded dynamically against the protocol.
pytest and Home Assistant are the grounding precedents, and they disagree in
the one place that matters: whether the host installs a plugin's declared
dependencies. Agents Live declares and verifies but does not install.

[decisions/plugin-loading.md](decisions/plugin-loading.md) carries the
decision, the precedent, and the alternatives rejected.

## Runtime repair and operational safety

The next P1/P2 cluster is runtime health. A watch that silently drops events,
reports a running agent after the process has died, or leaves the local logs and
transcripts unbounded is operationally worse than a failing but visible run.

The watch-health and retention work in #393 and #259 is closed. Continue with
local observability in [#105](https://github.com/johnshew/agents-live/issues/105),
and investigate the processor-control lifetime report in
[#519](https://github.com/johnshew/agents-live/issues/519). Make failures
explainable before expanding the runtime surface.

## Repository and execution policy

The remaining platform work keeps the repository model explicit and policy-led.
Registration, init, discovery, ownership opt-in, and the trust boundary for
provider-controlled hooks do not belong to one command or one directory, and
keeping them separate is the delivery strategy.

The ownership and execution-safety work in #365, #374, #375, and #376 is closed.
Remaining direction is the registration/init split in
[#388](https://github.com/johnshew/agents-live/issues/388), retirement of 5.x
compatibility in [#434](https://github.com/johnshew/agents-live/issues/434), and
the internal versus persisted identity boundary in
[#489](https://github.com/johnshew/agents-live/issues/489).

## Dashboard and usage

Separate configured model/effort display from editing and context controls;
the release disposition stays in [#508](https://github.com/johnshew/agents-live/issues/508)
while [#525](https://github.com/johnshew/agents-live/issues/525) owns the remaining
controls, proposed for 6.10. Numeric usage
summaries should explain recorded work without adding an execution bypass
([#478](https://github.com/johnshew/agents-live/issues/478)). Managed package-proxy
availability remains independent external verification
([#486](https://github.com/johnshew/agents-live/issues/486)).

## Maintaining this file

- Add a theme when work spans several issues or needs a stated direction
  of its own.
- Link each theme to its open issues; do not duplicate their content.
- Rewrite or remove a theme once it ships, in the same change that ships
  it.
