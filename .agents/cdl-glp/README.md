---
title: Continuous Development and Learning Adoption
description: Portable contracts and local authority, evidence, and privacy boundaries
---

# Continuous Development and Learning

Adopted portable bundle **1.16.2**: [CDL](cdl.md) contract **1.15.2** and
[GLP](glp.md) contract **1.4.0**. Provenance: the authorized portable instruction
bundle, reviewed for this adoption on 2026-10-04. Only canonical contract text is
vendored; source-project learning appends, operational records, private policies
and optional hook documentation are not part of this adoption. No source-project
identity or location is needed to use these contracts.

The contracts are operating instructions, not skills, hooks, schedules or
background services. [AGENTS.md](../../AGENTS.md#continuous-development-and-learning)
explicitly activates them and owns project-specific policy. Existing
[CLAUDE.md](../../CLAUDE.md) routes Claude clients to that entry point; clients
that load AGENTS.md use it directly. Other harnesses must supply the same entry
point to their agents. No fresh-client behavioral acceptance is claimed by
document or export checks alone.

## Local Adaptation

The canonical contracts below are preserved without project-specific additions.
The following explicit overrides in AGENTS.md take precedence over their defaults:

- In the GitHub Copilot app or Copilot CLI, harness session history replaces a
  local `logs/cdl/` session log and per-update checklist. Still provide answer-first
  final accounting of goals/workstreams, outstanding checks, learning, publication
  and open decisions; include evidence in delegate handoffs; and run GLP with
  independent readback. Other harnesses retain the log and checklist rules below.
- For other harnesses, session logs live in gitignored `logs/cdl/` and remain
  local-only. Do not track, commit or publish them, including receipt-only
  checkpoints. Sanitize instruction and learning changes for publication instead
  of publishing operational records.
  The ignore rule is not retroactive: preserve pre-adoption records already
  tracked on main unchanged as historical evidence. New session logs stay untracked.
- For other harnesses, use ASCII checklist markers `[x]` and `[ ]`, not Unicode
  symbols, under the project's existing style rule.
- Describe each workstream by its purpose for the developer, what is ready or
  blocked, and the next action. Omit session IDs, SHAs and code names unless
  requested or needed for a technical handoff.
- Project authority and current user restrictions control all writes, delegation,
  validation and delivery. CDL does not authorize publishing concurrent edits or
  bypassing release gates. Routine prose does not add broad validation gates.

GLP appends go to the owning existing `.agents/` guide or root instructions.
Existing learning records remain evidence; adoption does not migrate or erase
them. Domain knowledge remains in project docs and issues. Formal consolidation
is separately scoped and accounted for, not an automatic rewrite or publication.