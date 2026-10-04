---
type: instructions
---

# Grounded Learning Protocol (GLP)

Contract version: **1.4.0**.

## Purpose

GLP is how agents learn how to work. Its subject is agent learning: methods,
instructions, tools and judgment. It has two distinct phases: append
evidence-backed learning records to the owning instruction or guide, then
deliberately consolidate each document's accumulated records into a concise
canonical version with current cross-document links. Preserve sources,
corrections and uncertainty throughout; do not invent facts or duplicate claims.

GLP does not own a project's domain knowledge (what is known about the work
itself). That belongs to the project's own knowledge system, with its own records
and consolidation. Such a system may reuse GLP's evidence, append and per-document
consolidation pattern, but its records stay separate. When one pass yields both
kinds, route domain facts to the project's knowledge system and method lessons
to GLP; each record goes to exactly one system.

GLP is standalone. It requires no other workflow contract, goal-tracking system
or orchestration framework. Existing evidence, privacy, authorization and
write-safety requirements remain authoritative.

An invoking workflow may choose when to run GLP, but all learning, update-format,
verification and consolidation requirements are defined here. A person or agent
can use it directly with an explicit task or evidence set and approved knowledge
documents. Inputs may be source batches, investigations, document changes or
session learnings; no interactive session is required. Git and automated pipelines
are optional; publication means delivery through the project's authorized mechanism,
if one is required.

<a id="end-of-step-session-learning-pass"></a>
## Knowledge update pass

Declare the source set or change interval for the pass and compare it with what
was previously accounted for. Run when new evidence is ready, on an explicit review
request or at the invoking workflow's chosen boundary. Complete the required updates
and readback before reporting that pass complete or publishing its results.

For interactive work, review the delta at the end of each coherent step, including
user corrections and side requests, rather than waiting for the whole session to
end. Consider decisions, investigations, failed hypotheses, validated results,
recurring friction and unresolved concerns. For batch or document-driven work,
review the declared evidence set without inventing a session history. Use actual
evidence, not a generic retrospective or invented lesson.

Identify the key durable learnings and compare each with its owning knowledge
document using the protocol below. Use the searchable update format below for new
learning appends unless an existing machine-backed format is required.
Append dated, evidence-backed updates there,
including explicit corrections to prior claims when needed. Keep a compact
account of each learning, destination, disposition and readback in the existing
session or pass record. A summary append alone does not update the owning topic.
Record a supported `no_change` when nothing new warrants an update, rather than
creating duplicate or empty knowledge entries.

Immediate knowledge updates are append-only: do not rewrite earlier narrative,
erase old evidence or fold addenda into canonical prose during this pass. New
topics are permitted when no existing document owns the learning; make them
discoverable using project conventions. Formal consolidation is a separate,
deliberate phase, not an implicit part of every append. Requested code, configuration
or instruction corrections may still require targeted edits; record their durable
learning as append-only knowledge rather than labelling those edits consolidation.

Validate the appends and independently read them back before publication. If
integration or final checks reveal further learning, account for that new delta
before delivery. Report applied, no-change, blocked and pending-consolidation states
honestly; do not declare a required learning update complete while it is unapplied.

## Protocol

1. **Acquire and identify evidence.** Capture the source identity, exact supporting
   region/body or reproducible test/result, version/hash where available, source
   date and observation date. Preserve the required source material in the approved
   store. Distinguish literal evidence, user direction, inference and hypotheses;
   a user assertion about external state still needs reconciliation.
2. **Compare with current knowledge.** Retrieve relevant topics and prior claims,
   including corrections and unresolved conflicts. Record the compared revision
   and region. Search results and model summaries are candidate context, not proof
   of relevance, completeness or absence. Read the underlying evidence when needed.
   When a finding conflicts with or qualifies a rule, search the whole instruction
   set for the rule's key phrases and list every restatement (AGENTS.md, CDL,
   topic guides) in Previous Knowledge, so no copy is left contradicting the change.
3. **Decide the semantic delta.** Classify the finding as an addition, confirmation,
   correction, supersession, new topic, unresolved conflict, `no_change`, or
   `deferred`. Preserve what is unknown. Duplicate source copies, repeated quoted
   text and unchanged claims are not new learning. Deferral names the missing
   evidence or authority and next action; it does not count as applied knowledge.
4. **Propose a bounded learning record.** Specify the changed claim, target topic
   and expected revision, supporting evidence, dates, uncertainty, and prior claim
   affected. Select the approved durable home; improve an existing topic before
   creating a redundant one. New topics need the project's discovery/routing links.
5. **Validate and apply.** Recheck evidence and destination state immediately before
   writing. Reject unsupported claims, unauthorized paths and stale revisions.
   Reconcile concurrent changes instead of overwriting them. Append an addendum or
   explicit correction with provenance; preserve prior evidence/history and make
   the current conclusion unambiguous. Keep conclusions and evidence in sync.
