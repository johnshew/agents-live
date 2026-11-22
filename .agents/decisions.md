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
