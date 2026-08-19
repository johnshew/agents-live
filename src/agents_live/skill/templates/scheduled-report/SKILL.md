---
name: scheduled-report
description: Produces scheduled analysis for the local run log. Use when creating unattended reports without side effects.
metadata:
  agents-live.schema-version: "2"
  agents-live.selector: "claude"
  agents-live.mode: "plan"
  agents-live.schedule: "0 8 * * 1"
---

Describe the analysis or report. Output JSON if you plan to add a
post-processor later; prose is fine for log-only runs.
