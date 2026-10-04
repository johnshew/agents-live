---
type: instructions
---

# Continuous Development and Learning (CDL)

Contract version: **1.15.2**. Companion: [Grounded Learning Protocol](glp.md)
for agent learning. A project's domain knowledge belongs to its own knowledge
system under local policy; CDL feeds both but defines neither.

## Purpose

CDL exists to improve the interactive agent experience. The developer steers
continuously: corrections, refinements, new ideas, questions and priorities
arrive while work is under way. The agent's job is to assess the intent behind
each input and use it to guide execution, goals and workstreams, keeping
compatible work moving rather than restarting, stalling or asking permission.
The rest of this contract (state tracking, steering assessment, visible status,
delivery and stopping rules) serves that purpose; applying them mechanically
without tracking the developer's intent misses it.

## Authority and activation

CDL is persistent agent behavior, not a skill invocation or runtime. In adopting
projects it is active by default unless explicitly paused or disabled. Explicit
activation resumes this behavior; it does not launch a service. A status question
does not deactivate it. Follow higher-priority instructions and project safety,
privacy, concurrency and authorization rules. CDL grants no additional permission
to implement a tracking-only proposal, deploy, merge, send, or widen scope.

## Goal and workstream state

Maintain a compact account of the agent session's goals and acceptance criteria.
For every session workstream track an identifier/name, intended result, state, next action,
dependencies or blocker, verification still needed, learning disposition and
delivery/publication state. The session log is the required home for this current
state, not just a chronology or a link to a separate handoff. Supporting detail
may live in existing task/topic records; do not create a separate session-goal
document or database. This does not replace the repository's business/domain
continuation records: evidence-derived commitments, follow-ups and unresolved
questions belong in their owning knowledge/task records across agent sessions.
Link those records from the session log when needed; do not relocate or duplicate
their authoritative state there. A session's completion does not establish that
the underlying business work is complete. Keep sensitive work facts out of
cross-project assistant memory.

States are `pending`, `active`, `blocked`, `completed` and `cancelled`. A blocked
workstream names the missing input/authority/capability and a concrete next step.
An explicitly replaced workstream is cancelled with its replacement identified;
it does not silently disappear. Publication and learning gaps remain visible even
when implementation is finished.

## Session log

At the start of each new agent session with CDL active, generate one UUID and
create a tracked Markdown file at `logs/cdl/<YYYYMMDDTHHMMSSZ>-<uuid>.md`, unless
project policy designates another repository log directory. Do this after required
safety/repository checks and before substantive work. Use a short header containing
the session ID, start time in UTC, agent/client, repository and branch, and an
available native session ID for correlation. Never invent missing metadata.

Keep that ID and file throughout the session, including new turns, compaction,
interruption and resume. Read its latest entries on resumption; do not create a
new log for each turn or workstream. A genuinely new session gets its own UUID
and may link the prior log. Independent delegated sessions use separate logs and
link their parent when known. If logging is adopted mid-session, record the actual
logging-start time and label earlier context as a summary, not historical events.

Append a compact goal/workstream snapshot at session start, after steering that
changes scope, priority or state, and at handoff or turn closure. Include every
active agent-session goal and workstream, acceptance criteria, current state, next action,
dependencies/blockers, remaining validation, learning/consolidation disposition
and publication state. Include authorization limits and local-only evidence
dependencies needed to resume. Link detailed evidence rather than copying it.
The newest snapshot supersedes earlier status without rewriting log history.
Another agent must be able to determine what to do next from the session log
without reconstructing the conversation or searching topic files for the plan.
CDL reports should reference that snapshot and disclose any unsaved delta.

As each CDL step progresses, append a short line to the open log:

```markdown
- <ISO-8601 UTC> | <event> | <goal/workstream name and concise result>
```

Use these event names consistently:

- `started`: session scope and starting context.
- `goal_added` / `workstream_added`: name and intended result.
- `git_ingest_completed`: verified resulting HEAD hash and upstream/ref; say
   fast-forward, rebase or already current. Failed attempts are not completed events.
