---
name: changelog-maintenance
description: >-
  Review release readiness, update the package changelog, recommend a
  semantic version bump, and maintain the GitHub-issue backlog. Use when:
  "prepare a release", "do a release", "release readiness", "what version
  bump", "update the changelog", "what changed recently", "add changelog
  entry", "is the changelog current", "close out resolved issues", or
  "what needs logging".
---

# Changelog Maintenance

This repository has **one changelog**:
`src/agents_live/skill/docs/changelog.md`. It ships with the package,
and the backlog lives in **GitHub issues**, not in-tree docs
(see AGENTS.md). There are no other changelog or backlog files; if one
appears in-tree, that is a misfiling - move its content to the
changelog or to an issue and delete it.

## The release contract (read first)

`tools/release.py` consumes the changelog mechanically:

- The file must contain **exactly one** `## Unreleased` heading,
  followed by a blank line. Never rename, duplicate, or remove it.
- New entries go under `## Unreleased` as bullets. At release-prep the
  tool inserts `## <version> - <date>` below the Unreleased heading,
  moving the accumulated entries into that section. Never pre-stamp a
  version heading yourself.
- The first physical line of every bullet must be a standalone one-line
  summary. Put supporting detail on indented continuation lines. The release
  tool copies only the first line of each bullet into the GitHub release body.
- Release-prep **fails if Unreleased is empty**, so shippable work must
  have entries before a release is cut.

## Release handoff

Run `/changelog-maintenance` before the release preview. The handoff must:

1. Compare every commit since the latest release tag with `Unreleased`.
2. Add any missing user-visible entries and complete issue hygiene.
3. Recommend the minimum semantic version bump from the reviewed changes.
4. Leave changelog changes committed so the release starts from a clean tree.

The release tool independently enforces the minimum implied by conventional
changelog prefixes: `feat:` requires at least minor, `feat!:` or `fix!:` and
`BREAKING CHANGE:` footers require major, and fixes or documentation require
patch.

## Export boundary and PII

The changelog ships to PyPI. Entries describe the generic mechanism
only - no email addresses, account names, personal host names, or
`/home/<user>` paths. `tools/pre-release-audit.py` enforces emails and
home paths, but it is a backstop, not the boundary; host names, for
example, are not machine-checked. If a fix's story involves a concrete
machine or account, genericize it ("a packaged Windows install", not a
host name). Sanity check after editing:

```bash
grep -nEi '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|/home/[a-z]' \
  src/agents_live/skill/docs/changelog.md
```

## How to update the changelog

### 1. Identify what changed

```bash
# Everything since the last release
git --no-pager log --oneline "$(git describe --tags --abbrev=0)"..HEAD
```

Filter out noise: release version-bump commits, changelog-only commits,
and merge commits that only combine work already summarized by their
PR. When a PR merged multiple commits, write one entry for the PR's
substance, not one per commit.

### 2. Check what's already logged

Read the `## Unreleased` section and compare against the commit list.
Entries for hand-authored commits tend to be written at commit time by
the developer. Agent-authored PRs (Copilot, cloud agents) never touch
the changelog - assume every merged PR since the last release needs an
entry unless you find one already covering it.

### 3. Write entries

Don't write entries from commit subjects alone - agent-authored PRs in
particular carry terse implementation-level subjects. Read the diff
first; `src/agents_live/skill/docs/` changes usually state the
user-visible behavior directly and are the best source for the entry's
wording.

Match the existing format: conventional-commit-style bullets whose first line
is a standalone summary. Put elaboration on wrapped, indented continuation
lines. Keep fixes before feats (matching the existing section), most
significant first within each group:

```markdown
- fix: isolate shared crontab mutations by project.
  Projects sharing a user crontab can no longer remove one another's entries.
- feat: add `agents-live upgrade` for refreshing managed skill payloads.
  The command updates the runtime and every registered project by default.
```

Rules:

- Prefix `fix:` / `feat:` / `docs:` / `chore:` to match the change.
- Mark a breaking change with `!` before the colon, such as `feat!:` or
  `fix(parser)!:`. Use an optional `BREAKING CHANGE:` footer for migration
  detail, not a bold Markdown prefix.
- Make the first physical line read as a complete summary without depending on
  continuation lines. The release-prep pass must fix entries that violate this
  rule before recommending a release.
- Describe the user-visible behavior change and why it matters, not
  the implementation diff. One entry per logical change.
- No commit hashes - the git history is the audit trail. Cite a GitHub
  issue as `(#N)` only when the entry closes a tracked issue and the
  link aids the reader. Beware: the `(#N)` in a merged PR's commit
  subject is the **PR** number, not the issue - map to the tracking
  issue via `gh issue list --state closed` before citing.
- Internal-only refactors with no behavior change are optional; log
  them as `chore:` only when they change how contributors work.

### 4. Issue (backlog) hygiene

The backlog is `gh issue list`. When updating the changelog:

1. **Close out resolved issues.** For each recent commit or PR, check
   whether it resolves an open issue. Commits should carry `Fixes #N`
   so the merge closes the issue; if one merged without it, close the
   issue manually with a comment citing the commit.
2. **File follow-up issues** for work uncovered during the session -
   both follow-ups surfaced by a commit and deferred items from the
   conversation (design tensions, gaps, refactors postponed). Re-read
   the session when running this skill; a non-trivial decision to defer
   something becomes an issue with a one-line "why deferred" note.
   Label priority when the user expressed urgency.
3. **No in-tree backlog.** Never add TODO/backlog sections to docs;
   convert them to issues.

## Commit sequencing - when to run this skill

Run this skill **between** content commits, not bundled into them:

1. **Work commit(s) first.** Code, tests, and doc changes for the
   feature, with `Fixes #N` references where applicable. No changelog
   edits in these commits.
2. **Run the skill.** With the commits visible in `git log`, update
   `## Unreleased`, close resolved issues, file follow-up issues.
3. **Log commit.** A single follow-up commit (e.g. `Update changelog
   for <feature>`) containing only the changelog edit - trivially
   auditable and independently revertable.

For sessions that produce no commits, skip the changelog and only file
issues if something genuinely worth tracking came up.
