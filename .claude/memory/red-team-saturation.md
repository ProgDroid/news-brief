---
name: red-team-saturation
description: "When to stop spec red-team rounds — objections that migrate to a downstream consumer's concerns mean the spec has absorbed another sub-project's scope; cut scope, don't revise again"
metadata:
  node_type: memory
  type: feedback
  originSessionId: 3643de5b-dee7-49f4-8b2e-34e984499482
  modified: 2026-10-02T15:32:54.710Z
---

On the event-census spec (2026-09-28, 6 red-team rounds), the user asked "are these objections even relevant? ... are we just being pedantic at this point?" The honest split: rounds 1–4 found defects that would have broken the census itself (unstartable labeller, spend CHECK violation, wrong MDE arithmetic). Rounds 5–6 attacked the SCORING protocol, which belonged to sub-project 1 — the spec had grown to pre-register a downstream consumer's rules, so every revision gave the reviewer a new surface. The fix was a scope cut (rev 7 moved scoring into a mandatory checklist, §14, for SP1's spec), not a 7th round.

**Why:** red-team rounds saturate. Each revision that ADDS content creates new attack surface; a fresh reviewer will always find something in added pre-registration text.

**How to apply:**
- Track which layer each round's objections hit. When they migrate from "this breaks the thing being built" to "this affects how a later consumer uses it", stop and ask whether that content belongs in this spec at all.
- Separate what must be fixed before DATA COLLECTION from what must be fixed before ANALYSIS. Blind labels don't depend on scoring rules, so scoring pre-registration belongs to the scoring spec — hand it forward as a checklist.
- When rounds stop finding build-breaking defects, recommend stopping, and say so plainly with the evidence (which rounds found what). The user values the honest "diminishing returns" call over another round. Related: [[user-working-style]], [[subagent-review-stalls]].

**Confirmed again 2026-10-02** (census gap-check STOP, [[event-census-sp0]]): offered four paths, he
leaned "brainstorm" but said *"we've been going in circles for a while ... wary of simply continuing
to shift things around"* and asked for a recommendation. The one he took: the option that REUSES
the design as built (an existing band label + an existing §13 deferral), recorded as a ruling in
§11, with the downstream objection handed forward as a §14 checklist item — not a rev 8. **When he
signals loop fatigue, recommend decisively, and prefer the path that changes the least design;
an open-ended brainstorm is the circle he is warning about once the facts are measured.**
