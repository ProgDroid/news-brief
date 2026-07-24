---
name: user-working-style
description: "How the user engages with recommendations and design decisions — present honest tradeoffs, expect to defend \"recommended\""
metadata: 
  node_type: memory
  type: user
  promoted_to: global
  originSessionId: 0dcbbd13-5be1-4f55-92f5-0638d2a112d1
---

The user is a hands-on decision-maker who does NOT rubber-stamp recommendations.
When offered options with one marked "(Recommended)", he will challenge it and ask
for the rationale rather than just picking it — e.g. on the Phase-3 momentum close
trigger he replied *"interested in option 2, why is 1 recommended?"* and chose the
alternative after weighing the defence. He reviews specs/plans before approving and
engages thoughtfully with each design question.

**How to apply:** when recommending an option, lead with the honest tradeoff (real
pros AND cons of the alternatives), not a terse steer. Make "recommended" defensible
or don't label it. Offering a genuine middle path he hadn't considered (as with the
"optional target + 4w backstop" superset) lands well. This reinforces the global rule
to ask questions to 95% confidence — he wants to be the regulator, not a yes-man.
Related: [[multi-asset-trading-build]].

**Refinement (2026-06-19, sources/daemon session): "(no option selected) + a notes
comment" is a deliberate move, not indecision.** Twice he left an AskUserQuestion
unanswered and used the notes field to think out loud ("still debating this in my
head…", "not sure about this one, let's dig deeper") and to widen scope mid-decision
("we can make it real-time… explore your memories"). Do NOT re-ask the same question
or push him to pick — treat it as a request to investigate further, surface a genuinely
different option he hadn't framed, and name which of his stated concerns each path
actually solves. The env-var → file-on-volume → Telegram-daemon progression only
happened because each "non-answer" was read as "go deeper," not "choose now." He'll
also green-light a bigger scope ("tackle the deferred items now, no deferrals") once
the tradeoffs are clear — so flag deferrals explicitly; he may pull them back in.

**Verifies suspicious data himself + rejects loose hacks (2026-06-26, source-mining session).**
Two reinforcing moves: (1) when I anchored on an "energy gap," he made me STEP BACK — *"have
we just overindexed on this? is it even a gap?"* — which surfaced the better frame (complete the
half-built perspective matrix). Don't let an early hypothesis harden; pressure-test "is this even
the real gap?" before building. (2) On a feed validated via a keyword Google-News proxy he said
*"that seems suspicious… I'd prefer no keyword queries"* and asked me to hand him the raw URL so he
could check himself — then chose the clean NATIVE feed (lower volume) over the loose high-volume
proxy. **How to apply:** show the raw numbers/URLs behind a data-derived choice so he can verify
(don't ask him to trust a count); when a solution feels hacky/loose, say so and prefer the clean
option even at a quality/volume cost — he'll take clean-and-smaller over clever-and-loose. Matches
the global API-validation-discipline (treat unexpected results as suspicious, investigate, be honest).

**Cadence for multi-item work (2026-06-25):** when research/evaluation surfaces several
candidate items, he wants them RECORDED AS A PERSISTENT BACKLOG first, then worked
**one item per session** — and "this session we decide to SKIP it" is a valid, expected
outcome, not a failure. Capture learnings after each. So: write the backlog to memory
with per-item STATUS, don't try to resolve everything at once, and treat a reasoned skip
as closing the item. See [[external-geo-dashboards-backlog]].
