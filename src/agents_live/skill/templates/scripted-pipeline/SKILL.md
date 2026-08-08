---
name: scripted-pipeline
description: Runs a deterministic scripted pipeline without a model. Use when creating scheduled local automation.
metadata:
  agents-live.schema-version: "1"
  agents-live.selector: "none"
  agents-live.mode: "plan"
  agents-live.schedule: "*/30 * * * *"
  agents-live.pre-processor: "scripts/prepare.py"
  agents-live.post-processor: "scripts/process.py"
---

Document what the pre-processor gathers and what the post-processor does.
