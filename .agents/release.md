---
title: Releasing Agents Live
description: Required checks and commands for publishing agents-live releases
---

Checklist for cutting a release from this repository. The full
narrative, including the upstream assembly step, is in
[release-process.md](../src/agents_live/skill/docs/release-process.md).
Use [testing.md](testing.md) to validate source, target-version artifacts, and
the installed PyPI tool as separate execution modes.

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
uv build
```

The audit must report no personal information, secrets, or nonportable
paths, and its adapter-resolution and doc-link checks must pass.
Inspect the wheel and sdist: `agents-live --help` reports the
documented commands, `agents-live init` installs the vendored skill,
and no private adapter or deployment-specific agent is present.

## Publish

Preview the next patch release without changing files or remotes:

```bash
uv run --script tools/release.py --dry-run
```

Prepare the release locally:

```bash
uv run --script tools/release.py --prepare --yes
```

Pass `--bump minor` or `--bump major` when the change requires it. The
script requires a clean `main` synchronized with `origin/main`. It updates
all package, skill, documentation-link, and changelog versions; runs every
release gate; and creates the release commit and annotated tag locally.
Inspect the target-version artifacts under `dist/` and review the commit.

Publish the prepared commit and tag:

```bash
uv run --script tools/release.py --publish --yes
```

For the initial push, publication reruns all gates, requires the tagged release
commit to be exactly one commit ahead of `origin/main`, pushes the commit and
tag atomically, and creates the GitHub release.

Publishing the GitHub release triggers `.github/workflows/publish.yml`,
which rebuilds and publishes to PyPI through trusted publishing. If a
failure or interruption occurs before the release commit, the script restores
every version file and clears its staged changes. A failure after the commit
remains visible for recovery. Rerun `--publish --yes` if GitHub release
creation fails after the atomic push; publication accepts the exact tagged
commit locally or on `origin/main` and skips a release that already exists.
Do not rewrite or delete a pushed release tag.
