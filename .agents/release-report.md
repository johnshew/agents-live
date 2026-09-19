---
title: Release Cycle Reporting
description: Concurrent release targets, exact candidate evidence, and all in-flight work
---

# Release cycle reporting

Choose a stable target, prepare and activate numbered local RCs, then accept and
publish the selected final packages. There is no bake channel. The canonical
business requirements and industry evidence are in
[development-release-process.md](../docs/development-release-process.md).

At the start of repository work, refresh authorized remote state and generate:

```bash
git fetch origin --prune
uv run --script tools/release-report.py
uv run --script tools/release-report.py --json
```

Markdown and JSON use the same calculation. The gitignored report is a
point-in-time observation, not permission to publish or a replacement for
package acceptance. Follow the repository's identity checks before remote
operations. Missing, stale or truncated evidence must be explicit.

## Routing and decisions

[.github/release-cycles.toml](../.github/release-cycles.toml) uses schema 2:

```toml
schema = 2
development_branch = "main"
default_cycle = "1.2.3"

[cycles."1.2.3"]
branch = "main"
next_rc = "1.2.3rc1"

[cycles."1.2.3".approval]
decision = "testing"
```

Develop on `main` by default. Configure a stabilization branch only when an
older source must remain separate from next-version development. Every cycle
keeps its own scope, selected RC, source identity, historical attempts, deployment
observations and explicit issue decisions. An older selected RC is not superseded
merely because newer source or a higher RC number exists.

Publication approval records `decision = "approved"`, the exact full source
`commit`, and `decided_on = "YYYY-MM-DD"`. A different runtime commit requires
renewed validation and approval. An open issue can contain work delivered in an
RC but not yet released; show those states separately. A developer-approved
deferral is not a claim that the issue is fixed.

Use the primary checkout only when clean and already on the intended branch;
otherwise use an isolated worktree. Verify ancestry before committing or pushing.
Remove task worktrees after delivery, preserving retained attempt worktrees and
their immutable receipts. Never discard another writer's changes.

## Evidence and report contents

- Show every configured release cycle, its full source commit, selected and next
  RC, approval, decisions, blockers, recommendation and concrete next action.
- Show retained candidate and final attempts, including prepared but unselected,
  rejected, finalized, missing and invalid evidence. Do not convert local
  readiness or historical observations into operational acceptance.
- Report the observed local runtime separately from recorded deployment history.
  Selection is scoped to the queried environment, not universal across hosts.
- Include all open PRs, review/check state and target branches, plus merged
  unreleased work. Do not silently hide work outside the default cycle.
- Include delivered, planned, partial, deferred and decision-needed issues, and
  all unassigned open issues. Keep issue closure separate from package delivery.
- Report latest stable GitHub release and each cycle's publication state.
  PyPI and proxy availability need independent verification; an index delay
  must not cause another publication of the same version.
- Include generation time and the provenance or limitation of every observation.

Git refs identify source. GitHub supplies issues, PRs and public release state.
The manifest records decisions those APIs cannot infer. Retained receipts bind
source, package hashes, environment and checks. Public CLI observations identify
the current local runtime. None of these sources alone proves all the others.

## Local testing and publication

From a clean synchronized configured source branch:

```bash
uv run --script tools/local-deploy.py --repo <live-repository> --rc <configured-next-rc>
```

Activation, state preservation, dashboard/watcher restoration and rollback are
official parts of the RC loop. Source changes require another RC number; never
overwrite consumed package identities. Full final preparation and operational
acceptance remain independent gates before publication. Use
[release.md](release.md) for those operations and [testing.md](testing.md) for
source, artifact and installed-state evidence boundaries.

When GitHub reports a stable target published, suppress preparation/publication
instructions for that target and direct development to a later cycle. Keep the
published cycle's historical evidence visible. Do not infer PyPI availability
from GitHub publication.

Keep this guide, `AGENTS.md`, the canonical process, manifest and generator
consistent. Write conclusions and next actions in ordinary release vocabulary,
with explicit uncertainty instead of an optimistic readiness label.