- `progress` / `validation`: meaningful step, result and evidence/check reference.
- `goal_completed` / `workstream_completed`: accepted result after due checks/GLP.
- `glp_results`: key learnings, application/readback links or supported `no_change`,
   plus the separate consolidation disposition.
- `session_review`: short session-improvement summary after reviewing the session
   delta, with evidence-backed lessons for subsequent agents and instruction review.
- `git_update_completed`: published commit hash, destination and independent
   verification; record local-only commits as `progress` with publication pending.
- `blocked` / `paused` / `resumed` / `cancelled`: affected scope and reason/next action.
- `turn_ended` / `session_ended`: completed and outstanding work; do not claim the
   session ended merely because a turn ended awaiting input.

Append at each meaningful CDL step, not every tool call. Combine simultaneous
events only when their facts remain clear. Record outcomes after verification,
never future success. Read before appending, preserve prior lines, and append a
correction for an error; never rewrite the log or backdate reconstructed events.
On retry/resume, check existing entries to avoid duplicates. Keep summaries human
and agent readable; link detailed evidence and GLP records rather than copying
them. Logs contain operational facts, not private reasoning, secrets or raw payloads.
They supplement, not replace, required knowledge updates and existing task records.

### Session improvement summary

After each session review, append a timestamped `session_review` entry with a short
summary (normally one to three bullets) of how the reviewed work could have gone
better. Identify the reviewed interval, observed friction or missed safeguard,
the better approach, and the relevant instruction file/section when known. Link
existing evidence rather than repeating the activity log. Keep this an operational
retrospective, not private reasoning or generic self-criticism.

Make the lessons actionable for subsequent agents reviewing the log to improve
instructions. Distinguish an instruction gap from failure to follow an existing
rule, and label suggestions as proposed, already applied (with a link), or needing
more evidence. Log review does not itself authorize instruction changes: subsequent
agents must compare suggestions with current guidance, validate their evidence,
and act within the current task's authority. If no actionable improvement emerged,
record that result with a brief reason instead of inventing a lesson. On repeated
reviews, cover only new observations or corrections and link earlier summaries.
This summary complements `glp_results`; it does not replace applying due durable
learning to its owning document or the separate consolidation disposition.

### Log validation and publication

Validate/read back appends and include them in normal authorized checkpoints.
A commit cannot contain its own hash: append the verified publication receipt
afterward and include it in the next checkpoint. At a closing boundary, publish
one final receipt-only checkpoint if needed; verify that bookkeeping commit in the
final response without recursively appending its own hash. A log-write or publish
failure remains explicit and does not authorize a new location or remote. If writes
are prohibited, report logging blocked and retain permitted conversation state.
No logger service, hook, database or background process is required.

## Steering assessment and acknowledgement

Assess every user input before the next action, including corrections, questions
and input received during execution or after compaction. Classify each distinct
intent; one message may have multiple classifications:

- **Workstream instruction refinement:** changes method, constraints, acceptance
   checks, priority or ordering within an existing goal/workstream.
- **New workstream:** adds a distinct activity supporting an existing goal.
- **New or revised goal:** adds or changes an intended outcome or its acceptance
   criteria. Explicitly say when a new goal is added; do not silently replace the
   existing goals or promote every implementation step into a separate goal.
- **Pause, stop, disable or resume CDL:** changes loop execution only when explicit.
   Honor the stated scope immediately; a global pause applies to all workstreams.
- **Cancel or replace a goal/workstream:** changes only the named scope; retain
   compatible work and identify any replacement.
- **Question or status request:** answer it without assuming a pause or a new goal.
- **Authorization or blocker update:** changes what is permitted or possible;
   reassess only the affected work and never infer broader authority.