6. **Read back and account.** Independently verify the resulting knowledge, source
   references and routing. Record applied/no-change/deferred state with verification
   and commit/publication references as applicable. Retrying must not duplicate an
   already-applied operation. Record remaining learning gaps in the existing task
   or follow-up record, with a next action and any blocker; no task system is required.
7. **Consolidate knowledge.** Fold a document's accumulated appends into a new
   canonical version of that document using the formal phase below. Applied
   addenda are input to consolidation, not proof it has happened.

## Formal knowledge consolidation

Consolidation works **one document at a time** on agent learning (see
[Purpose](#purpose)). It turns a document and its
accumulated appended updates into a new concise, clear canonical version that
incorporates the learning and no longer carries the folded appends or their
historical narrative. That version is the new starting point: later updates
append to it. It is not concatenating addenda or generating a summary.

Evidence is not lost. Under version control, the revision before consolidation
retains every folded append with its evidence, and the consolidated document and
commit record that base revision and the folded IDs. Source evidence files
(change logs, dated records, cached bodies) are not deleted by consolidation.
Without version control, archive the folded appends in an approved location
before removing them.

At a task or evidence-review completion boundary, identify documents with pending
appends and either consolidate them, record a supported `no_change`, or retain an
explicit pending/blocked follow-up. A project's cadence may batch documents;
report provisional learning as applied but unconsolidated until its document is
consolidated. Do not label deferred consolidation complete.

For each document:

1. **Start from the current shared version.** Synchronize with the shared
   repository, confirm the document is not already being consolidated elsewhere
   (recent history or a project claim), and record the base revision.
2. **Inventory its pending input.** List every append in the document (update
   IDs, machine-backed blocks, dated appended lines) and project records that
   target it. Give each a disposition: folded, rejected with reason, retained as
   an unresolved conflict, or retargeted to another document.
3. **Rewrite for clarity.** Integrate supported findings into the canonical text.
   Resolve duplicates and contradictions by source and date, never by which edit
   is newer. State current claims plainly; keep a short source reference for each
   consequential claim; keep genuinely unresolved conflicts explicit with both dated
   values. Remove folded append blocks and superseded narrative; history lives in
   the base revision, not in the new document.
4. **Link horizontally.** Update cross-links, index entries and vocabulary that the
   change affects, at both ends where reciprocal links are required. Shared names
   alone are not evidence of a meaningful relation.
5. **Validate.** Check that every inventoried append has a disposition, that
   consequential claims remain supported, and that links and project structural
   invariants hold. Diagnostics alone do not prove semantic correctness.
6. **Record the marker.** Record in the document the consolidation time and base
   revision, and list the folded IDs in the commit message so `git log --grep <id>`
   finds where each was folded.
7. **Commit and publish before the next document.** Commit that document (and its
   reciprocal-link endpoints) alone, synchronize, publish and verify, so parallel
   appenders start from the new version. An append that arrived during the edit is
   kept verbatim after the new text and waits for the next consolidation.

Retrieval coverage and consolidation markers are separate; consolidation never
advances collection/processed-source coverage by inference.

### Source investigation during consolidation

Consolidating agents may initiate targeted investigations when contradictions,
stale claims, missing provenance or suspected divergence between canonical knowledge
and source material threaten correctness. They are not restricted to the supplied
summaries or frozen delta. Within existing read authority, investigate without
waiting for a separate request for every source lookup. Hypotheses about divergence
are reasons to check, not conclusions to record as facts.

- State the disputed claims, source question and a bounded scope; start with the
   relevant original records, code, tests or cached evidence. Follow necessary
   source/thread links and obtain fresher authoritative evidence when warranted,
   using project identity, retrieval, privacy and cost safeguards.
- Record investigative evidence with source/version/date, reason for retrieval,
   affected claims and result, in the project's approved evidence home. Do not move
   coverage markers or count a targeted lookup as a complete refresh.
- Reconcile the finding into canonical knowledge with exact evidence and dates.
   If sources do not settle it, preserve both claims and label the conflict unresolved.
   Rerun checks affected by the new evidence; retain unrelated completed checks.
- Bound effort by relevance and the current work budget. Broader investigations
   become explicit follow-up tasks and require additional authority when applicable.
   Inaccessible sources or an exhausted budget leave a visible unresolved concern,
   not an invented resolution; continue unrelated unblocked consolidation.

Use the project's existing consolidation procedure and records; GLP does not
impose a particular storage database, folder layout, schedule, topic vocabulary
or routine broad source refresh.

## Searchable update format

Use one self-contained Markdown block per durable learning, appended in the owning
knowledge document. The literal `<!-- glp-update:v1 id=` prefix is its discovery
marker; a stable HTML anchor makes it linkable. Fixed labels are case-sensitive.
The marker, anchor and `Update-ID` must contain the same lowercase UUID. Generate
the ID once, retain it on retries, and check for it before appending. Do not use a
timestamp alone as identity. The closing marker must match the opening ID.

```markdown
<!-- glp-update:v1 id=<uuid> -->
<a id="glp-<uuid>"></a>
### GLP Update: <short searchable title>

- Update-ID: <uuid>
- Recorded-UTC: <ISO 8601 UTC timestamp>
- Kind: <addition|confirmation|correction|supersession|new_topic|unresolved_conflict|no_change|deferred>
- Topics: <existing topic keys, or descriptive search terms if no vocabulary exists>
- Workstream: <task, investigation or workstream name; existing record link if available>
- Target: <owning document and section; compared commit/hash or explicit unknown>
- Source-Session: <source batch, review/pass, session/run record or dated conversation reference>
- Evidence-Basis: <observed|user_direction|inference|mixed>
- Application: <applied|proposed|blocked|no_change>
- Consolidation: pending

#### Change
<What was learned or changed, why it matters, and its scope. For no_change or
deferred, explain the disposition without inventing a new claim.>

#### Evidence
- <Source link/citation, exact supporting region or result, source date/version,
  and observation date. Preserve required full source material in its approved home.>

#### Previous Knowledge
<Compared claim and link/anchor/ID; state what is added, confirmed, corrected or
superseded. Use none with a reason when no earlier claim exists.>

#### Verification and Limits
<Checks and actual results, uncertainty, conflicting dated values and missing
evidence. Do not claim readback before performing it.>

#### Follow-up
<Next action and owner/workstream if known, or none with a reason.>
<!-- /glp-update:v1 id=<uuid> -->
```

Every label and section is required; use explicit `unknown` or `none` with a reason
instead of silently omitting information. Keep the claim concise but sufficient
to interpret without reopening the whole session. Link large evidence rather than
copying sensitive bodies, attachments or logs. Use relative repository links and
project citation conventions. This is a versioned Markdown format, not an installed
JSON Schema or a new automated validation gate.

`Workstream` is a context label, not a dependency on a workflow system. A plain
task or investigation name is sufficient for standalone use. `Source-Session`
retains its v1 name for compatibility; a source-batch or review record is sufficient
when there was no conversation or interactive session.

The owning document holds the canonical block. The session or pass record links to its
anchor using `GLP-Update: <uuid>` and records independent readback and publication
results when available; do not duplicate the full block there. Application records
what was actually done, not whether the claim is true. An appended proposal may
still be `proposed`; verified source support belongs in Evidence and Verification.
Keep the block immutable until consolidation folds it. Later correction or status
changes are new updates referencing the earlier ID; retrying the same append keeps
the original ID.

A block still present in its owning document is pending consolidation; a folded
block is absent from the current revision and findable through the consolidation
commit that lists its ID. Discover pending blocks by the marker prefix across the
approved knowledge documents (tracked files when Git is in use), excluding
templates, examples and scratch. Identical copies are one input; different content
with the same ID is an unresolved conflict, never last-writer-wins. A missing field,
invalid kind or unmatched marker is visible malformed input to repair or
disposition, not an excuse to skip it. Legacy prose appends are consolidated the
same way; adoption requires no retroactive migration. Existing pipeline formats
may map equivalent fields instead of changing their runtime payloads.

## Logical learning record

This is a semantic contract, not an installed JSON schema or a new mandatory file.
Use the searchable format for new learning appends, or the project's existing
machine-backed record/outbox format with equivalent information. A code-backed implementation must define and validate
its exact serialization separately.

| Field | Requirement |
| --- | --- |
| Identity | Stable learning-operation identity and owning workstream; enough to recognize retries. |
| Disposition | Semantic kind above and reason; `no_change` and `deferred` are explicit outcomes. |
| Claim | Proposed knowledge or exact correction; omit an invented claim for no-change/deferred outcomes. |
| Target | Existing or proposed topic, compared revision and affected region; prior claim reference for corrections/supersession. |
| Evidence | Source IDs/citations, supporting spans/results, source versions and retrieval/observation provenance. |
| Time and confidence | Source/effective date where known, observation date, uncertainty and unresolved conflicts. |
| Application | Proposed/applied/verified/deferred state, readback evidence, commit/publication result where relevant. |
| Consolidation | Pending/consolidated/no-change/blocked state, owning pass and input bounds, canonical destination and closure evidence. |

## Agent and deterministic responsibilities

In a pipeline, deterministic preparation assembles version-bound sources and
candidate topic excerpts. Cheap-model enrichment is an optional optimization;
when skipped or unavailable, the advanced reviewer can discover more topics and
retrieve more evidence itself. It retains that capability when enrichment succeeds.
Label model-derived summaries; reproducible assembly does not make their claims
deterministic facts.

The reviewer owns semantic interpretation and learning proposals. The deterministic
postprocessor owns schema/path/version/citation checks, idempotent application,
formatting and readback. It must not invent semantic content to repair an invalid
proposal. Route conflicts or missing evidence back for reconciliation.

For interactive agents without a postprocessor, the agent performs the same logical
validation, targeted application and independent readback using authorized tools.
Do not claim those operations are mechanically enforced. Adopting GLP does not
require building a pipeline and does not grant write authority.