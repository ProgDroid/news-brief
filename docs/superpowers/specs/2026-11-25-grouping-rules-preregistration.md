# Grouping rules: pre-registration for a second week

**Status:** pre-registered 2026-11-25, before any data from the test week exists. Bead
`news-brief-8om`, under storylines `news-brief-al7.7`. The scorer is
`scripts/grouping_rules_check.py`, committed together with this file. **Nothing here changes
after a test-week number has been seen.** A change is a new pre-registration with a new week.

## Why

Calibration on the week ending 2026-11-23 recommended no space: no vector-alone threshold
reached weighted precision 0.95 (plan 1, Rollout step 7). A local re-analysis of that week's
labels found two things. Pairs from different outlets at similarity ≥ 0.80 were all judged the
same event. Pairs from one outlet were often recurring series with near-identical text and a
different event each day. The rules below were found by looking at those same 600 labels, and
several variants were tried, so their week-1 numbers are optimistic. This file fixes them in
advance and scores them once on a week they have never seen.

## What is scored

- **Space:** `modal:Octen/Octen-Embedding-8B@1024`. It was chosen on retrieval (nDCG@30 0.716,
  the best of four), which was not fitted to the pair labels.
- **Pairs:** `embed_calibrate`'s labelled pairs in that space's own neighbour pool, with
  `unclear` left out. Each pair is weighted by `embed_calibrate`'s band weights (thin bands
  excluded), the same weights the calibration uses. Weighted precision and recall are
  `embed_calibrate.weighted_prf`. Recall counts every pair labelled same, including
  same-outlet pairs.
- **Same outlet:** the two items' `outlet` fields in `snapshot.json` are equal.
- **Rule A:** join when the outlets differ and similarity ≥ **0.80**.
- **Rule B:** join when the outlets differ and similarity ≥ **0.75**. Week 1's grid search
  returned join 0.75, pair 0.75 and title 0.95. Because pair equals join, the title clause never
  adds a join, so rule B is the threshold alone.
- **Option 1:** join under rule A, or when similarity ≥ **0.65** and Haiku labels the pair
  `same`. Haiku gets `embed_calibrate`'s pair request unchanged (`pair_request`, the same
  prompt and tool, effort `low`); only the model differs (`claude-haiku-5-5`). A Haiku
  `unclear` is no join. Its prompt is not tuned on any labels.

## Pass criteria

| Rule | Passes when | Week-1 number (exploratory, same labels) |
|---|---|---|
| A | weighted precision ≥ 0.95 | 49 of 49 correct, wP 1.000, wR 0.138 |
| B | weighted precision ≥ 0.90 | wP 0.922, wR 0.281 |
| Option 1 | weighted precision ≥ 0.90 **and** weighted recall ≥ 0.60 (the spec's bar) | not yet measured |

- Rule A also reports the Wilson 95% lower bound on correct joins out of all its joins. Even at
  50 of 50, that bound is about 0.93.
- Rule B has no recall criterion. Its option accepts low recall by design, and the operator
  decides whether that is enough.
- The Haiku run on week 1 is a first look only. It is fair, because the prompt is not tuned, but
  the test week decides.

## The test week

- **Week ending 2026-11-16**, that is items in [2026-11-09, 2026-11-16) UTC. It shares no
  items with the week ending 2026-11-23.
- `embed_calibrate` runs with that one space, 600 pairs, and the judge `claude-opus-5-5` at
  effort `low`, as on week 1.
- Then `grouping_rules_check.py` scores it once.

## Commitments

- The thresholds, model, prompt, weighting and pass criteria above are not changed after a
  test-week number is seen.
- All three results are reported, a failure as a failure.
- What each outcome means for the options is the operator's call, made after the result. This
  file does not pre-decide it.
