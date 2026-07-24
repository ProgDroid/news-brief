---
name: tradingagents-patterns-to-mine
description: "TauricResearch/TradingAgents evaluated 2026-06-24 — NOT adopted; mine two patterns for a separate later spec (trading/signals subsystem, NOT enrichment)"
metadata: 
  node_type: memory
  type: project
  originSessionId: 1d521749-8691-4a25-9c31-095802fc2144
---

**2026-06-24:** evaluated `github.com/TauricResearch/TradingAgents` (88k★, Apache-2.0, LangGraph multi-agent, multi-LLM incl. Anthropic, v0.3.0) while hunting bigdata.com alternatives. **DECIDED: do NOT adopt the framework** — it's a *decision* framework (analyst→bull/bear debate→trader→risk-manager→buy/sell), i.e. a heavier duplicate of the existing lean `trading.py`/signals pipeline; it's research-grade, expensive (many LLM calls per decision), and **not self-improving** so it doesn't fix the stated blocker (no self-improving mechanism + thin test data). Borrow, don't adopt.

**MINE these two for a SEPARATE later brainstorm/spec — this is "track C", the trading/signals subsystem, NOT the enrichment work in [[bigdata-mcp-enrichment-brainstorm]]:**
1. **Bull/bear adversarial debate + a dedicated risk-manager stage** — a signal *generation + vetting* enhancement to `trading.py`/signals (improves on single-pass "ask LLM → emit signals JSON"). This is the main idea worth lifting.
2. **Its Alpha Vantage integration** — use as a *code reference* when building the AV adapter for the [[av-sentiment-backtest-validated]] backtest and the AV live-fallback provider.

Keep it parked as its own spec (one subsystem per spec); do NOT braid it into the MCP-enrichment design.
