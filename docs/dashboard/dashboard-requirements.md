---
title: Dashboard Requirements Catalog
description: Detailed, prioritized requirements for the Agents Live operational dashboard
ms.date: 2026-08-29
ms.topic: concept
---

# Dashboard requirements catalog

This is the canonical catalog of detailed, testable dashboard requirements.
The product decision, experience contract, user journeys, and success measures
live in
[dashboard-product-requirements.md](dashboard-product-requirements.md); when a
requirement here and the experience contract there disagree, the contract
wins and this catalog is corrected.
Delivery sequence, dependencies, risks, and open decisions live in
[dashboard-implementation.md](dashboard-implementation.md).

Supporting contracts live in
[dashboard-validation.md](dashboard-validation.md), which holds acceptance
scenarios, evidence, and release gates.

Requirement priorities use **P0** for release-blocking operational utility,
**P1** for complete product behavior, and **P2** for valuable follow-on
behavior. Implementation status belongs in the delivery issue or pull request,
not this product contract.

## Functional requirements

Requirements are grouped by user-facing capability. Every implementation issue
must name the requirement IDs it satisfies and the validation evidence that
will close them.

### Header and system status

| ID | Priority | Requirement |
|---|---|---|
| HDR-01 | P0 | The header must identify Agents Live, the current host identity, the selected repository or aggregate scope, and the runtime channel. |
| HDR-02 | P0 | Overall health must distinguish healthy, degraded, unavailable, stale, and check-in-progress states using text as well as color. |
| HDR-03 | P0 | The header must show the age of the displayed data and provide a manual refresh action. |
| HDR-04 | P1 | Primary global actions must be limited to frequent operational commands. Configuration and repair actions belong in settings. |
| HDR-05 | P1 | Destructive or broad actions such as Stop all must name their scope, require confirmation, and state how many agents are affected. |
| HDR-06 | P1 | A health warning must open the supporting details or the relevant host-service settings section. |
| HDR-07 | P1 | Header content must wrap or collapse without obscuring the agent inventory or log at narrow widths. |
| HDR-08 | P1 | The header must summarize items requiring attention across repositories, including failed agents, missed expected runs, dead or degraded watchers, unavailable repositories, and failed host maintenance. |
| HDR-09 | P1 | The header must distinguish live, refreshing, disconnected, and stale data states and show when the last coherent observation completed. |
| HDR-10 | P0 | Dashboard health definitions must use the same failure, expected-run, watcher-liveness, and staleness contract as the CLI and other supported status surfaces. |
| HDR-11 | P0 | Every health or attention summary must state the scope it covers and must not contradict detail visible in the same view. A summary derived only from host infrastructure must not present itself as overall health while a failing agent is displayed. |

### Agent inventory

| ID | Priority | Requirement |
|---|---|---|
| AGT-01 | P0 | Each row must show agent name, effective state, owner, runtime, effective or configured model, trigger summary, latest success, latest failure, and recent list cost when available. |
| AGT-02 | P0 | An agent whose newest completed run failed must be visibly failing even when it remains started or has a recent earlier success. |
| AGT-03 | P0 | The inventory must scroll independently and must not reduce the log below ten visible lines at common desktop viewport sizes. |
| AGT-04 | P0 | A local started agent must enable Stop and disable Start; a local stopped agent must enable Start and disable Stop. Run and Claim must reflect actual eligibility, and every disabled action must provide a reason. |
| AGT-05 | P0 | Lifecycle actions must apply to the canonical agent and selected repository shown to the operator; ambiguous names must never select a target silently. |
| AGT-06 | P1 | The operator must be able to filter by text, repository, state, health, owner, runtime, model, trigger type, and recent failure without a server round trip. |
| AGT-07 | P1 | Active filters, sorting, and selected agent must survive automatic and manual refreshes for the dashboard session. |
| AGT-08 | P1 | Columns must have stable widths or responsive priorities so action controls and critical state cannot be pushed off-screen by long paths, triggers, or model names. |
| AGT-09 | P1 | Long values must be truncated visually and available in full through an accessible detail mechanism. |
| AGT-10 | P1 | Empty, loading, unavailable, and malformed-definition states must be distinct and must provide actionable guidance. |
| AGT-11 | P1 | A selected agent must expose recent runs and correlated log activity without requiring the operator to manually construct a log query. |
| AGT-12 | P2 | Operators should be able to choose visible columns and retain that choice for the dashboard session. |
| AGT-13 | P1 | Each scheduled agent must show its next expected run and whether the latest expected run was completed, skipped, missed, or remains in progress. |
| AGT-14 | P1 | Each watched agent must distinguish declared started intent from observed watcher liveness. |
| AGT-15 | P1 | Operational summaries must expose run rate, error count or rate, duration, and queued or saturated work for the selected time window when those values are available. |
| AGT-16 | P2 | The inventory should support compact and comfortable density choices without changing the underlying scope or data. |
| AGT-17 | P1 | Column order must keep agent identity, effective state, and failure adjacent for scanning; action controls must not be placed between identity and the state, recency, or failure fields. |

