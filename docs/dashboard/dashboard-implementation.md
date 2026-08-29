---
title: Dashboard Implementation Plan
description: Diagnosis of the shipped dashboard and the delivery slices that bring it to the product bar
ms.date: 2026-08-29
ms.topic: concept
---

# Dashboard implementation plan

## Purpose

[dashboard-product-requirements.md](dashboard-product-requirements.md) states
what the dashboard should be. This plan states what the current build actually
does, why it falls short, and the order in which to change it. Requirement IDs
come from [dashboard-requirements.md](dashboard-requirements.md); evidence
obligations come from [dashboard-validation.md](dashboard-validation.md).

This is where everything that moves lives: current state, delivery sequence,
dependencies, risks, and undecided questions. A durable product decision found
here moves into the PRD, and a resolved question is recorded there as a
decision. A scheduled unit of work moves into a GitHub issue.

Each slice must leave the dashboard in a coherent usable state. A slice's exit
gate is the matching section of the validation plan; the `P0`, `P1`, and `P2`
priorities in the catalog, not this ordering, decide what blocks release.

## What ships today

The dashboard is one module, `src/agents_live/cli/scripts/dashboard.py`, of
roughly two thousand lines that mixes collection, HTTP endpoints, and two
independent page builders.

| Surface | Entry point | Behavior |
|---|---|---|
| Single repository | `agents-live dashboard` | Read and write, one repository at a time |
| All repositories | `agents-live dashboard --all-repos` | Read only; the page tells the operator to restart with `--repo NAME` to act |
| First run | No resolvable repository | Header, a hint, host services, repository settings |

Two builders sharing row helpers but not layout is the split the PRD product
decision retires. It is also why every layout fix currently costs twice and
drifts once.

## Diagnosis

Observations below come from a 1280 x 720 session of the current build against
a host with three agents, one of which had a failed newest run.

### Configuration wins the viewport, operations lose it

Approximate vertical budget of the first screen:

| Region | Share | Notes |
|---|---|---|
| Header | 14% | Identity, health word, two buttons, refresh age |
| Host services card | 33% | Roughly half of it empty space below its buttons |
| Repository settings | 5% | Collapsed expansion row |
| Filter row | 9% | Four controls and a checkbox, always expanded |
| Agent inventory | 20% | Three rows, the third clipped mid-row |
| Cost totals | 3% | |
| Log | 0% | The `Log` label sits on the bottom edge; the log is below the fold |

The cause is specific and checkable. `.dashboard-body` declares three grid
rows for five children: the host services card, the repository settings
expansion, the agent card, the log label, and the log. The first explicit
track, `minmax(12rem, 1fr)`, is spent on the host services card, which is why
it renders tall and half empty. The last two children fall into implicit auto
tracks under a container that is already full height, which is why the log is
not on screen at all. `LOG-01`, `AGT-03`, and `RSP-01` all fail on this one
declaration.

### Health is narrated four times and never answered

The first screen carries a grey `healthy 2m` in the header, a grey
`refreshed 6m` beside it, a green `Healthy, idle` in the host services card,
and a maintenance line with a cron expression, a smoketest verdict, and a
duration in milliseconds. Four vocabularies, two unexplained ages, no ranking.

Meanwhile the only fact that needed the operator was that one of three agents
had a failed newest run. That fact is carried entirely by red text on the
agent name and state cells: no count, no icon, no summary, nothing above the
inventory, and nothing that survives a color-vision difference. `HDR-08` has
no implementation, and `ACC-03` fails on the one signal that matters most.

### Prominence is inverted

The most prominent control on the page is a filled primary `Run health check`,
a rare maintenance operation. The second is `Stop all`, which is destructive,
global, and fires without confirmation, against `HDR-05`. The operations an
operator actually performs, run and start and stop a single agent, are four
unlabeled icon buttons wedged into column three, between State and Owner,
where they interrupt the reading path.

### Actions do not tell the truth about eligibility

Rows carry `can_pause`, `can_activate`, and `can_claim`. There is no
`can_run`, and the Run control has no disable binding, so Run is offered for
an agent owned by another host in the same row where Start is correctly
disabled and explained. That is `AGT-04` failing in the most misleading
direction available: the control that starts real work is the one that lies.

### Freshness is displayed but not modeled

