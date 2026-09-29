# Red team, revision 2: gold set by window census (KB redesign, sub-project 0)

**Reviewed:** `docs/superpowers/specs/2026-09-28-gold-set-labelling-design.md`, revision 2
**Prior review:** `docs/superpowers/reviews/2026-09-28-gold-set-labelling-redteam.md` (rev 1)
**Stance:** hostile staff engineer, asking whether this is the wrong thing to build.
**Evidence rules:** **verified** means read in the repo at the cited line. **Estimate** means
arithmetic on figures the repo already records, with the inputs named. **Unverified** means
known platform behaviour that was not reproduced here. No production data was queried, because
the corpus lives on the host.

---

## Verdict

The revision fixed the instrument. A window census is the right shape, and it answers rev 1's
yield, stratifier and Trap 7 objections. What is wrong now is mostly around the census:

- **(a)** It builds a permanent platform before it has two windows of evidence that the census
  is viable. The estimates below say the §4.3 cap refusal is a likely outcome.
- **(b)** It assumes the operator's census is complete. Nothing in the design measures that,
  and the pairs he is most likely to miss are the low-lexical ones the census exists to reach.
  That is Trap 5 again, this time through the annotator.
- **(c)** It welds a temporary labelling tool into the production container's startup path and
  resident set, which is where it will hurt once the census is finished.

The K arithmetic is right as far as it goes. Its design effect uses the wrong mean for unequal
group sizes and resamples the wrong unit. Both errors make K too small.

---

## Objection (a): a simpler design. Pilot two windows with a throwaway static page, and build only if the pilot passes.

**The objection.** The durable value is the census data: `gold_windows`, `gold_window_items`,
assignments and the readout. The spec also builds a network service to collect it:

- a new resident mode (§6.1);
- a LAN port on the production service;
- token and cookie auth (§6.2);
- an autosave write API with retries (§6.3);
- Telegram progress and nudges (§6.4).

All of that serves roughly K × 15 minutes of use. The spec's own cap puts that at about 6 hours,
and my estimate below puts it higher. Worse, the build comes **before** the only numbers that
decide whether the census is viable: yield per window, m̄, and operator minutes. Those arrive
after window 2 (§4.3). The spec's own path to failure, "K > 30 → refuse, return to the
operator", is only reached once everything already exists.

**Evidence that the refusal is a likely outcome (estimate).**

- **Positives per window.**
  - The spike found 671 production-detected cross-outlet pairs ≤ 2 h apart among 12,711
    material items (spike doc, output lines 17–20). Over the ~22 days since capture began
    (2026-09-04), that is 176 three-hour slots, or about 3.8 detected pairs per slot.
  - About 23% of ≤ 2 h pairs would be split by a 3 h boundary. Derivation: the spike split 34%
    at W = 2 h (spike doc lines 70–72), which implies a mean gap of about 0.68 h, and
    0.68 / 3 ≈ 0.23.
  - The detector's recall is at most batched recall@30 (44% after 2026-09-09, and 13–27%
    before; `comprehend.py:1601–1603`), and the model can still decline a match.
  - **So true cross-outlet positives run about 5–11 per window**, before any human misses.
- **The design effect**, corrected as in §4.3 below, is about 2.3–5.7 for plausible group-size
  mixes. That puts pairs needed at about 280–700.
- **So K ≈ 26–140**, and most of that range breaches the 30 cap. Even the spec's own DE formula
  gives 183–244 pairs, so K ≈ 17–49.

**What I would do instead.**

1. **Pilot first.** A throwaway script on the host freezes window 1 and writes one
   self-contained HTML page: items in capture order, the same grouping controls, and state in
   `localStorage`. **Finish** produces a short code: window id, then one base-36 symbol per item
   position (0 = ungrouped, `?` = unsure, 1–z = group), plus page-open and finish timestamps.
   For 100 items that is about 110 characters, far inside Telegram's 4,096-character message
   limit.
   - The operator pastes the code to the bot. The **existing** `commands` daemon, already
     single-user-gated (`brief.py:3552`, per rev 1), writes the `gold_*` rows.
   - The page reaches the phone as a Telegram document, so there is no server, port, token or
     cookie.
2. **Pre-register the go/no-go** before the pilot. Build the platform only if the pilot
   measures:
   - K ≤ 30 under the corrected DE (§4.3 below);
   - operator minutes ≤ 20 per window, timed from page-open to Finish.
