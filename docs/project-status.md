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
yet as mature as Linux and WSL. The decisive remaining gap is that the native
Windows Copilot smoketest cannot pass end to end. Current P1 defects involve
Windows PATH resolution, text encoding, detached process cleanup, and handler
portability, plus one shared pipeline dependency defect first exposed on
Windows.

## Priority summary

No open issue currently warrants P0 or critical severity. No open issue is
primarily a performance problem; the major Windows process-table and dashboard
performance fixes have shipped.

### P1 bugs

| Issue | Severity | Platform | Impact |
|---|---|---|---|
| [#232](https://github.com/johnshew/agents-live/issues/232) | High | Windows | A timed-out smoketest can wedge maintenance and leave detached watchers. |
| [#238](https://github.com/johnshew/agents-live/issues/238) | High | Windows | An earlier PATH shim can hide an installed `copilot.exe`. |
| [#240](https://github.com/johnshew/agents-live/issues/240) | High | Shared | An unpinned bridge dependency breaks Copilot pipeline mode. |
| [#239](https://github.com/johnshew/agents-live/issues/239) | Medium | Windows | Shipped examples use shell handlers that Windows refuses. |
| [#241](https://github.com/johnshew/agents-live/issues/241) | Medium | Windows | ANSI decoding makes successful smoketest steps report failure. |

### P1 reliability work

- [#184](https://github.com/johnshew/agents-live/issues/184) replaces weak
  mock-boundary confidence with invariant and outcome coverage.
- [#123](https://github.com/johnshew/agents-live/issues/123) escalates agents
  that repeatedly fail instead of allowing infrastructure health to conceal
  chronic run failures.
- [#180](https://github.com/johnshew/agents-live/issues/180) aligns testing
  policy with what the suite can and cannot prove.

P2 contains bounded transaction defects, watcher upgrade policy, and
operational documentation. P3 contains dashboard expansion, adapter-family
extensibility, local span tracing, and additional examples. Those should not
displace cross-platform reliability work.

## Testing strategy decision

Treat #184 as a cross-cutting acceptance criterion for each P1 fix, not as a
standalone rewrite of the test suite.

A broad conversion of mock-driven tests would consume a large implementation
window before proving which replacements improve defect detection. Deferring
all of #184 would preserve the same blind integration boundaries that allowed
the current defects to ship. The chosen approach is incremental: every P1 bug
adds the smallest invariant or real outcome test that would have caught that
exact defect.

| P1 issue | Incremental #184 slice |
|---|---|
| #238 | Test PATH plus PATHEXT enumeration with a refused shim before a real executable, then prove the native Copilot runtime starts. |
| #240 | Assert that package and inline bridge dependency constraints agree, then start the bridge in a fresh uv script environment. |
| #232 | Exercise timeout cleanup with a descendant retaining pipe handles and a detached watcher; assert bounded exit and complete cleanup. |
| #241 | Assert every captured text subprocess in the smoketest uses explicit UTF-8, then run the native Copilot smoketest through the affected step. |
| #239 | Add a host-capability contract test and an end-to-end Windows example using the chosen portable handler path. |

After these fixes, review what remains of #184. Convert additional
mock-driven classes only where the P1 work identifies another concrete
boundary invariant or where a real outcome test can replace a mocked boundary
at reasonable cost. Do not use test or line counts as the objective.

## Recommended sequence

Use native Windows as the primary development and validation platform for the
first five steps. Run the focused Windows check after each change and retain
Linux and WSL CI as the shared-code regression gate.

1. Fix #238 so the supported Copilot executable is reachable on a normal
   Windows PATH.
2. Fix #240 so Copilot pipeline mode starts reliably on a fresh dependency
   resolution.
3. Fix #241 so the Windows smoketest reports real outcomes instead of decoding
   artifacts.
4. Fix #232 so failures and timeouts terminate cleanly without poisoning later
   maintenance runs.
5. Resolve #239, preferably with a portable generic handler and truthful
   documentation. Add shell probing only if shell handlers remain a supported
   Windows contract.
6. Run the full native Windows Copilot smoketest and make that outcome part of
   the release gate.
7. Complete the targeted #184 and #180 policy updates based on the tests added
   above.
8. Implement #123 so sustained agent failure becomes visible in status,
   maintenance health, and doctor output.
9. Address P2 transactional and lifecycle work before beginning P3 feature
   expansion.

## Next stable milestone

The next stabilization milestone is reached when:

- the native Windows Copilot smoketest passes end to end from an installed
  artifact;
- timeout cleanup is bounded and leaves no watcher, fixture, trigger, pipe, or
  lock residue;
- the same release candidate passes Linux and WSL regression checks;
- every repaired P1 boundary has an invariant or outcome test that would have
  failed before the fix; and
- the release process requires those checks rather than treating a mock-heavy
  unit suite as sufficient evidence.