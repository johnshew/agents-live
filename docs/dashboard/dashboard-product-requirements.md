---
title: Dashboard Product Requirements
description: Durable product direction, experience contract, journeys, and outcomes for the Agents Live dashboard
ms.date: 2026-08-29
ms.topic: concept
---

# Dashboard product requirements

This document states the durable target for the dashboard. It changes when the
target changes, not as work proceeds. Current state, sequencing, dependencies,
risks, and undecided questions live in
[dashboard-implementation.md](dashboard-implementation.md).

## Document status

| Field | Value |
|---|---|
| Decision owner | Repository maintainers |
| Product surface | Local browser and packaged native dashboard |
| Scope | One host and every repository registered on it |
| Detailed requirements | [dashboard-requirements.md](dashboard-requirements.md) |
| Implementation plan | [dashboard-implementation.md](dashboard-implementation.md) |
| Validation | [dashboard-validation.md](dashboard-validation.md) |

## Executive summary

The dashboard must become the primary local operating surface for all Agents
Live repositories on a host. Its first screen must answer three questions:

1. What needs attention?
2. Which repository and agent does it belong to?
3. What evidence and safe action are available now?

The product decision below states the shape that answers them.

## Product decision

The dashboard will use one adaptable operational experience rather than
separate single-repository and aggregate products.

- All registered repositories appear by default.
- Every repository is a visible group with name, path, health, availability,
  agent count, and repository-level actions.
- Every eligible agent action is available in place and carries a canonical
  repository-qualified target.
- A compact search and filter control replaces the permanent filter form.
- Agent inventory and activity are peer surfaces and remain simultaneously
  useful at common desktop sizes.
- Settings contain repository registration, defaults, and host-maintenance
  administration, while operational warnings remain visible in context.
- Focused repository, agent, run, and failure views are drill-down states of the
  same product and preserve a direct route back to the prior view.

### Alternatives considered

| Alternative | Why it was rejected |
|---|---|
| A separate aggregate dashboard beside the single-repository dashboard | Two surfaces drift in health semantics, actions, and layout, and force the operator to pick a surface before knowing where the problem is. |
| Keep the current layout and only shrink the host and repository panels | Reclaims viewport without fixing scope-blind actions, ambiguous repository identity, or unexplained health. |
| Keep all operations in the CLI and make the dashboard read-only | Splits investigation from intervention, so the operator reconstructs state on every action. |
| A hosted control plane with a remote UI | Contradicts the local-only posture and adds authentication and network surface a local operator does not need. |

## Product principles

1. **One experience.** Scope, views, and drill-down adapt a single dashboard.
   A new investigation never justifies a new dashboard.
2. **Consumer, not source.** Health, ownership, usage, and log semantics come
   from the contracts the CLI already uses. The dashboard adds presentation,
   not new truth.
3. **CLI recoverability.** Every dashboard mutation has a supported CLI
   equivalent, so no recovery path requires a browser.
4. **Honest state.** Unknown, stale, unavailable, and degraded are first-class
   and are never rendered as healthy, current, or zero.
5. **Local by construction.** Loopback binding, no telemetry export, and no
   general command surface.
6. **Fail closed.** An ambiguous, stale, or unresolvable target stops the
   action and explains itself.

## Product definitions

- **Coherent observation:** health, agent, action, and summary values collected
  for one refresh boundary rather than mixed from independently completed
  refreshes.
- **Stale:** the newest coherent observation is older than the canonical
  freshness threshold for its data source, or a refresh failed and that
  threshold has elapsed. The dashboard shows the observation age, threshold,
  and reason; it does not invent a second threshold.
- **Unavailable:** the product cannot currently obtain the value or dependency
  required to determine state.
- **Degraded:** the product remains partially operable but has evidence of a
  failed, missed, dropped, delayed, or inconsistent condition.
- **Needs attention:** a view over failed, missed, dead, degraded, unavailable,
  or materially stale work, using the same shared health contract as the CLI.