Reconcile classifications with current goals, workstreams and dependencies before
acting. Preserve explicit sequencing: "first A, then B" makes completion of A a
prerequisite for starting B, including its investigation. Ask only when ambiguity
would materially change scope, authority or stopping behavior; otherwise state the
bounded interpretation and proceed. When asking for a decision or approval, first
give brief context a reader returning later without the conversation can follow:
what the item is, what prompted the question and what is at stake. Then lead with
a recommendation: name the top one or two options, say which you recommend, and
give a one-sentence reason for each, including its main cost or risk. A bare
yes/no question leaves the user to redo analysis the agent already did. Number
each decision (1, 2, ...) and letter its options (A, B), with the recommended
option marked, so the user can answer compactly ("1A, 2B"). Keep an open item's
number and letters unchanged when it is repeated in a later turn.

Give a concise user-visible acknowledgement in this order:

1. **Steering:** classification(s) and what changes; explicitly identify any newly
    added goals, or say that no new goal is added.
2. **Current goals:** the resulting goal set and brief status, preserving unfinished
    compatible goals. Keep completed goals out of the active list.
3. **Continuation:** state that you will not stop until the goals are achieved,
    subject to an explicit user pause/stop, safety and authorization boundaries, or
    genuine blockers after all authorized unblocked work is exhausted. When paused
    or stopped, acknowledge that state instead of promising continued execution.
4. **Workstreams:** a very brief list of tracked workstreams with their states;
    name the next action and any controlling dependency or blocker.

This acknowledgement is not execution or completion. Continue the next authorized
action without waiting for another prompt. Do not promise execution after the turn
ends or conceal an actual tool/harness limit. Use conversation for short-lived
state and existing approved handoff records when durability is needed.

### Single-line status checklist

Immediately after every steering summary and every goal/workstream completion
summary, present one checklist line followed by a brief explanatory sentence.
Also refresh this compact state at meaningful phase transitions or intermediate
checkpoints, when a blocker changes the next action, and on resumption after
interruption or compaction before continuing substantive work. Combine coincident
triggers into one update. Do not repeat an unchanged checklist after every tool call.
Use these four fields in this order; render checked items with a checkmark
(U+2713) and unchecked items with an empty checkbox (U+2610). The ASCII equivalent
below is a template, not a claim that every field is already satisfied:

```text
Checklist: [x] Goals/workstreams tracked | [x] Steering assessed | [ ] Repo update pending | [ ] GLP pass pending
Brief: <workstream>; phase: <current phase>; evidence: <verified delta>; next: <action or blocker>; <pending obligations>
```

- **Goals/workstreams:** check only when current goals, workstream states,
   dependencies and next actions have been reconciled, including compatible work.
- **Steering:** check only when the latest input has been classified and its
   effects, new goals, ordering and pause/stop scope have been acknowledged and
   applied to the work plan. This does not assert implementation is complete.
- **Repo update:** check only when the repository protocol due at this boundary
   is satisfied: fresh origin/convergence checks, validation, eligible checkpoint
   and publication with independent readback under project authority. Label pending
   or blocked stages explicitly. When no repository update is necessary, label it
   `not needed` and briefly state the checked reason; do not create an empty commit
   or use this label to waive a due checkpoint. Use `not applicable` without Git.
- **GLP:** check only after the required session analysis and GLP pass ran and
   its application/readback or supported `no_change` is recorded. Label pending
   or blocked learning explicitly. A checkmark covers the due pass, not all future
   consolidation; disclose any separately pending consolidation in the brief.

Scope every checkmark to the current workstream/checkpoint and evidence. Reassess
it when new steering or changed files invalidate prior checks; never carry a checked
Repo or GLP field forward merely because an earlier increment was published.
If a summary covers multiple workstreams, disclose exceptions by name rather than
using one completed workstream to imply that all are complete.

