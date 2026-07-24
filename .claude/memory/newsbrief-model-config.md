---
name: newsbrief-model-config
description: Sonnet-5 swap + NEWSBRIEF_MODEL override + explicit per-call thinking config; the adaptive-thinking-by-default gotcha
metadata: 
  node_type: memory
  type: project
  originSessionId: 40aa02f5-7c80-41e3-83b3-331d1377553b
---

2026-07-02 BUILT+PUSHED (e118a76 → origin/main, Docker deploy triggered; 551 tests): the three Sonnet constants moved `claude-sonnet-4-6` → `claude-sonnet-5` (common.MODEL, brief.SIGNALS_MODEL, claim_verify.VERIFY_MODEL). Haiku constants untouched (brief_memory.RECONCILE_MODEL, backtest/scorer_llm.py). Single-knob `NEWSBRIEF_MODEL` env override, default `claude-sonnet-5`, read at IMPORT TIME by all three Sonnet constants — set it on the deploy host to swap models with no code change/redeploy (host-config pattern like [[live-state-on-deploy-host]]).

**Why (the non-obvious gotcha):** on `claude-sonnet-5`, OMITTING `thinking` runs *adaptive thinking* (Sonnet 4.6 ran thinking-OFF when omitted), and thinking tokens count against `max_tokens` → truncation on tight/forced-tool calls (the recurring [[signals-parse-error-is-truncation]] failure). Before this change every call omitted `thinking`, so the model bump silently flipped all of them on.

**SECOND budget-eater found live 2026-07-02 — the tokenizer, independent of thinking.** Sonnet 5 also emits ~30% MORE tokens for the same content than 4.6 (new tokenizer), so a fixed `max_tokens` sized for 4.6 can truncate even with thinking DISABLED. The signals forced-tool call (thinking off, `max_tokens=2048`) truncated on the first signal-rich brief → `emit_signals` returned an incomplete tool input → `missing 'signals' list` parse_error, signals wiped that day. Fix dfabcd1: `SIGNALS_MAX_TOKENS 2048→8192` + log stop_reason before parse. LESSON: after a model swap, both levers shrink effective room — audit EVERY tight fixed `max_tokens` (not just the ones you flipped thinking on), and prefer a named constant with headroom over an inline literal. Full incident: [[signals-parse-error-is-truncation]] recurrence #4.

**How to apply:** set `thinking` EXPLICITLY on every Anthropic call — never rely on the per-model default (it changes across model bumps). Rule of thumb: forced-tool / tight-budget extraction → `{"type":"disabled"}`; prose synthesis or a reasoning judge with headroom → `{"type":"adaptive"}`. Current per-call config: brief synthesis (submit_batch, 16384 batch, no HTTP timeout) = adaptive; claim verify (grounding judge) = adaptive + VERIFY_MAX_TOKENS raised 4096→8192; signals / in_depth / trading (2048 forced-tool or raw-JSON) = disabled.

**Watch on enable:** claim_verify pairs adaptive thinking with a forced `tool_choice:{type:tool}`. First-party API allows this combo (only Bedrock forbids it), but verify is flag-gated OFF + fail-safe — when you set `CLAIM_VERIFY_ENABLED=1`, check the first `verification-{day}.json`; if it 400s, switch that call's `tool_choice` to `auto` or drop thinking. Response parsing is already thinking-safe (fetch_batch_results joins only `type=="text"`; claim_verify filters the `emit_claim_checks` tool_use block), so thinking blocks won't corrupt extraction.
