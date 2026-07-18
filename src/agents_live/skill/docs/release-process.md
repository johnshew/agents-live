---
title: Agents Live Release Process
description: Assemble, audit, build, and publish the agents-live Python package
ms.date: 2026-07-18
ms.topic: how-to
---

## Release boundary

The release assembler creates a normal Python package under
`src/agents_live/`. It includes the CLI modules, the vendored skill payload,
generic templates, the domain guide, and release tooling. The build produces a
wheel and source distribution from the generated `pyproject.toml`.

Deployment agents are not exported. They are ordinary Claude Code or GitHub
Copilot agent definitions whose prompts may contain deployment-specific or
personal data. Public examples come from the vendored templates. Runtime logs,
runtime data, credentials, IDE state, and private notes are also excluded.

The assembled tree has this shape:

```text
agents-live-release/
├── README.md
├── pyproject.toml
├── .agents/
│   └── agents-live.md
├── Agents/
│   ├── handlers/
│   │   └── write-files.sh
│   └── logs/
├── src/
│   └── agents_live/
│       ├── __init__.py
│       ├── cli.py
│       ├── ...
│       └── skill/
│           ├── SKILL.md
│           ├── docs/
│           └── templates/
├── tests/
│   └── test_smoke.py
└── tools/
  ├── pre-release-audit.py
  └── release.py
```

## Assemble and audit

Run the source-tree audit before assembly:

```bash
uv run --script .claude/skills/agents-live/scripts/pre-release-audit.py
```

Assemble into repository-local scratch space:

```bash
bash .claude/skills/agents-live/scripts/assemble-release.sh \
  .trash/agents-live-release
```

Review the printed file tree, then audit the assembled tree:

```bash
cd .trash/agents-live-release
uv run tools/pre-release-audit.py
```

The audit must scan the assembled tree, report no personal information,
secrets, or nonportable paths, and exit successfully. Review every exported
file even when the automated scan passes.

In the assembled tree the audit additionally enforces two release gates
(both skipped in the source checkout, where they resolve trivially):

- **Adapter resolution**: every exported agent or template that declares
  a `runtime:` must resolve through the exported adapter registry — the
  packaged registry minus `private` adapters. A release must never ship
  an agency-dependent agent; ship public-adapter prompts or omit.
- **Doc links**: every relative `.md` link in the export must resolve
  inside the export. Docs that stay in the life repo (backlog.md,
  agency cli.md, review docs) are stripped or delinked by
  `assemble-release.sh`; a new dangling link means the assembly lists
  need updating.

## Validate and build

Run the focused engine suite from the source repository:

```bash
uv run --script .claude/skills/agents-live/scripts/test_headless.py
```

Run the exported smoke suite from the assembled tree:

```bash
cd .trash/agents-live-release
uv run --with-editable . python -m unittest tests.test_smoke
```

Build both package artifacts from the assembled tree:

```bash
cd .trash/agents-live-release
uv build
```

Inspect the wheel and source distribution before publication. Verify that the
`agents-live` console entry point starts, `agents-live --help` reports the
documented command surface, `agents-live init` installs the vendored skill,
and no deployment agent or private adapter is present.

## Publish

Publish from the assembled release repository after the audit, tests, artifact
inspection, and version review pass. Preview the default patch release first:

```bash
uv run --script tools/release.py --dry-run
```

Run the release after reviewing the plan:

```bash
uv run --script tools/release.py --prepare --yes
```

The script requires a clean `main` synchronized with `origin/main`. It bumps
all version surfaces, moves the changelog's Unreleased notes under the new
version, reruns the audit, smoke suite, and build, and creates the release
commit and annotated tag locally. Inspect the target-version wheel and source
distribution under `dist/`, then publish:

```bash
uv run --script tools/release.py --publish --yes
```

Publication verifies that the tagged release commit is exactly one commit
ahead of `origin/main`, reruns every gate, pushes the commit and tag atomically,
and creates the GitHub release. That release triggers trusted publishing to
PyPI.

Use semantic versioning:

| Change | Version bump |
|---|---|
| Breaking CLI, configuration, or frontmatter contract | Major |
| Backward-compatible command or engine capability | Minor |
| Bug fix or documentation correction | Patch |

Pass `--bump minor` or `--bump major` when required. Do not publish directly
from the private consuming repository.

## Release checklist

* [ ] Focused engine tests pass
* [ ] Source-tree audit passes or every finding is resolved
* [ ] Assembled-tree audit passes with no findings
* [ ] Export contains no deployment agents, private adapters, logs, or data
* [ ] Wheel and source distribution build successfully
* [ ] Installed CLI and `init` payload match the documented contract
* [ ] Documentation matches current behavior
* [ ] Version and release tag follow semantic versioning
* [ ] Release script preview shows the expected version and file set
* [ ] Target-version artifacts inspected after preparation