# agents-live: Triggered Agents

Create, test, activate, and tear down scheduled (cron) and file-watch
triggered agents. Supports `claude`, `copilot`, `agency claude`,
`agency copilot`. Agents can have multiple triggers (cron + watcher,
multiple watch paths) - each fires the pipeline independently.
Pre-handlers run before the agent for gating and enrichment.

Full instructions: `.claude/skills/agents-live/SKILL.md`

## Layout

- `Agents/` - triggered agent definitions with frontmatter config
- `Agents/docs/` - design docs for multi-stage workflows
- `Agents/handlers/` - deterministic scripts run by triggered agents
- `Agents/logs/` - triggered agent execution logs
- `.claude/skills/agents-live/docs/approach.md` - design notes

## Key Learnings: Model Selection (May 2026)

Under June 1, 2026 usage-based billing (1 AI credit = $0.01, token-based):

| Model | Input/MTok | Cached/MTok | Output/MTok | Typical run (~52k fresh, 121k cached, 1.2k out) |
|-------|-----------|------------|------------|------------------------------------------------|
| Claude Haiku 4.5 | $1.00 | $0.10 | $5.00 | ~7 credits ($0.07) |
| Gemini 3.5 Flash | $1.50 | $0.15 | $9.00 | ~11 credits ($0.11) |
| Claude Sonnet 4.6 | $3.00 | $0.30 | $15.00 | ~21 credits ($0.21) |

Gemini 3.5 Flash dominates for agents-live workloads:
- Significantly outperforms Sonnet 4.6 on coding/agentic benchmarks (MCP Atlas 83.6% vs 69.5%, OSWorld 78.4% vs 72.5%, ARC-AGI-2 72.1% vs 58.3%) at half the credit cost.
- Massive quality upgrade over Haiku 4.5 for only ~4 credits/run more.
- Source: https://deepmind.google/models/gemini/flash/#performance

Note: Before June 1, the old premium-request multiplier system makes Gemini 3.5 Flash 14x (wildly overpriced). After June 1, token-based billing makes it the clear best value for agentic coding tasks.

