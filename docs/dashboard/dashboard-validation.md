---
title: Dashboard Validation Plan
description: Acceptance scenarios, evidence, and release gates for the Agents Live dashboard
ms.date: 2026-08-29
ms.topic: concept
---

# Dashboard validation plan

## Purpose

This plan defines how the product outcomes in
[dashboard-product-requirements.md](dashboard-product-requirements.md) and the
requirement IDs in [dashboard-requirements.md](dashboard-requirements.md) earn
acceptance. It specifies observable evidence, not implementation structure.

The repository-wide testing policy in
[testing-methodology.md](../testing-methodology.md) still controls what earns an
automated test and which execution layer proves it.

## Evidence principles

1. Validate the user journey, not the presence of a widget or source literal.
2. Exercise the installed artifact before publication; editable-source success
   does not prove packaged behavior.
3. Assert semantic outcomes and resulting state, not exit code alone.
4. Correlate actions and evidence by repository, canonical agent, and run ID.
5. Preserve failures, malformed data, unavailable dependencies, and partial
   success in the acceptance set.
6. Demonstrate that a regression check fails when its fix is removed.
7. Record viewport geometry and screenshots for layout requirements, not visual
   inspection alone.

## Requirement evidence record

Each implementation issue or pull request must record:

| Field | Required content |
|---|---|
| Owner | Implementation issue and pull request responsible for the evidence |
| Requirement IDs | Every catalog requirement implemented or changed |
| Journey | The product journey affected |
| Environment | Source, built wheel, installed tool, or live host |
| Platform | Linux, WSL, or native Windows |
| Dataset | Repository and agent scale plus relevant state variants |
| Action | Exact user-visible operation exercised |
| Expected result | Semantic state and visible evidence |
| Actual evidence | Test name, screenshot, API response, or correlated run IDs |
| Residual risk | Unproved platform, scale, or failure mode |

The evidence record lives in the implementation issue while work is active and
in the pull request before merge. Release acceptance links those records rather
than copying their details into a new report.

## Environments

| Layer | Required evidence |
|---|---|
| Source | Fast behavior checks for changed composition and state rules |
| Built wheel | Dashboard startup, page construction, APIs, and critical actions |
| Installed tool | Browser operation through the public `agents-live` entry point |
| Live host | Real history, ownership, watcher, maintenance, and performance behavior |

A pass at one layer is not evidence for a layer above it.

## Dataset profiles

### Minimal

- No selected project.
- One available repository with no agents.
- One repository with one stopped agent.

### Operational

- Multiple available repositories with duplicate display names.
- Started and stopped clock, watch, handler-only, and model-backed agents.
- Local, remote-owned, unowned, malformed, and unavailable agents.
- Successful, failed, skipped, timed-out, queued, and in-progress runs.
- Numeric usage, unavailable usage, and true zero usage.

### Degraded

- Missing repository path.
- Unavailable ownership backend.
- Dead watcher with persisted started intent.
- Failed and stale maintenance verdicts.
- Damaged and truncated structured log records.
- Dashboard reconnect after client and transport interruption.

### Scale

- 100 agents across 10 repositories for first-useful-render acceptance.
- 1,000 loaded rows for local filtering and sorting response.
- 10,000 agents for progressive-rendering and accessibility accounting stress.

The 10,000-agent profile is a stress ceiling, not a declared supported product
scale. A later implementation issue must set the supported scale before that
number becomes a release commitment.

## Core journey acceptance

### Start from an empty host

1. Start the installed dashboard with no registered repositories.
2. Verify the first-run state names what is missing and routes to repository
   registration without exposing an otherwise empty operational layout.
3. Register a repository with no agents and verify that state reads differently
   from the no-repository state.
4. Make the repository unreadable and verify that state reads differently from
   both of the above.

Primary requirements: `REP-08`, `AGT-10`, `MUL-01`.

### Monitor all repositories

1. Start the installed dashboard with multiple registered repositories.
2. Verify the first operational view is all repositories, grouped by default.
3. Verify every group header exposes repository name, full path, availability,
   health, agent count, and applicable actions.
4. At 1280 x 720, verify repository-grouped agent rows and at least ten log
   lines are simultaneously visible with settings closed.
5. Verify unavailable repositories remain visible and explain their state.

