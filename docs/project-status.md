---
title: Project Status
description: Platform maturity, prioritized risks, and the next stabilization milestone for Agents Live
ms.date: 2026-07-30
ms.topic: concept
---

Agents Live 5.4.2 has a mature Linux and WSL implementation and a
substantially complete native Windows implementation that is still
stabilizing. The shared architecture is sound, but recent history shows
that integration boundaries have not been exercised as effectively as
isolated units.

## Platform assessment

### Linux and WSL

Cron scheduling, inotify watchers, ownership, WSL heartbeat, agent CLI
dispatch, health checks, and repository configuration have sustained the
longest operational use. Linux and WSL are the stable regression platforms
for shared behavior.

The remaining WSL-specific concern is documentation accuracy rather than a
runtime gap. The WSL runbook uses `bash -lc` where `bash -ic` is required to
load tools initialized by `.bashrc`.
([#242](https://github.com/johnshew/agents-live/issues/242))

### Native Windows

Native Windows has Task Scheduler integration, directory-change watchers,
process-tree management, windowless launches, dashboard support, upgrades,
uninstall, and PowerShell completion. Recent releases fixed scheduler
correctness, uninstall cleanup, process enumeration performance, dashboard
startup, credential redaction, release portability, and transactional
upgrades.

The implementation is feature-complete enough for normal use, but it is not
yet as mature as Linux and WSL. The native Windows Copilot smoketest now passes
all 14 steps from editable source after repairing PATH resolution, text
encoding, detached process recovery, handler portability, and one shared
pipeline dependency defect first exposed on Windows. Each of those was fixed in
the host-runtime seam rather than at its call site, so the shared code carries
no more platform special cases than before - fewer, in fact.

What remains is installation rather than runtime. Installation guidance and
first-run diagnostics still need to match the supported native toolchain,
including the one-time PowerShell completion profile step, before the
end-to-end path is release-ready from an installed artifact.
([#243](https://github.com/johnshew/agents-live/issues/243),
[#244](https://github.com/johnshew/agents-live/issues/244))

## Priority summary

No open issue currently warrants P0 or critical severity. No open issue is
primarily a performance problem; the major Windows process-table and dashboard
performance fixes have shipped.

### P1 stabilization fixes, resolved and unreleased

These are fixed on `main` and awaiting the next release. Each closed with an
invariant or outcome test that fails without the fix.

| Issue | Severity | Platform | What changed |
|---|---|---|---|
| [#232](https://github.com/johnshew/agents-live/issues/232) | High | Windows | Timeout recovery is bounded, and fixtures a killed run leaves behind stay inert. |
| [#238](https://github.com/johnshew/agents-live/issues/238) | High | Windows | Executable pinning continues past refused PATH shims to a native CLI. |
| [#240](https://github.com/johnshew/agents-live/issues/240) | High | Shared | The independently resolved pipeline bridge shares the package's MCP bound. |
| [#239](https://github.com/johnshew/agents-live/issues/239) | Medium | Windows | The shipped generic handler and active examples are portable Python. |
| [#241](https://github.com/johnshew/agents-live/issues/241) | Medium | Windows | Captured child output states its encoding instead of taking the locale's. |

### Windows seam work completed alongside those fixes

Each defect above turned out to be a seam that was incomplete rather than
absent, so every fix landed at the seam rather than at the call site. This is
now the standing rule for platform defects: a fix that adds a special case to
common code, or a mode that compensates for behavior elsewhere, is a sign the
behavior elsewhere is the defect.

| Seam | Was | Now |
|---|---|---|
| Child text decoding | ~40 captures across 15 modules inherited the platform locale, which is the ANSI code page on Windows | One `hostruntime.CHILD_TEXT` member states UTF-8, and an `ast` invariant refuses a capture that names no encoding. Children that write something else settle it themselves: `wintasks` decodes `schtasks` as `oem`, and `powershell_argv` tells PowerShell to write UTF-8 |
| Trigger store | `wintasks` was a real store; the crontab half lived inside `headless`, so `schedules` repeated a platform branch 16 times | `crontasks` is the peer of `wintasks` with matching signatures, and `schedules` chooses a store once |
| Command-line parsing | `headless` imported the Windows leaf inline to read back a command line | `hostruntime.split_command_line` sits beside the enumeration that produced the string |
| Ephemeral fixtures | Five modules open-coded `name.startswith("_")`, and the watcher restart sweep was the one place that did not | `headless.is_ephemeral` names the concept once, and the sweep no longer adopts a fixture a killed run left behind |

The last row is the clearest case. #232 was first fixed with a hidden
`smoketest --cleanup-only` mode so a killed run could be cleaned up by the
process that killed it. Tracing the code showed the residue was not the
problem: a timeout writes a `fail` verdict and the next hourly pass re-runs
the smoketest anyway. The real fault was that the maintenance sweep treated a
leftover fixture as durable intent, tried to restart it, and let the failed
restart suppress the very run whose cleanup would have removed the residue.
The mode came out and the seam gained the rule it was missing.

### P1 reliability work

- [#184](https://github.com/johnshew/agents-live/issues/184) replaces weak
  mock-boundary confidence with invariant and outcome coverage.
- [#123](https://github.com/johnshew/agents-live/issues/123) escalates agents
  that repeatedly fail instead of allowing infrastructure health to conceal
  chronic run failures.
- [#180](https://github.com/johnshew/agents-live/issues/180) aligns testing
  policy with what the suite can and cannot prove.

P2 contains bounded transaction defects, watcher upgrade policy, operational
documentation, and native Windows installation readiness
([#243](https://github.com/johnshew/agents-live/issues/243),
[#244](https://github.com/johnshew/agents-live/issues/244)). P3 contains
dashboard expansion, adapter-family extensibility, local span tracing, and
additional examples. Those should not displace cross-platform reliability
work.

## Testing strategy decision

Treat #184 as a cross-cutting acceptance criterion for each P1 fix, not as a
standalone rewrite of the test suite.

A broad conversion of mock-driven tests would consume a large implementation
window before proving which replacements improve defect detection. Deferring
all of #184 would preserve the same blind integration boundaries that allowed
the current defects to ship. The chosen approach is incremental: every P1 bug
adds the smallest invariant or real outcome test that would have caught that
exact defect.

| P1 issue | Completed incremental #184 slice |
|---|---|
| #238 | Test PATH plus PATHEXT enumeration with a refused shim before a real executable, then prove the native Copilot runtime starts. |
| #240 | Assert that package and inline bridge dependency constraints agree, then start the bridge in a fresh uv script environment. |
| #232 | Exercise a hung run whose detached descendant still holds the output handles, and assert the wait ends on the process rather than the pipes; separately assert the sweep never restarts a leftover fixture watcher. |
| #241 | Walk the package AST and refuse any subprocess capture that names no encoding. This runs on every host, including the ones where the defect cannot be observed. |
| #239 | Run the shipped handler through the real dispatcher and assert both containment and that the two shipped copies are one file. |

The #241 slice is the shape to repeat. The first version of it asserted that a
literal appeared twelve times in one file: it would have broken on any
unrelated edit and proved nothing about decoding. Replacing it with a rule
about the whole package cost fewer lines, cannot drift, and closed the defect
class rather than one file's instance of it. Every slice above was also checked
the other way: the fix was removed and the test observed to fail.

Two findings from this round change what #184 still has to decide.

**The mock-driven population is growing, not shrinking.** #184 measured the
suite at 45 classes, 420 tests, and 632 patch calls. It is now 58 classes, 527
tests, and roughly 750 patch calls: about 1.45 patches per test then and now.
The incremental slices have added value without changing the balance #184 was
filed about, so the choice it poses - convert the mock-driven classes, or state
plainly that their job is import and signature breakage rather than behaviour -
is still open and should be made rather than outrun.

**A flaky Windows-only test is worse than no test.** CI runs Linux and WSL, so
Windows behaviour is asserted almost entirely on a developer machine. The
timeout test added for #232 failed about half the time before it was repaired,
which means the only signal for the platform under most change was one that
could not be trusted. That is also the strongest argument for the invariant
category: an invariant about the whole package runs on every host, including
the ones where the defect it guards cannot be reproduced.

After these fixes, review what remains of #184. Convert additional
mock-driven classes only where the P1 work identifies another concrete
boundary invariant or where a real outcome test can replace a mocked boundary
at reasonable cost. Do not use test or line counts as the objective.

## Recommended sequence

The five P1 stabilization defects are fixed and the seams behind them are
closed. Native Windows stays the primary validation platform, with Linux and
WSL CI as the shared-code regression gate.

1. Resolve #243, then #244 in the same installation-readiness pass. Verify the
   documented native installation path, clean first-run health, platform-aware
   doctor guidance, and the explicit PowerShell completion profile step from
   an installed artifact. This is the only work standing between the current
   state and the milestone below.
2. Run the full native Windows Copilot smoketest from that installed artifact
   and make the outcome part of the release gate.
3. Decide #184's open question - convert the mock-driven population or state
   its role - and land the matching #180 policy text. The measurement above is
   the input; the incremental slices have gone as far as they usefully can
   without that decision.
4. Implement #123 so sustained agent failure becomes visible in status,
   maintenance health, and doctor output.
5. Fix #242 so the WSL runbook matches the shell the tools actually need.
6. Address the remaining P2 transactional and lifecycle work before beginning
   P3 feature expansion.

## Next stable milestone

The next stabilization milestone is reached when:

- the native Windows Copilot smoketest passes end to end from an installed
  artifact;
- the documented native Windows installation and first-run path ends in an
  accurate healthy state and makes PowerShell completion activation explicit;
- the same release candidate passes Linux and WSL regression checks;
- every repaired P1 boundary has an invariant or outcome test that would have
  failed before the fix; and
- the release process requires those checks rather than treating a mock-heavy
  unit suite as sufficient evidence.

The third and fourth conditions are met for the defects fixed so far. Timeout
recovery is bounded and fixtures a killed run leaves behind are inert, so
residue no longer suppresses the next run; that condition has moved from this
list into the resolved table above.