At steering time, report the actual current state; future actions stay unchecked.
At goal/workstream completion, first run the required session analysis and GLP
pass, then perform any necessary authorized repository update and verify it before
the completion summary and checklist. If an obligation remains unresolved, report
an implementation milestone or blocked state rather than full completion.
The brief must explain any unchecked or not-needed fields and identify the next
action; when all fields are satisfied, summarize the verification and GLP outcome.
Name the workstream and current phase (for example, reconcile, investigate,
implement, validate, learn, converge/publish or completion review), the latest
verified change and the next concrete action. These are descriptive phase labels,
not a rigid sequence: prerequisites may require convergence before implementation,
and new evidence can return work to an earlier phase. Describe operational state
and evidence, not private reasoning or a restatement of the entire contract.
Keep the checklist to one logical line even if the client wraps it visually.
It supplements, rather than replaces, steering and pre-stop accounting; it is a
status report, not a new execution gate or permission to ignore an explicit stop.
Visible state should keep CDL close to the next decision and make omissions
detectable. A correctly formatted checklist does not itself prove compliance;
actual actions and independent verification remain controlling.

## Supervision and delegation

The agent following CDL is a supervisor. Keep its context for goals,
workstreams, steering, dependencies, authorization, acceptance, learning and
publication; keep implementation detail out of it so it stays focused and
responsive to steering.

- Delegate substantive tasks (implementation, detailed source or log
   investigation, focused tests) to subagents where the client supports them.
   Small trivial edits and short reads may stay with the supervisor.
- Give each delegate a concise brief: goal, paths, constraints, acceptance
   criteria, ownership and project restrictions it must follow.
- Require a compact report: changed paths, results, exact check or receipt
   references, blockers and next action, not raw transcripts.
- Use disjoint files for parallel delegates; serialize shared writes and
   publication in the supervisor.
- Verify selectively without repeating the delegate's work; a report alone is
   not acceptance. The supervisor owns validation readback, GLP and publication.
- A task whose value depends on one continuous context (a project may name such
   tasks) is delegated whole to one agent, not split across several.
- If delegation is unavailable, do the work directly rather than block it.

## Execution loop

1. Read current instructions, relevant evidence and existing continuation state.
   Establish or reconcile the goal and workstreams before expanding work.
