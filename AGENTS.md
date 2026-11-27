---
title: Agents Live Repository Guidance
description: Guidance for coding agents working in the agents-live repository
---

Guidance for coding agents (Claude Code, GitHub Copilot, others)
working in this repository.

**agents-live** is a Python package that adds safe, local automation -
cron and file-watch dispatch, safety wrappers, and operations - to
standard Claude Code and GitHub Copilot agent definitions. Start with
[README.md](README.md) for what the tool does; this file covers how to
work on it.

## Continuous Development and Learning

CDL and GLP are active by default unless the user explicitly pauses or disables
them. At task start, read and FOLLOW both [CDL](.agents/cdl-glp/cdl.md) and
[GLP](.agents/cdl-glp/glp.md), together with the
[project adaptation](.agents/cdl-glp/README.md). These are operating instructions,
not optional reference links or background automation.

Assess every steering input before acting: acknowledge its classification, new
goals (or none), current goals, continuation conditions and workstream states.
Begin the final message of every reply to a user message, in every harness,
with one line:
`Steering: <classification>; new goals: <list or none>; still open: <list or
none>; continue until: <condition>.` The final message is the text after the
last tool call, or the only message in a tool-free reply. Put the answers in
that same final message: earlier text collapses with the tool calls and is
hidden from the developer. Repository hooks enforce this:
[.github/hooks/steering.json](.github/hooks/steering.json) appends a reminder
to each prompt and forces one correction turn when the final message lacks the
line, and
[.claude/settings.json](.claude/settings.json) adds the reminder for Claude
Code. Both run [tools/steering-hook.py](tools/steering-hook.py) and load only in
a trusted checkout.
Preserve compatible unfinished goals and explicit sequencing dependencies. A
question or status request does not pause work. Continue authorized, unblocked
work until acceptance, an explicit pause/stop, or a genuine boundary prevents it.
Present developer decisions as plain text in the reply, following
[.agents/decisions.md](.agents/decisions.md); never use a blocking question
tool such as `ask_user`, because a pending form captures later messages as its
answer instead of steering. Continue independent work while a decision is open.
Give each decision plain-language context, lead with a recommendation, and
explain the benefit, cost, and risk of the alternatives.

After steering and completion summaries, provide a brief
workstream/phase/verified-delta/next-action update. Harnesses other than the
GitHub Copilot app and Copilot CLI also show a scoped single-line checklist for
goals/workstreams, steering, repository protocol and GLP. Refresh applicable
status at meaningful phase, checkpoint, blocker and resumption changes; do not
repeat unchanged ceremony. For those checklists, use ASCII `[x]` and `[ ]`
instead of the portable contract's Unicode symbols. Check only verified
obligations and name pending or
not-needed dispositions. The harness-aware exception and final-accounting duties
are defined in Local Policy below.

Before reporting completion, run session analysis and the GLP learning pass,
apply any necessary authorized durable updates and independently read them back.
Record supported `no_change` when appropriate; account for formal consolidation
separately. Before stopping, put the answer first, then account for every goal and
workstream, outstanding checks, learning, publication and open decisions in the
final visible message after the last tool call. Explain why stopping covers all
open work; continue if an authorized, unblocked action remains.