### Search, filters, and views

| ID | Priority | Requirement |
|---|---|---|
| FLT-01 | P0 | The primary view must use one compact search field and one compact filter control rather than a permanently expanded row of filter inputs. |
| FLT-02 | P1 | Filter selection must use progressive disclosure, such as a popover or menu, and must not permanently consume agent-list height. |
| FLT-03 | P1 | Active filters must appear as individually removable chips or an equivalent compact summary with an obvious Clear all action. |
| FLT-04 | P1 | The filter summary must show the number of matching agents and make an empty result distinguishable from an empty repository. |
| FLT-05 | P1 | Text search must match agent name, repository name, repository path, runtime, model, and trigger summary. |
| FLT-06 | P1 | Filters must support multiple values within a facet and make the combination semantics clear. |
| FLT-07 | P1 | Search, filters, grouping, sorting, time window, and selected scope must be representable in the local URL so a view can be bookmarked and reopened. |
| FLT-08 | P1 | Opening a bookmarked view must validate stale repository or agent references and explain any part of the view that can no longer be restored. |
| FLT-09 | P2 | Operators should be able to save, name, apply, and delete local views without creating separate dashboards. |
| FLT-10 | P2 | The product should provide built-in views for All agents, Needs attention, Running now, Recently failed, and Unavailable without duplicating the dashboard. |

### Log and activity

| ID | Priority | Requirement |
|---|---|---|
| LOG-01 | P0 | The log must be visible in the initial all-repositories viewport and display at least ten lines at common desktop viewport sizes. |
| LOG-02 | P0 | The log must show dashboard action queued, started, succeeded, failed, cancelled, and timed-out states with compact local timestamps, timezone abbreviations, and elapsed duration. |
| LOG-03 | P0 | Startup, manual refresh, periodic refresh, and action completion must produce an updated summary of recent errors grouped by agent, with framework errors separate. |
| LOG-04 | P0 | New entries must follow the bottom only while the operator is already at the bottom. Reading earlier entries must not be interrupted. |
| LOG-05 | P0 | Dashboard output must be backed by the structured observability/query facilities. The UI must not infer state by hand-parsing unrelated raw log files. |
| LOG-06 | P1 | The operator must be able to filter activity by agent, severity/outcome, event type, and time window. |
| LOG-07 | P1 | Selecting a failing agent or failure indicator must narrow or highlight the correlated activity while preserving a clear way back to the prior view. |
| LOG-08 | P1 | Log truncation, malformed records, unavailable files, and retention boundaries must be disclosed; the dashboard must not present incomplete data as complete. |
| LOG-09 | P1 | A clear-log action may clear only the current visual buffer or filters. It must not delete retained framework records. |
| LOG-10 | P1 | Full retained logs and timelines must remain available through supported Agents Live log operations, with the dashboard offering an appropriate path to them. |
| LOG-11 | P2 | The log should provide pause/resume, jump-to-latest, and unseen-entry count controls for long investigations. |
| LOG-12 | P1 | Every top-level dashboard-generated entry must begin with the same `[HH:mm:ss TZ]` local-time format; continuation lines may remain indented beneath it. |
| LOG-13 | P1 | Repository, agent, run, and action selections must scope or highlight the corresponding activity while clearly displaying the active log scope. |

