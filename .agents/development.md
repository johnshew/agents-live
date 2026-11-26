---
title: Developing Agents Live
description: Build, test, and run agents-live from source without replacing the installed tool
---

How to build, test, and change the code in this repository.

## Layout

- `src/agents_live/` - the Python package. Canonical behavior is grouped
  under `agent/`, `runtime/`, `state/`, `obs/`, `pipeline/`, and `cli/`;
  `dispatch.py` is the handoff between runtime firings and agent execution.
  `legacy/` contains one-major-cycle 5.x migration and cleanup code only.
  The vendored skill payload remains under `skill/`.
- `tests/test_smoke.py` - export-safe smoke suite. Runs against temp
  projects only; never touches this checkout's `Agents/` directory.
- `tests/test_seams.py` - architecture, process-policy, migration, and
  correctness invariants over memory hosts and temporary repositories.
- `tools/pre-release-audit.py` - scans for personal information,
  secrets, and nonportable paths. Must pass before any release.
- `Agents/` - local runtime directory (handlers, logs) used when
  agents-live is exercised in this checkout; not package source.
- `docs/` - design documents and the high-level backlog for the project
  itself; not part of the installed skill payload.
  See [docs/README.md](../docs/README.md).

## Commands

Always go through `uv` - never plain `python3` (system interpreters are
often too old; the package requires Python 3.12+).

On Microsoft-managed native Windows or WSL hosts where the public Python
artifact endpoint fails TLS negotiation, configure the approved package proxy
for that runtime before running these commands. Follow
[diagnostics.md](../src/agents_live/skill/docs/diagnostics.md), including its
exact-version check; the proxy may lag the public release.

```bash
# run every portable smoke and seam test
uv run --with-editable . python -m unittest discover -s tests -v

# run the public framework smoketest
uv run --with-editable . agents-live smoketest

# run the CLI from source
uv run --with-editable . agents-live --help

# audit the tree for release-blocking content
uv run --script tools/pre-release-audit.py
```

The `dev` dependency group supplies test-only dependencies such as DuckDB to
all `uv run` commands in this checkout. Do not add per-command `--with` flags
for dependencies already declared there; doing so creates separate ephemeral
environments and lengthens the edit loop.

## Source checkout and installed tool

See [testing.md](testing.md) for the full source, wheel, and installed-tool
validation matrix.

Use the editable project environment when testing code in this repository:

```bash
uv run --with-editable . agents-live --repo ~/repos/<target-project> doctor
uv run --with-editable . agents-live --repo ~/repos/<target-project> dashboard --dev
```

These commands execute the current checkout without replacing the user-level
tool. From another repository, bare `agents-live` executes the self-managed
version selected by the stable `current` path:

```bash
agents-live --repo ~/repos/<target-project> doctor
agents-live --version
agents-live upgrade
```

`agents-live --version` reports the selected stable release or numbered RC.
Self-managed installation retains complete versions side by side under
`versions/`; `current` selects one. `agents-live upgrade` installs and selects
the latest stable generation, includes plugin wheels declared by registered
repositories before sealing it, and converges host integrations through the
selected version. Local RC deployment uses a new numbered package version for
each changed candidate, so successive candidates do not collide.

Use `agents-live --repo <project> upgrade` to upgrade the runtime and refresh
that project's installed skill payload. `agents-live --repo <project> doctor`
reports any package and skill payload version mismatch.

## Conventions

- Standalone scripts carry PEP 723 headers and run via
  `uv run --script <path>`.
- Docs under `src/agents_live/skill/docs/` carry frontmatter
  (`title`, `description`, `ms.date`, `ms.topic`); update `ms.date`
  when you materially change a doc.
- Keep `README.md` and the skill docs consistent - the README's
  feature claims and Documentation links mirror
  `src/agents_live/skill/docs/overview.md`.
