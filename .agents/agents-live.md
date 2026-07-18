# agents-live: Triggered Agents

Create, test, activate, and tear down scheduled (cron) and file-watch
triggered agents. Supports `claude` and `copilot` out of the box;
installed plugins can register additional adapters (e.g. `agency claude`,
`agency copilot`). Agents can have multiple triggers (cron + watcher,
multiple watch paths) - each fires the pipeline independently.
Pre-handlers run before the agent for gating and enrichment.

Full instructions: `.claude/skills/agents-live/SKILL.md`

## Layout

- `Agents/` - triggered agent definitions with frontmatter config
- `Agents/docs/` - design docs for multi-stage workflows
- `Agents/handlers/` - deterministic scripts run by triggered agents
- `Agents/logs/` - triggered agent execution logs
- `.claude/skills/agents-live/docs/approach.md` - design notes

## Key Learnings: Model Selection (July 2026)

GitHub Copilot usage-based billing converts token cost to AI credits at
1 credit = $0.01. The estimates below use a representative agents-live run of
52k fresh input, 121k cached input, and 1.2k output tokens. They exclude tool
fees and Anthropic cache writes.

| Model | Intended tier | Input/MTok | Cached/MTok | Output/MTok | Typical run |
|-------|---------------|-----------:|------------:|------------:|------------:|
| Gemini 3 Flash | Lightweight | $0.50 | $0.05 | $3.00 | ~4 credits |
| GPT-5.6 Luna | Lightweight | $1.00 | $0.10 | $6.00 | ~7 credits |
| Gemini 3.5 Flash | Lightweight | $1.50 | $0.15 | $9.00 | ~11 credits |
| Claude Sonnet 5 | Versatile | $2.00 | $0.20 | $10.00 | ~14 credits |
| Gemini 3.1 Pro | Powerful | $2.00 | $0.20 | $12.00 | ~14 credits |
| GPT-5.6 Terra | Versatile | $2.50 | $0.25 | $15.00 | ~18 credits |
| Claude Opus 4.8 | Powerful | $5.00 | $0.50 | $25.00 | ~35 credits |
| GPT-5.6 Sol | Powerful | $5.00 | $0.50 | $30.00 | ~36 credits |
| Claude Fable 5 | Powerful | $10.00 | $1.00 | $50.00 | ~70 credits |

Claude Sonnet 5 is the current default for unattended, general-purpose
agents-live work. It targets coding and long-running agents, and its introductory
price makes it cheaper than Sonnet 4.6 while providing a 1M-token context window.
The price rises to $3 input, $0.30 cached input, and $15 output per MTok on
September 1, 2026, increasing the representative run to about 21 credits.

Choose by workload rather than benchmark winner:

* Use Gemini 3 Flash or GPT-5.6 Luna for simple, repetitive, well-validated tasks
* Use Sonnet 5 for the default balance of autonomy, coding quality, and cost
* Canary Gemini 3.5 Flash for bounded implementation work before using it unattended
* Try GPT-5.6 Terra when stronger reasoning is worth its higher cost
* Reserve Opus 4.8, GPT-5.6 Sol, or Fable 5 for difficult or high-stakes work

Developer feedback on Hacker News and the Google and OpenAI developer forums is
mixed enough to reject the previous single-model recommendation. Gemini 3.5
Flash receives praise for speed and implementation, but recurring reports cite
thinking loops, context churn, and coding regressions. GPT-5.6 is strong on
coding-agent benchmarks, but its July launch also produced reports of metering,
model-routing, and integration defects. Treat those reports as operational risk
signals, not controlled evaluations, and validate models on this repository's
real tasks before changing unattended defaults.

Sources: [GitHub Copilot model pricing](https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing),
[GitHub Copilot model guidance](https://docs.github.com/en/copilot/reference/ai-models/model-comparison),
[OpenAI GPT-5.6](https://openai.com/index/gpt-5-6/),
[Anthropic model overview](https://platform.claude.com/docs/en/about-claude/models/overview),
[Google Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing),
[Hacker News model discussions](https://hn.algolia.com/?q=agentic%20coding%20models),
[OpenAI Developer Community](https://community.openai.com/search?q=GPT-5.6%20coding),
and [Google AI Developers Forum](https://discuss.ai.google.dev/search?q=Gemini%203.5%20Flash%20coding).