### Action coordination and feedback

| ID | Priority | Requirement |
|---|---|---|
| ACT-01 | P0 | Dashboard actions must execute through one FIFO coordinator and must not overlap. |
| ACT-02 | P0 | A request made while another action is active must become visibly queued; identical pending requests must be coalesced. |
| ACT-03 | P0 | The UI must immediately acknowledge an action and must not imply success before the command reports semantic success. |
| ACT-04 | P0 | Failure of one action must not prevent the next queued action from running. |
| ACT-05 | P0 | On completion, the dashboard must refresh affected state and record the command outcome and duration. |
| ACT-06 | P1 | Long-running actions must show progress state, elapsed time, and their bounded timeout where one exists. |
| ACT-07 | P1 | Cancelling a browser connection must not cancel or orphan an already accepted action without an explicit cancellation contract. |
| ACT-08 | P1 | Notifications must supplement the durable visible activity record, not replace it. |
| ACT-09 | P0 | An action initiated from the all-repositories view must carry a canonical repository-qualified agent identifier through validation, execution, logging, and refresh. |
| ACT-10 | P1 | Applicable actions must be available at agent, repository, selected-set, and all-repositories scopes without navigating away from the primary view. |
| ACT-11 | P1 | A repository-level action must name the repository, preview the affected agents, require risk-appropriate confirmation, and report per-agent outcomes. |
| ACT-12 | P1 | Bulk selection must remain visible while the operator reviews scope, and partial success must identify which targets succeeded, failed, were skipped, or became stale. |

### Repository settings

| ID | Priority | Requirement |
|---|---|---|
| REP-01 | P1 | Repository registration and default management must be available from the settings surface, not inline in the primary operational layout. |
| REP-02 | P1 | Settings must list registered repositories, paths, availability, and current default. |
| REP-03 | P1 | Operators must be able to register, unregister, set default, and clear default through existing repository validation and persistence rules. |
| REP-04 | P0 | Unregistering must never delete repository files, agent definitions, logs, triggers, or runtime state, and the UI must state this before confirmation. |
| REP-05 | P1 | Selecting a repository for viewing must not silently change the default repository. |
| REP-06 | P1 | Missing or invalid registered paths must remain visible and removable, with the validation error shown inline. |
| REP-07 | P1 | Registry changes must refresh selectors and affected dashboard data without discarding unrelated view state. |
| REP-08 | P1 | With no repositories registered, the dashboard must present a first-run state that names what is missing, routes to registration, and remains distinguishable from a registered repository with no agents and from a repository that cannot be read. |

### Host-service settings

| ID | Priority | Requirement |
|---|---|---|
| HST-01 | P1 | Detailed automatic-maintenance status and repair controls must be available in settings. |
| HST-02 | P0 | A degraded or failed host service must still be summarized in the primary view without requiring settings to be open. |
| HST-03 | P1 | Maintenance status must distinguish installed, missing, running, healthy idle, degraded idle, failed idle, and stale. |
| HST-04 | P1 | A failed check must retain its verdict, reason, completion time, and duration until a later successful check replaces it. Refresh alone must not clear failure. |
| HST-05 | P1 | While maintenance is running, the UI must show elapsed time and prevent duplicate starts. |
| HST-06 | P1 | Run again, refresh status, view logs, and repair schedule must be distinct actions with labels matching their effects. |
| HST-07 | P1 | Repair must show its target and result and must use the same supported repair behavior as the CLI. |

### Ownership