Delegate substantive work when allowed and supported, within the current user's
scope, tool restrictions and project policies. Give delegates bounded ownership
and require compact evidence reports; the supervisor verifies acceptance. A
coordinator or session handoff carries standing rules and later developer
directions, plus each workstream's owner, branch or head SHA, state and next
action. For recurring coordination, follow the coordinator procedure in
[.agents/development.md](.agents/development.md#orchestrating-delegates-and-child-sessions):
run `uv run --script tools/coordinator-check.py` at each self-wake tick, then
dispatch, record and end the coordinator turn. Keep long work in detached child
sessions or processes. If delegation is unavailable or prohibited, proceed
directly within authority.

### Local Policy and Overrides

This policy overrides the portable contracts' tracked-log, checkpoint-all-edits
and publication defaults. Existing project safety, validation and release rules
prevail; CDL/GLP add no write, integration, release or deployment authority.

- **Plain-language reporting:** describe each workstream by its purpose for the
  developer, what is ready or blocked, and the next action. Omit session IDs, SHAs
  and code names unless requested or needed for a technical handoff.
- **Harness-aware bookkeeping:** in the GitHub Copilot app or Copilot CLI, the
  harness already records session history, so do not create a local `logs/cdl/`
  session log or print the per-update single-line checklist. Still begin the
  final message of each reply to a user message with the steering line and provide the
  answer-first final accounting of every goal and workstream, outstanding checks,
  learning, publication and open decisions before stopping; include evidence in
  delegate handoffs; and run the GLP learning pass with independent readback. Other
  harnesses keep the log and checklist rules below. This local exception overrides
  the portable CDL session-log and checklist defaults only for the named harnesses.
- **Session records:** in harnesses other than the GitHub Copilot app or Copilot
  CLI, create one UUID-named, UTC-timestamped Markdown log in gitignored
  `logs/cdl/`; reuse it across turns/resumption and append verified snapshots,
  outcomes and GLP dispositions. Logs remain local-only: never track,
  commit, push or publish them, including receipt-only checkpoints. Do not copy
  raw operational logs into docs, issues, PRs or memory. Publish only sanitized
  instruction/GLP changes under explicit authority. Preserve pre-adoption records
  already tracked on main as historical evidence; do not modify or add tracked
  session logs under this policy.
- **Knowledge:** domain facts, decisions and continuing project work belong in
  existing `docs/` records and GitHub issues under their current ownership rules.
  Method lessons append to their owning existing `.agents/` guide or root
  instructions using GLP's update format. Existing learning records remain
  evidence; do not migrate, erase or duplicate them on adoption. Formal
  consolidation is a separately scoped per-document phase with an explicit
  applied/unconsolidated, pending, blocked or supported no-change disposition.
- **Evidence and privacy:** cite relative document/section links, contract
  versions, compared revisions or hashes, source/observation dates and exact
  check results; label user direction, observation, inference and uncertainty.
  Sanitize durable records and external writes for the export/privacy boundary:
  no personal repository/account/machine names, source paths, secrets, raw logs
  or private source policies. Preserve required evidence in its approved home,
  not by importing private source material into this tree.
- **Validation:** use the existing development/testing and release gates for the
  touched boundary; do not weaken required gates or invent extra broad gates for
  routine prose. Documentation-only adoption uses the export/link audit and
  `git diff --check`; it does not claim source, artifact, installed-runtime or
  fresh-client behavioral acceptance. Reuse unchanged passing evidence under
  [.agents/testing.md](.agents/testing.md).
- **Authority and concurrency:** do not infer commit, push, PR, merge, deployment,
  runtime, schedule or hook permission from CDL/GLP activation. An explicit user
  request to open a PR authorizes committing the current branch's scoped changes,
  pushing that branch and opening the requested PR, unless a current restriction
  forbids those actions; it does not authorize merge or deployment. Future requests
  still need applicable project authority. Preserve concurrent work, review diffs
  and index scope, and stage explicit authorized paths only. Never automatically
  publish another writer's edits or sweep a worktree's uncommitted changes into a
  checkpoint. Retain an uncommitted worktree when the user requires that handoff.

## Load before acting

| When you are... | Read first |
|---|---|
| Selecting a subagent model | [.agents/subagent-models.md](.agents/subagent-models.md) |
| Asking the developer to decide | [.agents/decisions.md](.agents/decisions.md) |
| Coordinating delegates or child sessions | [.agents/development.md](.agents/development.md#orchestrating-delegates-and-child-sessions) |
| Changing code, running tests, or building | [.agents/development.md](.agents/development.md) |
| Understanding the development and release state machine | [docs/development-release-process.md](docs/development-release-process.md) |
| Comparing source, wheel, and installed-tool behavior | [.agents/testing.md](.agents/testing.md) |
| Adding, changing, or deleting a test | [docs/testing-methodology.md](docs/testing-methodology.md) |
| Cutting or preparing a release | [.agents/release.md](.agents/release.md) |
| Reporting release cycles and numbered RC state | [.agents/release-report.md](.agents/release-report.md) |
| Creating, running, or debugging triggered agents in this checkout | [.agents/agents-live.md](.agents/agents-live.md) |
| Changing the skill payload, docs, or templates | [src/agents_live/skill/SKILL.md](src/agents_live/skill/SKILL.md) and [docs/](src/agents_live/skill/docs/) |
| Recording a design decision or checking project direction | [docs/README.md](docs/README.md) and [docs/backlog.md](docs/backlog.md) |
| Investigating runtime behavior (debounce, watchers, adapters) | [approach.md](src/agents_live/skill/docs/approach.md), then [key-learnings.md](src/agents_live/skill/docs/key-learnings.md) |

## Quick commands

```bash
uv run --with-editable . python -m unittest discover -s tests -v # tests
uv run --with-editable . agents-live smoketest          # framework smoke
uv run --with-editable . agents-live --help              # CLI from source
uv run --script tools/pre-release-audit.py               # release audit
uv run --script tools/release.py --cycle-status          # retained attempts
uv run --script tools/coordinator-check.py               # what changed since last check
uv run --script tools/local-deploy.py --repo <live-repository> --rc <next-rc>
uv run --script tools/release.py --publish --attempt <final-attempt> --yes
```

## Workflow

The standard loop for any change that lands as commits:

1. Read the guide matching the task (table above), check `gh issue list` for
  related backlog, then refresh and read the release report before choosing a
  target branch:

  ```bash
  git fetch origin --prune
  uv run --script tools/release-report.py
  ```

  The generated `.reports/release-report.md` identifies the active release
  cycles, configured source branches, tested versions, and next actions. Treat it as
  required routing context, not as a release-only document.
2. Investigate in place; reads and searches are fine in the primary
   checkout.
3. Use the primary checkout only when it is clean and already on the intended
  target branch. Otherwise, create a dedicated worktree from that target;
  verify its ancestry before committing or pushing, and remove it when the
  task is complete.
4. Edit, then run the smoke tests and the release audit (Quick commands above).
  Use `tools/validate.py focused <unittest-selector>` during the edit loop and
  `tools/validate.py pr` for source gates; see `.agents/testing.md` for evidence
  reuse and artifact boundaries. Refresh origin at meaningful checkpoints and
  follow `.agents/development.md` for rebase overlap review and write identity.
  Reuse passing evidence when the tested inputs and environment are unchanged;
  do not rerun gates merely at a handoff or before preparation runs them itself.
  See `.agents/testing.md` for artifact and installed-state boundaries.
5. Commit, push, and open a pull request. Reference an issue only when
   one already covers the work.
6. After checks pass, merge with `gh pr merge <n> --merge`.
   `--delete-branch` works from this checkout, but it switches to
   `main` first, so only pass it with a clean tree.
7. Confirm the merged commits are reachable from `origin/main`, then
   switch to `main` and fast-forward. Delete the head branch
   (`git push origin --delete <branch>`) if the repository did not
   delete it already.

### Release cycle routing

Read `.github/release-cycles.toml` and the generated report. Develop on `main`
unless the selected cycle explicitly needs a stabilization branch. Test numbered
RCs within each release cycle. A branch or passing PR does not establish
acceptance or publication.
There is no bake branch, bake worktree, or bake-to-main promotion stage.
Short-lived development worktrees are cleaned up after integration. Retained
numbered-attempt worktrees under the common Git directory are immutable release
evidence, not development or bake worktrees; preserve them with their receipts.
Release refs are retained as tags: `rc/<attempt>` for each prepared RC,
`v<version>` for stable finals and `archive/...` for retired legacy attempts.
Branches kept are `main`, release branches for the last two official releases,
the active cycle's branch and only the current RC's candidate branch; see
`.agents/release.md` for the retention plan.
The report must show all active cycles, open work, retained candidate attempts,
the selected local runtime, and independent publication evidence.

Local activation is an official part of RC testing:

```bash
git pull --ff-only origin <configured-source-branch>
uv run --script tools/local-deploy.py --repo <live-repository> --rc <configured-next-rc>
```

Use a clean synchronized checkout or isolated worktree. The command preserves
immutable packages and restores dashboards and watchers, with rollback on failed
activation. Advance the RC number for changed bytes; never reuse a consumed
identity. Local readiness is not full operational acceptance or permission to publish.

Published `6.9.2` retains developer-approved RC5 runtime source.
Published `6.9.3` retains developer-approved `6.9.3rc1`, including #528 and #532
and initial #530/#531 work. Preserve its exact source and wheel approval identity.
The developer reassigned outstanding #530 and #531 to the `6.9.4rc1` development
scope on 2026-10-04, together with #547, #540, #542, #549 and #550. This is scope
assignment, not delivery or candidate acceptance. Preserve 6.9.3 publication
and retained evidence independently; #533 remains outside the requested scope.
Published cycles have no next RC; #530/#531 remain partial in 6.9.3, with their
outstanding work planned only in 6.9.4.
`6.9.4rc1` was prepared and consumed from `cd0be2e` on 2026-10-04.
Preserve its immutable candidate evidence. `6.9.4rc2` was allocated and consumed
from `243ec7e` on 2026-10-04, but preparation failed at packaged dashboard
repositories readiness and it was not accepted. Preserve its attempt, commit,
build records and artifacts. `6.9.4rc3` was prepared and consumed from
`8cda101` on 2026-10-04, passed isolated acceptance and was locally activated,
but is not publication-approved. RC4 was prepared and consumed from `dccdc4b`
on 2026-10-05, passed isolated acceptance and is selected locally on the
candidate channel, but is not publication-approved. On 2026-10-08, the
developer assigned #565 to RC5 for the main lock fix only after a maintenance
run on RC4 held the runtime launch gate for about 19 minutes and missed
scheduled launches. Task Scheduler query batching is a separate deferred
follow-up tracked by #566. RC5 was allocated and consumed from main with the
#565 fix. Initial preparation and a retry failed packaged dashboard readiness
because the validator did not drain the dashboard child's output pipe, blocking
logging; the failure reproduced at RC4 source and was not a product regression.
PR #571 corrected the validator, and requalification reused all four artifacts
byte-exact, passed all gates and passed isolated candidate acceptance.
The first deployment was refused by the overly restrictive requalification
allowlist, which excluded instruction-only changes from PR #570 since RC5
source. The orchestrator's tooling-only #572 correction merged in PR #573;
the requalified deployment then passed the package-input check, but activation
was refused while scheduled agent runs were in progress because activation
refuses while any run is active. The orchestrator also removed a stale
repository registration for a missing folder to clear the candidate doctor
check. RC5 was never locally activated and is superseded by RC6. On 2026-10-08,
the developer directed that activation must not wait for agent runs already in
progress; the orchestrator filed #574 for this package-changing product fix.
RC6 was prepared from source `3733230d61b5f94e67cd33bf4d5482bb4fa38601`,
passed packaged readiness and isolated hello-world acceptance, and was locally
activated on 2026-10-09. The installed RC4 runtime refused the first activation
while an agent run was in flight. After instruction-only commits advanced main,
local-deploy refused the older prepared source until tooling fix #578 merged in
PR #579. Local-deploy then reused retained RC6 bytes and activated them at the
first idle moment via a background retry. The installed version, all-repository
doctor check and RC6-generation watchers were verified. On 2026-10-09, the
developer approved the exact deployed RC6 source and retained wheel for 6.9.4
publication and moved #582 to 6.9.5, folding and closing #581 into its logging
scope. The developer explicitly approved shipping with the logging and watcher
observability gaps in #582; list that known deferred work in the release notes.
Preserve the approval identity in the cycle manifest and do not
retest functionality or change the local runtime. Stable `6.9.4` was published
from RC6 through retained attempt `6.9.4-final-1` on 2026-10-09. GitHub package
downloads and PyPI package digests matched the retained stable artifacts; the
package proxy did not yet list `6.9.4`. Do not prepare or publish it again.
Publication did not change the local runtime. #578's tooling-only correction
is included in the GitHub release notes; its shipped changelog entry remains
deferred to the next package change. Do not reuse consumed RC1 through RC6. Retain the
merged #556, #538, #560 and #561 work and the dashboard responsiveness
follow-up. Issues #565, #572, #574 and #578 are delivered; #566, #567 and #582
remain deferred. RC5 is not publication-approved.
The 6.9.5 cycle is the default development cycle on `main`. Its planned scope is
#582 lock, timing, runtime-identity and watcher-exit evidence, #591 named agent
start/stop admin events and the #590 test isolation fix, all merged to main
before RC1. #588 is on hold until evidence shows it is needed; #566 and #567
remain deferred. 6.9.5rc1 was prepared from `98b4b4d`, passed isolated acceptance and was
locally activated on 2026-10-10; it is not publication-approved. Do not reuse
consumed RC1. 6.9.5rc2 was prepared from `7743abd` with the dashboard and
status responsiveness work (#601, #602, #603, #539), passed isolated acceptance
and was activated locally on 2026-10-29. A watcher observer start-up timeout
during activation left no local-deploy receipt; see its cycle history. It is not
publication-approved. Do not reuse consumed RC2. 6.9.5rc3 was prepared from
`fff7743` with the Windows schedule time zone fix (#609), the Copilot
commentary-answer fix (#616), dependency compatibility fixes and help text,
passed isolated acceptance and was activated locally on 2026-11-22. It is not
publication-approved. Do not reuse consumed RC3. 6.9.5rc4 was prepared from
`6a9a261` with local wall-clock schedules (#627), passed isolated acceptance and
was activated locally on 2026-11-24, but no Windows scheduled task launched
under it, so it was rolled back to RC3 about 11 minutes later (#630). It is not
acceptable for publication. Do not reuse consumed RC4. 6.9.5rc5 was prepared
from `8c90b97` with the Windows trigger fix (#631), passed isolated acceptance
and was activated locally on 2026-11-24; runtime maintenance and a life clock
agent were then observed launching within seconds of their due minutes. It is
not publication-approved. Do not reuse consumed RC5. The next numbered RC is
`6.9.5rc6`. 6.9.5rc6 was prepared from `4cdf0fb` with the watcher startup
timeout and exit-log schema fixes from PR #635, passed isolated acceptance, and
was locally activated on 2026-11-26. On 2026-11-27 the developer approved the
exact deployed 6.9.5rc6 source and retained wheel for 6.9.5 publication without
rebuilding RC bytes, retesting functionality, or changing the local runtime.
Do not reuse consumed RC6. Changed package source must use RC7.
Retained 6.9.2 RC6 does not supersede the approved 6.9.2 RC5 selection. Preserve
all historical packages,
receipts and tags, including the rejected unpublished stable-tag conflict.

Record explicit publication approval in `[cycles."<target>".approval]`, binding
`decision = "approved"`, the RC `attempt`, full source `commit`, `wheel_sha256`,
and `decided_on = "YYYY-MM-DD"`. Developer acceptance of the exact deployed RC
authorizes publication without rebuilding RC bytes or functional retesting.
Prepare stable version metadata once with the export/privacy audit and build,
then finalize and publish retained files. The publishing workflow audits the
tagged source without rebuilding artifacts or running functional tests.
No stable acceptance receipt, runtime activation, provider probe, or CI test
matrix is required. Later runtime changes require a new RC and user feedback.
See [release-requirements.md](docs/release-requirements.md) for this contract.
Do not prepare or publish an already published version again; report GitHub,
PyPI and package-proxy availability separately. See
[development-release-process.md](docs/development-release-process.md) for the
business requirements, industry evidence and complete lifecycle, and
[.agents/release.md](.agents/release.md) for attempt commands.

Keep these instructions, `.agents/release-report.md`, and
`tools/release-report.py` aligned. When branch-routing or release-cycle guidance
changes, update the report policy and generated wording in the same change so
a new agent receives the same answer from either entry point.

## Rules

- **Use `uv`, never plain `python3`.** The package requires Python
  3.12+; scripts with PEP 723 headers run via `uv run --script`.
- **Keep the tree export-clean.** Everything here ships to PyPI. No
  personal information, secrets, or machine-specific paths - the
  pre-release audit enforces this, but don't rely on it to catch you.
  Machine names (hostnames) are PII under this rule, and the rule
  extends beyond the tree: they must not appear in GitHub issues, PR
  bodies or comments, or commit messages either. Refer to hosts
  generically (e.g. "a WSL deployment host", "the owning host").
- **Tests must stay portable.** The smoke and seam suites run against
  temp projects only; never couple it to this checkout's `Agents/`
  directory or any specific host.
- **Keep README and skill docs in sync.** The README mirrors
  [overview.md](src/agents_live/skill/docs/overview.md); a change to
  one usually implies a change to the other.
- **`Agents/` is runtime, not source.** Handlers and logs there
  support local use of the tool; package behavior lives under
  `src/agents_live/`.
- **Work items live in GitHub issues; only themes live in
  `docs/backlog.md`.** Check `gh issue list` before starting work. File
  an issue for work that outlives the current change: something
  blocked, deferred, or handed back to the developer needs a home that
  survives the session. Do not file one for work you are about to do,
  or for a finding you fix in the same pull request; the commit and the
  pull request are its record. Reference an existing issue from a
  commit (`Fixes #N` closes on merge). `docs/backlog.md` records
  direction and links to those issues; it never restates their detail.
- **Use the repository owner's GitHub account for GitHub writes.** An
  enterprise-managed account may be active by default and is refused (403) when
  it creates issues, PRs or comments here, including through agent issue tools.
  Before a `gh` write, switch to the account that owns `origin`. `GH_TOKEN` and
  `GITHUB_TOKEN` environment variables silently override the switch, and the
  switch may not persist to later agent shell processes, so clear both tokens,
  switch and verify in the same process as every write. In PowerShell:
  `$env:GH_TOKEN = $null; $env:GITHUB_TOKEN = $null; gh auth switch --hostname github.com --user <origin-owner>; gh api user --jq .login; gh ...`.
  Read-only queries may work under either account. When the app's own PR or
  issue tool returns the enterprise-account 403, any authority to file that
  issue or open that PR (a developer request, or these rules for deferred work)
  also authorizes `gh issue create` or `gh pr create` under `<origin-owner>`
  this way; do not stop to ask again.
  This adds no merge, push or release authority.
- **Treat GitHub issue dates as UTC.** For a rolling recent-issue review, use
  `updated:>=YYYY-MM-DD` or an exact timestamp and omit a local-calendar upper
  bound. A local late-evening issue may already be dated tomorrow by GitHub;
  `updated:<local-today>` silently excludes it.
- **Never hand-parse runtime logs.** Use `agents-live logs` and
  `agents-live logs timeline` - they correlate events across log
  files and agent transcripts. Reading `Agents/logs/*.log` directly
  has repeatedly led to wrong conclusions.
- **A dashboard command is a foreground server, not a one-shot check.** Start
  it in a persistent/async terminal, prove readiness through `/api/agents` or
  the packaged dashboard-readiness gate, and do not wait for the server process
  to exit. `dashboard list` reports managed dashboards only; an independently
  started foreground dashboard can be healthy without appearing there. Outside
  local installation work, stop only a dashboard this task deliberately started.
- **Restore dashboards across local installs and activation.** Local install,
  upgrade, and version activation authorize temporarily stopping pre-existing
  Agents Live dashboards without asking again. Record each running dashboard's
  repository, port, and modes before stopping it through the public CLI; verify
  it has exited before replacement. After activation and any RC diagnostics,
  restart only the dashboards that were previously running through the selected
  runtime, preserving their settings, and verify `/api/agents`. On failure,
  restore them through the recovered runtime and report any restoration failure.
  Identify independently started foreground dashboards explicitly; never stop an
  unrelated process merely because it holds a port. See `.agents/testing.md`.
- **Never `git checkout`, `git reset`, or `git stash` tracked
  files.** Other agents run concurrently in this checkout and may
  have uncommitted work; re-edit the file instead.
- **Isolate branch work when the primary checkout is occupied.** Use the
  primary checkout only when it is clean and already on the intended target
  branch. Otherwise, create a dedicated worktree from that target. Verify the
  target ancestry before committing or pushing, and always remove the worktree
  when the task is complete.
- **Keep every commit meaningful and reviewable.** Plans belong in the
  session, issue, or PR description, never in empty or planning-only
  commits. Before the first push, fold superseded fixes and documentation
  into the commit they correct while preserving intentional implementation,
  changelog, and release boundaries. Do not rewrite a shared branch without
  explicit developer approval, and never rewrite `main` or released tags.
- **Do not merge `origin/main` into a feature branch only to synchronize it.**
  Start work from current `origin/main`. Rebase a local, unshared branch
  before review when it falls behind; after sharing, ask before choosing a
  history-rewriting update. Incidental synchronization merges obscure the PR
  boundary and become permanent under merge-commit workflows.
- **No backward-compatibility shims.** Clean break, migrate all
  consumers; ask the developer before adding any compat code.
- **Keep agent memory to pointers.** Canonical facts live in the
  repo and GitHub issues; a memory entry holds only a pointer to
  that home, never the content itself. The one exception is
  machine-specific facts (personal paths, hostnames, deployment
  details): the export-clean rule keeps those out of the repo and
  its issues, so local memory is their designated home.
- No em dashes; no emojis or icons unless the developer asks.

## Structure

- `src/agents_live/` - package: CLI, runtime modules, and the vendored
  skill payload (`skill/` with SKILL.md, docs, starter templates)
- `tests/` - export-safe smoke suite
- `tools/` - release tooling (audit and guarded publish workflow)
- `docs/` - repository design documents and the high-level backlog (not
  shipped with the skill)
- `Agents/` - local triggered-agent runtime dir (handlers, logs)
- `.agents/` - agent-facing guides (this file's targets)
- `.github/workflows/` - CI: publish to PyPI on GitHub release

<!-- GLP consolidation: 2026-11-26T16:36:22Z; base=795d69753648eb72bde09c6af2f237668ba4d885 -->
