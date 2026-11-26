# Grouping decider: pre-registration for a third week

**Status:** pre-registered 2026-11-26, before any of the candidates below has been run, and
before the test week has labels. Bead `news-brief-k8s`, under storylines `news-brief-al7.7`.
It follows `2026-11-25-grouping-rules-preregistration.md`. There, option 1 passed its test week
(wP 0.902, wR 0.633) but failed recall on the other week (0.489). The loss was the decider's:
a perfect judge from 0.65 up would have reached 0.834. **Nothing here changes after a dev or
test number has been seen.**

## What stays fixed

Everything in option 1 except the decider: space `modal:Octen/Octen-Embedding-8B@1024`; rule A
(different outlets, similarity ≥ 0.80) joins without asking; the decider is asked about every
labelled pair from 0.65 up that rule A does not join; a decider `same` joins, `different` or
`unclear` does not. The weighting, the metrics and the bar (wP ≥ 0.90 and wR ≥ 0.60) are
`embed_calibrate`'s and `grouping_rules_check`'s. The decider gets `embed_calibrate`'s pair
request, the prompt and tool unchanged. Only the model and the effort vary.

## Candidates

| Model | Effort |
|---|---|
| `claude-haiku-5-5` | low (the decider that was tested; its saved labels are reused) |
| `claude-haiku-5-5` | medium |
| `claude-haiku-5-5` | high |
| `claude-sonnet-5-5` | low |
| `claude-sonnet-5-5` | medium |

## Dev weeks (tuning data, not evidence)

The weeks ending 2026-11-23 and 2026-11-16. Both have been looked at, so a result on them is
only used to choose. Scored by `scripts/grouping_decider_dev.py`, which also reports:

- the measured cost per pair (the saved replies' spend over the pairs they answered), and
- **an estimated monthly cost**: cost per pair × the week's neighbour pairs from 0.65 up × 52 / 12.
  It is an upper bound, because a live item is compared with its candidate groups, not with
  every neighbour. Week 1 had 10,708 such pairs.

## Selection, mechanical

1. **Eligible:** wP ≥ 0.90 on both dev weeks, and an estimated monthly cost of at most **$10**
   on each.
2. **Chosen:** among the eligible candidates that also reach wR ≥ 0.60 on both dev weeks, the one
   with the lowest cost per pair, pooled over both weeks. Ties go to the lower effort.
3. **If none is chosen,** stop. There is no test week, and the operator gets every candidate's
   numbers. That includes a candidate that passes the bar but costs more than $10: running it on
   the test week is the operator's call.

## The test week

- **Week ending 2026-11-09,** that is items in [2026-11-02, 2026-11-09) UTC. It shares no items
  with the dev weeks.
- `embed_calibrate` runs with the one space, 600 pairs, and the judge `claude-opus-5-5` at
  effort `low`, as before.
- Then `grouping_decider_dev.py` scores the chosen candidate once on it.
- **Passes** when wP ≥ 0.90 and wR ≥ 0.60.

## Commitments

- The candidates, the cap, the selection rule and the pass criterion are not changed after a dev
  or test number is seen.
- Every candidate's dev numbers and the test result are reported, a failure as a failure.
- What the outcome means for the spec is the operator's call.

## Results (appended 2026-11-26; nothing above was changed)

`grouping_decider_dev.py --dev` on the host. **Chosen: none.** Under rule 3 there is no test week,
and the week ending 2026-11-09 was not labelled.

| Decider | Week ending 2026-11-23 | Week ending 2026-11-16 | Est. $/month |
|---|---|---|---|
| Haiku low | agree 158/200, 110 of 117 joins correct, wP 0.949, wR 0.489: FAIL | agree 173/209, 122 of 132, wP 0.902, wR 0.633: PASS | 1.39 / 1.69 |
| Haiku medium | agree 166/200, 122 of 131, wP 0.895, wR 0.626: FAIL | agree 177/209, 139 of 162, wP 0.781, wR 0.788: FAIL | 2.50 / 2.70 |
| Haiku high | agree 183/200, 133 of 140, wP 0.945, wR 0.744: PASS | agree 171/209, 139 of 167, wP 0.739, wR 0.788: FAIL | 3.29 / 3.29 |
| Sonnet 5.5 low | agree 166/200, 111 of 115, wP 0.934, wR 0.572: FAIL | agree 179/209, 129 of 144, wP 0.824, wR 0.696: FAIL | 27.88 / 26.64 (over cap) |
| Sonnet 5.5 medium | agree 168/200, 113 of 116, wP 0.941, wR 0.598: FAIL | agree 183/209, 131 of 144, wP 0.853, wR 0.710: FAIL | 27.88 / 26.63 (over cap) |

Cost per pair: Haiku 0.0000301, 0.0000538 and 0.0000710 on week 1 (low, medium, high); Sonnet
0.0006009 on week 1 at both efforts.

**Reading (the operator's call follows).**

- No candidate passes both weeks. The two weeks pull in opposite directions. On week 1 the
  deciders are too cautious, so recall is short. On week 2, more effort makes them join more,
  and precision falls from 0.902 (Haiku low) to 0.739 (Haiku high).
- Sonnet would be over the cap even if it passed, and on week 2 it is no more precise than Haiku.
- **An inference from cost, not a measurement:** Sonnet's two efforts cost the same per pair,
  to four significant figures on week 1. Sonnet low costs almost exactly 20 times Haiku low,
  which is the ratio of their prices. So adaptive thinking probably did not engage at either
  Sonnet effort, and "Sonnet medium" did not test more reasoning. Haiku's cost rises with
  effort, so its efforts did engage.
- The truth labels are Opus at effort `low`. On week 2, Haiku high agrees with them less than
  Haiku low does (171 against 173 of 209) and joins 35 more pairs. Nothing here separates
  "Haiku is wrong" from "the Opus labels are noisy" on those pairs.
