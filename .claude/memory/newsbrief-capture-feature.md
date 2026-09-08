---
name: newsbrief-capture-feature
description: Epic 2 continuous capture SHIPPED 2026-09-03 (b42.1 closed, 16 commits, 1408 tests) — default OFF behind CAPTURE_ENABLED, writes rows nothing reads yet; two follow-up beads open, one of them the alerting my own spec wrongly claimed existed
metadata:
  node_type: memory
  type: project
  originSessionId: c7b3e1ab-57c0-4dbc-8e99-58bd09ddc7f3
  modified: 2026-09-03T15:50:07.699Z
---

**`news-brief-b42.1` is CLOSED.** `capture.py` polls all 26 feeds every 30 minutes as a supervisor job child, writing `outlets`/`items` plus three telemetry tables from migration 0008. Spec: `docs/superpowers/specs/2026-09-02-continuous-capture-design.md`; plan and ledger alongside it. **Default OFF** — set `NEWSBRIEF_CAPTURE_ENABLED` on the host to switch it on. **This unblocks `b42.2` (measure roll-off) and `bqa.4` (comprehension).**

**Design facts that are load-bearing and non-obvious:**

- **`captured_items` is DEAD — do not build it.** 0006 already created `outlets`/`items`; the staging table was superseded and would re-open the global-content-hash bug 0006's own comment records closing.
- **`items` deliberately has NO feed column.** "What the world published" and "which of my feeds showed it" are different objects. Per-feed telemetry lives in `feed_sightings` (keyed `(source_name, content_hash)`), and `source_name` is TEXT with no FK so a renamed source keeps its history.
- **`feed_polls` is the DENOMINATOR, not bookkeeping.** An item's absence only means "left the window" if a SUCCESSFUL poll followed its last sighting. Without it a feed 403ing for a day reads as its entire window rolling over — a large, clean, fictitious signal.
- **INVARIANT: `record_sightings` and `record_poll` must commit in the SAME transaction.** Postgres `now()` is `transaction_timestamp()`, so a same-pass poll has an identical timestamp and `polled_at > last_seen_at` excludes it. Moving the per-feed `conn.commit()` between them, or switching to `clock_timestamp()`, makes every feed report its whole window as rolled off every pass. Commented at the commit site; guarded by `rolled_off(...) == []` in the end-to-end test.
- **The Nitter 429 is an ADJACENCY bug, not a volume one.** The two X feeds sit next to each other in `RSS_FEEDS`; `order_by_host` interleaves so no two consecutive fetches share a host. At 48 passes/day the old behaviour would have recurred 48x daily.
- **Google News RSS caps at 100 entries** regardless of `when:` — measured with `when:2d`, `when:7d` and no window, all returning exactly 100. So the 8 proxies are the MOST truncation-exposed feeds, not the least; the `when:` parameter does not bound what is lost.
- **8 of 26 feeds carry an explicit `outlet` key** because their name is a product, not a publisher. Includes the non-obvious one: `Intersubjectively Transmissible` (jashap.substack.com) and `Jacob Shapiro (@jacobshap)` are ONE author across two media — unmapped they would read as two independent sources corroborating each other.
- **A pass is bounded at 600s** against a 30-minute interval, because 26 feeds x 3 attempts x 20s ≈ 26 min and `supervisor.py:834-851` telegram_alerts any job still running at its next fire time — 48 chances a day.

**Open follow-ups:**

1. **Capture has NO alerting path** (P2 bead filed 2026-09-03). Spec §10 states as fact that `monitor` raises capture health on anomaly. It was never built, and the plan's own self-review wrongly claimed it had been filed. Capture currently writes rows nobody reads; a capture degrading silently (half the feeds 403ing) is invisible. Threshold must come from `b42.2` data, not a guess.
2. **`news-brief-uh0`: retention** for `items`, `feed_sightings`, `feed_polls`, `capture_runs` **and `job_runs`** — capture takes `job_runs` from ~5 to 53 rows/day and `retention.py` prunes files only.
3. Deferred minors: a 404 classifies as `http_5xx`, and a genuinely quiet feed counts toward `feeds_failed`. Both spec-conformant, both will skew the alerting threshold in (1).

Related: [[tests-asserting-less-than-their-name]] (seven vacuous tests found in this build), [[newsbrief-kb-architecture-2026-08-29]], [[newsbrief-runtime-foundation-phase-1]].

## 2026-09-04 — CAPTURE IS ON IN PRODUCTION. The measurement clock starts here.

Confirmed by him: the newest `capture_runs` row reads `enabled = t`. **The b42.2 data window dates
from 2026-09-04** — before this, `feed_polls` held nothing but disabled-pass rows, so any roll-off
analysis must exclude everything earlier or it will measure a switched-off system.

Turning it on took a `settings` row write, not an environment change. He set the variable on the
host and recreated the container first, and it did nothing, for two independent reasons — the
anchor named a variable no code read (fixed in `b140a43`), and `import_settings_from_env` only
runs while `settings` is empty. Full write-up in [[env-var-needs-compose-passthrough]].

**Two readers now exist** (`a9q`, commits `dc92e55`), both health surfaces outside the brief path,
so the "a broken capture costs a log line, not a brief" boundary still holds: `/capture` on
Telegram, and a liveness block in `mode_monitor`. **Not yet deployed as of the flip** — capture
itself was already in the running image, which is why the clock could start ahead of the deploy.

### The design rule this converged on: LIVENESS needs no measurement, QUALITY does

Worth reusing anywhere an alert is blocked on "we haven't measured the threshold yet". Splitting
the question let half of `w3q` ship immediately:

- **Threshold-free, shippable now.** "It stopped firing" follows from the schedule
  (`scheduler.SCHEDULES`, read rather than copied, so retiming capture retimes the alert). "A pass
  died" needs **no constant at all**: a run whose `finished_at` is NULL *and which a later run has
  overtaken* can never complete. My first instinct — `DEADLINE_SECONDS` plus a margin — would have
  invented exactly the kind of number the bead exists to avoid. And the wedged-newest case falls
  out of the stale check free, since a stuck pass writes no new rows and ages past tolerance.
- **Needs data, stays deferred.** "Too many feeds failed", "too few new items" are RATES. A guessed
  rate is indistinguishable to the operator from a measured one, which is worse than no alert.

**Three deliberate silences, each with a test that fails if the silence is for the wrong reason:**
a disabled capture (it writes a row every fire, so it can never go stale — that is what
`capture_runs.enabled` is FOR); the newest run being unfinished (identical to a pass in flight);
and an empty table (cannot tell "never deployed" from "broken" — `news-brief-b42.3`, answerable
from `job_runs`, not from a guessed grace period).

**Dedupe lives in `runtime_state`, not in a set on an object.** The supervisor can remember in
memory because it is a resident; `mode_monitor` is a fresh job child every hour, so an in-process
set forgets between checks and turns one outage into 24 messages a day.

## 2026-09-07 — first real volume numbers (feeds `b42.2`)

Measured off the host: **700–900 new items/day across 24 outlets**, 48 passes/day, ~1,400 items
seen per pass of which ~1% are new. `2026-09-04`'s 1,926 is the first-fill artifact of capture
meeting each feed's whole window — **never use it in a rate**. Only ~25% of items carry 150+ chars
of body. Full numbers and what they imply downstream are in
[[newsbrief-comprehension-pipeline]]; they are the empirical input `b42.2` was waiting on.
