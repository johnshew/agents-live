---
title: Developing Agents Live
description: Build, test, and run agents-live from source without replacing the installed tool
---

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

## Source checkout and installed tool

See [testing.md](testing.md) for the full source, wheel, and installed-tool
validation matrix.

Use the editable project environment when testing code in this repository:

```bash
uv run --with-editable . agents-live --repo ~/repos/<target-project> doctor
uv run --with-editable . agents-live --repo ~/repos/<target-project> dashboard --dev
```

These commands execute the current checkout without replacing the user-level
tool. From another repository, bare `agents-live` executes the version
installed by uv:

```bash
agents-live --repo ~/repos/<target-project> doctor
uv tool list
agents-live upgrade
```

`uv tool list` reports the installed version. `agents-live upgrade` reinstalls
the latest stable uv-managed runtime and refreshes managed payloads in the
current initialized project and registered repositories. To restore a normal
PyPI installation after experimenting with `uv tool install --editable .`, run:

```bash
uv tool install --force agents-live
```

Use `agents-live --repo <project> upgrade --skills-only` to refresh only one
installed skill payload. `agents-live --repo <project> doctor` reports any
package and skill payload version mismatch.

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

## Commit hygiene

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
list`), deliberately not as in-tree docs. File bugs and design
questions there; reference them from commit messages (`Fixes #N`).

## Source of truth

Since 2026-07-18 this repository is the definitive source for the
agents-live framework; the earlier flow that assembled releases from a
private source repository is retired
([release-process.md](../src/agents_live/skill/docs/release-process.md)
describes it for historical context). Consumer repositories receive the
skill payload through `agents-live init`/`upgrade`; never treat an
installed payload as a source checkout or propose back-porting changes
into one.
