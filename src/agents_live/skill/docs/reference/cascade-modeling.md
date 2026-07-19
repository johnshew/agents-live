# Modeling Cascading File Modifications

When multiple file-watch tasks write into each other's watched directories,
changes propagate in chains that can loop indefinitely. This document
describes the methodology for proving those chains terminate and for
building the guards that enforce termination.

**Worked example:** The exercise subsystem. Multiple watchers
(`exercise-sync`, `taskflow-orchestrator`, etc.) write into each other's watched
directories; guard layers (content-hash, cooldown, idempotent writes)
ensure chains terminate.

## When to build a cascade model

Build a model any time two or more watchers can trigger each other:

- Watcher A writes file F; watcher B watches directory containing F.
- Both watchers write to the same file (e.g. Tracking.md).
- A pipeline writes output that a sync task reads and propagates back.

## Methodology

### 1. Enumerate actors, files, and user stories

Start with user stories - the behaviors the system must provide - not
implementation details. Stories create the circular dependencies; the model
proves those circles terminate.

| What to list | Example |
|--|--|
| **Actors**: watchers, cron tasks, the user | exercise-state-update, exercise-sync-checkboxes, user |
| **Files**: everything read or written by any actor | Tracking.md, Recommendations.md, Baseline.md, hash caches |
| **User stories**: each desired behavior | "Check in R → T updates", "Content edit → pipeline runs" |

### 2. Identify directional flows

For each user story, trace the full chain from trigger to quiescence:

```
User edits T
  → watcher fires on T
  → pipeline writes R
  → second watcher fires on R
  → sync writes T
  → first watcher fires on T again
  → ... must stop here
```

Name each flow (A, B, C, ...) and write it as an indented trace showing
every inotify event, guard check, and file write.

### 3. Add guards at every re-trigger point

Each point where a chain could loop needs a guard - a check that causes the
re-trigger to be skipped. Guards are layered (defense in depth):

| Layer | Guard type | Mechanism | Where |
|-------|-----------|-----------|-------|
| G1 | Dispatcher hash | SHA-256 per-file; skip if unchanged within cascade window | activate.py |
| G2 | Pre-proc mtime | Skip if source mtime ≤ output mtime | Task pre-processor |
| G3 | Pre-proc content hash | Skip if SHA-256(source) == cached hash | Task pre-processor |
| G4 | No-write | Skip write if new content == original content | Sync script |
| G5 | Hash cache update | After writing a file, update the upstream hash cache | Sync script |
| G6 | Deterministic enforcement | Post-proc restores state from a cache written by pre-proc | Task post-processor |

Not every guard is needed in every system. The minimum requirement: **at least
one guard must fire on every re-trigger path.** The model proves this by
showing that every flow terminates within a bounded number of dispatch cycles.

### 4. Prove termination

For each flow, show the maximum dispatch depth. A healthy system terminates
in ≤ 2 cycles (1 real dispatch + 1 skipped re-trigger). Annotate each
guard that fires:

```
Flow A: User edits T → pipeline RUN → sync no-write (G4) → DONE
Flow B: User toggles T checkbox → pipeline RUN → G6 enforces → sync no-write (G4) → DONE
Flow C: User toggles R checkbox → sync WRITE T + G5 → pipeline SKIP (G3) → DONE
Flow D: User edits R text → sync no-write (G4) + pipeline SKIP (G2) → DONE
```

If any flow doesn't terminate, add a guard.

### 5. Build a transition table

Compact summary showing which guards fire for each trigger:

| Trigger | G1 | G2 | G3 | Pipeline | Sync | Terminates |
|---------|----|----|----|----|------|------|
| Edit T content | pass | pass | pass | **RUN** | no-write (G4) | G2 or G3 |
| Toggle T checkbox | pass | pass | pass | **RUN** | no-write (G4) | G2 |
| Toggle R checkbox | n/a | n/a | n/a | skip (G3) | **WRITE** | G3 |
| Edit R text | n/a | n/a | n/a | skip (G2) | no-write (G4) | none needed |

## Key learnings

### Build the model before the code

The exercise cascade model was built after discovering a bug where the agent
overrode checkbox state (Flow B), causing sync to undo user completions.
The model made the bug obvious and showed exactly where G6 was needed.
**Lesson:** build the model first - verify the design terminates before
writing guards.

### Log every guard outcome

Every guard must log when it fires (skip) and when it passes (proceed).
Without this, cascade bugs are invisible - the system appears to work but
burns tokens on redundant runs, or silently drops user edits.

Guard log phrases to include:
- **Skip**: reason + key data (hash, mtime, cache hit)
- **Pass**: what triggered continuation
- **Enforcement**: what was corrected (G6 corrections logged as WARNING)
- **No-write**: "already matches" or "no changes"

### Shared logic must live in one place

When the same extraction logic appears in both the pre-processor (writing a
cache) and the post-processor (reading it), the two must agree exactly.
Duplication is the most likely source of silent guard failures. Extract
shared logic into a single helper.

### G6-style enforcement requires a pre-proc snapshot

The pattern: pre-processor captures deterministic state into a cache file
after the pipeline runs. Post-processor reads the cache and restores any
state the agent modified. This decouples the agent from state it shouldn't
control (e.g. checkbox marks come from Tracking.md, not from agent judgment).

The cache must be written **after** the pipeline but **before** the agent.
The post-processor reads it **after** the agent and **before** writing the
output file.

### Direct-script testing misses G1

<!-- NOTE: prep.py is illustrative - substitute your actual handler script -->
When testing flows by running scripts directly (`uv run --script prep.py`),
the dispatcher's G1 content-hash guard never fires - inotifywait isn't
involved. This is fine for verifying G2–G6, but a live-watcher test is
needed to validate the full chain including G1 and the debounce window.

### Unknown keys pass through G6 silently

`_restore_checkboxes()` only enforces keys present in the cache. If the
agent adds a new activity that wasn't in the pipeline output, no
enforcement happens. This is by design (the agent may legitimately add
items), but should log a debug message so missing cache entries are visible.

### Dead-code check after architectural changes

When changing the data flow (e.g. pipeline now writes both B and R
simultaneously), check whether earlier belt-and-suspenders guards become
unreachable. Dead guards aren't harmful but add confusion. Either remove
them or document why they're kept as defense-in-depth.

## Applying to other subsystems

The same methodology applies wherever multiple watchers share directories:

- **To Do**: `todo-push` and `todo-index` both watch `To Do/`. If
  `todo-push` writes `.md` files that `todo-index` then re-indexes,
  verify the chain terminates.
- **Flagged email**: `flagged-email-triage` and `flagged-email-sync`
  share the `To Do/Flagged Email/` path.
- **Any future system** with bidirectional sync between two files watched
  by different tasks.

For each: enumerate the actors, trace the flows, add guards, prove
termination.
