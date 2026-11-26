# Grouping: the operator labels the zone

**Status:** pre-registered 2026-11-26, before any pair has been labelled under it. Bead
`news-brief-al7.7.3`, under storylines `news-brief-al7.7`. The script is
`scripts/grouping_zone_labels.py`, committed with this file. **Nothing here changes after an answer
has been seen**, except by the one route stated under "After stage 1".

## Why

The zone (labelled pairs from 0.65 that rule A does not join) has no usable reference. The audit
(`2026-11-26-grouping-label-audit.md`) ruled Opus-low out, and the reference selection
(`2026-11-26-grouping-reference-selection.md`) found Opus at medium and high no better: 26 and 25 of
40 on the operator's answers, against a bar of 34. That document's fallback is this one: the
operator labels a whole zone by hand.

It also left a question open. On the 24 audit pairs where the decider joins and Opus-low does not,
the operator said "same" on 11, and every Opus effort scored as if it said "different" on all 24.
Either the operator and Opus draw the line of "one event" in different places, or the operator's
"same" there meant something broader, such as the same continuing story. The operator chose
(2026-11-26) to see that answered before labelling the rest of the zone.

## What the operator answers

Every pair is shown blind (the two texts only), one at a time, in the terminal on the host, and
answered with one of:

- **`e`, one event:** the judge prompt's own definition, shown verbatim: one happening (one
  decision, one attack, one release of figures), not merely the same topic, country or company.
- **`c`, the same continuing story, not one event:** for example a decision and the reactions to
  it, or a later stage of the same story.
- **`d`, different.**
- **`u`, unclear:** only when the text cannot tell.

**The reference:** `e` is the same; `c` and `d` are different; `u` is left out. Groups are events
(storylines spec §6.1: "Articles are grouped by event"); following a continuing story is the
storyline editor's job, not grouping's. The `c` answers say how often the broader notion would
have pulled a pair in.

## Two stages, week ending 2026-11-16

- **Stage 1: the pairs the label audit drew** (its audit pairs and controls, 41 on the host), asked
  again, four-way, in an order shuffled with a new seed (20261127). The 11 are among them and are
  not marked. The old two-way answers stay in `audit_labels.jsonl`; the new ones go to
  `zone_labels.jsonl`. `--judge` stops when stage 1 is done, and `--report` then prints stage 1's
  reading (below) at once, before any stage 2 pair is asked.
- **Stage 2: the rest of the zone** (168 on the host), in its own shuffled order with the same seed.
  Only after it is done does `--report` print the counts and the models' agreement.
- **The zone** is the audit's, rebuilt by the same code: 209 pairs on the host. Pairs Opus-low called
  `unclear` are not in it.

## After stage 1

The definition stays one event. The operator, having read stage 1's reading, may decide to amend
storylines spec §6.1 so that a group is a continuing story rather than one event. That is the only
change allowed after an answer has been seen, and it is the operator's decision, never automatic.
If it is taken, stage 2 waits for a new pre-registration that states the new mapping (for example
`e` and `c` as the same) and amends §6.1 in the same commit. The answers already given stay valid
either way, because the four-way answers record both notions.

## Readings, fixed now (printed after stage 1)

These describe the labels. Neither changes what the reference is.

- **The 11.** On the audit pairs where the operator said "same" at the audit and Opus-low said
  different, count the new answers. Counting only `e`, `c` and `d`:
  - more than half `c` or `d`: **mostly not one event: under the definition, Opus was right on
    most.** The audit's "same" there was mostly the broader notion.
  - more than half `e`: **mostly one event: the operator and Opus draw the event line
    differently.**
  - otherwise: **split.**
- **The retest.** On the other drawn pairs (less the 11, less those the audit answered `unclear`),
  an answer is consistent when its reference value equals the audit's answer. A new `u` counts as
  inconsistent. At least 80% consistent: **the operator is consistent.** Below: **noisy: the next
  pre-registration must allow for it**, for example with a wider bar or a second labelling of the
  pairs that flipped.

## Other weeks

- **The week ending 2026-11-23** is the other dev week. It has no audit answers, so no stage 1; the
  same script labels its whole zone later if the next pre-registration needs two dev weeks. This
  one does not require it.
- **The week ending 2026-11-09 is the test week** (`grouping_decider_dev.TEST_WEEK`). It is labelled
  only under the next pre-registration, after that is committed. On any week outside the two dev
  weeks the report prints the operator's counts and **no model's agreement**, so no model can be
  seen against the test week's labels early.

## Also reported after stage 2, not part of any rule (dev weeks only)

- Each model's agreement with the reference on the zone: Opus-low, Opus at medium and high where
  their saved answers exist, and the decider (Haiku-high). A model's `unclear` or missing answer
  counts as a miss.
- The decider's option 1 scored with the operator's labels in place of Opus-low's on the zone
  (pairs outside the zone keep Opus-low's labels; zone pairs answered `u` are dropped). This is dev
  data, not evidence.

## Predicted before running

From the reference selection's run, with no new data. If any is wrong, the script is wrong.

- **The zone of 16 Nov:** 209 pairs; **stage 1:** 41; **stage 2:** 168.
- **The 11:** n=11.
- **The retest set:** 29 pairs, the 41 drawn less the 11 less the 1 control answered `unclear`.

## Commitments

- **The reference selection is not reopened.** Opus is not the zone reference, whatever the 11
  turn out to be. Opus at any effort can still be a decider candidate in the next
  pre-registration, scored against these labels.
- No model is chosen here. The next pre-registration decides how these labels are used: the
  candidates, the bar, the dev weeks and the test week.
- The report prints numbers only. Texts are shown only by `--judge`, in the operator's terminal.
- The operator knows these labels will judge the models, has seen the earlier results, and knows
  the 11 are somewhere in stage 1. The blind order within stage 1 and the shown texts are the only
  safeguards against that.

## Running it

```
docker compose run --rm --entrypoint python newsbrief \
  scripts/grouping_zone_labels.py --week-ending 2026-11-16 --judge
docker compose run --rm --entrypoint python newsbrief \
  scripts/grouping_zone_labels.py --week-ending 2026-11-16 --report
```

`--judge` saves each answer at once; `q` stops and a rerun resumes. It stops at the end of stage 1;
run `--report` for the reading, then `--judge` again for stage 2. `--report` refuses until stage 1
is answered, and prints the zone counts only when every zone pair is.
