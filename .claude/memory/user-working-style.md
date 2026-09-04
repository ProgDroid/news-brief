---
name: user-working-style
description: "How the user engages with recommendations and design decisions — present honest tradeoffs, expect to defend \"recommended\""
metadata: 
  node_type: memory
  type: user
  promoted_to: global
  originSessionId: 0dcbbd13-5be1-4f55-92f5-0638d2a112d1
  modified: 2026-08-09T14:44:29.411Z
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

**Naming the missing evidence beats guessing — he will go get it (2026-08-09,
PolyGram 400 session).** Told plainly *"I don't know what `side` should contain, here
are the two readings, and here is exactly what would settle it — the venue docs or the
request payload from DevTools"*, he came back with the answer next message. The same
turn he **redirected mid-work to the higher-risk direction**: *"let's make sure that
closing positions also includes side: sell, we should make sure selling works as that's
important to get right"* — unprompted, correctly identifying that a broken exit strands
real capital while a broken entry only costs an opportunity. **How to apply:** when a
contract/API detail is genuinely unknown, do NOT ship an inferred value and do NOT
stall the whole task — deliver everything that does not depend on it, then state the
uncertainty concretely with the specific artefact that would resolve it. He supplies
it. And when triaging a batch of fixes, rank by blast radius rather than by what broke
today; he already thinks that way and will reorder you if you don't. Matches the global
"ask to 95% confidence" rule and [[http-error-body-is-the-diagnosis]] (the venue's own
error body named the field — logging it is what ended a three-week hunt).

**Cadence for multi-item work (2026-06-25):** when research/evaluation surfaces several
candidate items, he wants them RECORDED AS A PERSISTENT BACKLOG first, then worked
**one item per session** — and "this session we decide to SKIP it" is a valid, expected
outcome, not a failure. Capture learnings after each. So: write the backlog to memory
with per-item STATUS, don't try to resolve everything at once, and treat a reasoned skip
as closing the item. See [[external-geo-dashboards-backlog]].

**Sequences work by CONTEXT BUDGET, and checks disclosure before publication (2026-08-29,
Epic 1 implementation session).** Two moves worth copying. (1) Mid-session he asked
*"should we brainstorm instead of TDD? or is the task straightforward?"* — an invitation to
say which parts were genuinely settled and which were not, rather than a request to switch
process. Answering honestly ("the mechanism is settled; two decisions inside it are not")
produced the two design questions that shaped the whole session. (2) At 71% through the epic
he drew the line himself: *"let's do the cheap ones, then we can end the session before we
tackle the bigger stuff on fresh context."* He treats remaining context as a resource and
wants the large, judgement-heavy item started clean — so when work splits into cheap-mechanical
and large-with-open-questions, SAY SO and let him place the boundary; don't start the big one
late in a session.

**He will reverse his own approved decision on evidence, immediately and without friction
(2026-09-02, ledger cutover session).** He had ruled three times in a row — take the heavier
option on both `bqa.9` extras, "push to the end state" rather than the smallest diff, "ship
together, documented as one-way". Then a review found that the derived-staleness design he had
approved on my recommendation recreated a bug migration 0006 was explicitly written to prevent.
Told plainly that this undermined a decision he had made on my advice, and that the simpler
alternative now dominated on every axis, he replied with the one-word switch. No defence of the
prior call, no friction, no asking me to patch around it.

**How to apply:** when new evidence undermines a decision the user already approved, SAY SO
directly and early — do not defend the earlier ruling because he blessed it, and do not quietly
engineer around it. Lead with what changed, name which of his prior decisions it invalidates, and
give the honest recommendation even when it reverses your own design and his own choice. The
approval is not a commitment he expects you to protect; the evidence is what he actually wants.
The corollary is that "he already decided this" is never a reason to withhold a finding — and
re-litigating without new evidence is still off-limits, so the discriminator is whether you are
bringing a *fact* or an *opinion*.

**And he audits what gets published.** Asked to commit preserved data he replied *"what kind of
data is in that? anything that can't be public?"* before approving — which surfaced that the
repo is public and that `.gitignore` already forbade it. **How to apply:** never propose adding
runtime artifacts to git without stating what is in them and checking repo visibility and
`.gitignore` first; he will ask, and the honest answer may reverse the recommendation. See
[[live-state-on-deploy-host]].

## 2026-09-04 — once the direction is set, he wants execution, not further gates

Two corrections in one session, both terse: **"do it now"** after I laid out the next task and
offered to scope it, and **"continue now"** after I paused mid-task to ask whether fixing nine
pre-existing test violations was in scope.

**Why:** he is not against being asked — he answered four `AskUserQuestion` prompts promptly and
picked the *Recommended* option every time, which is what makes the pattern legible. The question
he wants is the one that CHANGES the work (which surface, how far to go, which of three beads
first). The one he does not want is confirmation that I should proceed with something he has
already pointed at.

**How to apply:** ask the branching question up front, in one call, with a defended
recommendation — then run to completion and report. When something unplanned turns up mid-task
(the nine network violations, `bz1`), state the finding and the chosen path in a sentence and
keep going; do not stop and wait unless the finding genuinely changes what he asked for. Reserve
a blocking stop for outward-facing or hard-to-reverse steps — a push, a host write, publishing
anything to the public repo — where he has consistently wanted the call to be his.

Consistent with the standing entries: lead with the more thorough option, expect to defend it,
and treat friction as the master variable — a confirmation gate he did not need IS friction.