3. If the pilot passes, the same static page may simply **be** the tool. Its cost is one paste
   per window. It has no auth surface, and it makes (c) impossible.

**Stated honestly.**
- **Unverified:** whether the operator's phone browser runs JavaScript in an HTML file opened
  from Telegram. iOS Quick Look previews may not; Android usually hands off to Chrome. A
  one-line test file settles it in 5 minutes.
- If it fails, the fallback is (c)'s separate compose service, **not** a resident.
- `localStorage` can be purged mid-window, so a lost draft means relabelling that window. The
  spec's server-side autosave is genuinely better here, and it is the one thing this design
  gives up.

---

## Objection (b): an assumed requirement. The census is "complete", and nothing measures that.

**The objection.** The whole advantage of a census over rev 1's pair sampler is coverage.
Every within-window pair gets a label, including the 70% of true pairs below trigram 0.35 that
rev 1 could not reach. That holds only if the operator actually **finds** every same-event
partner among about 100 time-ordered items. The spec assumes that he does. The design has no
validity measurement at all:

- **§4.4's consistency pass measures reliability, not validity.** Pass 2 is scored against
  pass 1 as truth. A pair missed in both passes is invisible to it. Pairs are most likely to be
  missed in both passes for the same reason, which is low lexical overlap: "Israel strikes depot
  near Tyre" and "Lebanon says strike killed two" share almost no words. The spec calls this
  check "the hard cases directly". It covers only the hard cases the operator found.