## Non-goals

- Remote or internet-accessible dashboard hosting.
- Off-host telemetry export or a hosted control plane.
- Editing agent definitions, prompts, schedules, models, or processors.
- General shell access, arbitrary command execution, or repository file
  browsing.
- Replacing the supported CLI log and timeline interfaces.
- Treating provider-normalized list cost as invoice or accounting data.
- Observing repositories or agents that live on another host.
- Multi-user access control; the dashboard assumes one trusted local operator.
- Long-term metric storage or trend analysis beyond retained observability
  data.

## Users and jobs

### Primary user

A technical operator supervises unattended agents across one or more local
repositories. The operator keeps the dashboard open, returns when work needs
attention, investigates evidence, and intervenes without reconstructing state
from several commands.

### Secondary user

A developer configures repository registration, ownership, and automatic
maintenance. These less frequent jobs may require opening settings but must
remain connected to the operational state that prompted them.

### Jobs to be done

- Get me from a fresh install to a first useful view without reading a CLI
  reference.
- Tell me whether unattended work is healthy and current.
- Show me what failed, was missed, stopped watching, or is still queued.
- Tell me what changed since I last looked at this.
- Keep repository identity visible while I compare similarly named agents.
- Let me inspect the run and log evidence behind a state.
- Let me act at agent, repository, selected-set, or all-repositories scope.
- Tell me whether the action actually changed the intended state.
- Let me configure repositories and maintenance without losing my investigation.
- Let me leave this open all day without it quietly going out of date.

### What the operator must never have to do

These are requirements stated as prohibitions, because each one is a failure
the product has to be designed against rather than a feature it can add later.

- Restart the dashboard with different arguments to gain an action.
- Keep a second window, or a terminal, open to know whether the host is healthy.
- Rely on color to notice that something failed.
- Read a raw log file to learn why an agent failed.
- Guess which repository a row belongs to.
- Discover that an action was unavailable by clicking it.
- Re-apply filters, re-select a row, or re-find a reading position after a
  refresh, an action, or a configuration change.
- Compare two timestamps in the interface to work out which one is current.
- Read an important value that is truncated with no way to expand it.

## Core journeys

### Start from an empty host

A new operator opens the dashboard before registering anything. The empty state
names what is missing, routes to repository registration, and describes what
the view will show once agents exist. No repositories registered, a registered
repository with no agents, and a repository that cannot be read are visibly
different states.

### Monitor all repositories

The operator opens one URL and sees a compact attention summary, repositories
grouped by name and path, agent state, and current activity. Healthy detail is
available but does not obscure work requiring intervention.

### Investigate a failure

The operator selects a failure, reaches the repository-qualified agent and run,
reviews correlated activity without losing the inventory, and follows directed
links to retained logs or timeline evidence when needed.

### Operate an agent

The operator invokes an eligible action, sees its exact scope, queued and
running state, terminal outcome, elapsed time, and refreshed agent state. A
disabled action explains why it is unavailable.

### Operate a repository or selection

The operator selects a repository or a set of agents, previews the targets,
confirms risk-appropriate actions, and receives per-target outcomes without the
view resetting after partial success.

### Configure the host

The operator follows an operational warning into settings, changes repository
registration or repairs maintenance through supported operations, and returns
to the same dashboard context.

## Experience contract

This is the durable design law of the product. The requirements catalog encodes
it as testable IDs; this section states what those IDs are for and which rule
wins when two of them compete.

### Information hierarchy

1. Host connection, freshness, and items requiring attention.
2. Repository groups with persistent name and full-path headers.
3. Agent identity, effective state, trigger, ownership, model, recency, and
   usage.
4. Selected repository, agent, run, and log evidence.
5. Settings and administrative detail on demand.

Rank order is also yield order. When space is scarce, level five collapses
first and level one never collapses.

### Layout contract

At the supported desktop minimum the first screen must simultaneously show the
attention summary, repository identity, agent rows, and live activity. Those
four are the product; everything else is a guest in the viewport.

