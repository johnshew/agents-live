---
title: Subagent Model Preferences
description: Preferred development and review models and availability disclosure
---

# Subagent Model Preferences

For routine substantive development, prefer GPT-6 Luna with medium through
xhigh reasoning effort.

For judgment-heavy work, use GPT-6 Astra to develop the result, then have Opus
5.5 review and improve it after Astra finishes. Keep these stages sequential.
Small trivial edits and short reads may stay with the supervisor under CDL.

Model and effort selection depend on the available subagent interface. If a
preferred model is unavailable, tell the developer what was used instead or
that the Opus 5.5 review was skipped; never substitute silently. If the interface
does not expose effort controls, disclose that and treat effort as guidance.

<!-- glp-update:v1 id=71591bc6-7a43-4e8b-a0d7-9016c5f8ab77 -->
<a id="glp-71591bc6-7a43-4e8b-a0d7-9016c5f8ab77"></a>
### GLP Update: Latest Luna and Sol for delegation, with cross-model review

- Update-ID: 71591bc6-7a43-4e8b-a0d7-9016c5f8ab77
- Recorded-UTC: 2026-10-08T14:21:29Z
- Kind: supersession
- Topics: subagent models, delegation, code review
- Workstream: 6.9.4rc5 (#565) orchestration
- Target: .agents/subagent-models.md, whole document; compared origin/main dccdc4b
- Source-Session: CDL session 2026-10-08 (local log only)
- Evidence-Basis: user_direction
- Application: applied
- Consolidation: pending

#### Change
The supervisor orchestrates. It delegates most work to the latest GPT Luna
model, or the latest GPT Sol model when the work is complex. Code written by
one of these models is reviewed by the other strong model family: Sol work is
reviewed by the latest Claude Opus, and Opus work by the latest Sol. As of this
update those are GPT-6 Luna, GPT-6.1 Sol and Claude Opus 5.5.

#### Evidence
- User direction, 2026-10-08: delegate most work to the latest Luna or Sol
  model according to complexity, and have the latest Sol and Opus models
  review each other's code.

#### Previous Knowledge
The text above prefers GPT-6 Luna for routine work and GPT-6 Astra followed by
an Opus 5.5 review for judgment-heavy work. This update replaces Astra with the
latest Sol and makes the review cross-model in both directions. The disclosure
rule for unavailable models is unchanged.

#### Verification and Limits
Applied in this session: Luna scoped the design, and Sol triaged the issues
and implemented #565. The Opus review is pending at the time of recording.

#### Follow-up
Fold into the model table at the next consolidation of this document.
<!-- /glp-update:v1 id=71591bc6-7a43-4e8b-a0d7-9016c5f8ab77 -->
