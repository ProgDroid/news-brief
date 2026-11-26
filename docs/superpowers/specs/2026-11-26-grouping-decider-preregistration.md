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
