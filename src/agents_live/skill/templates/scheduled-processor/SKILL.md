---
name: scheduled-processor
description: Produces a scheduled report and passes it to a deterministic processor. Use when creating unattended report automation.
metadata:
  agents-live.schema-version: "2"
  agents-live.selector: "claude"
  agents-live.mode: "plan"
  agents-live.schedule: "0 6 * * *"
  agents-live.post-processor: "scripts/process.py"
  agents-live.output-schema: '{"required":["summary"],"type":"object"}'
  agents-live.output-provenance: "strict"
---

Describe the job. Tell the provider exactly what JSON to produce for the
post-processor, and nothing else.
