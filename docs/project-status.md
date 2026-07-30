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
encoding, detached process cleanup, handler portability, and one shared
pipeline dependency defect first exposed on Windows. Installation guidance
and first-run diagnostics still need to match the supported native toolchain,
including the one-time PowerShell completion profile step, before that
end-to-end path is release-ready from an installed artifact.
([#243](https://github.com/johnshew/agents-live/issues/243),
[#244](https://github.com/johnshew/agents-live/issues/244))

## Priority summary

No open issue currently warrants P0 or critical severity. No open issue is
primarily a performance problem; the major Windows process-table and dashboard
performance fixes have shipped.

### Completed P1 stabilization fixes

| Issue | Severity | Platform | Impact |
|---|---|---|---|
| [#232](https://github.com/johnshew/agents-live/issues/232) | High | Windows | Timeout recovery is bounded and removes detached smoketest residue. |
| [#238](https://github.com/johnshew/agents-live/issues/238) | High | Windows | Executable pinning continues past refused PATH shims to a native CLI. |
| [#240](https://github.com/johnshew/agents-live/issues/240) | High | Shared | The independently resolved pipeline bridge shares the package's MCP bound. |
| [#239](https://github.com/johnshew/agents-live/issues/239) | Medium | Windows | The shipped generic handler and active examples are portable Python. |
| [#241](https://github.com/johnshew/agents-live/issues/241) | Medium | Windows | Every smoketest text capture decodes UTF-8 explicitly. |

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
| #232 | Exercise timeout cleanup with a descendant retaining pipe handles and a detached watcher; assert bounded exit and complete cleanup. |
| #241 | Assert every captured text subprocess in the smoketest uses explicit UTF-8, then run the native Copilot smoketest through the affected step. |
| #239 | Add a host-capability contract test and an end-to-end Windows example using the chosen portable handler path. |

After these fixes, review what remains of #184. Convert additional
mock-driven classes only where the P1 work identifies another concrete
boundary invariant or where a real outcome test can replace a mocked boundary
at reasonable cost. Do not use test or line counts as the objective.

## Recommended sequence

The first five stabilization steps are complete. Retain native Windows as the
primary validation platform and Linux and WSL CI as the shared-code regression
gate for the remaining sequence.

1. Resolve #243, then #244 in the same installation-readiness pass. Verify the
  documented native installation path, clean first-run health, platform-aware
  doctor guidance, and the explicit PowerShell completion profile step from
  an installed artifact.
2. Run the full native Windows Copilot smoketest from that installed artifact
   and make the outcome part of
   the release gate.
3. Complete the targeted #184 and #180 policy updates based on the tests added
   above.
4. Implement #123 so sustained agent failure becomes visible in status,
   maintenance health, and doctor output.
5. Address the remaining P2 transactional and lifecycle work before beginning
   P3 feature expansion.

## Next stable milestone

The next stabilization milestone is reached when:

- the native Windows Copilot smoketest passes end to end from an installed
  artifact;
- the documented native Windows installation and first-run path ends in an
   accurate healthy state and makes PowerShell completion activation explicit;
- timeout cleanup is bounded and leaves no watcher, fixture, trigger, pipe, or
  lock residue;
- the same release candidate passes Linux and WSL regression checks;
- every repaired P1 boundary has an invariant or outcome test that would have
  failed before the fix; and
- the release process requires those checks rather than treating a mock-heavy
  unit suite as sufficient evidence.