Primary requirements: `HDR-*`, `AGT-*`, `MUL-*`, `LOG-01`, `LOG-03`.

### Investigate a failure

1. Begin with similarly named agents in different repositories and one newest
   run failure.
2. Select the attention signal and verify it resolves the correct repository,
   canonical agent, run, and correlated activity.
3. Verify the inventory and prior filter state remain available while details
   are open.
4. Follow the supported path to retained logs or timeline evidence and return
   without reconstructing the view.

Primary requirements: `AGT-02`, `AGT-11`, `LOG-06` through `LOG-13`, `NAV-*`.

### Filter and restore a view

1. Apply repository, health, owner, runtime, model, and trigger facets using the
   compact filter control.
2. Verify active filters, matching count, and Clear all remain visible while
   the control is closed.
3. Refresh and reconnect; verify filters, sorting, scope, selection, and scroll
   position remain stable.
4. Reopen the local URL and verify the view restores or explains stale targets.

Primary requirements: `AGT-06`, `AGT-07`, `FLT-*`.

### Operate an agent

1. Exercise Run, Start, Stop, and Claim from an all-repositories row.
2. Verify the accepted target remains repository-qualified through queued,
   running, terminal, log, and refreshed states.
3. Verify started and stopped eligibility, disabled reasons, duplicate
   coalescing, timeout, failure, and continuation to the next queued action.
4. Verify a browser disconnect does not cancel or orphan accepted work.

Primary requirements: `AGT-04`, `AGT-05`, `ACT-01` through `ACT-09`.

### Investigate a watched agent

1. Start with one healthy resident watcher, one watcher with no matching
   changes, one non-matching expression, one delayed queue, and one dead watcher
   whose started intent remains persisted.
2. Verify startup and liveness evidence distinguishes the healthy and quiet
   watcher from the dead watcher.
3. Trigger matched work and verify matched-path count and debounce context reach
   the repository-qualified agent evidence without exposing sensitive paths by
   default.
4. Exercise overflow, queue drop, bounded-rescan truncation, and terminal root
   failure; verify each condition changes the correct agent state and exposes
   time, reason, and recovery guidance.
5. Verify the dashboard and CLI apply the same watcher-liveness and health
   verdict.

Primary requirements: `HDR-10`, `AGT-14`, `WCH-*`, `REL-03`, `DAT-05`.

### Operate a repository, selection, or all repositories

1. Preview the exact repositories and canonical agents affected.
2. Confirm a risk-bearing action and allow at least one target to become stale
   before execution.
3. Verify stale targets fail closed, valid targets continue, and per-target
   success, failure, and skip outcomes remain visible.
4. Verify the view refreshes affected groups without resetting scope.

Primary requirements: `ACT-10` through `ACT-12`, `MUL-07` through `MUL-12`.

### Configure repositories and host maintenance

1. Open settings from an operational warning.
2. Register, unregister, set default, clear default, refresh maintenance, run
   maintenance, inspect logs, and repair a schedule through supported actions.
3. Verify unregister never deletes repository files or runtime state.
4. Verify failed maintenance remains failed after passive refresh and changes
   only after a later canonical terminal result.
5. Close settings and verify the prior operational context is restored.

Primary requirements: `REP-*`, `HST-*`.

## Layout and responsive acceptance

Validate 1280 x 720, 1440 x 900, and a narrow mobile viewport with short and
long labels, queued actions, multiline errors, and the scale profiles.

For each viewport, record:

- bounding boxes for header, repository inventory, log, and settings surface;
- body and region overflow;
- visible repository identity and critical agent state;
- visible log-line count;
- intersections among controls, labels, and dynamic content;
- focused-element visibility during keyboard navigation.

The page fails acceptance if settings permanently consume operational height,
the log falls below ten visible lines at 1280 x 720 or larger, or page-level
scrolling replaces independent inventory and log scrolling.

## Reliability acceptance

- Repeat browser connect, refresh, disconnect, reconnect, and forced client
  reset while readiness and agent APIs remain available.
- Interrupt refresh and verify the last coherent snapshot remains visibly stale
  rather than mixing observation times.
- Inject malformed definitions, ownership failures, missing repositories, and
  damaged log rows independently; verify each degrades only its owned surface.
- Inject truncated and malformed log records and an expired retention boundary;
   verify the dashboard discloses incomplete evidence and never presents the
   resulting query as complete.
