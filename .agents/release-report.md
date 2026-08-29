---
title: Release Channels and Reporting
description: Channel ownership, promotion policy, and the generated release report
---

Agents Live uses a progressive-delivery model with two named channels. A
channel is a governed stream of changes and artifacts, not merely a branch.
The report joins source, review, work-item, validation, and deployment evidence
so that a green pull request is never mistaken for a promoted release.

## Channel model

| Channel | Branch | Artifact identity | Promotes to |
|---|---|---|---|
| `bake` | `bake/v<version>-local` | PEP 440 development version with commit metadata | `release` by pull request to `main` |
| `release` | `main` plus immutable `v<version>` tag | Stable semantic version | GitHub Release, then verified PyPI publication |

Feature and fix branches enter the lowest channel that needs the change. During
an active bake, focused pull requests target the bake branch. Work reaches the
release channel only through one reviewable promotion pull request from bake to
`main`. The official `release/v<version>-candidate` branch remains an ephemeral
branch created by `tools/release.py` after the promotion lands on clean,
synchronized `main`; it is not a long-lived third channel.

GitHub closes linked issues only when commits reach the default branch. An open
issue can therefore be `delivered` to bake without being released. Reports must
show issue disposition and channel separately.

## Sources of truth

- Git refs and tags establish exactly which commits are in each channel.
- GitHub pull requests establish review, merge, and CI evidence.
- GitHub issues establish whether work remains open or closed.
- [.github/release-channels.toml](../.github/release-channels.toml) records
  decisions APIs cannot infer: partial delivery, explicit deferral, promotion
  decisions, and the last deployed bake artifact.
- Preparation and acceptance receipts remain authoritative for official
  candidate acceptance. The report summarizes them; it never replaces a gate.

Keep the manifest small. Do not copy PR titles, issue titles, check results, or
commit counts into it because the generator reads those live. Update its
deployment fields only after installing and validating that exact artifact.

## Required report contents

Every generated report must include:

- each active channel, its branch and immutable tip, artifact identity, current
  state, and next promotion target;
- the latest published release and divergence between release, `main`, and
  bake;
- pull requests merged into each channel since its upstream release, open pull
  requests targeting a channel, and their required-check state;
- issues delivered, partially delivered, deferred, awaiting a promotion
  decision, or not yet assigned to a channel;
- the last deployed bake artifact, its validation date, and its distance from
  the current bake tip;
- an explicit promotion-readiness result and the decisions or failed evidence
  preventing promotion;
- the ordered promotion path; and
- generation time and full source SHAs so the report can be cited as a
  point-in-time observation.

The report must keep GitHub issue state separate from channel delivery state.
It must not infer successful deployment from a merged PR, infer release
readiness from a green check alone, or replace release preparation and
acceptance receipts.

## Channel states

The generated summary uses evidence-based states:

- `idle`: no changes beyond the upstream channel.
- `baking`: changes are integrated but no promotion pull request is open.
- `promotion proposed`: a bake-to-release pull request is open.
- `candidate`: release preparation produced a receipt-bound candidate.
- `released`: an immutable stable tag has a published GitHub release.
- `blocked`: a required check failed or the developer explicitly declared a
  work item blocking.

State is not readiness. Promotion readiness additionally requires every
open promotion decision resolved, all required checks green, the deployed
artifact at the channel tip, complete changelog and issue review, and every
gate in [release.md](release.md).

## Generate the report

Refresh remote refs first, then generate the local snapshot:

```bash
git fetch origin --prune
uv run --script tools/release-report.py
```

The default output is `.reports/release-report.md`, which is intentionally
gitignored. Use `--output <path>` for another local destination and `--check`
to verify that an existing local report still matches current Git and GitHub
data. The report carries its generation time and source SHAs so readers can
recognize a stale snapshot. Generate it for release reviews, after channel
promotion, and whenever the manifest's deployment or disposition decisions
change. Do not commit generated reports.