| ID | Priority | Requirement |
|---|---|---|
| OWN-01 | P1 | Claim and transfer controls must state the current owner, action direction, and destination identity before execution. |
| OWN-02 | P1 | Operators must be able to claim an agent to the current host and, when supported by the configured ownership backend, transfer it to a named destination. |
| OWN-03 | P1 | Dashboard ownership operations must have equivalent supported CLI behavior so recovery never requires a browser. |
| OWN-04 | P1 | Unavailable ownership, unknown destinations, and disabled shared ownership must fail closed with recovery guidance. |

### Aggregate repositories

| ID | Priority | Requirement |
|---|---|---|
| MUL-01 | P0 | The default view must show every registered repository, including unavailable repositories and their errors. |
| MUL-02 | P0 | Agents must be grouped by repository by default, with a sticky group header showing repository name and full path; rows must be sortable by meaningful displayed fields with visible direction and stable tie-breaking. |
| MUL-03 | P1 | Repository selection, grouping, and sorting must survive refreshes for the dashboard session. |
| MUL-04 | P0 | The aggregate view must provide Run, Start, Stop, and Claim for each eligible agent without requiring navigation to a single-repository view. |
| MUL-05 | P1 | The operator must be able to move from an aggregate repository or agent to its single-repository operational view without losing aggregate filters or sorting. |
| MUL-06 | P1 | Aggregate layouts must use the same health definitions, labels, timestamps, and list-cost semantics as the single-repository view. |
| MUL-07 | P0 | Every aggregate mutation must resolve the repository and canonical agent before execution; unavailable, removed, or ambiguous targets must fail closed with an actionable message. |
| MUL-08 | P1 | Successful aggregate actions must refresh the affected repository and aggregate summaries without resetting the operator's view state. |
| MUL-09 | P1 | Cross-repository bulk actions must preview the repositories and agents affected, require explicit confirmation, and report per-target outcomes. |
| MUL-10 | P0 | Health inspection, logs, filters, sorting, costs, agent details, and eligible actions must remain available within each repository group. |
| MUL-11 | P1 | Repository headers must remain visible while their agents scroll and must provide collapse, focus, and repository-level action controls without hiding health or availability. |
| MUL-12 | P1 | Selecting a repository for focus must preserve a direct route back to the prior all-repositories view and its exact state. |

### Cost and time semantics

| ID | Priority | Requirement |
|---|---|---|
| DAT-01 | P1 | The dashboard must label provider-normalized completed-run value as **List cost** and show 24-hour and 1-week windows. |
| DAT-02 | P1 | Totals must sum reported numeric values only; an agent with no cost-bearing run in the window must show `-`, while an explicit reported zero must show zero. |
| DAT-03 | P1 | The dashboard must not describe list cost as billed, actual, forecast, or invoice cost. |
| DAT-04 | P1 | Relative times must expose the exact local timestamp and timezone through an accessible detail. |
| DAT-05 | P1 | Every data source must use its canonical freshness threshold across the header, host maintenance, and agent activity, and the dashboard must expose the observation age, threshold, and stale reason. |
| DAT-06 | P1 | The dashboard must retain and present provider-reported usage units, including credits, requests, and token categories when available, without inventing missing values. |
| DAT-07 | P1 | Usage and cost displays must name their unit and source and distinguish no run, telemetry unavailable, explicit zero, and conversion failure with consistent semantics across providers. |
| DAT-08 | P1 | Any conversion from provider usage to list cost must use an explicit documented rate and preserve the original reported quantity. |
| DAT-09 | P1 | When provider usage exists but list-cost conversion fails or is unavailable, the original usage must remain visible and list cost must be marked unavailable rather than zero. |
| DAT-10 | P1 | Every placeholder or sentinel rendered in place of a value must have its meaning available where it appears, including owner, model, and usage placeholders. |

### Watcher observability

| ID | Priority | Requirement |
|---|---|---|
| WCH-01 | P0 | A watched agent must not be presented as healthy solely because started intent exists; the dashboard must incorporate observed watcher liveness. |
| WCH-02 | P1 | Watcher evidence must include startup, heartbeat or equivalent liveness, matched firing, degradation, and terminal stop events. |
| WCH-03 | P1 | A firing event must show at least the matched-path count and debounce window; sensitive paths must remain hidden unless a separate disclosure policy permits them. |
| WCH-04 | P1 | Buffer overflow, queue drop, bounded-rescan truncation, and unreadable or deleted roots must surface as degraded or failed states with time and reason. |
| WCH-05 | P1 | Operators must be able to distinguish watching with no matching changes, a non-matching expression, delayed work, and a dead watcher from dashboard evidence. |