- Terminate the dashboard unexpectedly and verify a durable reason and outcome
  are recorded when the runtime can observe them.
- Verify `dashboard list`, process registry, readiness API, and browser response
  agree before and after start and stop.

## Performance acceptance

- First useful operational render completes within 3 seconds for 100 agents in
  10 repositories under normal supported-host load.
- Filtering and sorting 1,000 loaded rows updates visible results within 100
  milliseconds.
- Manual refresh acknowledges immediately and completes or shows continuing
  progress within 2 seconds.
- The 10,000-agent profile does not create one active rendered row per agent and
  remains keyboard and screen-reader navigable.
- Periodic refresh does not block filter, scroll, log, or action feedback.

## Accessibility acceptance

Validate the core journeys with keyboard only and with at least one screen
reader on each supported host presentation, against the `ACC` family of the
catalog. Acceptance includes:

- keyboard completion of every core journey;
- grid and toolbar focus management;
- visible focus under scrolling and progressive rendering;
- state and failure legible without color;
- contrast, target size, zoom, reflow, light and dark themes, and reduced
  motion;
- refresh and log updates that do not steal focus.

Automated accessibility checks are required but do not replace keyboard and
screen-reader journey validation.

## Delivery gates

The slices themselves, with their scope and exit requirement IDs, are defined
in [dashboard-implementation.md](dashboard-implementation.md).

### v6.6 recovery: Restore the current operational viewport

Issue [#419](https://github.com/johnshew/agents-live/issues/419) requires the
current single-repository inventory and at least ten log lines to remain visible
together at 1280 x 720, administration to consume no standing operational
height, and the settings drawer to remain inside a narrow viewport. Evidence is
required from the built wheel on Linux and Windows. This gate proves `AGT-03`
and only partial progress toward requirements that explicitly require the
default all-repositories view.

Issue [#421](https://github.com/johnshew/agents-live/issues/421) is the separate
qualified-action gate for the v6.6 bake. It requires Run, Start, Stop, and Claim
from aggregate rows, repository and canonical-agent revalidation at acceptance,
semantic command outcomes, and repository-qualified durable evidence. The v6.6
bake is not complete until the installed test version passes this gate together
with the #419 viewport recovery.

### Deferred slice 1: Unify operational utility

Requires one default all-repositories page over a coherent snapshot, layout
geometry, default grouping, visible logs, settings separation, and
compact-filter acceptance from the installed artifact.

### Deferred slice 2: Complete read/write parity

Requires agent, repository, selected-set, and all-repositories action scenarios,
including ambiguity, staleness, partial success, and durable action evidence.

### Deferred slice 3: Make state explainable

Requires directed failure investigation, expected-run and watcher-liveness
scenarios, log correlation, and bookmark restoration.

### Deferred slice 4: Harden the product

Requires all supported platforms, built-wheel and installed-tool gates, scale,
disconnect, degraded-data, and accessibility acceptance.

## Traceability index

| Validation section | Requirement families |
|---|---|
| Start from an empty host | `REP`, `AGT`, `MUL` |
| Monitor all repositories | `HDR`, `AGT`, `MUL`, `RSP` |
| Investigate a failure | `AGT`, `LOG`, `DAT`, `NAV` |
| Filter and restore a view | `AGT`, `FLT`, `REF` |
| Operate an agent | `AGT`, `ACT`, `OWN`, `SEC` |
| Investigate a watched agent | `HDR`, `AGT`, `WCH`, `REL`, `DAT` |
| Operate a repository, selection, or all repositories | `ACT`, `MUL`, `SEC` |
| Configure repositories and host maintenance | `REP`, `HST` |
| Layout and responsive acceptance | `HDR`, `AGT`, `LOG`, `MUL`, `RSP` |
| Reliability acceptance | `DSC`, `REL`, `REF`, `WCH` |
| Performance acceptance | `PRF` |
| Accessibility acceptance | `ACC` and every interactive requirement |

## Release exit

- Every P0 requirement assigned to the release milestone in
   [dashboard-implementation.md](dashboard-implementation.md) has attached
   passing evidence.
- Every shipped P1 requirement has evidence; every deferred P1 requirement has
  an explicit user-visible release decision.
- No open failure invalidates a success measure or core journey.
- The requirement-to-evidence record is reviewable without reconstructing the
  implementation history.
