# AGENTS

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
| Cutting or preparing a release | [.agents/release.md](.agents/release.md) |
| Creating, running, or debugging triggered agents in this checkout | [.agents/agents-live.md](.agents/agents-live.md) |
| Changing the skill payload, docs, or templates | [src/agents_live/skill/SKILL.md](src/agents_live/skill/SKILL.md) and [docs/](src/agents_live/skill/docs/) |
| Investigating runtime behavior (debounce, watchers, adapters) | [approach.md](src/agents_live/skill/docs/approach.md), then [key-learnings.md](src/agents_live/skill/docs/key-learnings.md) |

## Quick commands

```bash
uv run --with-editable . --script tests/test_smoke.py   # tests
uv run --with-editable . agents-live --help              # CLI from source
uv run --script tools/pre-release-audit.py               # release audit
```

## Rules

- **Use `uv`, never plain `python3`.** The package requires Python
  3.12+; scripts with PEP 723 headers run via `uv run --script`.
- **Keep the tree export-clean.** Everything here ships to PyPI. No
  personal information, secrets, or machine-specific paths - the
  pre-release audit enforces this, but don't rely on it to catch you.
- **Tests must stay portable.** `tests/test_smoke.py` runs against
  temp projects only; never couple it to this checkout's `Agents/`
  directory or any specific host.
- **Keep README and skill docs in sync.** The README mirrors
  [overview.md](src/agents_live/skill/docs/overview.md); a change to
  one usually implies a change to the other.
- **`Agents/` is runtime, not source.** Handlers and logs there
  support local use of the tool; package behavior lives under
  `src/agents_live/`.

## Structure

- `src/agents_live/` - package: CLI, runtime modules, and the vendored
  skill payload (`skill/` with SKILL.md, docs, starter templates)
- `tests/` - export-safe smoke suite
- `tools/` - release tooling (pre-release audit)
- `Agents/` - local triggered-agent runtime dir (handlers, logs)
- `.agents/` - agent-facing guides (this file's targets)
- `.github/workflows/` - CI: publish to PyPI on GitHub release
