---
title: Release Channels and Reporting
description: Channel ownership, promotion policy, and the generated release report
---

Agents Live has two named steps between writing a change and publishing it. We
call those steps channels. The report brings together code changes, reviews,
issues, test results, and the version installed for testing so that a passing
pull request is never mistaken for a public release.

## Channel model

| Channel | Branch | Version | Moves to |
|---|---|---|---|
| `bake` | `bake/v<version>-local` | Development version ending in `.dev`, with its commit ID | `release` by pull request to `main` |
| `release` | `main` plus immutable `v<version>` tag | Stable semantic version | GitHub Release, then verified PyPI publication |

Feature and fix branches enter the lowest channel that needs the change. During
an active bake, focused pull requests target the bake branch. Work reaches the
release channel only through one reviewable promotion pull request from bake to
`main`. The official `release/v<version>-candidate` branch is a temporary branch
created by `tools/release.py` after bake moves into a clean, up-to-date `main`;
it is not a third channel.

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
- The records created by release preparation and final testing remain the
  authority for approving the official candidate. The report summarizes them;
  it never replaces a required check.

Keep the manifest small. Do not copy PR titles, issue titles, check results, or
commit counts into it because the generator reads those live. Update its
deployment fields only after installing and validating that exact artifact.

## Required report contents

Every generated report must include:

- a plain-English answer to "Are we ready to release?", followed by the
  specific decisions and actions still needed;
- a recommended decision for each unresolved release question, plus one clear
  overall recommendation about whether to keep testing in bake or move to
  `main`;
- each active channel, the branch and version it uses, where its work stands,
  and where that work goes next;
- the latest public release and how much newer work exists in `main` and bake;
- pull requests already merged into each channel, pull requests still open,
  and whether their checks passed;
- issues delivered, partially delivered, deferred, awaiting a promotion
  decision, or not yet assigned to a channel;
- the version currently installed for testing, when it was tested, and whether
  it includes the newest bake changes;
- the ordered steps needed to move bake into release; and
- generation time and full source SHAs so the report can be cited as a
  point-in-time observation.

The report must keep GitHub issue state separate from channel delivery state.
It must not infer successful deployment from a merged PR, infer release
readiness from a green check alone, or replace release preparation and
acceptance receipts.

## Writing style

Write for a product owner or startup operator who understands releases but
should not have to translate Git internals.

- Lead with the conclusion: whether the version can be released now.
- Use short sentences and familiar verbs: `finish`, `decide`, `test`, `move`,
  and `publish`.
- Say "move bake to release" instead of "promote the channel."
- Say "version installed for testing" instead of "deployed artifact."
- Say "current bake branch" instead of "channel tip."
- Say "newer work" instead of "divergence" and "checks passed" instead of
  "required-check state."
- Describe what an issue means for the release, not only its tracking label.
- Give a recommendation, not just a list of choices. Explain whether to finish
  the work now, include only the completed part, defer the remainder, or accept
  a known risk. State why in one or two sentences.
- Distinguish "include the completed work in this release" from "finish every
  remaining item in the issue." Large issues may span more than one release.
- End the recommendation with the next concrete action and where it must happen.
- Keep branch names, commit IDs, counts, and exact version strings in tables or
  a final evidence section. They support the explanation; they are not the
  explanation.
- Define any unavoidable release term on first use. Do not use internal process
  words such as `artifact`, `tip`, `upstream`, `provenance`, or `disposition` in
  the summary.
- Never use a label such as `not ready` without immediately saying what a
  person must do next.
- Do not claim that moving bake to `main` is recommended while any report
  recommendation still says work must be fixed or tested in bake.

## Channel states

The generated summary uses evidence-based states:

- `idle`: no changes beyond the upstream channel.
- `baking`: changes are integrated but no promotion pull request is open.
- `promotion proposed`: a bake-to-release pull request is open.
- `candidate`: release preparation produced a receipt-bound candidate.
- `released`: an immutable stable tag has a published GitHub release.
- `blocked`: a required check failed or the developer explicitly declared a
  work item blocking.

These labels are shorthand for the evidence tables. The opening summary must
translate them into plain English. Moving bake to release still requires every
open decision resolved, all checks passing, the installed test version matching
the current bake branch, complete changelog and issue review, and every gate in
[release.md](release.md).

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