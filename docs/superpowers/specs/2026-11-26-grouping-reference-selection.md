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

## Results (2026-11-26)

Run once on the host and finished in that run. The output, verbatim apart from the first line:

```
week=2026-11-16 S=40 pass at 85%
opus-low (baseline): agree=21/40 (audit: decider joins, Opus-low not 13/24; Opus-low same, decider not 2/9; controls 6/7)
claude-haiku-5-5@high (baseline): agree=23/40 (audit: decider joins, Opus-low not 11/24; Opus-low same, decider not 6/9; controls 6/7)
claude-opus-5-5@medium: agree=26/40 (audit: decider joins, Opus-low not 13/24; Opus-low same, decider not 7/9; controls 6/7) zone=209 unclear=2 agree_with_opus_low=183/209 usd=0.3872
claude-opus-5-5@high: agree=25/40 (audit: decider joins, Opus-low not 13/24; Opus-low same, decider not 6/9; controls 6/7) zone=209 unclear=3 agree_with_opus_low=185/209 usd=0.4756
chosen: none
  note: no candidate reaches the bar: the fallback is the operator labelling a whole zone (its own pre-registration)
```

**Reading, as pre-registered:** medium agrees on 26 of 40 and high on 25, against a bar of 34.
**Neither passes, so Opus is not used as the zone reference** and the fallback applies: the operator
labels a whole zone by hand, under its own pre-registration. Nothing was run on 23 Nov. The two
runs cost $0.86 in total, under the $1–3 per candidate estimate and far under the $15 cap.

**The predictions:**

- **Opus-low, 21 of 40: correct.** S has 40 pairs, split 24, 9 and 7 as in the audit, so the script
  rebuilt S as the audit drew it.
- **Haiku-high, 24 of 40: wrong by one (23).** The prediction's generator was wrong, not the
  script. The audit scores the decider in two classes, `same` or not, so a decider `unclear` on a
  pair the operator called different counted as agreeing. This script counts any label that is not
  equal to the operator's, `unclear` included, as a miss, as the rule fixes for the candidates. The
  audit's H=18 splits as 11 on the 24 pairs (the operator's `same`) and 7 on the 9 (the operator's
  `different`, because Opus-low gets the other 2). This run gives 11 and 6. So one of those 9 pairs
  carries a decider label that is neither `same` nor `different`, which is `unclear`. That is
  inferred from the arithmetic; the decider's file on the host would confirm it. The baselines are
  not part of the rule, and the result does not depend on it.

**What qualifies it (none of this changes the reading):**

- **Effort barely moves Opus.** Medium and high agree with Opus-low on 183 and 185 of the 209 zone
  pairs, about 88%. On the 24 pairs where the decider joins and Opus-low does not, all three Opus
  efforts score exactly 13, which is the number the operator called different. That fits Opus
  calling all 24 different at every effort, though a mix of changes in both directions would give
  the same count. On the 9 reverse pairs, medium gains (7 against Opus-low's 2) and high gains
  less (6).
- **So the disagreement looks like where the line is drawn, not noise that more thinking removes.**
  The operator saw the prompt's definition at the audit, and still called 11 of the 24 the same
  event where Opus, at every effort, did not. The cause is not measured here.
- **The bar was a long way off.** Even if every zone `unclear` fell in S and were counted as an
  agreement, medium would reach 28 (26 + 2) and high 28 (25 + 3), both short of 34.
- **S is small and biased.** It is the 40 pairs where two models disagreed, plus the controls. A
  candidate could do better on the zone as a whole than on S. That is why the rule scored only on
  S, and it is also why the fallback labels a whole zone.

**Consequences:**

- The zone still has no usable reference on either dev week. Every zone number scored against Opus
  labels stays suspect, as the audit concluded.
- The candidates' labels are kept on the host (`reference-claude-opus-5-5-<effort>.jsonl` in the 16
  Nov folder). They are not a reference, but they cost nothing more to keep.
- Next is the fallback's pre-registration: the operator labels a whole zone. Its 40 audit answers
  can be reused, so the 16 Nov zone needs about 170 more answers.
