---
name: changelog-maintenance
description: >-
  Review and update the package changelog and GitHub-issue backlog.
  Use when: "update the changelog", "what changed recently", "add
  changelog entry", "is the changelog current", "close out resolved
  issues", "what needs logging".
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
- Release-prep **fails if Unreleased is empty**, so shippable work must
  have entries before a release is cut.

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
Entries here tend to be written at commit time by the developer, so
expect the section to be mostly current; the common gap is a PR that
merged without touching the changelog.

### 3. Write entries

Match the existing format - conventional-commit-style bullets, wrapped
to the file's line width, most significant first:

```markdown
- fix: scope crontab matching to the current repository, so projects
  sharing a user crontab cannot remove one another's entries.
- feat: add `agents-live upgrade` as the explicit post-package-upgrade
  workflow for refreshing a project's managed skill payload.
```

Rules:

- Prefix `fix:` / `feat:` / `docs:` / `chore:` to match the change.
- Describe the user-visible behavior change and why it matters, not
  the implementation diff. One entry per logical change.
- No commit hashes - the git history is the audit trail. Cite a GitHub
  issue as `(#N)` only when the entry closes a tracked issue and the
  link aids the reader.
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