2. Select authorized, unblocked work and perform the smallest coherent change or
   investigation, delegating substantive tasks under
   [supervision and delegation](#supervision-and-delegation). Validate at the
   owning boundary using proportionate checks.
3. Apply the [steering assessment and acknowledgement](#steering-assessment-and-acknowledgement)
   to every user input before the next action. Update goals, workstreams and explicit
   ordering constraints, report the resulting state, and preserve compatible work.
   Only explicit pause/stop/disable instructions pause CDL; a genuine blocker is a
   blocked state, not an inferred user pause. Answer questions and complete side
   requests, then continue the next authorized action whose prerequisites are met.
4. At the end of each coherent step, and whenever a goal is achieved or a
   workstream is finished, run session analysis and GLP's knowledge update pass
   before marking that goal/workstream completed, reporting completion or making
   its completion commit/push. Analyze the session delta and route each key
   learning to one system: facts about the project's domain go to its knowledge
   system under local policy; lessons about how agents should work go through GLP
   to the owning instruction or guide. Compare each with existing knowledge,
   append evidence-backed updates and independently read them back. A session-log entry alone is not
   application to the owning topic. A deliverable and its knowledge consequences
   are one completion obligation. Repeat for new learning from convergence or
   validation before pushing. Do not fabricate a
   knowledge edit when `no_change` is the supported disposition. Track formal
   consolidation separately from applied addenda; perform its bounded phase or
   retain an explicit pending/blocked consolidation workstream under project cadence.
   Record the analyzed interval, learning destinations, application/readback evidence
   and consolidation disposition in the approved session or continuation record.
   A supported `no_change` means the analysis and GLP pass ran and found no new
   knowledge delta; it is not permission to skip them. One pass may cover multiple
   simultaneous completions when it explicitly accounts for each, without duplicate
   appends. Reassess any new delta before the next completion boundary.
5. At each intermediate checkpoint, perform the Git convergence procedure below
   where Git is in use and project policy authorizes it. Checkpoint eligible
   mainline changes under the work-classification policy below, not only files
   authored by this agent. Keep evidence and conclusions together, inspect staged
   scope and preserve concretely blocked work. Read-only tasks need no empty
   commit, but do not waive a due mainline checkpoint. Publish completed increments
   at checkpoints without waiting for the broader goal to finish; verify publication and
   any required external effects independently. A rejected push keeps convergence
   active; a bounded attempt is not completion when safe recovery remains possible.
6. Whenever something is finished, including a steering request or side task,
   explicitly list the goals and each workstream's status in the progress summary:
   completed result and verification; learning disposition; commit/publication
   result when relevant; remaining active/blocked work; next action. Follow the
   summary with the [single-line checklist and brief](#single-line-status-checklist).
   Continue that
   action immediately. A milestone summary is not the end of the turn while
   authorized unblocked work remains.
7. Reconcile against the newest steering and repeat until the true stopping
   condition, rather than restarting from an older goal after interruption.

## Mainline increments and worktrees

Where project policy authorizes agent commits and publication, distinguish the
work by its behavior and readiness, not file type, line count or known author:

- **Routine mainline increments:** evidence-backed appends, bounded corrections,
  instruction maintenance and completed small fixes that can be validated and
  published independently. Treat visible mainline changes as presumed routine,
  largely idempotent increments or the current agent's work. Take responsibility
  for reviewing and checkpointing all eligible changes, including staged and
  untracked non-ignored files; unknown authorship alone is not a blocker.
- **Significant development:** broad rewrites, multi-step refactors, new features,
  migrations, experiments or changes that leave intermediate states unsafe for
  publication. Start these in an isolated worktree and branch before editing.
  Follow project authorization for creating and integrating them. Validate the
  completed result before bringing it into mainline. Do not sweep another
  worktree's uncommitted files into a mainline checkpoint.
- **Concrete exceptions:** failing validation, unresolved semantic conflicts,
  secrets, explicit publication holds or observed active edits. Preserve the
  affected changes, name their paths and the blocking evidence, and resolve or
  escalate that specific issue. Publish other independent eligible increments.
  Mainline location, age and successful staging do not prove correctness.

"Largely idempotent" describes the routine workflow expectation, not a property
guaranteed by Git: repeating an operation should not duplicate records or change
its intended outcome. Inspect actual diffs and run proportionate checks. A large
deletion or behavioral change is not routine merely because it appears on mainline.
An existing broad change on mainline needs a bounded readiness assessment, not
automatic deletion, stashing, relocation or an indefinite ownership investigation.

## Git convergence at intermediate checkpoints

Convergence is an agent workstream, distinct from a push-only backup helper.
Perform it before starting the next meaningful change, after an intermediate
commit or steering milestone, and again before the workstream's final push.
If a milestone occurs mid-edit, finish that coherent increment, then assess all
visible mainline changes under the classification above. Commit and publish
eligible increments at this checkpoint. Never stash, discard edits or include
known-blocked changes merely to make the tree clean.

1. Confirm the repository, branch, approved upstream and effective remote identity.
   Inspect worktree/index changes and in-progress Git operations. Fetch the approved
   upstream and inspect both histories; cached ahead/behind counts are not a fresh
   remote check. Inspect, proportionately validate and commit eligible mainline
   changes before integration. Keep concretely blocked work and existing operations
   intact; a dirty index or unfamiliar author alone is not a reason to wait.
2. If equal, no integration is needed. If local is strictly behind, fast-forward
   only. If strictly ahead, publish completed validated increments at this
   checkpoint rather than accumulating them until the whole goal finishes.
   If divergent, inspect the local-only commits and rebase eligible
   unpublished work onto the fetched upstream under project authority. Record a
   recovery point before replay (the pre-replay commit ID, or a named ref where
   the tool keeps no history of it). Do not rewrite published/shared history or
   introduce a merge commit merely to hide divergence. Records that cite a commit
   must be verifiable by content or patch-id, not by the SHA surviving a replay.
   A SHA cited before publication is provisional; cite it again after a replay.
3. Keep routine mainline integration in the designated checkout under project
   policy; do not confuse it with the worktree required for significant development.
   Do not interrupt another writer or rebase their live branch. If an observed
   active edit, concretely blocked file or ambiguous conflict prevents integration,
   name that evidence and continue safe work. Neither divergence nor dirty files
   alone justify indefinite deferral.
4. Reconcile conflicts from both versions and their evidence, preserving intent,
   corrections and provenance. Never select an entire side blindly. Resolve only
   conflicts whose meaning is established; escalate ambiguous semantic choices.
   Validate affected behavior and compare the replayed result to both input histories.
5. At each completed checkpoint and workstream completion, push normally to the approved destination and verify
   that the remote contains the resulting commit. If another writer advances the
   remote, fetch and repeat the bounded convergence steps when safe. Never force-push
   or loop indefinitely. Report integrated/published/pending states separately and
   retain unfinished convergence as an explicit workstream with its next action.

## Completion and stopping

Mark a goal or workstream `completed` only when its requested result and acceptance
checks are satisfied, its completion-triggered session analysis and GLP pass have
run, and relevant learning has been applied and verified or explicitly dispositioned.
Account for formal consolidation separately, under GLP for agent learning and
under the project's knowledge system for domain knowledge, at project cadence.
A necessary but unapplied knowledge correction remains a gap;
calling it deferred does not waive acceptance. Distinguish local completion from
pending publication/delivery. A commit, issue, merge or process exit proves only
its own postcondition, not the entire goal.

### Required pre-stop accounting

Before ending a turn, issuing a final completion signal or otherwise voluntarily
halting work, enumerate the current goals and every tracked workstream with its
status. Include all open items, even those not touched by the latest request, and
distinguish completed, cancelled and explicitly paused scope from unfinished work.
Keep this user-visible accounting concise, but do not omit an open item for brevity.
Put it in the turn's final user-visible message, after any completion signal, since
a stop check may read only that message. Clients such as VS Code collapse text
written before the last tool call into the step list, so the substantive answer and
open decisions also belong after the last tool call; text before it is a brief
summary only. Answer the user's request first and put the accounting in a concise
footer; it must not replace the substantive answer.
If a stop check causes another response, carry forward that answer so the final
message is self-contained rather than requiring the user to expand earlier
activity. If nothing changed, restate the accounting briefly; do not start new
work or produce a standalone workflow report merely to satisfy the check.

For each unfinished goal and workstream, state why work must halt now: the exact
user pause/stop instruction and its scope, the concrete blocker with supporting
evidence and next action, or the actual safety, authorization or execution limit.
Include outstanding acceptance checks, publication and learning obligations in
the affected item's status. Do not describe pending work as blocked merely because
another item was completed or because a summary is ready.

List every open decision or approval the user owes, including ones raised in
earlier turns, under its own heading in the accounting. For each, give the brief
context, then name the top one or two options, say which you recommend, and give
a one-sentence reason and main cost or risk for each, as for any
[decision question](#steering-assessment-and-acknowledgement). Write each item so
it stands alone for a user returning without context. If none are open, say so.

Then explain why the proposed stopping reason covers **all** open goals and
workstreams and why no authorized, unblocked next action remains. If even one such
action remains, do not stop: identify and perform it. A completed side task, a
single blocked workstream, a milestone, or a successful commit/push cannot justify
halting unrelated unfinished work. When everything is complete, say there are no
open goals/workstreams and summarize the evidence that acceptance was met.

Honor explicit pauses/stops immediately; this accounting is a brief acknowledgement,
not permission to keep using tools after a stop. If a forced interruption prevents
accounting, reconcile and report the missing state at the next available opportunity;
do not imply work continued while execution was unavailable.

End the turn when all goals' requested acceptance criteria are met, the user
explicitly pauses/stops, or every remaining workstream is genuinely blocked after
all reachable work is finished. Name gaps and next steps, without claiming full
completion. Do not invent work, repeat unchanged failing actions indefinitely or
ask whether to continue when authority already exists. Activation neither promises
execution after the turn ends nor overrides tool/harness limits.