- Keep diffs minimal and match the style of the surrounding code and docs.
- When a correction itself needs another fix, reassess and simplify the design
  before adding another workaround. Remove machinery made unnecessary by the
  simpler model, preserve required behavior and validation, and verify the
  observable result at the real boundary. Fewer paths alone do not prove
  correctness. This approach was demonstrated in [PR #631](https://github.com/johnshew/agents-live/pull/631).

## Subagent models and effort

Follow [subagent-models.md](subagent-models.md) when selecting development and
review models. Disclose unavailable model or effort controls rather than
silently substituting. Small trivial edits and short reads may stay with the
supervisor under CDL. For large coordinated work, use one reusable integration
tester per RC that returns the tested head, base, scope and result. One agent
owns live local runtime mutation and restoration during RC deployment; serialize
integration. Do not rerun a gate already passed on the same head and environment.

## Orchestrating delegates and child sessions

These rules refine CDL supervision for a supervisor that coordinates subagents
or app child sessions. They add no commit, merge, release or runtime authority.

- **Stay steerable.** Run long builds, test suites, preparation and live runs
  asynchronously and check them later; keep synchronous shell calls short so
  developer input is not blocked. While the developer is active, report and
  take steering before starting another batch. Run status checks with explicit
  repository paths so a delegate's working directory cannot mislead the report.
- **Attach one self-wake automation while coordinating active work.** In the
  GitHub Copilot app, attach a single same-session automation that wakes about
  every 10 minutes and clear it on explicit shutdown with a final handoff. Use
  this step list as its prompt template, in order:
  0. Preflight: before any `gh` write, clear `GH_TOKEN` and `GITHUB_TOKEN`, run
     `gh auth switch --hostname github.com --user <origin-owner>` in the same
     process, and verify `gh api user --jq .login`.
  1. Check: run `uv run --script tools/coordinator-check.py`, then follow the
     heartbeat procedure below for child state and new coordination-channel
     posts. When the check has no findings and no child or channel item needs
     attention, end the tick silently.
  2. Unblock: read session status; answer every owned child awaiting input or
     plan approval within existing authority and resume interrupted children.
     Investigate each `STALLED` worker. Escalate only new scope or authority,
     with options and a recommendation.
  3. Pull requests: for each PR the check reports ready, merge only a PR the
     developer authorized, one head at a time with the full-SHA guard, then
     read the result back; otherwise report it ready for review.
  4. Runtime health: for reported runtime errors, query detail with
     `agents-live logs --all --since 15m --errors`; investigate through a
     delegate and never change the live runtime without authority.
  5. Release state: when release-relevant state changed, run
     `uv run --script tools/release-report.py` and note changes in readiness,
     blockers or next action.
  6. Cleanup: archive finished owned children (see below), preserving needed
     artifacts first. Remove only clean worktrees the check reports as
     `HYGIENE`, without `--force`.
  7. Report: message the creator only when something changed or needs a
     decision, in plain language. Silence is fine on a no-change tick. The
     heartbeat reports but never acts or posts to the issue; change its
     behavior through the script.
- **A coordinator turn dispatches, records and ends.** Use the coordinator only
  for commands that finish in seconds, decisions and verification of delegate
  evidence; delegate tests, builds and implementation. Long work belongs in
  detached child sessions or detached processes, not in-session background
  subagents: an interrupted coordinator session cancels its in-session
  subagents and loses their unfinished work. End the turn promptly so queued
  developer messages arrive.
- **Track progress with the deterministic check.** Run
  `uv run --script tools/coordinator-check.py` (about 10 seconds, read-only
  apart from its gitignored state under `.reports/`). It prints only changes:
  new `origin/main` commits, new runtime errors and runtime version changes,
  and findings that appear, change band or clear: a worktree with uncommitted
  edits or an open PR but no activity for 20 minutes (`STALLED`), a merged clean
  worktree (`HYGIENE`), commits on no open PR (`UNPUBLISHED`), failing or
  conflicting PRs, PRs ready for a merge decision and a primary checkout that is
  off main, dirty or behind. Unchanged red or unknown findings repeat every two
  hours. Exit 0 means no change, 1 means something changed and 2 means a lookup
  failed. Treat every `STALLED` or `UNKNOWN` line as a prompt to investigate
  the owning worker now, not at the next report. Empty output is the evidence
  that nothing changed; do not re-check the same items by hand.
- **Start a background worker as a child session.** For any slice longer than
  a few minutes (implementation, a test run, RC preparation or an
  investigation):
  1. Create it with `create_session` and a clear title (for example
     `Fix: <topic>`). In the app, pass the repository as `workspace` with
     `worktree: true`; if that fails, start it without isolation and have its
     first step be `git worktree add <worktree> -b <branch> origin/main` under
     the repository's worktree folder. Post the returned chat link in your
     reply.
  2. Keep the prompt short (about 1,500 characters) and state the goal and
     acceptance check, quoting the developer's request; the exact worktree,
     branch and owned paths; the paths and runtime it must not touch; its
     authority (for example commit and push its branch, open a PR, but no
     merge); the ban on checkout, reset, restore and stash; and the expected
     return: changed paths, check results, blockers and next action. Every
     brief also says, in its own words, never to use `ask_user` or any question
     form and to send decisions to the coordinator with `send_message` while
     continuing other work: a pending form blocks the worker and the
     coordinator's steering messages from reaching it.
     Put longer standing detail in a gitignored file the prompt points to.
  3. Read the session back to confirm its model and working directory; a
     session that failed startup is recreated, not revived by message.
  4. Require work on the remote before the session ends: the branch pushed
     with a continuation note in its PR (a draft if unfinished). Local edits
     are invisible to a successor.
  5. Talk to it with `send_message` using the full chat link: `steer` for a
     correction or blocker mid-turn, `queue` for an idle worker. Read its state
     with `get_session_context` on each heartbeat instead of interrupting it.
  6. Keep slices under about 45 minutes; when one merges, start the next in a
     fresh session from current `origin/main` rather than reusing the old one.
  A long command with no reasoning needed (a build or gate run) can instead
  run as a detached hidden process through
  `uv run --script tools/run-hidden.py --detach --log <file> -- <command>`,
  with the log in a gitignored location. It prints the process ID and returns
  at once; on Windows neither the command nor anything it starts opens a
  window. Without `--detach` it waits and relays the output and exit code.
  Use in-session task subagents only for short checks whose verdict is all
  that matters.
- **Restart a hung coordinator; do not retry it.** If every tool call in a
  coordinator session hangs, including trivial reads, searches and session
  creation, the session itself is the fault. Stop retrying, record the state
  you can, and tell the developer to start a fresh coordinator session. The new
  session resumes from the prior session's record (its handoff, open PRs,
  worktrees and the check's state), not from a rebuilt plan.
- **When blocked, ask and keep moving.** A coordinator or worker that is
  blocked requests unblocking strategies through the cross-team coordination
  issue, with no private content. Meanwhile it switches to other authorized,
  unblocked work rather than waiting idle.
- **Run the heartbeat as its own worker.** Use a same-session automation where
  the harness offers one. In other harnesses, start a `Heartbeat` child session
  with the same check, issue and child-state responsibilities above, about
  every 10 minutes. Retire it when the goals are met.
- **Name the workspace in every brief.** State the exact worktree and tested
  head the delegate may read, edit or test, and the excluded locations (for
  example the primary checkout and the live runtime). Repeat the repository's
  ban on checkout, reset, restore and stash. Verify the delegate's actual
  working directory from its report; do not infer isolation from its name.
- **Never stall a child.** The supervisor owns its children's questions and plan
  approvals. Answer them within the developer's goals and recorded authority,
  give a next action, and escalate to the developer only a genuinely new
  authority or scope decision, framed with options and a recommendation.
- **Replace a child stuck on a question form.** A child waiting on a question
  form cannot receive steering. Do not wait for it. First preserve its handoff,
  local edits, unpushed commits and needed scratch. Stop its turn and start a
  replacement whose brief includes the decision and points to the preserved
  work. Keep the original child's worktree and branch until normal close-out
  conditions are met.
- **Children report only on outcomes.** Brief each child to message its
  supervisor only at a merge-ready result, a blocker or a decision it cannot
  make, never through a blocking dialog or question form, and to state its next
  action whenever it pauses. Use `send_message` for decisions so a pending form
  cannot block steering. Progress between those points is visible to the check.
- **Carry plan conditions into review.** When approving a child's plan with
  conditions, copy them verbatim into the reviewer's or tester's brief as
  pass/fail checks.
- **A pass belongs to one head.** A tester or reviewer leads its report with
  result, tested head and base, scope and evidence reference. A pass on an older
  head does not cover a newer one. When merges are authorized, merge one passing
  head at a time with the full SHA guard below and read back the PR state and
  merge commit before the next.
- **Close out finished children.** A child is finished when its worktree is
  clean, it has no unpushed commits, its PR is merged or closed and it owns no
  ongoing work or automation. Before teardown, preserve any ignored scratch a
  successor still needs in an approved durable home. Archive through the
  harness lifecycle (in the GitHub Copilot app, only the creating session can
  archive its child); never remove an app-managed worktree by hand. Remove
  app-created worktrees that share `main` as soon as the release step that
  created them finishes; advancing `main` otherwise leaves their working tree
  stale with phantom reverse-staged changes. On Windows, `git worktree remove`
  can finish Git teardown but fail to remove an empty folder held open by
  another process. Verify the registration is absent from `git worktree list`,
  verify the local branch is gone when deletion was intended, and verify the
  remaining folder is empty. If those checks pass, Git close-out is complete;
  remove the residual folder later when its handle is released. Do not force
  removal or terminate an unrelated process to hide the warning.

## Commit hygiene

Refresh `origin` at meaningful checkpoints: before branching, after a long
implementation or test phase, and before publication. Rebase unshared work
onto the latest intended target, then inspect overlapping imports, call sites,
and tests even when Git reports no conflict. Do not rebase a published branch
without approval, and do not add synchronization merge commits. Fast-forward a
clean primary checkout after verifying the merged commit is reachable from
`origin/main`.

Before each `gh` write, clear `GH_TOKEN` and `GITHUB_TOKEN`, switch to the
repository owner's account and verify `gh api user --jq .login` in the same
process as the write, following [AGENTS.md](../AGENTS.md#rules). Never print
tokens. For a Git push, use the owner's credential only in the push process.
If needed, obtain it with `gh auth token --user <origin-owner>` after verifying
the owner login, then set `GH_TOKEN` only for the push process and clear
`GITHUB_TOKEN`. If using `gh auth git-credential`, an inherited
`GIT_CONFIG_PARAMETERS` can override a generic helper reset; clear it and use
two process-scoped URL-specific helper entries, the first empty to reset the
list and the second `!gh auth git-credential`. Do not change shared Git
configuration. In PowerShell, pass issue and PR bodies with `--body-file` when
inline quoting may alter their contents.

Use the full 40-character tested head SHA with guarded merges. Check that the
merge succeeded before cleanup, then verify the merged commit is reachable
from `origin/main` with `git merge-base --is-ancestor`. The repository may
delete the remote head branch automatically; a later remote-delete failure
does not undo a successful merge. Fetch and report a moved head instead of
merging a different revision silently.

Keep PowerShell validation scripts fail-fast: capture `$LASTEXITCODE` immediately
after the native command and propagate it before formatting or another command
can replace it. Prefer one check per invocation or the validation runner, which
records and returns each failing command's status. Summarize outcomes and the
next decision, not each successful tool invocation.

Review the branch history before its first push:

```bash
git fetch origin main
git log --oneline origin/main..HEAD
git diff --check origin/main...HEAD
git rev-list --no-merges origin/main..HEAD | while read -r commit; do
  if ! git diff-tree --root --no-commit-id --name-only -r "$commit" | grep -q .; then
    git show -s --format='%h %s' "$commit"
  fi
done
```

The final command prints empty non-merge commits. It should produce no output.
The full branch review must also confirm that:

- Every commit describes one meaningful repository state with an imperative,
  conventional-commit subject.
- Plans and progress notes remain in the session, issue, or PR description.
- Each implementation commit is understandable and testable on its own.
- A commit does not exist only to correct behavior or documentation introduced
  earlier on the same unshared branch.
- Implementation commits remain separate from the single follow-up changelog
  commit, and release preparation remains a distinct commit.
- The branch contains no incidental `Merge remote-tracking branch
  'origin/main'` synchronization commit.

Clean up superseded or empty commits while the branch is still local and
unshared. If review has started or the branch is on the remote, do not rewrite
it without explicit developer approval. Never rewrite commits reachable from
`main` or a release tag. If an unshared branch falls behind, rebase it onto
current `origin/main`; do not merge `origin/main` into it merely to synchronize.

## Backlog

Pending work is tracked as GitHub issues on this repo (`gh issue
list`). File bugs and design questions there; reference them from commit
messages (`Fixes #N`).

[docs/backlog.md](../docs/backlog.md) holds only the high-level themes
and links them to their issues. Keep acceptance criteria, repro steps,
and status in the issue, never in the backlog document.

## Source of truth

Since 2026-07-18 this repository is the definitive source for the
agents-live framework; the earlier flow that assembled releases from a
private source repository is retired. Consumer repositories receive the skill
payload through `agents-live init`/`upgrade`; never treat an installed payload
as a source checkout or propose back-porting changes into one.

<!-- GLP consolidation: 2026-11-26T16:11:04Z; base=4cdf0fbad92c9ae9d3ddc1952ba8d9d435c79c1a -->
