---
title: Release Business Requirements
description: Business requirements for RC evaluation, developer acceptance, and publication
---

# Release BRD

## Goal

Let the developer evaluate release candidates in situ and decide when their
exact runtime bits are ready. Publishing an accepted RC must not start another
functional verification cycle.

## Requirements

| ID | Requirement |
|---|---|
| REL-01 | Complete source, platform, packaged-wheel, bootstrap, dashboard, provider, and operational checks as part of the RC process. Deploy the RC for developer evaluation. |
| REL-02 | Record explicit developer acceptance of the exact RC attempt, source commit, wheel digest, and decision date. A deployed RC presented for acceptance is assumed to have completed its RC checks; publication does not rerun them. |
| REL-03 | From RC promotion through final packaging, tagging, GitHub Release, and PyPI publication, run no additional functional tests. Do not require a stable acceptance receipt, runtime activation, hello-world run, consumer-agent run, or test-matrix rerun. |
| REL-04 | Stable packaging may change version and release metadata only in the approved runtime. Runtime changes require a new RC, evaluation, and developer acceptance. Reviewed release-tooling and documentation changes must not change the approved runtime source. |
| REL-05 | Retain immutable RC evidence and approval. Bind the final artifact hashes, commit, and tag to that decision. Publication may check identity, provenance, hashes, permissions, upload results, and public availability. These checks must not execute package functionality. |
| REL-06 | Publish the retained stable wheel and source distribution without rebuilding them. Preserve failed packaging attempts and refuse changed artifacts, approvals, or tags. Rejection consumes an RC or packaging-attempt identity, not another stable patch version. |
| REL-07 | Local runtime selection and restoration are independent of publication. Publishing one accepted release must not require activating it or prevent evaluating a later RC locally. |
| REL-08 | RC provider probes use a temporary repository without mandatory mail, Teams, calendar, or consumer-agent dependencies. Use agency Copilot when its source plugin is available, otherwise built-in Copilot. A supplied broken plugin or execution failure must not silently fall back. Prefer Luna with low reasoning effort for agency probes. |

## Acceptance Criteria

- A prepared RC with exact developer approval can be packaged, finalized, and
  published with every functional-check entry point disabled.
- Neither final preparation nor the automatic/manual PyPI workflow invokes
  source suites, platform matrices, installed-tool checks, or provider probes.
- Missing or mismatched approval, changed runtime source, altered artifact
  bytes, and substituted tags fail closed without silently retesting.
- The release process and generated report recommend publication, not a new
  acceptance run, after the approved RC is packaged.
- Publication and local restoration are reported and verified separately.

## Authority And Traceability

Developer decision, 2026-09-19: "there should be no additional functionality
testing moving from rc to publication. it should rely on testing being done as
part of the rc process and the developer acceptance."

This decision supersedes earlier requirements for independent stable acceptance
in [#511](https://github.com/johnshew/agents-live/issues/511) and historical
process notes. The lifecycle regression
`test_rc_approval_promotes_without_functional_retesting_and_retries` exercises
the promotion boundary; its synthetic artifacts are not live RC evidence.

See [the release process](development-release-process.md) and
[the release runbook](../.agents/release.md) for implementation and commands.