- **The task is recall, not recognition, and it breaks a standing rule the spec says it
  keeps** (§3: "labelling is by recognition, not recall"; global rule "Ask for RECOGNITION,
  never RECALL"). With capture-time order and no similarity help (§6.3), labelling item 87
  means remembering that something about Tyre went by 60 items and several screens earlier.
  The allowed text filter makes it worse, because typing a keyword is recalling the keyword,
  and the keyword the operator recalls is lexical. The "Add to… ▾" menu lists group numbers
  (§6.3 mock-up), so choosing G4 means remembering what G4 is.
- **Low prevalence.** Most items are singletons, with an estimated 5–11 positive pairs among
  about 100 items. Rev 1 already cited the evidence that rare targets are missed
  disproportionately.
- **The direction of the bias is the dangerous one.** The operator's misses will be enriched
  for low-trigram pairs, so the gold positives will be enriched for lexically similar pairs.
  Production retrieves by pg_trgm (`comprehend.py:1647`), so a lexical system would score
  inflated recall on this gold set. That is Trap 5, which the revision exists to escape, coming
  back through the annotator instead of the stratifier.

**Also assumed: that confirmation lives within 3 h (§4.5).**
- The evidence cited, "the spike used pairs ≤ 2 h apart", is circular. The spike chose ≤ 2 h
  by construction, so it cannot show where confirmation lives.
- The only unrestricted figure in the repo points the other way. bqa.18 recorded 899
  production-merged cross-outlet pairs in a 7-day window, about 128 a day. The spike's ≤ 2 h
  pairs come to about 30 a day. The definitions and date ranges differ, so this is
  **suggestive, not established**, but it hints that most confirmed pairs are more than 2 h
  apart.
- The spec's own boundary case 2 ("kills 12" at 06:00, "toll rises to 40" at 08:30) is
  2.5 h apart. Aligned 3 h slots split such a pair with probability 2.5/3 ≈ 83%, so the worked
  example mostly cannot occur in the population.

**What I would do instead.**

1. **Measure the operator's census recall.** After he finishes a window blind (unchanged), show
   him the pairs he left **apart** that any of a diverse set of detectors joins:
   - entity overlap;
   - trigram;
   - an LLM clustering of the whole window;
   - later, the systems under test.

   He answers same or different, which is a recognition task.
   - Record each addition with `source = 'adjudicated'`, separate from `blind`.
   - This yields the operator's measured blind recall, and a gold set equal to the union after
     adjudication.
   - Pairs that every detector **and** the human miss stay invisible, so state that the result
     is a floor, not a ceiling.

   This is the standard double-annotation-plus-adjudication pattern of coreference corpora. It
   relaxes "no system opinion is shown" only **after** the blind pass, so pass-1 labels stay
   blind. That trade is the operator's to accept.
2. **Show group content, not numbers,** in "Add to…": the first title of each group, most
   recent first.
3. **Measure the gap distribution before fixing W.** Use production's own cross-outlet merges
   for windows it comprehended. It is a floor-biased distribution, but it is free. Then either
   choose W from it, or keep 3 h and report the lost share as a number rather than asserting
   "mostly within hours".

---

## Objection (c): the 6-month collision. A temporary tool welded into the production container.

**The objection.** At about one window a day, K ≤ 30 is finished within roughly a month. For
the five months after that, the spec leaves three things behind in the one container that runs
capture, briefs, the bot and alerting.

1. **A published port on the production service.** Verified: `docker-compose.yml` has no
   `ports:` today. The service has no inbound network surface, and this would be its first.
   §6.1 binds the port to a host LAN address.
   - **Unverified, but known Docker behaviour:** if that address is absent when the container
     starts, the start fails. That happens on a DHCP change, a router swap, or a boot where
     Docker comes up before the interface. So does a host-side port conflict. Docker does not
     retry a start that failed on a port bind.
   - Either way, **the whole `newsbrief` container does not start**, and with it capture,
     comprehension, briefs and the bot.
   - §9's "port conflict: the supervisor restarts it" models the wrong layer. The publish fails
     in Docker, before the supervisor exists.
   - The outage is **silent**. The monitor is an in-container scheduled job
     (`scheduler.py:74`), and every alert path is `telegram_alert` from inside the same
     process.
2. **A resident that never leaves.**
   - `RESIDENT_MODES` means "children that stay up" (`supervisor.py:31–34`).
   - A resident fault triggers a crash-loop alert, then a down reminder **every hour, forever**
     (`supervisor.py:48–54`, `:373–395`).
   - After K is done, the labeller still runs and still pages on any fault, whether the DB is
     unreachable or the port is lost, like an outage of the operator's control channel.
   - "A hook that always speaks is a hook you will switch off" (global rules). Here, switching
     it off means editing `RESIDENT_MODES` and redeploying.
3. **A config line that lives only on the host.** §6.1 says the repo compose file is a stale
   template, so the port line exists only in the host's compose file. CI, review and this repo
   cannot see it. This is the invisible-config class the memory `env-var-needs-compose-
   passthrough` records.

**What I would do instead.**
- Prefer (a), which removes all three.
- If a server is kept, run it as a **separate compose service** under a profile: same image,
  `command: [labeller]`, its own `ports:`, and an environment limited to `POSTGRES_*`.
  - Start it for the census and remove it afterwards.
  - Its failures cannot stop `newsbrief`, and it does not inherit the trading and API keys (see
    §6.2 below).
  - The supervisor's own seam invariant anticipates this: "promoting a child to its own
    container later is a compose edit rather than a refactor" (`supervisor.py:9–12`).
- In either case, the labeller is a `brief.py` mode. A resident is spawned as
  `python brief.py <mode>` (`supervisor.py:186`), and `MODES` (`brief.py:3898`) must contain it,
  or the usage branch exits 1 on every spawn. The spec describes only `labelling.py`.

---

## Specific checks requested

### §4.3: the K formula and the design effect

**The formula is sound for a paired binary comparison.**
- `n = (z_α/2 + z_β)² · p_disc / Δ²` is the conservative form of Connor's (1987) McNemar sample
  size.
- The exact form, `[z_α/2·√p_d + z_β·√(p_d − Δ²)]² / Δ²`, gives **119.6** against the spec's
  121.96. The spec is about 2% conservative, which is fine.
- It needs `d ≥ Δ`: 0.35 ≥ 0.15 holds, with the discordant cells split 0.25 / 0.10.
- The arithmetic reproduces: 2.8² × 0.35 / 0.0225 = 121.96.

**d = 0.35 is plausible but on the optimistic side, and the repo holds evidence for higher.**
- Under independence, two systems with recall 0.44 and 0.59 disagree on
  0.44 × 0.41 + 0.59 × 0.56 ≈ 0.51 of pairs. So d = 0.35 assumes a between-system correlation
  of φ ≈ 0.33 (estimate). That is plausible, because easy pairs are easy for everyone.
- Against it:
  - LLM noise alone makes Sonnet disagree with **itself** on 23.4% of link decisions (M2
    link-only subset, spike doc lines 305–309, 324–326).
  - Architectures as different as the spike's T and RT differed by 43.5 points in visibility
    (spike doc line 33). That alone forces d ≥ 0.435 on that axis.
- The comparison §2 names is retrieve-then-judge against clustering. Nothing in the repo
  suggests those two correlate more than Sonnet with itself.
- **Sensitivity:** d = 0.5 gives n_eff = 174 (+43%), and d = 0.6 gives 209.
- Fix: pre-register K at d = 0.5, or print K at d ∈ {0.35, 0.5, 0.6} and commit to the
  largest.
- "Sub-project 1 re-checks d" comes after the labour. That is Trap 7's ordering again.

**The design effect is wrong in the under-sizing direction.**
1. **Wrong mean.** `DE = 1 + (m̄ − 1)ρ` holds for equal cluster sizes. With unequal sizes the
   pair-weighted mean applies, `m̄* = Σmᵢ² / Σmᵢ` (Kish; equivalently
   `1 + ((1 + CV²)m̄ − 1)ρ`). Pairs grow quadratically with outlets, since a 6-outlet event
   gives 15 pairs, so the size distribution is heavily skewed. Worked examples:
   - pair counts {1,1,1,1,1,3,6}: m̄ = 2.0 and DE = 1.5, but m̄* = 3.6 and DE = 2.3. Pairs
     needed rise from 183 to 280.
   - {1,1,1,1,1,3,15}: m̄ = 3.3 and DE = 2.1, but m̄* = 10.4 and DE = 5.7. Pairs needed rise
     from 261 to 697.
2. **Wrong unit.** The design samples **windows** (§4.2). Groups are nested inside windows, and
   windows share a news cycle and a band. Two things follow:
   - The primary sampling unit is the window. §8's "bootstrap over groups" skips it, which is
     Trap 1 cited by the spec itself and then applied one level too low.
   - Bootstrap over windows, or two-stage: windows, then groups within them. With K ≈ 20–30,
     that is feasible.
   - Size K for the window level too. Simulation over windows 1–4's measured group
     distribution is simpler and more honest than a closed-form DE.
3. **ρ = 0.5 has no source, and it can be measured for free.** Windows from 2026-09-18 to about
   2026-09-25 were comprehended by production under the current ranking. Production's own links
   on the gold pairs give its within-group intraclass correlation after window 2, for $0.
   Caveat: comprehension has been off since the phase-1 build, so later windows have no links.
4. **The estimand weights mega-events quadratically.** Pair recall lets one 8-outlet event
   (28 pairs) outweigh 20 two-outlet events. Big events are also the most heavily covered and
   the most lexically similar, which are the easy ones. For goal 1 ("reported by more than one
   outlet"), an **item-level** metric (B-cubed recall) or a **group-level** one ("was the
   multi-outlet group connected at all?") matches the purpose better. Either one also shrinks
   the design effect.
5. **K from windows 1–2 is band-biased.** Round-robin interleaving means windows 1 and 2 come
   from two of the four 6-hour bands. Band volumes differ severalfold, and one mega-event
   swings the per-window yield by 2–3×. Compute K after window 4, one window per band.
   Better, use an information-based stopping rule: stop when the corrected effective n reaches
   the target. That rule reads only the amount of information, never the outcome, so it costs
   no α.

### Can the operator census 100 items in 10–15 minutes on a phone? Probably not (estimate).

**Volume.** The spec assumes 800 items a day, about 100 per window.
- The spike implies about 670 non-quote items a day on average: 12,711 material items at 86%
  material, over ~22 days. That is about 84 per window.
- Busy windows are much larger. The spike's pair-bearing hours averaged 40.7 material items an
  hour (spike doc line 140), about 140 items per 3 h once non-material items are added back.
- §4.2 sets a minimum of 20 items and **no maximum**, so a breaking-news window can run to
  200 or more.

**Time per item.**
- A title plus a ~150-character lead is about 37 words. At 200–250 wpm on a phone, that is
  about 9–11 s just to read.
- Finding partners across roughly 10 screens of time-ordered items adds search time. See (b).
- Estimate: 10–20 s per item, so **17–33 min for 100 items and 23–47 min for 140**.
- Reaching 10–15 min needs about 6–9 s per item including search. That is plausible only
  from headlines alone, and headlines alone contradict the guideline's "even after opening the
  link", since opening an article costs 30–120 s.

**The timing instrument undercounts.** §8 measures "first to last assignment timestamp". That
excludes the reading done before the first tap and the checking done after the last tap. Record
page-open to Finish.

**Fixes.**
- Pre-register a per-window time budget, checked on window 1.
- Express §4.3's cap in **operator-minutes measured**, not in windows. The spec's "30 windows ≈
  6 hours" assumes 12 minutes a window. At a measured 25 minutes, 30 windows is 12.5 hours.
- Add a maximum window size, or split oversized slots, and record which.

### §6.2 security

Ranked by consequence.

1. **Stored XSS from feed content (hole).**
   - Titles, leads and URLs come from about 26 internet feeds, Nitter tweet feeds and
     operator-added temp sources. They are rendered into an authenticated page built on
     `http.server`, which has no template auto-escaping.
   - A `<script>` in a title runs with the page's origin. HttpOnly stops cookie theft but not
     same-origin POSTs, so the script can rewrite the gold set.
   - A `javascript:` URL in `items.url` becomes a live 🔗 link.
   - Neither case is in §6.2 or in §10's Access tests.
   - Fix:
     - `html.escape` on every field;
     - an http/https scheme allowlist on links, with `rel="noopener noreferrer"`;
     - a strict CSP: `default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'`;
     - `X-Content-Type-Options: nosniff`;
     - tests with a hostile title and a hostile URL.
2. **Blast radius (hole).**
   - Every child inherits the supervisor's whole environment. `child_env = dict(os.environ)`
     (`supervisor.py:193`), and `env=` only **adds** keys (`:197–198`).
   - So the labeller process holds `T212_API_KEY` (a live trading endpoint by default,
     `docker-compose.yml` anchor), `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`,
     `BIGDATA_API_KEY` and the owner-privileged DB password.
   - The token guards only the application layer. `http.server`'s request parsing runs **before
     auth**, for anything on the LAN, and Python's own documentation says `http.server` is not
     recommended for production.
   - Fix:
     - an environment **allowlist** for this child (only `POSTGRES_*`);
     - a DB role with `SELECT` on `items` and `outlets` and `INSERT`/`SELECT` on the `gold_*`
       tables only.

     (a) or (c)'s separate service delivers the environment half for free.
3. **SameSite=Strict plus a redirect from Telegram likely 403s the first visit (functional,
   unverified).**
   - The navigation starts outside the site, in the Telegram app. Chromium evaluates a
     redirect chain's same-site status from its cross-site start, so the Strict cookie set on
     `/?t=` may not be sent on the redirected `GET /`.
   - Verify on the operator's phone.
   - Fix: render the page directly on the token URL and strip the token with
     `history.replaceState`, or use `Lax` with an `Origin` check on POSTs.
4. **The token sits in plaintext in `runtime_state`, and so in every daily `pg_dump`.**
   - `backup.py` dumps the whole database, and no exclusion flag appears in it.
   - That contradicts the repo's own rule: "A credential in `settings` lands in every pg_dump"
     (`docker-compose.yml:115`).
   - Fix, at no cost: store `sha256(token)`, compare `sha256(cookie)` in constant time, and add
     a `/label reset` that rotates the token.
5. **Cleartext on the LAN.** The token (first visit) and the cookie travel over HTTP, so the
   cookie cannot be `Secure`. Any device on that network segment can replay it, including IoT
   devices and guest Wi-Fi that is not isolated. The asset is only label integrity, so this can
   be an accepted risk, but the spec should say so.
6. **Smaller items:**
   - compare `?t=` in constant time too, since §6.2 names only the cookie;
   - check the `Host` header against an allowlist, as defence in depth against DNS rebinding
     reaching the pre-auth parser;
   - send `Cache-Control: no-store`;
   - state the token entropy, for example `secrets.token_urlsafe(32)`.
   - `/label` reposting the tokenised link keeps the token in Telegram's cloud history, which
     is acceptable for one user but should be stated. Link previews are already disabled on
     every send path (`common.py:522`, `:568`, `:607`, `:625`).

---

## Rev 1's objections: how revision 2 answered them

| Rev 1 objection | Revision 2's answer | Grade |
|---|---|---|
| Yield: 6–30 positives in 600 taps | Window census | **Answered** |
| Trap 5: pg_trgm stratifier | No stratifier, no system help | **Answered in the sampler, reintroduced through the annotator** (b) |
| Trap 7: no MDE before labelling | Δ = 15 pts, 80% power, K after window 2 | **Answered in form.** The inputs d and ρ are assumed and the DE formula under-sizes (§4.3 above) |
| Full-history replay | 14-day look-back per window | **Partly answered.** See below |
| Moving population | Frozen membership | **Answered for items, not for slots.** See below |
| Consistency re-shows were Trap 8 | A whole window re-served | **Better, with two defects.** See below |
| No guideline | §5 with 6 cases | **Answered.** Case 2 mostly falls outside the population (b) |
| Quote-page predicate re-derived in SQL | `common.is_quote_page` called in Python | **Answered** |
| `created_at` backlog | Exclude items with `published_at` > 24 h before capture | **Answered.** NULL `published_at` is unspecified; state whether it is kept |

**Replay, in detail.**
- The 14-day look-back is right for today's retriever. It matches `CANDIDATE_WINDOW_DAYS = 14`
  (`comprehend.py:1519`), which I checked because it could have sunk the design. It does not
  cover a system with longer memory, such as sub-project 2's stories or entity history.
- §4.2's claim that replay cost is "proportional to K windows, not to the whole history" is
  false for uniform draws.
  - Suppose the slots are frozen around 2026-10-01. The eligible range is then 2026-09-18 to
    2026-10-01, about 104 slots. K = 20–30 windows drawn from them have look-backs whose
    union is **the entire history since 2026-09-04**.
  - Independent per-window replays would cost K × 14 days, about 280–420 capture-days, which
    is 10–16× the history.
  - So the harness must be one chronological replay of the whole span. That is the as-of
    replay rev 1 flagged as the repo's most error-prone step: M2's cut-off blocker and the
    entity-birth re-dating.
- Cost is bounded by the history at freeze time. **Estimate:** about 18k items over 27 days,
  times a 30–86% material rate, times $0.0025 per item (M2), gives $14–39 per arm per run. At
  least two runs are needed given Sonnet's self-disagreement, so $30–80 per arm. That is lower
  than rev 1's $120–175, but because history is short now, not because of K.
- Fix: draw from a **contiguous pre-registered block** (for example 7 days, stratified by band
  and weekday), and state the span the replay must cover.

**Moving slots.**
- §4.2 does not say **when** the eligible slot list is enumerated. If it is re-enumerated at
  each draw, the list grows and the seeded shuffle of a longer list is a different
  permutation. The "window order" then moves, which is rev 1's moving-population defect at the
  window level.
- Eligibility also depends on `is_quote_page`, whose rules can change, and on the ≥ 20-item
  count.
- Fix: persist the ordered slot list at first draw, in a `gold_slot_order` table.
- The test "the seed reproduces the order" passes on a fixture, and it cannot catch this.

**Consistency pass.**
- The window header prints the date, time and item count ("Tue 16 Sep 06:00–09:00 UTC · 104
  items"). The pass-2 copy of window 2 shows exactly what the operator saw before, so the page
  does mark it as a repeat, contrary to §4.4.
  - Fix: show relative labels only, and shuffle the displayed window number.
- One repeated window carries about 5–11 positive pairs, so the "operator's ceiling" arrives
  with a very wide interval. Report the interval, or repeat two windows.

---

## Other findings

- **§1 quotes a retired measurement.**
  - Where it comes from: "78% of true duplicates are never offered for hub entities (500+
    events)" is bqa.18's hub table (`.claude/memory/newsbrief-comprehension-pipeline.md:338`).
    That table was computed by `was_retrievable`'s copy of the **recency** ordering. `bqa.26`
    found this, and `reconstruction-drifts-from-production` says the figure "was quoted as live
    three times in one session before anyone checked".
  - The problem: this spec is a fourth quotation of it.
  - What current data says instead: current retrieval is 44% batched recall@30. The claim that
    retrieval is the cause survives, but the 78% should be removed or re-measured through
    `candidate_events(as_of=…)`.
- **Out-of-order retries corrupt "latest row wins" (§6.3, §7).** Suppose action A fails,
  action B succeeds, and A's retry then lands. The stale A becomes the item's state. Stamp each
  action with a client sequence number and let the highest sequence win. Order by `id`, never
  by `created_at`, since `now()` is the transaction start time.
- **Case 6 (the same Reuters story through two feeds) is mostly already one item.** Identity is
  the guid, or else the URL, per outlet (`capture.py:45–55`). Reuters Business and Reuters World
  share the outlet "Reuters" (`brief.py:171`, `:186`), so a second copy survives only when the
  guid or URL differs. The case costs nothing, but "counted separately" will count close to
  zero.
- **No window-level escape.** If a window turns out to be unlabellable (too large, or mostly one
  mega-event), the spec has no "skip with reason" after opening. Rather than letting the
  operator abandon it silently, record `status = 'abandoned'` with a reason, and count it in
  the readout.
- **The pass-2 window's place in K is unstated.** It is labour but not a scoring window. Say
  whether the cap counts it.
