---
title: Releasing Agents Live
description: Required checks and commands for publishing agents-live releases
---

Checklist for cutting a release from this repository, the definitive
source since 2026-07-18 (the retired assembly flow is described for
historical context in
[release-process.md](../src/agents_live/skill/docs/release-process.md)).
Use [testing.md](testing.md) to validate source, target-version artifacts, and
the installed PyPI tool as separate execution modes.

## Changelog readiness

Invoke `/changelog-maintenance` before previewing a release. It compares every
commit since the latest tag with `Unreleased`, adds missing user-visible notes,
completes issue hygiene, and recommends the minimum semantic version bump.
Commit any resulting changelog update before continuing because preparation
requires a clean tree.

## Versioning

Semantic versioning; the version lives in `pyproject.toml`.

| Change | Bump |
|---|---|
| Breaking CLI, configuration, or frontmatter contract | Major |
| New commands, adapters, or compatible features | Minor |
| Fixes and documentation | Patch |

## Gates (all must pass)

```bash
uv run --script tools/pre-release-audit.py
uv run --with-editable . --script tests/test_smoke.py
uv run --with-editable . agents-live smoketest
uv build
```

The audit must report no personal information, secrets, or nonportable
paths, and its adapter-resolution and doc-link checks must pass.
The framework smoketest must pass end to end: it exercises the real
trigger/run/status loop in this checkout, catching integration breaks
the unit suite cannot. `tools/release.py` runs all of these gates
itself during `--prepare` and `--publish`.
For machine-specific names that generic patterns cannot detect, create the
gitignored `.agents-live-machine-names` file at the repository root. Put one
literal machine name on each line; blank lines and lines beginning with `#`
are ignored. The names remain local, while the audit reports every
case-insensitive match in shipped text with its file and line number.
Inspect the wheel and sdist: `agents-live --help` reports the
documented commands, `agents-live init` installs the vendored skill,
and no private adapter or deployment-specific agent is present.

## Publish

Preview the selected release without changing files or remotes:

```bash
uv run --script tools/release.py --dry-run --bump patch
```

Prepare the release locally:

```bash
uv run --script tools/release.py --prepare --bump patch --yes
```

Replace `patch` with the bump recommended by changelog maintenance. The script
rejects an empty `Unreleased` section and any bump below the minimum implied by
`feat:`, conventional `type!:` or `BREAKING CHANGE:` notes. Every changelog
bullet must start with a standalone one-line summary; supporting detail belongs
on indented continuation lines. The script requires a clean `main`
synchronized with `origin/main`, updates all package, skill,
documentation-link, and changelog versions, runs every release gate, and
creates the release commit and annotated tag locally. Inspect the
target-version artifacts under `dist/` and review the commit.

Publish the prepared commit and tag:

```bash
uv run --script tools/release.py --publish --yes
```

For the initial push, publication reruns all gates, requires the tagged release
commit to be exactly one commit ahead of `origin/main`, pushes the commit and
tag atomically, and creates the GitHub release. The release body starts with
one first-line summary per changelog entry and a link to the full changelog at
the release tag, followed by GitHub's generated notes (merged pull requests and
the compare link).

Publishing the GitHub release triggers `.github/workflows/publish.yml`,
which rebuilds, attaches the wheel and sdist to the GitHub release, and
publishes the same artifacts to PyPI through trusted publishing. Wait for that
workflow to succeed, verify both artifacts are attached, then follow the
two-stage PyPI and installed-tool checks in [testing.md](testing.md). In an
interactive terminal, `gh run watch <run-id> --exit-status` can wait for the
workflow. Automation should use noninteractive run-status APIs or
`GH_PAGER=cat gh run view <run-id>` after completion; `gh run watch` may take
over the terminal's alternate screen.

PyPI's versioned JSON endpoint can expose a release before the Simple API used
by package resolvers. A successful workflow and HTTP 200 from the versioned
JSON endpoint confirm publication. Exact-version `uvx` resolution separately
confirms consumer availability. If JSON succeeds while `uvx` reports that the
version does not exist, allow the Simple API to propagate and retry the exact
check. Do not republish or alter the tag.

If a failure or interruption occurs before the release commit, the script
restores every version file and clears its staged changes. A failure after the
commit remains visible for recovery. Rerun `--publish --yes` if GitHub release
creation fails after the atomic push; publication accepts the exact tagged
commit locally or on `origin/main` and skips a release that already exists. Do
not rewrite or delete a pushed release tag.