- Configuration never occupies an operational row. A configuration surface the
  operator did not open costs no vertical space.
- Health earns space in proportion to its deviation from healthy. A healthy
  host says so in a few characters.
- Activity is an always-visible peer of the inventory, not an appendix beneath
  it. Space is recovered from configuration and from healthy detail, never from
  the log.
- Inventory and activity scroll independently, and the operator can rebalance
  them without losing state.
- Nothing that arrives asynchronously, such as a long error, a queued action,
  or a widened label, may resize the page so that an operational surface leaves
  the view.

### Attention model

Needs attention is the first screen, not a separate view.

- Attention is summarized above the inventory, is absent when nothing needs
  attending to, and stays reachable while anything is wrong.
- Attention is counted, not merely indicated. "3 failing" outranks a red dot.
- Every attention item is actionable: selecting it scopes the inventory and the
  evidence to what it describes.
- A summary states its scope and never contradicts the detail beneath it. One
  word in the most prominent position is read as a claim about everything on
  the screen, so a summary covering only part of the system names that part.
- Nothing enters attention that the shared health contract does not support.

### Scope and identity

- The operator can answer "which repository is this" without moving the
  pointer. Repository identity survives grouping, collapse, column collapse,
  and horizontal scroll.
- Identical or similar agent names in different repositories are
  distinguishable at a glance, and every action names its repository-qualified
  target before it runs.
- The current scope is stated in the interface, never inferred from how the
  process was started.

### Action model

Control prominence follows frequency multiplied by reversibility.

| Class | Example | Presentation |
|---|---|---|
| Frequent and reversible | Run, start, or stop one agent | Labeled control in place, on the row it affects |
| Infrequent | Claim, repair maintenance, register a repository | Overflow menu or settings, always labeled |
| Destructive or broad | Stop all, unregister, bulk actions | Overflow plus confirmation that names scope and count |

- An offered control is one the target can actually perform. Availability is
  derived from observed state, never assumed.
- A withheld control states its reason where its label is, not only in a hover
  tooltip that keyboard and touch operators never receive.
- An icon-only control is permitted only with an accessible name, and the same
  action must also be reachable with a visible label.
- No action reads as successful before the underlying operation reports
  semantic success.

### Interaction budget

| Operation | Budget |
|---|---|
| See what needs attention | Zero; it is on the first screen |
| Reach the evidence behind a failure | Two |
| Apply a filter | Two |
| Clear one filter, or all filters | One |
| Act on an agent already visible | One, plus one confirmation when destructive |
| Return from any drill-down to the prior view | One |
| Reach repository or host configuration, and return | Two out, one back |

### State legibility

- One vocabulary. A state carries one name across the header, inventory,
  detail, activity, and CLI.
- One clock. The interface shows the age of the last coherent observation. Any
  other age is labeled with what it measures and sits beside the value it
  qualifies.
- Color is never the only carrier of state. Anything that changes what the
  operator should do also changes text or shape.
- Relative time is for scanning and exact local time is for evidence; the exact
  value is always one interaction away.
- State has a bounded maximum age. A cadence slow enough that a live view and a
  frozen one look alike is a defect whether or not the age is displayed.
- Truncation always has a recovery. A value the operator can neither finish
  reading nor expand is a defect.
- Every placeholder is legible. A glyph standing in for a value states what it
  means where it appears.
- Units, windows, and sources are named. A bare number is not shippable.

### Search, filters, and views

The toolbar carries one compact search input and one filter control. Filters
open on demand, support operational facets, and collapse into removable active
filter summaries with a visible match count. Scope, filters, sorting, time
window, and selection are bookmarkable locally. Built-in and saved views adapt
this dashboard instead of creating more dashboards.

### Settings model

- Settings is an on-demand surface layered over the operational view, never a
  page the operator navigates away to.
- Opening and closing settings returns the operator to the exact prior context.
- The operational consequence of configuration stays in the operational view: a
  failed host service is visible without opening settings, and the repair lives
  inside them.
