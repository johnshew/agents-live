---
# Starter: scheduled agent - output is logged, no post-processor.
# Copy into `.claude/agents/` as <agent-name>.md. Good for reports,
# audits, and summaries you read from the logs (`agents-live logs`).
description: Scheduled analysis agent (log-only output). Never delegate to this agent.
disable-model-invocation: true
runtime: claude
mode: plan
schedule: "0 8 * * 1"
# Cap runaway output (always enforced; 1 MiB default):
# output-max-bytes: 65536
---

Describe the analysis or report. Output JSON if you plan to add a
post-processor later; prose is fine for log-only agents.
