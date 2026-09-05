---
title: Choose a model for an Agents Live definition
description: Select models by workload and validate unattended defaults with provider telemetry
ms.date: 2026-09-05
ms.topic: concept
---

# Choose a model

This guidance is current as of September 5, 2026. Provider availability,
retirements, and prices change. Check the official
[GitHub Copilot model list](https://docs.github.com/en/copilot/reference/ai-models/supported-models),
[Copilot pricing reference](https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing),
[Anthropic model overview](https://platform.claude.com/docs/en/about-claude/models/overview),
[OpenAI models](https://platform.openai.com/docs/models), and
[Google Gemini models](https://ai.google.dev/gemini-api/docs/models) before
changing an unattended selector.

## Agents Live recommendations

- **Lightweight, repetitive, strongly validated work:** choose a fast,
  economical model and keep deterministic validation in the processor.
- **General-purpose unattended work:** choose a versatile coding model with
  reliable tool use and enough context for the repository.
- **Difficult or high-stakes work:** use a stronger reasoning model, narrower
  permissions, and explicit human review.

These are Agents Live recommendations, not provider guarantees. Canary a model
on bounded runs before changing a scheduled default. Repository-specific
evidence, including completion quality, retries, latency, and operator review,
should outweigh generic benchmarks.

## OpenAI Codex

Install the native [Codex CLI](https://learn.chatgpt.com/docs/codex/cli), run
`codex` to sign in, and verify the credential with `codex login status`.
On Linux and WSL:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

On native Windows:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"
```

Agents Live reuses that authentication but starts each run with user and
repository configuration, instructions, rules, hooks, and undeclared MCP
servers disabled.

Use `codex` for the CLI default model or `codex/<model>` for an explicit model.
Codex validates model availability. Supported reasoning effort suffixes are
`low`, `medium`, `high`, and `xhigh`, for example `codex/gpt-5.5:high`; the
selected model may support only a subset.

The Codex provider supports `plan` and `write`. Plan runs use the read-only
sandbox. Write runs use workspace-write with ambient `/tmp` and `$TMPDIR`
write grants removed. Named stdio and streamable HTTP MCP servers are supported
through `agents-live.mcps`; each selected server is required and pre-approved,
while undeclared servers remain unavailable. Codex enforces declared output
schemas during generation. Codex MCP credentials must use environment-variable
indirection: `env_vars` for stdio, or `bearer_token_env_var` and
`env_http_headers` for HTTP. Literal MCP `env` and header values are rejected
so secrets do not enter process arguments or transcripts.

Codex does not currently support Agents Live `pipeline` mode or
`agents-live.allow-tools`, because Codex CLI 0.153.4 does not expose the
tool-denial controls needed to prove those restrictions. The CLI also exposes
no direct turn-limit option, so Agents Live enforces the definition timeout
but not a separate Codex turn limit. Codex JSONL reports token usage but no
completed-run currency value, so the dashboard does not show a list cost for
Codex runs.

## Completed-run cost

Agents Live uses provider telemetry for completed runs. Claude's reported
`total_cost_usd` becomes `list_cost_usd`. Copilot's AI credits are retained as
`ai_credits` and converted to `list_cost_usd` using GitHub's fixed
1 credit = $0.01 list-price equivalent. The dashboard labels this value
**List cost**.

List cost is not a known invoice charge. Subscription allowances, promotions,
and negotiated discounts can change what an account pays. Agents Live does not
reconstruct completed-run cost from this guide and does not use a packaged
pricing catalog for forecasting.
