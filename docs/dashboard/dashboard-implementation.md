---
title: Dashboard Implementation Plan
description: Current dashboard state, delivery sequence, release boundaries, dependencies, and risks
ms.date: 2026-09-05
ms.topic: concept
---

# Dashboard implementation plan

This document records current state and delivery sequence for the durable target
in [dashboard-product-requirements.md](dashboard-product-requirements.md).
Requirement definitions remain canonical in
[dashboard-requirements.md](dashboard-requirements.md), and evidence remains
canonical in [dashboard-validation.md](dashboard-validation.md).

## Release context

As of 2026-08-29:

- v6.5.0 is the latest public release.
- v6.6 is still being tested in the `bake/v6.6.0-local` branch. It has not
  moved to `main` and is not an official release candidate.
- The last installed v6.6 test version predates the dashboard viewport repair.
- [#419](https://github.com/johnshew/agents-live/issues/419) is merged into
  bake through [PR #420](https://github.com/johnshew/agents-live/pull/420),
  restoring the current dashboard viewport.
- [#421](https://github.com/johnshew/agents-live/issues/421) is the remaining
  v6.6 dashboard work. After it merges, the resulting exact bake commit must be
  built, installed locally, and validated before v6.6 moves to `main`.

The generated release report remains the source for the latest branch, test,
and installed-version details. This section explains why the dashboard work is
part of the release plan.

## v6.6 decision

The v6.6 bake repairs the existing dashboard before broader redesign work
begins. It does not include the full A-D dashboard roadmap and does not claim
completion of the dashboard product target.

- [#419](https://github.com/johnshew/agents-live/issues/419) is the mandatory
  usability recovery: keep the single-repository inventory and log together in
  the first viewport and move administration into settings.
- [#421](https://github.com/johnshew/agents-live/issues/421) completes the v6.6
  dashboard slice by adding repository-qualified Run, Start, Stop, and Claim
  controls to aggregate rows.
- The unified page, complete truth model, and continuity work are deferred to
  their own implementation issues. They do not block v6.6 unless a later
  release decision explicitly brings them into that milestone.

A requirement marked P0 blocks the milestone to which this plan assigns it. P0
does not make every unrelated package release wait for the complete dashboard
redesign.

## P0 assessment

| Delivery state | P0 requirements | Durable owner |
|---|---|---|
| Existing baseline, retained by v6.6 | `REL-01`, `SEC-01`, `SEC-02`, `SEC-04`, `CMP-01` | Existing dashboard and artifact gates |
| v6.6 viewport recovery | `AGT-03`; partial evidence toward `LOG-01` and `RSP-01` for the current single-repository page | [#419](https://github.com/johnshew/agents-live/issues/419) |
| v6.6 qualified aggregate actions | `AGT-04`, `AGT-05`, `ACT-09`, `MUL-04`, `MUL-07`, `SEC-03`, `SEC-06` | [#421](https://github.com/johnshew/agents-live/issues/421) |
| Unified product surface | `HDR-01`, `HDR-11`, `FLT-01`, `LOG-01`, `MUL-01`, `MUL-02`, `MUL-10`, `NAV-06`, `RSP-01` | Implemented by [#422](https://github.com/johnshew/agents-live/issues/422) |
| Deferred truthful state and evidence | `HDR-02`, `HDR-03`, `HDR-10`, `HDR-11`, `AGT-01`, `AGT-02`, `AGT-04`, `LOG-02` through `LOG-05`, `HST-02`, `WCH-01`, `ACC-03` | [#423](https://github.com/johnshew/agents-live/issues/423) |
| Deferred continuity and hardening | `ACC-01`, `REF-03`, plus P0 defects exposed by reliability and accessibility acceptance | [#424](https://github.com/johnshew/agents-live/issues/424) |

Partial evidence is not closure. In particular, #419 proves geometry for the
current single-repository page; it does not satisfy requirements whose text
requires the default all-repositories experience.

## Delivery sequence

The letters below describe the order of the longer dashboard program. They do
not mean that all four stages ship in v6.6. Only the work assigned to v6.6 in
the preceding section belongs in the current bake.

### A. Stabilize the v6.6 bake

Land #419 first, then #421 as a small follow-up using the existing public CLI
boundary. Adding `--repo` is the main execution change, but the dashboard must
also carry the row's registered repository path and canonical agent identifier
to that boundary. The worker invokes
`agents-live --repo <path> <command> --name <identifier>`, revalidates both
values, and fails closed when the target has changed.

The v6.6 dashboard exit is an installed test version containing #419 and #421.
It must pass the viewport, disconnect, action-scope, semantic-result, and
durable-action-evidence gates on Linux and Windows.

### B. Unify the operational page

[Issue #422](https://github.com/johnshew/agents-live/issues/422) replaces the
separate single-repository and aggregate compositions with one adaptable page
over one coherent all-repositories snapshot. All registered repositories are
grouped by default, repository focus is view state, the shared inventory and
activity surfaces remain in the first desktop viewport, and configuration
remains in settings. The existing `/api/agents`, `/api/all-repos`, and
`--all-repos` boundaries remain compatible.

Editable-source evidence executes both the ordinary and `--all-repos` launch
forms at 1280 x 720, verifies grouped repository identity, compact search and
filters, focused scope without a second page, an always-visible ten-line log,
and repository-qualified Run evidence. Canonical health, activity, and
continuity semantics remain assigned to #423 and #424.

[Issue #455](https://github.com/johnshew/agents-live/issues/455) completes the
post-#422 repository-settings slice without adding another dashboard state
model. Settings is a full-viewport modal over the operational page, so closing
it restores the existing scope, filters, inventory, and activity context.
Repository observations distinguish availability, discovery failure, and a
successful zero-definition repository; registry mutations return a durable
semantic result and refresh settings, selectors, and the shared operational
snapshot without reloading the page. Unregister remains registry-only and
requires an explicit non-deletion confirmation.

Editable browser acceptance runs the ordinary and `--all-repos` launch forms
at 1280 x 720, 1440 x 900, and 390 x 844. It asserts full-screen modal
geometry, unchanged dashboard bounds, focus restoration, readable repository
state, zero-definition registration outside the current view scope, live
selector refresh, safe unregister feedback, and preservation of the browser
document across both mutations.

### C. Make state explainable

Implement [#423](https://github.com/johnshew/agents-live/issues/423). Derive
health, attention, eligibility, and activity from shared CLI and structured
observability contracts. Add the compact filter and directed investigation
journeys only after the page reads from one state model.

### D. Preserve continuity and harden

Implement [#424](https://github.com/johnshew/agents-live/issues/424). Preserve
view state through refresh and reconnect, complete keyboard and screen-reader
acceptance, and prove progressive rendering and degraded-data isolation at the
supported scale.

## Dependencies and risks

- B depends on one coherent observation model; duplicating state across the two
  current page builders would make later health and refresh work harder.
- C depends on canonical watcher, run, and freshness semantics. The dashboard
  must consume those contracts rather than invent UI-only verdicts.
- D validates the final interaction model. Structural accessibility must still
  be considered in earlier slices so controls do not require replacement.
- #421 is mechanically small but safety-sensitive. Adding `--repo` without
  carrying and revalidating the row's repository path would leave the action
  scope implicit and would not satisfy the aggregate mutation requirements.

## Change control

Current state, sequencing, and release assignment change here. Durable product
principles change in the product requirements document. Detailed acceptance
criteria and implementation progress live in the linked GitHub issues and pull
requests.