The refresh cadence is 600 seconds on both pages. The age is rendered as
`refreshed 6m` and never becomes stale, so ten-minute-old state is presented
with the same confidence as fresh state. `HDR-09` and `DAT-05` have no
implementation.

### Investigation state is discarded

Every repository mutation ends in `window.location.reload()`, which destroys
filters, selection, scroll position, and the log buffer. `REF-03` cannot hold
while that call exists.

### Truncation has no recovery

Model, owner, trigger, and repository path clip mid-glyph with no detail
affordance, against `AGT-09`. The screen shows a truncated model name and a
truncated owner identity with no way to read either.

## Where the issues stand

The issues that motivated this work are closed. The regression is a
product-shape problem, not a backlog of unimplemented features: #229 landed the
host-service and repository panels that now own the viewport, and nothing since
has rebalanced them against the surfaces they displaced.

| Issue | Relationship to this plan |
|---|---|
| [#401](https://github.com/johnshew/agents-live/issues/401) | Windows dashboard exits on a Proactor connection reset. Blocks `REL-01` and, on that host, every other measure. |
| [#388](https://github.com/johnshew/agents-live/issues/388) | Separating registration, initialization, and skill installation defines what the settings surface must contain (`REP-01`, `REP-03`). |
| [#405](https://github.com/johnshew/agents-live/issues/405) | Retrieving transcripts through the CLI is the supported path behind `AGT-11` and `LOG-10`. |
| [#370](https://github.com/johnshew/agents-live/issues/370) | The read-model extraction below is the concrete case that discussion needs. |

## Proposed layout

The rules that govern this layout are the experience contract in the PRD:
information hierarchy and yield order, the layout contract, the attention
model, the action model and interaction budget, state legibility, the settings
model, and feedback and continuity. They are not restated here. What follows is
one concrete realization of them, and the parts of that realization this plan
is choosing.

### Regions at 1280 x 720

```
+---------------------------------------------------------------+
| Agents Live   host / channel        live . updated 12s  [R][:] |  status bar
+---------------------------------------------------------------+
| ! 1 agent failing   1 watcher dead   maintenance failed        |  attention, only when non-empty
+---------------------------------------------------------------+
| [search        ]  [Filter v]  state:started x  Clear   3 of 12 |  toolbar
+---------------------------------------------------------------+
| repo-one    <path>    available    3 agents        [Run] [:]   |  sticky group header
|   agent          state         last run    model    [Run] [:]  |
|   agent          failed 4m     ...                  [Run] [:]  |  inventory, 1fr, min 12rem
+============================ splitter ==========================+
| log, at least ten lines, scrolled independently                |  min ten lines
+---------------------------------------------------------------+
```

### Realization choices

- Settings is a right-side drawer over this layout, never a row inside it.
- Host services keep no standing viewport. Their contribution to the operator
  is one attention chip when degraded and nothing at all when healthy.
- The inventory and log are separated by a draggable splitter rather than fixed
  proportions, so the operator sets the balance and it survives refresh.
- Row actions are a labeled primary control plus an overflow menu at the row
  end, not an icon cluster in the middle of the reading path.
- The status bar carries exactly one age, the age of the last coherent
  observation. The health beacon's own age lives in the health detail.

## Structural moves that pay for the rest

Three duplications in `dashboard.py` generate most of the remaining cost. Each
is a collapse rather than a feature, and each deletes work from the items that
follow it.

**One page instead of three.** `_build_page`, `build_all_repos_page`, and
`_build_no_project_page` carry three CSS blocks, three headers, three refresh
strategies, and two unrelated toolbars. The merge is cheaper than its size
suggests because the halves already converge: `all_repo_groups()` produces the
grouped shape the target layout wants, and `_build_page` has the action model
and the log. Take the group shape from the aggregate and the interaction model
from the single-repository page, and scope stops being a mode and becomes a
filter. That deletes `--all-repos` as a second product, the
restart-with-`--repo` dead end, the parallel column sets, and the hand-rolled
sort and grouping state.

**Settings in a drawer.** `host_service_panel()` and
`repository_settings_panel()` are already self-contained refreshable functions,
so moving them into a right-side drawer is a call-site change. It removes two
of the five children from `.dashboard-body`, which is what makes the
three-track grid overflow, so the log returns to the screen without touching
the grid math. It is the largest viewport recovery in the plan for the smallest
diff, and it is independent of the page merge, so it can land first and survive
it.

**One snapshot read model.** `system_health()` reads the host beacon,
`agent_rows()` reads agents, and `_refresh_views()` refreshes four surfaces that
each recompute. The header's health word and the rows beneath it come from
different observations with no shared timestamp, which is exactly why a scoped
summary can contradict its own detail. A single snapshot carrying one
observation time, repositories, agents, host state, and attention makes
`HDR-11` a property of the code rather than a rule to remember, gives freshness
one age to model, turns the attention bar into a projection instead of a new
computation, and is the concrete case
[#370](https://github.com/johnshew/agents-live/issues/370) asked for. The HTTP
endpoints become projections of it.

A fourth, smaller collapse rides along with the read model: a row should carry
one list of actions with an enabled flag and a reason, not parallel `can_pause`,
`can_activate`, and `can_claim` booleans consumed by four handlers and a slot
template. The host service dict already models eligibility that way and binds it
correctly; the agent Run control, which starts real work, has no eligibility
field at all.

## Slice 1: restore operational utility

Default to all repositories grouped under name and path headers, keep the agent
inventory and at least ten log lines visible together, move repository and host
administration to settings, and replace the expanded filter form with compact
search and filter controls.

Nothing in this slice requires a new runtime event. It is layout, information
design, eligibility, and one crash fix. The order below front-loads the changes
where the product actively misreports, then makes the structural collapse once,
then refines the surface that collapse defines. Work that reshapes the toolbar
or the inventory geometry waits for the merge, because that work genuinely does
cost twice. A call-site move, a projection, an eligibility field, and a cell
renderer do not, and they should not wait behind a large diff.

### First: stop the crash

| # | Change | Why | Requirements |
|---|---|---|---|
| 1 | Survive browser disconnect on Windows ([#401](https://github.com/johnshew/agents-live/issues/401)) | A dashboard that exits when a tab closes makes every other measure unobservable on that host | `REL-01` |

### Then: make the first screen operational

| # | Change | Why | Requirements |
|---|---|---|---|
| 2 | Move host services and repository settings into a right-side settings drawer | Two call-site moves return the log and the clipped agent row to the first screen | `LOG-01`, `AGT-03`, `RSP-01`, `REP-01`, `HST-01` |
| 3 | Add the attention bar over data the build already computes: failed newest runs, unavailable repositories, degraded or failed host maintenance | Turns the one important signal from red text into the first thing read, and gives host services a presence that costs no viewport when healthy | `HDR-08`, `HST-02`, `ACC-03` |

### Then: stop misreporting

| # | Change | Why | Requirements |
|---|---|---|---|
| 4 | Qualify the header health summary to its scope, or fold agent state into it, so it cannot read as a claim about the rows beneath it | The header reads `healthy` while an agent below it is failing | `HDR-11`, `HDR-02` |
| 5 | Give agent rows a run eligibility with a reason and bind the Run control to it, without yet moving the controls | The control that starts real work is the one that lies about eligibility | `AGT-04`, `AGT-08` |
| 6 | Encode state as icon plus word plus elapsed time, with the newest-run failure explicit | Removes the color-only dependency on the product's most important state | `AGT-02`, `ACC-03`, `DAT-04` |

### Then: collapse to one page

| # | Change | Why | Requirements |
|---|---|---|---|
| 7 | Collapse the page builders into one page over one snapshot read model; `--all-repos` becomes a scope default, not a second product | Every later item edits layout once instead of twice, and the read-only aggregate page stops being a dead end | `NAV-06`, `MUL-01`, `MUL-02`, `HDR-11` |

### Then: refine the surface it defines

| # | Change | Why | Requirements |
|---|---|---|---|
| 8 | Rebuild the body as attention, toolbar, inventory, splitter, log | Lets the operator set the inventory-to-log balance and keeps it across refresh | `RSP-01`, `RSP-02`, `AGT-03` |
| 9 | Replace the expanded filter row with one search input, one filter popover, and removable active-filter chips | Returns roughly a tenth of the viewport and makes the applied filter state legible | `FLT-01`, `FLT-02`, `FLT-03`, `FLT-04` |
| 10 | Move actions to the row end as one labeled primary control plus an overflow menu whose items carry their disabled reason, and order columns so identity, state, and failure stay adjacent | Ends the inverted prominence and the interrupted reading path | `AGT-17`, `HDR-04`, `HDR-05` |
| 11 | Model freshness: a live, updating, and stale progression with a much shorter cadence and a documented bound | Ten-minute-old state reads as current, and the cadence has no ceiling | `HDR-03`, `HDR-09`, `DAT-05`, `REF-01`, `REF-07` |
| 12 | Remove `window.location.reload()` from repository mutations in favor of a targeted refresh | Investigation state must survive configuration changes | `REF-03`, `REP-07` |
| 13 | Reduce the two health buttons to one action with one label, in settings and the overflow | Two controls with different labels invoking the same operation is a correctness problem, not a polish problem | `HST-06`, `HDR-04` |
| 14 | Give every truncated value and every placeholder an accessible detail affordance, including the owner sentinel | Truncated model and owner values are unrecoverable, and the sentinel explains itself nowhere | `AGT-09`, `DAT-10`, `RSP-06` |

Items 1 through 6 move the dashboard from unusable to usable, and none of them
needs the merge first. Items 8 through 14 are what make it feel designed.

### Interim honesty

Between this slice and the next the grouped view is the default, but agent
actions still resolve against the selected repository. Groups whose actions
cannot yet execute must say so in place, with the command that does work.
Silently disabled controls in that window would be a worse state than the
read-only page they replace.

### Exit criteria

`MUL-01`, `MUL-02`, `MUL-10`, `AGT-01`, `AGT-03`, `AGT-10`, `LOG-01`, `FLT-01`,
`REP-01`, `REP-08`, and `RSP-01` carry installed-artifact evidence, and from
that artifact at 1280 x 720:

- named repository groups, agent rows, and at least ten log lines are visible
  with the settings drawer closed;
- a failing agent is identifiable without relying on color, and its count
  appears above the inventory;
- every offered row action is one the target can actually perform, and every
  withheld action states why;
- applying a filter takes two interactions and clearing one takes one;
- a repository registration change leaves filters, selection, and log position
  intact;
- closing and reopening a browser tab on every supported host leaves the server
  running.

Recorded as viewport geometry and screenshots per the validation plan. Captured
evidence must not carry host names or absolute personal paths.

## Slice 2: complete read/write parity

Enable repository-qualified agent actions in every group, add repository,
selected-set, and all-repositories actions with previews and per-target
results, and make lifecycle and ownership eligibility match observed state.

Exit: `AGT-04`, `AGT-05`, `ACT-09` through `ACT-12`, `MUL-04`, `MUL-07`,
`OWN-01`, and `SEC-06` carry evidence including partial and stale-target
outcomes.

## Slice 3: make state explainable

Connect failures and health summaries to repository, agent, run, and log
evidence, distinguish expected runs, watcher liveness, stale data, and missing
data, and preserve directed drill-down and bookmarkable view state.

Exit: `HDR-08`, `HDR-10`, `AGT-11`, `AGT-13`, `AGT-14`, `WCH-01`, `LOG-07`,
`FLT-07`, `FLT-08`, and `NAV-01` carry evidence from the degraded dataset.
Open decisions 6 and 7 must close before this slice's health semantics are
final.

## Slice 4: harden the product

Meet performance, installed-artifact, cross-platform, disconnect, and damaged
data requirements, complete keyboard, screen-reader, responsive, and
reduced-motion acceptance, and prove dashboard discovery, readiness, and
durable failure evidence.

Exit: the `PRF`, `REL`, `ACC`, `DSC`, and `CMP` families carry evidence on
every supported platform.

## Dependencies

Each dependency must be confirmed to exist, or scheduled, before the slice that
consumes it starts. Open decision 7 tracks which of these still require new
runtime events.

| Dependency | Consumed by | Needed for |
|---|---|---|
| A shared health contract with the CLI | `HDR-02`, `HDR-10`, `MUL-06` | Slice 1 |
| A coherent cross-repository read model and refresh snapshot | `HDR-09`, `REF-02`, `REL-05`, `PRF-06` | Slice 1 |
| Provider-normalized model, usage, and list-cost data | `AGT-01`, `DAT-01` through `DAT-09` | Slice 1 |
| Repository-qualified action routing for aggregate mutations | `ACT-09`, `MUL-07`, `SEC-06` | Slice 2 |
| Structured dashboard action and terminal-exit records | `ACT-05`, `LOG-02`, `REL-02` | Slice 2 |
| A retained observability query interface for logs and timelines | `AGT-11`, `LOG-05`, `LOG-10` | Slice 3 |
| Structured watcher lifecycle and liveness evidence | `AGT-14`, `WCH-01` through `WCH-05` | Slice 3 |
| Expected-run semantics for schedules | `HDR-08`, `AGT-13` | Slice 3 |
| Accessible grid and toolbar primitives that stay correct under progressive rendering | `ACC-02`, `ACC-06`, `PRF-03` | Slice 4 |

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Aggregate reads make the first view slow | Share enumeration, load attention state first, and progressively render healthy detail. |
| Dense groups overwhelm small screens | Preserve repository and state first; move secondary fields into contextual details. |
| Bulk actions target stale rows | Re-resolve canonical targets at execution and report stale targets individually. |
| Live refresh interrupts investigation | Preserve view state and update coherent snapshots without forcing scroll. |
| New health summaries disagree with CLI state | Define one shared health contract and keep the dashboard a consumer. |
| Provider usage is unavailable for common runtimes, leaving cost surfaces mostly empty | Distinguish no run, unavailable telemetry, and explicit zero, and never render an unavailable value as zero. |
| Settings grows into a second dashboard | Keep settings to registration, defaults, and maintenance; operational warnings stay in context. |
| The packaged native surface diverges from the browser surface | Keep one implementation behind the public entry point and validate the installed artifact on each supported host. |
| Saved or linked views expose sensitive values | Keep links local, exclude secrets and log payloads, and validate restored references. |
| A large requirement set becomes one unreviewable change | Deliver vertical slices with requirement-to-evidence mapping. |

## Open decisions

Each of these is undecided product behavior. Resolving one updates the PRD or
the catalog; it is not closed by an issue thread alone.

| # | Question | Refines | Resolved by | Needed before |
|---|---|---|---|---|
| 1 | Is 10,000 agents a supported product scale or only a stress-test ceiling? | `PRF-03` | Measurement on a live host at scale | Slice 4 |
| 2 | Which repository actions belong on the group header, and which in its overflow menu? | `ACT-10`, `MUL-11` | Operator trial of the grouped layout | Slice 2 |
| 3 | Which actions require confirmation, and which stay one-click and recoverable? | `HDR-05`, `ACT-11`, `ACT-12` | Blast-radius review of each action | Slice 2 |
| 4 | What time window defines Needs attention and Recently failed? | `HDR-08`, `FLT-10` | Sampling real run history on a live host | Slice 3 |
| 5 | Do saved views persist per browser profile or in host-scoped state? | `FLT-09`, `REF-06` | Decision weighed against `SEC-05` | Slice 3 |
| 6 | What liveness evidence is sufficient for clock-only agents compared with resident watchers? | `HDR-10`, `AGT-14`, `WCH-01` | Runtime design for schedule and watcher evidence | Slice 3 |
| 7 | Which health calculations require new runtime events before the dashboard can represent them honestly? | `HDR-08`, `AGT-13`, `WCH-02` through `WCH-05` | Runtime gap analysis against the shared health contract | Slice 3 |

## Sequencing and pull request shape

- One slice 1 group, one pull request, except that item 7 is its own and may
  split into the read model and the page that consumes it if the diff is not
  reviewable in one pass.
- Items 2 through 6 land before item 7, even though item 7 reshapes the page.
  A drawer move, an attention projection, an eligibility field, and a cell
  renderer are rewritten for free while writing the merged page; a toolbar, a
  splitter, and a column order are not. Items 8 through 14 wait for item 7.
- Do not let item 7 hold the misreporting fixes. A product that tells an
  operator a failing host is healthy has a defect shipping today, and it should
  not wait behind the largest diff in the slice.
- Each item becomes a GitHub issue when it is scheduled. This plan sequences
  the work; it does not replace the issue that tracks it.

## What not to do

- Do not add a third page. A needs-attention view is a filter over the one
  inventory, not a new surface.
- Do not introduce dashboard-local health vocabulary. Everything the operator
  reads must trace to the shared contract the CLI uses.
- Do not recover space by shrinking the log. The log is the evidence surface;
  configuration is what yields.
- Do not defer item 1. Every measure taken on a host where the server exits on
  disconnect is unreliable.
- Do not commit screenshots that carry host names, owner identities, or
  absolute personal paths.
