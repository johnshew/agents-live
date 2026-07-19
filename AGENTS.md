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
| Comparing source, wheel, and installed-tool behavior | [.agents/testing.md](.agents/testing.md) |
| Cutting or preparing a release | [.agents/release.md](.agents/release.md) |
| Creating, running, or debugging triggered agents in this checkout | [.agents/agents-live.md](.agents/agents-live.md) |
| Changing the skill payload, docs, or templates | [src/agents_live/skill/SKILL.md](src/agents_live/skill/SKILL.md) and [docs/](src/agents_live/skill/docs/) |
| Investigating runtime behavior (debounce, watchers, adapters) | [approach.md](src/agents_live/skill/docs/approach.md), then [key-learnings.md](src/agents_live/skill/docs/key-learnings.md) |

## Quick commands

```bash
uv run --with-editable . --script tests/test_smoke.py   # tests
uv run --with-editable . agents-live --help              # CLI from source
uv run --script tools/pre-release-audit.py               # release audit
uv run --script tools/release.py --dry-run --bump patch   # release preview
uv run --script tools/release.py --prepare --bump patch --yes # prepare patch
uv run --script tools/release.py --publish --yes          # publish prepared
```

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
- **Tests must stay portable.** `tests/test_smoke.py` runs against
  temp projects only; never couple it to this checkout's `Agents/`
  directory or any specific host.
- **Keep README and skill docs in sync.** The README mirrors
  [overview.md](src/agents_live/skill/docs/overview.md); a change to
  one usually implies a change to the other.
- **`Agents/` is runtime, not source.** Handlers and logs there
  support local use of the tool; package behavior lives under
  `src/agents_live/`.
- **The backlog lives in GitHub issues, not in-tree docs.** Check
  `gh issue list` before starting work; file new findings as issues
  and reference them from commits (`Fixes #N` closes on merge).
- **Never hand-parse runtime logs.** Use `agents-live logs` and
  `agents-live logs timeline` - they correlate events across log
  files and agent transcripts. Reading `Agents/logs/*.log` directly
  has repeatedly led to wrong conclusions.
- **Never `git checkout`, `git reset`, or `git stash` tracked
  files.** Other agents run concurrently in this checkout and may
  have uncommitted work; re-edit the file instead.
- **Do branch work in a git worktree, not the primary checkout.**
  Any task that creates a branch and commits (a PR, an experiment)
  belongs in its own worktree so the primary checkout stays on a
  clean `main` for the agents sharing it. Quick reads and
  investigation can happen in place.
- **No backward-compatibility shims.** Clean break, migrate all
  consumers; ask the developer before adding any compat code.
- **Keep agent memory to pointers.** Canonical facts live in the
  repo and GitHub issues; a memory entry holds only a pointer to
  that home, never the content itself.
- No em dashes; no emojis or icons unless the developer asks.

## Structure

- `src/agents_live/` - package: CLI, runtime modules, and the vendored
  skill payload (`skill/` with SKILL.md, docs, starter templates)
- `tests/` - export-safe smoke suite
- `tools/` - release tooling (audit and guarded publish workflow)
- `Agents/` - local triggered-agent runtime dir (handlers, logs)
- `.agents/` - agent-facing guides (this file's targets)
- `.github/workflows/` - CI: publish to PyPI on GitHub release
