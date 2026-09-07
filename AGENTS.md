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

## Load before acting

| When you are... | Read first |
|---|---|
| Changing code, running tests, or building | [.agents/development.md](.agents/development.md) |
| Understanding the development and release state machine | [docs/development-release-process.md](docs/development-release-process.md) |
| Comparing source, wheel, and installed-tool behavior | [.agents/testing.md](.agents/testing.md) |
| Adding, changing, or deleting a test | [docs/testing-methodology.md](docs/testing-methodology.md) |
| Cutting or preparing a release | [.agents/release.md](.agents/release.md) |
| Reporting bake and release channel state | [.agents/release-report.md](.agents/release-report.md) |
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
uv run --script tools/release.py --dry-run --bump patch   # release preview
uv run --script tools/release.py --prepare --bump patch --yes # prepare patch
uv run --script tools/release.py --publish --yes          # publish prepared
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
  phase, configured bake branch, tested version, and next action. Treat it as
  required routing context, not as a release-only document.
2. Investigate in place; reads and searches are fine in the primary
   checkout.
3. Use the primary checkout only when it is clean and already on the intended
  target branch. Otherwise, create a dedicated worktree from that target;
  verify its ancestry before committing or pushing, and remove it when the
  task is complete.
4. Edit, then run the smoke tests and the release audit (Quick commands above).
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

### Active bake routing

Use the generated release report together with `.github/release-channels.toml`.
When the configured `bake.branch` exists and contains work not yet in `main`,
that branch is the integration target for the active bake cycle. Being asked
to change the bake branch is sufficient evidence that the work belongs to the
bake. Focused pull requests should target the bake branch instead of `main`;
direct commits are acceptable for small administrative changes, but
substantive fixes should retain PR review and CI evidence.

If an active bake exists but a request names `main`, do not assume the change
should bypass bake. Ask whether the intent is to fix the current bake, perform
release promotion, or make independent post-release work before editing or
branching. A request to prepare or publish a release follows `.agents/release.md`
and the report's ordered next actions.

After a change reaches the bake branch, deploy its exact synchronized commit:

```bash
git pull --ff-only origin <configured-bake-branch>
uv run --script tools/local-deploy.py --repo <live-repository>
```

Run these commands from a clean checkout of the configured bake branch, using
the primary checkout or a dedicated worktree according to the workflow above.

The deployment creates and selects a commit-qualified
`<target>.dev0+g<commit>` generation. `--allow-downgrade` is required only
when intentionally moving to a lower numeric `major.minor.patch` release; it
is not needed between a stable candidate and a bake on the same release line.
When the developer approves bake promotion, update `[bake.promotion]` in
`.github/release-channels.toml` to `decision = "approved"` and record the exact
full bake `commit` plus `decided_on = "YYYY-MM-DD"`. Approval applies only to
that commit; new bake changes require renewed validation and approval. Then
move the approved bake to `main` through one promotion pull request and prepare
a new official candidate from the resulting clean `main`.

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
  started foreground dashboard can be healthy without appearing there. Stop
  only a dashboard process this task deliberately started.
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
