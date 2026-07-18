# Developing agents-live

How to build, test, and change the code in this repository.

## Layout

- `src/agents_live/` - the Python package. Runtime modules (`cli.py`,
  `run.py`, `activate.py`, `headless.py`, ...) plus the vendored skill
  payload at `src/agents_live/skill/` (SKILL.md, docs/, templates/).
- `tests/test_smoke.py` - export-safe smoke suite. Runs against temp
  projects only; never touches this checkout's `Agents/` directory.
- `tools/pre-release-audit.py` - scans for personal information,
  secrets, and nonportable paths. Must pass before any release.
- `Agents/` - local runtime directory (handlers, logs) used when
  agents-live is exercised in this checkout; not package source.

## Commands

Always go through `uv` - never plain `python3` (system interpreters are
often too old; the package requires Python 3.12+).

```bash
# run the test suite (CI runs the unittest equivalent:
# uv run --with-editable . python -m unittest tests.test_smoke)
uv run --with-editable . --script tests/test_smoke.py

# run the CLI from source
uv run --with-editable . agents-live --help

# audit the tree for release-blocking content
uv run --script tools/pre-release-audit.py
```

## Conventions

- Standalone scripts carry PEP 723 headers and run via
  `uv run --script <path>`.
- Docs under `src/agents_live/skill/docs/` carry frontmatter
  (`title`, `description`, `ms.date`, `ms.topic`); update `ms.date`
  when you materially change a doc.
- Keep `README.md` and the skill docs consistent - the README's
  feature claims and Documentation links mirror
  `src/agents_live/skill/docs/overview.md`.
- Minimal diffs; match the style of the surrounding code and docs.

## Backlog

Pending work is tracked as GitHub issues on this repo (`gh issue
list`), deliberately not as in-tree docs. File bugs and design
questions there; reference them from commit messages (`Fixes #N`).

## Upstream note

This tree matches the assembled release layout described in
[release-process.md](../src/agents_live/skill/docs/release-process.md),
which is generated from a private source repository. Before large
refactors of the vendored skill payload, consider whether the change
belongs upstream; small fixes and doc corrections are fine here.
