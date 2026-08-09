---
title: Project Status
description: Current implementation state, platform maturity, validation, and remaining work
ms.date: 2026-08-09
ms.topic: concept
---

# Project status

Agents Live 6.0 is implemented on the
[`copilot/implement-runtime-and-agent-seams-refactoring`](https://github.com/johnshew/agents-live/pull/256)
branch and is under review. It is a breaking architecture and definition-format
release, not yet the published stable version.

The current implementation matches [architecture.md](architecture.md): agent
definitions and providers, host runtime automation, state, observability,
pipeline resources, and CLI composition have explicit owners. The remaining
5.x code is isolated under `legacy/` except for two durable-path entry points
that must remain at package root through 6.x.

## Implemented in 6.0

- Agent Skill bundles and configured flat definitions share one strict,
  namespaced metadata contract.
- Canonical definition identifiers include a repository-relative path hash.
- Runtime convergence uses structured subscriptions and native artifacts.
- Linux, native Windows, WSL, and memory host adapters share runtime protocols.
- Provider selection and launch normalization live behind the agent port.
- Scheduled and watched firings enter one dispatch path.
- Started intent, optional ownership, repository registration, and runtime
  artifacts are separate facts.
- Agent and administrative events use the observability schema and query layer.
- CLI commands and optional-dependency scripts live under `cli/`.
- 5.x definitions and durable triggers have explicit migration or refusal paths.

## Platform maturity

| Platform | Current state | Remaining work |
|---|---|---|
| Linux | Primary POSIX implementation; schedule, watch, process, and lifecycle paths are implemented | Continue release-candidate regression coverage |
| WSL | POSIX runtime plus verified distro-scoped Windows liveness | Correct the detailed shell runbook in [#242](https://github.com/johnshew/agents-live/issues/242) |
| Native Windows | Task Scheduler, change notifications, process trees, windowless actions, dashboard, upgrade, and uninstall are implemented and tested in CI | Installation and first-run guidance [#243](https://github.com/johnshew/agents-live/issues/243), PowerShell profile guidance [#244](https://github.com/johnshew/agents-live/issues/244), and refused-shim diagnosis [#246](https://github.com/johnshew/agents-live/issues/246) |
| macOS | Expected to follow the POSIX adapter but not validated | File an issue before claiming support |

See [windows-support.md](windows-support.md) and
[wsl-support.md](wsl-support.md) for platform architecture.

## Validation baseline

As of 2026-08-09 on this branch:

- 32 portable smoke and seam tests pass; one POSIX-only PTY assertion is
  skipped on Windows;
- the public `agents-live smoketest` passes;
- dynamic CLI command and script targets resolve;
- the pre-release export and documentation audit passes; and
- source and wheel builds are part of the release gate.

The suite deliberately emphasizes complete temporary-repository workflows,
architecture invariants, memory-host convergence, and fake-provider outcomes.
Issue [#184](https://github.com/johnshew/agents-live/issues/184) and policy issue
[#180](https://github.com/johnshew/agents-live/issues/180) remain open because
the wider historical suite relied too heavily on mocked implementation details.

## Remaining work

### P1

- [#255](https://github.com/johnshew/agents-live/issues/255): complete review
  and merge of the 6.0 runtime and agent seams.
- [#184](https://github.com/johnshew/agents-live/issues/184) and
  [#180](https://github.com/johnshew/agents-live/issues/180): settle the role
  and policy of mock-driven tests.
- [#123](https://github.com/johnshew/agents-live/issues/123): expose sustained
  agent failure in status and health.

### P2 release and lifecycle work

- Native Windows installation and diagnostics:
  [#243](https://github.com/johnshew/agents-live/issues/243),
  [#244](https://github.com/johnshew/agents-live/issues/244), and
  [#246](https://github.com/johnshew/agents-live/issues/246).
- Transactional host changes:
  [#226](https://github.com/johnshew/agents-live/issues/226) and
  [#227](https://github.com/johnshew/agents-live/issues/227).
- Processor dependency validation:
  [#254](https://github.com/johnshew/agents-live/issues/254).
- Git-index diagnosis:
  [#253](https://github.com/johnshew/agents-live/issues/253).
- Watcher restart policy after upgrade:
  [#204](https://github.com/johnshew/agents-live/issues/204).

The complete work-item list remains in GitHub issues. Themes rather than issue
details belong in [backlog.md](backlog.md).

## Compatibility horizon

6.x recognizes enough 5.x state to migrate or safely remove durable artifacts.
It does not accept the old definition format as valid input. In 7.0:

- remove `legacy/`;
- remove the root `hidden.py` persisted-task entry point;
- remove the root `windows-heartbeat.sh` scheduled-task wrapper; and
- remove retired-field diagnostics after the migration window closes.

## Next release sequence

1. Merge and revalidate the 6.0 architecture branch.
2. Complete native Windows installation and first-run documentation.
3. Run source and built-wheel smoke gates on Windows and Ubuntu.
4. Resolve the P1 testing-policy decision before expanding feature scope.
5. Prepare the release with changelog and artifact inspection gates.
