# Grouping: an audit of the reference labels

**Status:** pre-registered 2026-11-26, before any pair has been shown to the operator. Bead
`news-brief-al7.7.1`, under storylines `news-brief-al7.7`. The script is
`scripts/grouping_label_audit.py`, committed with this file. **Nothing here changes after an
answer has been seen.**

## Why

The decider selection (`2026-11-26-grouping-decider-preregistration.md`) chose none. On the week
ending 2026-11-16, more effort made Haiku join more pairs and lowered its precision against the
reference, from 0.902 to 0.739. The reference is Opus at effort `low`. Nothing measured so far
separates "the decider is wrong" from "the reference is wrong" on the pairs where they disagree.
This audit asks the operator on exactly those pairs.

## What is shown

- **Week:** ending 2026-11-16. **Decider:** `claude-haiku-5-5` at effort `high`, with its saved
  dev answers.
- **Audit pairs:** every labelled pair in the zone (option 1's: similarity ≥ 0.65 in the Octen
  space and not joined by rule A) where the decider's join (it said `same`) differs from the
  Opus label (`same`). Pairs Opus labelled `unclear` are not scored and are not shown.
- **Controls:** up to 4 zone pairs where both say same, and up to 4 where both say not same,
  drawn with a fixed seed.
- The operator sees only the two texts the judge saw, in a fixed shuffled order, with no
  outlet and no label. The definition shown is the judge prompt's: the same event is one
  happening (one decision, one attack, one release of figures), not merely the same topic,
  country or company. Answers are `same`, `different` or `unclear`.

## Reading, fixed now

Let D be the audit pairs the operator answers `same` or `different`. H is the number of those
where the operator's answer agrees with the decider's join, and O the number where it agrees with
Opus (H + O = D).

| H / D | Reading |
|---|---|
| ≥ 0.50 | The Opus labels are not a usable reference in the zone. A better reference comes before any more decider work. |
| ≤ 0.20 | The Opus labels hold. The deciders are inconsistent, and decider tuning stops (options 2 or 3). |
| between | Mixed. The operator decides, with the rescored numbers below. |

**Controls:** if the operator agrees with the joint label on fewer than 75% of the controls they
answer `same` or `different`, the operator's notion of the same event differs from the prompt's.
The reading above is then about the definition, not about label noise.

**Also reported, not part of the reading:** the decider's wP and wR on the week with Opus's
labels, and again with the operator's answers in place of Opus's on every audited pair (an
operator `unclear` drops the pair). This is dev data, so a pass here is not evidence: any decider
adopted on it needs a new pre-registration and a test week.

## Commitments

- The pair set, the controls, the thresholds and the reading are not changed after an answer is
  seen.
- The report prints numbers only. Texts are shown only in the operator's terminal on the host,
  never in git or in chat.
