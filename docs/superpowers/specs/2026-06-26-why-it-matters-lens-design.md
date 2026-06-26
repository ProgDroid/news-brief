# "Why it matters" lens on TOP STORIES — design

**Date:** 2026-06-26
**Status:** Approved (brainstorm), pending implementation plan
**Backlog origin:** External geo-dashboards borrow-backlog item #5a (Realpolitik "fallout
prediction" prompt structure). See `external-geo-dashboards-backlog` memory.

## Problem

The daily brief leads its interpretation with forward-looking material, but it does not
explicitly demand that each significant development answer a sharp "why does this matter."
Realpolitik (MIT, idea-borrowable) structures its analysis as three beats — Context (what
people don't already know) / Stakes (what happens next + timeframe) / Connection (how it
touches daily life). Those beats are a good analytical checklist for sharpening our "so what."

## Design decisions

**Form — invisible lens, not a visible template.** The brief's personality is concise and
anti-formulaic; `SYSTEM_PROMPT` already teaches analytical *standards* ("lead with
forward-looking material", "attribute framing to its vantage", "don't echo priced headlines")
rather than imposing output templates. The lens is the same species of instruction: a standard
for *how* to explain significance, woven into prose. **No labeled "Context / Stakes /
Connection" headers appear in the output.**

**Scope — TOP STORIES only.** The three-beat depth applies to the `🌍 TOP STORIES` bullets,
where the "so what" matters most. Dynamic topic sections, MACRO SIGNAL, etc. are unchanged.

**Third beat re-pointed to market transmission.** Realpolitik's "Connection (how it touches
daily life)" is written for a mass audience. Our reader "tracks geopolitics as a leading
indicator for markets, not as an end in itself", so the third beat becomes the **market
transmission mechanism** — which asset / rate / currency the development pushes and through
what channel — *not* a human-interest angle.

**Overlap guard — one dedup clause, not a section reframe.** Two beats overlap existing
sections:
- "Stakes (next move + timeframe)" overlaps `👁 WATCH / FORWARD`.
- "Connection (markets)" overlaps `📌 POSITION SIGNALS` + portfolio scoring.

To prevent the model repeating a hot situation's forward-look both in-line and again in
WATCH/FORWARD, add a single anti-repetition clause to the WATCH/FORWARD instruction: surface
forward catalysts *not already covered in-line above*. This extends the brief's existing "do
not pad or repeat" ethos by one section; it is **not** a behavioral reframe of what
WATCH/FORWARD is (rejected option: "WATCH/FORWARD becomes purely net-new catalysts", which
would force the model to track everything it already said — fragile, token-hungry).

The genuine net-new value of the lens, after reconciling overlap:
1. **Context** — the non-obvious thing the reader doesn't already know. The prompt does not
   explicitly demand this today; biggest gain.
2. **Stakes** — next move + rough timeframe, stated *in-line per story* (not only aggregated
   in WATCH/FORWARD).
3. **Connection** — the market transmission mechanism, distinct from POSITION SIGNALS' held-
   position scoring.

## Changes

Pure prompt edits to `brief.py`. No code logic, no new functions, no data, no schema changes.

1. **`SYSTEM_PROMPT` (~brief.py:1654-1672)** — append a short paragraph teaching the lens: when
   a development is genuinely significant, the "why it matters" should cover, woven into prose
   and only where warranted, (a) the non-obvious context the reader lacks, (b) the likely next
   move and a rough timeframe, (c) the market transmission — which asset/rate/currency it moves
   and through what mechanism. Explicitly: do not label these beats, do not force all three on
   an item that doesn't warrant it, stay within the no-padding rule.

2. **`build_daily_prompt` (~brief.py:1779-1801)** —
   - TOP STORIES instruction gains a line pointing the lens at those bullets (tight prose, not
     a checklist).
   - WATCH/FORWARD instruction gains the one-clause dedup nudge.

## Testing

`tests/test_signals.py` and `tests/test_commands.py` already assert on `SYSTEM_PROMPT`
substrings. Add assertions that the lens language is present in `SYSTEM_PROMPT` and that the
TOP STORIES / WATCH-FORWARD guidance is present in a built daily prompt — a cheap regression
guard, consistent with how perspective-tagging was TDD'd. No behavioral test (output is
model-generated prose; we guard prompt content, not model output).

## Explicitly NOT doing (YAGNI)

- Visible "Context / Stakes / Connection" labels in the output.
- A new dedicated section.
- Applying the lens to dynamic topic sections / MACRO / all sections.
- Any WATCH/FORWARD behavior reframe beyond the single dedup clause.
- Any code, function, data, or schema change.
- Severity-weighted retention (backlog item #5b — separate item).
