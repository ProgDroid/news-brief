# Grouping: choosing a better reference for the zone

**Status:** pre-registered 2026-11-26, before any candidate has labelled a pair. Bead
`news-brief-al7.7.2`, under storylines `news-brief-al7.7`. The script is
`scripts/grouping_reference_select.py`, committed with this file. **Nothing here changes after a
candidate's answer has been seen.**

## Why

The label audit (`2026-11-26-grouping-label-audit.md`) found that the Opus-low labels are not a
usable reference in the zone. The operator sided with the decider on 18 of the 33 pairs where it
and Opus-low disagreed. That audit's own rule says a better reference comes before any more decider
work. The operator chose this route: try Opus at higher effort first, and fall back to labelling a
whole zone by hand only if that fails.

## What is measured

- **The gold pairs, S:** the operator's answers in `audit_labels.jsonl` for the week ending
  2026-11-16, on the pairs the audit drew (33 audit pairs and up to 8 controls). Answers of
  `unclear` are left out. The audit scored 33 audit pairs and 7 controls, so S should have 40 pairs
  on the host. The script draws the audit set again with the audit's own code and refuses to run
  unless every drawn pair has an answer.
- **Candidates:** `claude-opus-5-5` at effort `medium` and at `high`. The request is the judge's
  pair request, unchanged apart from the effort: the same prompt, chunks of 20 pairs, a 16000-token
  limit and a 300 s timeout.
  - **Left out:** `low` is the reference that failed, and is reported as a baseline. `xhigh` and
    `max` would need a larger token limit and a longer timeout than the judge has, which makes them
    a different request, not only a different effort. The documentation also warns that `max` can
    overthink structured output.
- **What each candidate labels:** every zone pair of the week ending 2026-11-16, 209 on the host.
  Each is scored only on S. Labelling the whole zone keeps the gold pairs inside ordinary chunks
  rather than in chunks made only of hard pairs, and it leaves the winner's labels ready to use.
  The zone is the audit's: labelled pairs from 0.65 that rule A does not join. Pairs Opus-low called
  `unclear` are not in it.

## The rule, fixed now

- **Agreement:** a candidate's agreement is the number of pairs in S where its label equals the
  operator's. A candidate's `unclear`, or a pair it did not answer, counts as a disagreement.
- **Pass:** at least 85% of S agree, which is 34 of 40.
- **Choice:** among the candidates that pass, the one with the most agreements wins; a tie goes to
  the lower effort.
- **Then:** the chosen candidate also labels the zone of the week ending 2026-11-23, so that both
  dev weeks have a zone reference.
- **If none passes:** Opus is not used as the reference. The fallback is the operator labelling a
  whole zone by hand, which needs its own pre-registration.
- **Spend:** this is capped at $15 in total, across every file the script writes. The script
  checks the cap before each candidate's run on a week, so one run can go past it. My estimate is
  $1–3 per candidate per week. It is a rough figure from token counts, not a measurement.

**Why 85%:** S is the hardest kind of pair, and the controls showed the operator agreeing with a
joint model label 6 times in 7. A perfect reference would therefore score around 36 of 40, not 40.
With 40 pairs, a candidate whose true agreement is 90% reaches 34 about 90% of the time. At 80%
true agreement it reaches 34 about 29% of the time, and at 75% about 10%.

## Predicted before running

These come from the audit's report, with no new calls. They check that the script rebuilds S
correctly. If either is wrong, the script is wrong.

- **Opus-low on S:** 21 of 40 (15 audit pairs and 6 controls).
- **Haiku-high on S:** 24 of 40 (18 audit pairs and 6 controls).

Both are low by construction, because S was drawn from the pairs where those two disagreed.

## Also reported, not part of the rule

For each candidate:

- its agreement on the audit pairs, split by direction (decider joins that Opus-low called not the
  same, and the reverse), and on the controls;
- its `unclear` count in the zone;
- its agreement with Opus-low on the zone;
- what it cost.

For the chosen candidate on 23 Nov: the same zone counts.

## Commitments

- S, the candidates, the bar, the choice and the cap are not changed after a candidate's answer is
  seen.
- The output is numbers only.
- S is the only operator-labelled set, and choosing on it is choosing on dev data, with no test
  set behind it. The bar is the only safeguard. Whatever decider work follows on the new reference
  needs its own pre-registration and a test week.