### Navigation and operator productivity

| ID | Priority | Requirement |
|---|---|---|
| NAV-01 | P1 | Repository, agent, run, failure, and log evidence must form a directed drill-down path with a clear return to the previous context. |
| NAV-02 | P1 | Repository, agent, and run details must have stable local deep links that restore the relevant scope and evidence without exposing the dashboard off-host. |
| NAV-03 | P1 | Agent details must open in a side panel or equivalent contextual surface that preserves the grouped inventory and log state. |
| NAV-04 | P2 | A keyboard-accessible command palette should provide fast navigation and eligible actions across repositories and agents. |
| NAV-05 | P2 | Common operations should expose documented keyboard shortcuts without preventing normal browser or assistive-technology shortcuts. |
| NAV-06 | P1 | The dashboard must use one adaptable primary experience with views and scope controls rather than separate duplicated dashboards for common investigations. |

### Dashboard discovery and lifecycle

| ID | Priority | Requirement |
|---|---|---|
| DSC-01 | P1 | Dashboard startup and `dashboard list` must present the actual clickable loopback URL for each running dashboard while retaining the numeric port identifier. |
| DSC-02 | P1 | Operators must be able to request the first available port at or above the product default without changing explicit-port or omitted-port behavior. |
| DSC-03 | P1 | The selected URL must be announced before serving, and a race that claims the port before bind must produce a clear structured conflict. |
| DSC-04 | P1 | Dashboard list, readiness, registry, and browser availability must agree on whether a dashboard is answering. |

## Cross-cutting requirements

### Responsive layout

| ID | Priority | Requirement |
|---|---|---|
| RSP-01 | P0 | At 1280 x 720 and larger, repository-grouped agent rows and at least ten log lines must be simultaneously visible without page-level vertical scrolling. |
| RSP-02 | P1 | At shorter desktop heights, operators must be able to resize or switch between agent and log surfaces without losing state. |
| RSP-03 | P1 | At narrow widths, secondary columns may collapse into details, but repository identity, agent identity, effective state, failure, and eligible actions must remain available. |
| RSP-04 | P1 | Fixed headers, controls, and status text must not overlap or cause horizontal page scrolling; wide grids may scroll within their own region. |
| RSP-05 | P1 | Dynamic labels, queued actions, and long errors must not resize the page so operational surfaces become hidden. |
| RSP-06 | P1 | Repository identity must remain visible when columns collapse or a grid region scrolls horizontally. |

### Accessibility

