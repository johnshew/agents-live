---
title: Choose a model for an Agents Live definition
description: Select models by workload and validate unattended defaults with provider telemetry
ms.date: 08/12/2026
ms.topic: concept
---

# Choose a model

This guidance is current as of August 12, 2026. Provider availability,
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
