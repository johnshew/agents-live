# Decision prompts

How to present a decision to the developer. This adds no commit, push, PR,
merge, release, deployment or runtime authority; those still follow
[AGENTS.md](../AGENTS.md) and the release guides.

- **Ask in chat, not a dialog.** Ask in the reply as plain text. Question
  dialogs such as `ask_user` hold the turn open: a later message becomes the
  dialog's answer instead of being processed as steering, and queued messages
  and notifications wait.
- **Never block on a decision.** After asking, continue every piece of work
  that doesn't depend on the answer. Park only the dependent work, with a
  named blocker, and end the turn when nothing independent remains.

## Format

Every decision prompt stands alone:

- **Context.** Explain the choice and its stakes in plain language: what
  problem it addresses, what changes, and when. Put issue numbers and internal
  names second, never alone.
- **Recommendation first.** Lead with the recommended option and mark it.
- **Options.** Give the top one or two alternatives. For each, give the main
  benefit, the main cost or risk, and a one-sentence reason.
- **Numbering.** Number the decisions and letter their options (`1A`, `1B`,
  `2A`) so the developer can answer briefly, for example "1A, 2B". Keep the
  numbers and letters stable while a decision is open. Numbers start at 1
  each day.
- Never send bare yes/no questions or unexplained option labels.
- Group related decisions in one prompt. Do not mix unrelated topics.

## While the developer is away

When the developer says they will be away, for example overnight, keep the
authorized work moving so most of it is done on their return.

- **Decide the safe calls yourself.** Make good, low-risk, reversible choices
  with a best answer, and record each one with its reason so it can be
  reviewed and undone.
- **Queue the real choices.** Write each decision that is not low-risk or not
  reversible as a decision prompt in the format above, park only the work that
  depends on it, and carry on with everything else.
- **Route around blocks.** Look for another path, ask the other coordinators,
  and switch to other authorized work rather than waiting.
- **Look things up instead of asking.** Do not queue questions whose answers
  are in the repository, issues, notes or earlier records.
- **Write it down at once.** Record the away instruction and every call made
  under it in the session record or handoff as it happens, so both survive a
  restart.
- **Limits still hold.** Being away grants no new authority: no merge,
  release, deployment or other irreversible step without existing approval.
- **Report on return.** Lead with what was finished, then the calls made on
  the developer's behalf, then the open decisions.

<!-- glp-update:v1 id=07bf9437-fcd3-454a-8fcc-9f04efa500ff -->
<a id="glp-07bf9437-fcd3-454a-8fcc-9f04efa500ff"></a>
### GLP Update: Keep working while the developer is away

- Update-ID: 07bf9437-fcd3-454a-8fcc-9f04efa500ff
- Recorded-UTC: 2026-11-24T09:56:09Z
- Kind: addition
- Topics: developer absence, decisions, progress when blocked, records
- Workstream: developer-away guidance
- Target: .agents/decisions.md, While the developer is away; compared origin/main 6a9a261
- Source-Session: Copilot app worker session 2026-11-24 (harness history)
- Evidence-Basis: mixed
- Application: applied
- Consolidation: pending

#### Change
When the developer is away, agents keep authorized work moving, make and
record low-risk reversible calls themselves, queue the remaining choices as
decision prompts, route around blocks, look up answers instead of asking, and
record the instruction and each call at once. No new authority is granted.

#### Evidence
- User direction, 2026-11-24: the developer chose to add guidance for working
  while they are away to this guide. Standing developer preferences: do not
  block on questions, fill in best answers and list open items at the end, and
  do not ask for facts that can be looked up.
- Observation, 2026-11-23 and 2026-11-24: an away-for-the-night instruction
  reached only one of several coordinators, and it survived a context reset
  only because that coordinator had already written it down; another
  coordinator lost it on restart. The rule is written here in our own words,
  generalized from a record in another repository the developer maintains; no
  text specific to that repository was copied.

#### Previous Knowledge
This guide covered how to present a decision and not blocking on it, but not
what to do with decisions and blocks while the developer is unavailable for
hours.

#### Verification and Limits
Documentation only. Not yet exercised in this repository.

#### Follow-up
Fold into the guide body at the next consolidation of this document.
<!-- /glp-update:v1 id=07bf9437-fcd3-454a-8fcc-9f04efa500ff -->