The target is WCAG 2.2 AA. Composite interactions follow the WAI-ARIA Authoring
Practices for [grid](https://www.w3.org/WAI/ARIA/apg/patterns/grid/) and
[toolbar](https://www.w3.org/WAI/ARIA/apg/patterns/toolbar/); those patterns are
normative here and their key bindings are not restated. `ACC-08` onward record
what those patterns do not settle for this product.

| ID | Priority | Requirement |
|---|---|---|
| ACC-01 | P0 | Every core journey must be operable by keyboard with visible, predictable focus. |
| ACC-02 | P1 | Interactive grids and compact toolbars must implement the APG grid and toolbar patterns, so each is one page-level tab stop with managed focus rather than an unbounded tab sequence. |
| ACC-03 | P1 | Health, ownership, eligibility, selection, progress, and failure must never rely on color alone. |
| ACC-04 | P1 | Text, controls, focus indicators, zoom, reflow, pointer targets, and text-spacing overrides must meet WCAG 2.2 AA without clipping labels, timestamps, or paths. |
| ACC-05 | P1 | Dynamic action and log updates must be announced without moving focus or repeatedly interrupting reading. |
| ACC-06 | P1 | Progressive rendering must expose logical row and column counts, positions, selection, and sorting to assistive technology, and keyboard navigation must reach rows that are not currently rendered. |
| ACC-07 | P1 | Motion and automatic movement must respect reduced-motion preferences. |
| ACC-08 | P1 | Landmarks must identify header, inventory, log, and settings, and a skip mechanism must reach the inventory and the log directly. |
| ACC-09 | P1 | A repository group's accessible name must carry repository identity and availability, with the full path as description rather than repeated per row. |
| ACC-10 | P1 | Sorting, filtering, collapsing, and refresh must preserve the focused logical item; if it no longer exists, focus must move to the nearest meaningful item and the reason must be announced. |
| ACC-11 | P1 | Closing a transient surface must return focus to the control that opened it, and a confirmation must state action, repository scope, target count, and material side effects before its confirm control. |
| ACC-12 | P1 | A withheld action must remain discoverable through an accessible reason without entering the normal action sequence. |
| ACC-13 | P1 | The log's live region must carry concise action and failure summaries only, never full payloads; auto-follow must not move focus, and new entries away from the bottom must increment an announced unseen count instead of scrolling. |
| ACC-14 | P2 | The search input must retain text-editing arrow keys, either as the last toolbar control or through a documented interaction that separates editing from toolbar navigation. |

### Performance

| ID | Priority | Requirement |
|---|---|---|
| PRF-01 | P1 | The first useful operational view must render within 3 seconds for 100 agents and 10 repositories on a supported local host under normal load. |
| PRF-02 | P1 | Filtering and sorting 1,000 loaded rows must update visible results within 100 milliseconds. |
| PRF-03 | P1 | A 10,000-agent stress profile must use progressive or virtual rendering rather than creating one active row element per agent. |
| PRF-04 | P1 | Manual refresh must acknowledge immediately and complete or show continuing progress within 2 seconds. |
| PRF-05 | P1 | Periodic refresh must not block scrolling, filtering, log reading, or action feedback. |
| PRF-06 | P1 | Expensive host enumeration must be shared within a coherent refresh pass rather than repeated per widget or row. |

### Reliability and recovery

| ID | Priority | Requirement |
|---|---|---|
| REL-01 | P0 | Browser refresh, navigation, disconnect, reconnect, or client reset must not terminate the dashboard server. |
| REL-02 | P1 | An unexpected dashboard exit must leave a durable reason, outcome, and correlation context when the runtime can observe them. |
| REL-03 | P1 | A damaged log, unavailable ownership backend, invalid definition, or missing repository must degrade only the affected information. |
| REL-04 | P1 | Refresh failure must leave the last coherent data visible, marked stale, with an actionable error. |
| REL-05 | P1 | Reconnect must obtain one coherent snapshot before resuming updates and must not present mixed observation times as current. |
| REL-06 | P1 | The foreground process, dashboard registry, readiness endpoint, and browser availability must agree on whether the dashboard is running. |

### Safety and security

| ID | Priority | Requirement |
|---|---|---|
| SEC-01 | P0 | The dashboard must bind to loopback by default and must not expose host, repository, agent, or log inventory to the network without a separate security design. |
| SEC-02 | P0 | The browser must not gain a general command surface; mutations must route through supported, validated Agents Live operations. |
| SEC-03 | P0 | Repository and agent identifiers must be canonicalized and authorization revalidated when an action is accepted. |
| SEC-04 | P1 | Paths, labels, log text, and provider output must render as data, never executable markup. |
| SEC-05 | P1 | Secrets and sensitive provider payloads must not enter summaries, notifications, URLs, or client-side persistence. |
| SEC-06 | P0 | Aggregate mutations must enforce repository-qualified scope at the action boundary, not only in client-side labels or controls. |

### Compatibility

| ID | Priority | Requirement |
|---|---|---|
| CMP-01 | P0 | The dashboard must work from the installed wheel and editable source through the public `agents-live dashboard` entry point. |
| CMP-02 | P1 | Supported behavior must be equivalent on Linux, WSL, and native Windows except for explicit host-maintenance details. |
| CMP-03 | P1 | Existing readiness APIs used by operational gates must remain deterministic or be versioned when their contract changes. |
| CMP-04 | P1 | The dashboard must not require a public Python API or freeze internal module paths for external consumers. |

### Refresh and session behavior

| ID | Priority | Requirement |
|---|---|---|
| REF-01 | P1 | Operational state must refresh automatically on a documented cadence and show the last successful coherent refresh time. |
| REF-02 | P1 | Manual refresh must update health, agents, summaries, and relevant settings as one coherent observation whenever practical. |
| REF-03 | P0 | Refresh must preserve filters, sorting, grouping, selection, split allocation, settings context, and non-bottom log position. |
| REF-04 | P1 | Refresh must preserve bookmarkable URL state without adding browser history for every automatic update. |
| REF-05 | P1 | An active or queued action must remain visible through refresh until its terminal outcome is known. |
| REF-06 | P1 | Browser storage may retain view preferences but must never become the source of repository or host state. |
| REF-07 | P1 | The automatic refresh cadence for operational state must be bounded so displayed state cannot silently age past its freshness threshold between refreshes. |

## Traceability

Success measures and non-goals are canonical in
[dashboard-product-requirements.md](dashboard-product-requirements.md).
Acceptance scenarios, evidence records, and release gates are canonical in
[dashboard-validation.md](dashboard-validation.md).

| Requirement families | Primary source | Validation journey |
|---|---|---|
| `HDR`, `AGT`, `MUL`, `RSP` | [#104](https://github.com/johnshew/agents-live/issues/104), [#229](https://github.com/johnshew/agents-live/issues/229), [#276](https://github.com/johnshew/agents-live/issues/276) | [Monitor all repositories](dashboard-validation.md#monitor-all-repositories) |
| `FLT` | [#104](https://github.com/johnshew/agents-live/issues/104), product review | [Filter and restore a view](dashboard-validation.md#filter-and-restore-a-view) |
| `LOG`, `NAV` | [#104](https://github.com/johnshew/agents-live/issues/104), [#356](https://github.com/johnshew/agents-live/issues/356) | [Investigate a failure](dashboard-validation.md#investigate-a-failure) |
| `ACT`, `OWN`, `SEC` | [#276](https://github.com/johnshew/agents-live/issues/276), [#289](https://github.com/johnshew/agents-live/issues/289) | [Operate an agent](dashboard-validation.md#operate-an-agent) and [operate a repository](dashboard-validation.md#operate-a-repository-selection-or-all-repositories) |
| `REP`, `HST` | [#229](https://github.com/johnshew/agents-live/issues/229), [#325](https://github.com/johnshew/agents-live/issues/325) | [Configure repositories and host maintenance](dashboard-validation.md#configure-repositories-and-host-maintenance) |
| `DAT` | [#294](https://github.com/johnshew/agents-live/issues/294), [#356](https://github.com/johnshew/agents-live/issues/356) | [Investigate a failure](dashboard-validation.md#investigate-a-failure) |
| `WCH` | [#393](https://github.com/johnshew/agents-live/issues/393) | [Degraded dataset](dashboard-validation.md#degraded) and [reliability acceptance](dashboard-validation.md#reliability-acceptance) |
| `DSC`, `CMP`, `REL`, `REF` | [#285](https://github.com/johnshew/agents-live/issues/285), [#367](https://github.com/johnshew/agents-live/issues/367), [#401](https://github.com/johnshew/agents-live/issues/401) | [Reliability acceptance](dashboard-validation.md#reliability-acceptance) |
| `ACC` | WAI-ARIA Authoring Practices and product review | [Accessibility acceptance](dashboard-validation.md#accessibility-acceptance) |
| `PRF` | Product review and live-host evidence | [Performance acceptance](dashboard-validation.md#performance-acceptance) |

Implementation work belongs in GitHub issues. Each issue and pull request must
name the requirement IDs it changes and link the corresponding evidence.