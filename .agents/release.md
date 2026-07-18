# Releasing agents-live

Checklist for cutting a release from this repository. The full
narrative, including the upstream assembly step, is in
[release-process.md](../src/agents_live/skill/docs/release-process.md).

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

Publishing is automated: `.github/workflows/publish.yml` builds and
publishes to PyPI via trusted publishing when a GitHub release is
published (or via `workflow_dispatch` with a tag). It re-runs the smoke
suite first. To release: bump the version, commit, tag `v<version>`,
push, then publish a GitHub release for that tag.