- A configuration change updates the affected data in place. Nothing reloads
  the page.

### Feedback and continuity

- Every accepted action acknowledges immediately, shows queued and running
  state, and ends in a terminal outcome with elapsed time.
- The durable visible activity record is the source; notifications sit on top
  of it and are never the only place an outcome appears.
- Refresh is a data event, never a layout event. Filters, sorting, grouping,
  selection, split allocation, and reading position all survive it.
- New activity follows the bottom only while the operator is already there.
- Disconnection is stated rather than silent, and reconnection resumes from one
  coherent observation instead of mixed observation times.

### Input, density, and reach

- Every journey completes from the keyboard with visible, predictable focus,
  and focus returns to its origin when an overlay closes.
- Dense grids use managed focus so the operator is never trapped in a long
  page-level tab sequence.
- Progressive and virtual rendering must not misreport counts, positions,
  sorting, or selection to assistive technology.
- The interface stays operable under zoom and reflow and respects a
  reduced-motion preference.

The target is WCAG 2.2 AA, and composite interactions follow the WAI-ARIA
Authoring Practices. The `ACC` family of the catalog records what those
patterns leave open.

### Language

- One term per concept, matching the term the CLI uses.
- A label states its control's effect. Two controls invoking one operation
  under different labels is a correctness defect, not a wording preference.
- Implementation vocabulary such as cron expressions, process identifiers, and
  internal paths belongs in detail surfaces, not the primary view.
- An error states what failed, what it affected, and what to do next.

### Empty, partial, and error states

Every surface that can be empty says why it is empty and what to do next. The
product visibly distinguishes nothing registered, registered with nothing to
show, filtered to nothing, not yet loaded, unreadable, and unavailable. Partial
results are labeled as partial and never presented as complete.

## Success measures

These are product outcomes, not acceptance steps. The requirement IDs that
implement them live in the catalog, and the evidence that closes them lives in
the validation plan. Because the product exports no telemetry, every measure is
checked locally by the maintainer against the validation datasets rather than
instrumented from operator behavior.

| Measure | Target | How it is checked |
|---|---|---|
| Time to see what needs attention | The attention summary, named repository groups, agent state, and recent activity are all readable in the first view at 1280 x 720 with settings closed | Layout acceptance with recorded viewport geometry |
| Symptom to evidence | Two or fewer interactions from a failure signal to the correlated run and log evidence | Failure-investigation journey, counted as operator interactions |
| Filtering cost | Two or fewer interactions to apply any supported filter, one to clear any active filter | Filter-and-restore journey |
| Action trust | Every accepted action shows its exact scope, queued or running state, terminal outcome, and durable evidence | Action acceptance at agent, repository, and selection scope |
| Investigation continuity | Refresh, reconnect, and drill-down never lose scope, filters, selection, or reading position | Refresh and reliability acceptance |
| Honest state | No agent with a dead watcher appears healthy, and no stale or unavailable value appears as current or zero | Watcher, staleness, and degraded-data acceptance |
| CLI fallback | The core journeys complete without dropping to the CLI, except where the CLI is the documented destination for full logs and timelines | Maintainer review before a release |
| Scale without a second product | Adding repositories changes density and filtering, never the number of dashboards an operator runs | Scale dataset review against the aggregate view |
| Platform parity | The core journeys complete from the installed artifact on Linux, WSL, and native Windows | Compatibility acceptance on each supported host |

## Traceability and change control

The detailed catalog maps stable requirement IDs to capability families. Every
implementation issue and pull request must name the IDs it addresses and attach
the evidence required by the validation plan.

This document is a target, not a status report. It changes only when a
principle, non-goal, journey, success measure, or the product decision itself
changes, and such a change updates the PRD in the same commit that makes it. Everything that moves as work proceeds, including current state, delivery
sequence, dependencies, risks, and undecided questions, belongs in the
implementation plan. A question resolved there is recorded here as a decision
rather than left as a question.