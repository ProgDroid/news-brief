---
name: event-census-sp0
description: "KB sub-project 0, the event census — built and pushed, host runbook in progress; a CODE FREEZE on census.py / census_metrics.py / census_* schema runs from runbook step 3 to step 10, and 0017 is applied on the host"
metadata:
  node_type: memory
  type: project
  originSessionId: 3643de5b-dee7-49f4-8b2e-34e984499482
  modified: 2026-10-02T15:54:12.754Z
---

**State as of 2026-09-30.** Built and pushed (code through `3428111`, main at `608cbae`,
CI green). Host runbook `docs/2026-10-01-host-runbook-census.md`: steps 1 (deploy, `0017_census`
confirmed applied) and 2 (c439ade deploy timestamp confirmed, operator holds it) DONE on
2026-09-30. Step 3 (`census_prepare`) refuses before **2026-10-01**. Steps 4–6 (env, grants,
start labeller) do not depend on step 3 and may run before it; step 7's phone save check needs a
prepared window. Epic `news-brief-vd9` stays open until the census is labelled; open follow-up
`news-brief-vd9.12` (P3, label.js keyboard after stop).

**2026-10-02: step 3 REFUSED — gap check STOP (67.1% at 6h).** Breakdown + 12h/24h shares in
spec §11; blocked on bead `news-brief-vd9.13` (operator ruling on window length / headline).
24h passes on share (39.6%) but is infeasible as designed (11-day block, no strata, ~4× labour).
Do not re-run the decomposition — it is recorded. **RULED same day: keep 6h, headline narrowed
to within-6h** (spec §11, §14 item 12). Built, committed and pushed 2026-10-02:
`CENSUS_GAP_RULING=within_6h_only` on `census_prepare`; host must deploy + repin
`LABELLER_IMAGE` BEFORE step 3 (runbook step 3 preamble). Don't reopen the window length.
**STEP 3 DONE 2026-10-02** (redeployed + repinned first): block 2026-09-18 → 2026-09-29, 16 windows
+ repeat, 3 skipped, `within_6h_only by operator ruling` (2549 pairs, 52 windows); `vd9.13` closed.
**The FREEZE IS NOW IN FORCE** and the retention hold is armed. Next: step 7 (`/label`).
**`c439ade` date CORRECTED 2026-10-02:** recorded 2026-09-30T15:30, but `feed_polls` shows the code running by 2026-09-26 10:30:12Z, so the block DOES straddle it (spec §11 correction). Only the readout's straddle line and by-half split read it. The stored `census_block.c439ade_deployed_at` was CORRECTED by the operator the same day.

**Why this matters to any future session:**
- **FREEZE: from runbook step 3 to step 10, no semantic change to `census.py`,
  `census_metrics.py` or the `census_*` schema.** The page runs a pinned image
  (`LABELLER_IMAGE` digest) while `/label` and the readout's callers run the main image; a change
  makes them disagree mid-census. Queue census fixes as beads until step 10.
- **0017 is applied on the host**, so any census schema change is a NEW migration (0018), never an
  in-place edit.
- **Do not land `news-brief-115`** (removes `scripts/` from the image) before census step 10:
  steps 5 and 8 run `scripts/census_grants.py` / `scripts/census_report.py` from the image.

**Design lesson worth reusing (D1a, 2026-09-29):** a server-side write guard keyed on a
time-dependent "what is being served" rejected a legitimate first action when the answer changed
under an already-loaded page (the repeat became eligible at the operator's usual labelling
hour). Fix: make serving stable — stamp a claim at serve time (`serve_page` computes the task and
records `open` in one transaction) and let the scheduler honour it. A guard is only as stable as
the function it asks.

**How to apply:** before touching census code, check the runbook Status table / ask how far the
host is. Record and rulings: `docs/2026-09-29-event-census-implementation-record.md`. Related:
[[red-team-saturation]], [[parallel-implementers-share-test-db]], [[postgres-role-grant-gotchas]].
