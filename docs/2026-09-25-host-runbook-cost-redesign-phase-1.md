# Host runbook — comprehension cost redesign, phase 1 restart

**Date:** 2026-09-25
**Beads:** `news-brief-2r5` (epic — stays open until every step below is verified by
effect), `news-brief-2r5.11` (this runbook), `news-brief-0rg` (billing-error charging
fixed in this phase), `news-brief-li9` (makes `zp8` check continuity — not yet built,
which is why step 2's ordering below is a hard constraint), `news-brief-eqs` (timed-out
requests billed but possibly never debited — watch for in step 7)
**Spec:** `docs/superpowers/specs/2026-09-25-comprehension-cost-redesign-design.md`
**Supersedes, for the restart:** `docs/2026-09-22-host-runbook-comprehension-restart.md`
— that runbook's step 5 (the Amendment 2 gate) is still run from there; nothing else in
it applies to this restart.

This exists because every remaining step is a **host** action. The phase-1 code (the
Reuters quote-page filter, the surge alert, the account-failure abort, migrations
0014/0015, the triage rules, newest-first with a 14-day horizon, the spend ledger, the
token bucket, and the abort/budget alerts) is committed and green. Nothing further
happens in this repo until the operator runs the steps below, in this order, on the host.

## Status

| step | state |
|---|---|
| 1 — deploy with `COMPREHEND_ENABLED` still false | outstanding |
| 2 — run the OLD gate (not before 2026-09-30 00:13:37Z) | outstanding — **do this before step 3** |
| 3 — set `NEWSBRIEF_TRIAGE_MODEL` | outstanding |
| 4 — run the §4.6 recovery SQL | outstanding |
| 5 — flip `COMPREHEND_ENABLED` | outstanding |
| 6 — deliberate exhaustion check | outstanding |
| 7 — after 7 days, record and compare | outstanding |

---

## Step 1 — deploy phase 1, with `COMPREHEND_ENABLED` still false

Capture's changes (the Reuters quote-page filter, the per-outlet surge alert) take
effect immediately on deploy and need no flag. Comprehension's changes stay inert until
step 5.

**Getting `<deploy time>`.** As with the old runbook's step 0, this host's image-pull step
is not verified to be automatic — confirm the running container is on the new image first,
the same way step 0 there asks, and use the moment of that confirmation as `<deploy time>`.
A container restart time (`docker inspect --format '{{.State.StartedAt}}'`) shows only the
*last* start, so treat it as a lower bound on the deploy, not proof of this specific one.

Confirm by effect that capture changed — never by reading a row back:

```sql
SELECT count(*) FROM items i JOIN outlets o ON o.id = i.outlet_id
 WHERE o.name = 'Reuters' AND i.created_at > '<deploy time>'
   AND (i.title ~ '^[A-Z0-9^=][A-Z0-9.^=\-]*\s+-\s+Reuters$'
        OR i.title ILIKE '%stock price & latest news%'
        OR i.title ILIKE '%stock price &amp; latest news%');
```

Expected: **0**.

**Positive control first.** Run the same query with `i.created_at BETWEEN '2026-09-20'
AND '2026-09-21'`. It must return hundreds. Without that control, a 0 cannot be told
apart from a pattern that matches nothing (`the-corpus-is-about-the-thing-you-searched-for`
territory — a probe that never fires proves nothing about the world).

Also check the capture log for the `quote pages dropped` field in its per-pass tally
line (`{tally.quote_pages_dropped} quote pages dropped, ...` in `capture.py`) — it must
be non-zero on the next capture pass after deploy.

## Step 2 — run the OLD gate, on or after 2026-09-30 00:13:37Z

**Run this exactly as `docs/2026-09-22-host-runbook-comprehension-restart.md` step 5
says**, and record the output verbatim in `docs/`.

**Comprehension must stay off until this step has run — do not flip
`COMPREHEND_ENABLED` before this.** From spec §6.1, verbatim:

> `zp8` sees a corpus that stopped. It cannot see a HOLE. It checks only the newest
> event, never continuity through the window. If comprehension restarted before
> **2026-09-29 12:13:37Z** (`end − 6h` for a run at the earliest honest time),
> `last_event_at` would be fresh. The gate would then return a binding, no-appeal
> verdict on a cohort mixing the old pipeline, a multi-day gap and the new one. The
> first draft of this spec missed this; the red-team caught it.
>
> So the ordering is a hard constraint. Comprehension stays OFF until the old gate has
> run. The runbook's step 5 runs as written, on or after 2026-09-30 00:13:37Z, and its
> output is recorded verbatim. Only then does phase 1 flip (D7). Phase 1 takes days to
> build, so the constraint costs nothing. It must still be written into the runbook,
> because nothing in code enforces it. `news-brief-li9` makes `zp8` check continuity
> (e.g. the largest gap between consecutive events inside the cohort), so a future hole
> refuses on its own rather than relying on the ordering being remembered. If it returns
> a verdict rather than refusing, that verdict stands.

Nothing appeals this run. Whatever `docs/2026-09-22-host-runbook-comprehension-restart.md`
step 5 returns is the recorded result.

## Step 3 — set the `NEWSBRIEF_TRIAGE_MODEL` row

Same `INSERT ... ON CONFLICT (key) WHERE user_id IS NULL` shape as the old runbook's
step 2:

```sql
INSERT INTO settings (key, user_id, value)
VALUES ('NEWSBRIEF_TRIAGE_MODEL', NULL, 'claude-haiku-4-5')
ON CONFLICT (key) WHERE user_id IS NULL
DO UPDATE SET value = EXCLUDED.value, updated_at = now()
RETURNING key, value, now() AS cutover;
```

**The key is `NEWSBRIEF_TRIAGE_MODEL`, NOT `TRIAGE_MODEL`.** The knob is declared with
`env="NEWSBRIEF_TRIAGE_MODEL"` in `common.py`'s `KNOBS` table, and `Knob.key` stores and
reads that env name — a `TRIAGE_MODEL` row is accepted and read by nothing, and triage
would silently stay on Sonnet at twice the planned price.

**Verify by effect, after the first pass:**

```sql
SELECT model, count(*) FROM comprehend_spend WHERE stage = 'triage' GROUP BY 1;
```

must show `claude-haiku-4-5`. The model name is not in the call's log line, and Sonnet
also returns `tool_use`, so nothing else can tell you.

**M2: this row has no effect yet.** Comprehension stays off (`COMPREHEND_ENABLED` is
still `false`) until step 5, so setting this row now only stages it — the first pass
that actually calls Haiku happens under step 5, which is where watching it belongs; see
that step for what to expect and what to revert if it goes wrong.

## Step 4 — run the §4.6 recovery SQL

Copied verbatim from the spec:

```sql
UPDATE item_triage SET integrate_attempts = 0, integrate_defers = 0
 WHERE integrated_at IS NULL AND (integrate_attempts > 0 OR integrate_defers > 0);
UPDATE item_triage SET attempts = 0 WHERE verdict = 'failed';
```

This is blanket rather than scoped to the outage window — there is no `updated_at` on
`item_triage` to scope it by. Acceptable because `failures={}` since 2026-09-08 says
genuinely bad items are rare; a few re-fail and are re-charged at bounded cost. Most
outage-hit items will be past the 14-day horizon by then and become `stale` anyway.

**Record the two row counts both statements return.**

## Step 5 — flip `COMPREHEND_ENABLED`

**Pre-flip check (M3).** Confirm every model settings row this deploy might read is
actually priced, BEFORE the flip can spend against one that is not:

```sql
SELECT key, value FROM settings
 WHERE key IN ('NEWSBRIEF_MODEL', 'NEWSBRIEF_INTEGRATE_MODEL', 'NEWSBRIEF_TRIAGE_MODEL')
   AND user_id IS NULL;
```

(These are the real key names — verified against `common.py`'s `KNOBS` table, which
declares `MODEL` with `env="NEWSBRIEF_MODEL"`, `INTEGRATE_MODEL` with
`env="NEWSBRIEF_INTEGRATE_MODEL"`, and `TRIAGE_MODEL` with `env="NEWSBRIEF_TRIAGE_MODEL"`
— `Knob.key` reads and writes exactly that env name, the same trap step 3 already warns
about for the triage row.) Every `value` returned must be one of the keys in
`comprehend.PRICES_PER_MTOK` — currently `claude-sonnet-5`, `claude-haiku-4-5`, and
`claude-haiku-4-5-20251001`. A row naming anything else means the pass aborts on
`unpriced_model` the moment it flips on (spec 4.3) — better caught here than on the
first `comprehend_abort_alert`. A row absent from the result is fine: an unset knob
falls back to `MODEL` (Sonnet), which is always priced.

Same statement shape as the old runbook's step 2:

```sql
INSERT INTO settings (key, user_id, value)
VALUES ('COMPREHEND_ENABLED', NULL, 'true')
ON CONFLICT (key) WHERE user_id IS NULL
DO UPDATE SET value = EXCLUDED.value, updated_at = now()
RETURNING key, value, now() AS cutover;
```

**Record the returned `cutover` verbatim, in `docs/`** — the same place step 2's gate
output is recorded. It is the `<flip>` value step 7 needs, seven days from now.

**M1: the first 24h can spend up to roughly 2x the daily allowance, and that is by
design, not a bug.** The token bucket accrues continuously and caps at
`COMPREHEND_BUDGET_MAX_DAYS` (default 3) days of `COMPREHEND_DAILY_BUDGET_USD` (default
$1.50); a bucket that has never been debited opens at one full day's allowance
(`accrue`'s `fresh` case), so the FIRST hour is bounded by that one day's allowance
($1.50) plus at most one more call before `can_spend()` next gets checked, and the
first 24h as a whole can spend up to the two days' worth (~$3.00) sitting in the bucket
at flip time. It settles to the steady-state ~$1.50/day rate from the second day on.

**Watch the first pass after this flip (moved from step 3, M2: setting
`NEWSBRIEF_TRIAGE_MODEL` in step 3 had no effect while `COMPREHEND_ENABLED` was still
false, so THIS is the first pass that actually calls Haiku).** The
`thinking: {"type": "disabled"}` shape the triage call sends is what Sonnet 5 requires;
whether Haiku 4.5 accepts the same shape is **documented-not-observed** — the
claude-api skill's error tables list a 400 on this shape only for Fable 5/5.1, Opus 5.5,
and Opus 5 at `xhigh`/`max` thinking, not for Haiku 4.5. A 400 is a 4xx, and
`comprehend.py`'s `_is_transient` check spares only 429 and 5xx — so a 400 here would be
**charged to the batch's items**, not merely logged.

Expect, in the log, a `Comprehend: triage call took ...s stop_reason=tool_use ...` line
and **no** `Comprehend: triage batch failed` warning. If a 400 appears instead — either
as the failure warning or as a non-`tool_use` `stop_reason` — **revert
`NEWSBRIEF_TRIAGE_MODEL` immediately** (back to `''` or the prior value) before the next
hourly pass (or flip `COMPREHEND_ENABLED` back off), so the items just charged are the
only ones.

**Verify by effect within the hour:**

```sql
SELECT count(*), max(at) FROM comprehend_spend;
```

plus a `stale` count and a `quote_page` count in `item_triage`, plus a log line showing
spend and balance.

**Use the real field names.** The pass's per-run `Tally` (`comprehend.py`) is logged
whole as `Comprehend: {tally}` at the end of `run()`, and its dataclass repr prints every
field by name. The fields that matter here are `stale`, `quote_pages`, `aged_out`,
`spent_usd`, and `budget_balance_usd` — read them out of that log line, not out of
memory of what they might be called. (`quote_pages` is the run-level tally on the
comprehend side; capture's own tally field for the same idea is spelled differently,
`quote_pages_dropped` — don't cross-reference the two names as if they were one field.)

## Step 6 — the deliberate-exhaustion check

**Check first whether this has already been proven (I3).** `_alert_once` (`brief.py`)
keys the budget alert on `comprehend_budget_alert` (that is the real key —
`brief.COMPREHEND_BUDGET_ALERT_KEY` — not a guess), and stores the episode key
`budget:<UTC date>` as a JSON-encoded string, e.g. `"budget:2026-10-02"` (note the
literal double quotes — `config.runtime_state()` decodes it with `json.loads`, so the
row's raw value carries them). Run:

```sql
SELECT value FROM runtime_state WHERE key = 'comprehend_budget_alert';
```

If it already holds **today's** key (`"budget:<today's UTC date>"`), the bucket has
already run dry naturally today — likely on flip day, working through the backlog — and
the alert path has already fired and been verified by that natural exhaustion. **Record
that value here as the verification and skip the deliberate $0.01 step below**, forcing
it now would spend real money to re-prove something today's log already proved, and
because `_alert_once` keys on the date, this deliberate check would send NO message at
all (correct behaviour, but indistinguishable from broken without this note). If the row
is absent, or holds an older date, proceed with the deliberate check below on a UTC day
that has not yet naturally exhausted the budget.

Set the `COMPREHEND_DAILY_BUDGET_USD` row to `0.01` for one pass.

**Expect the pass to stop with `budget_exhausted=True`.** The cap on accumulated balance
is `COMPREHEND_BUDGET_MAX_DAYS` (default `3`), so at a `0.01`/day allowance the balance
can hold up to **$0.03** (3 days × $0.01) before this test starts — the pass may spend up
to roughly that much before it stops. That is expected, not a bug in the test.

**Expect exactly ONE Telegram budget message.** `budget_verdict` keys the alert on the
UTC day the budget ran dry (`budget:<date>`), so it fires once per day the budget runs
out, not once per pass. It may not arrive immediately: `comprehend` and `monitor` are two
**independent** interval schedules in `scheduler.SCHEDULES` (both `every_minutes=60`,
not chained to each other), so the alert can lag up to a further ~60 minutes after the
pass that actually hit zero, arriving on `monitor`'s own next tick rather than the
instant comprehension stopped.

Then **restore `COMPREHEND_DAILY_BUDGET_USD` to `1.50`.**

## Step 7 — after 7 days, record and compare

`<flip>` is the `cutover` step 5 recorded in `docs/`. If it was not recorded, recover it
with:

```sql
SELECT updated_at FROM settings WHERE key = 'COMPREHEND_ENABLED' AND user_id IS NULL;
```

That fallback is safe here: `settings.updated_at` is `TIMESTAMPTZ NOT NULL DEFAULT now()`
(`migrations/0001_runtime_foundation_up.sql`), set by the same `ON CONFLICT ... DO UPDATE
SET ... updated_at = now()` step 5 ran, and nothing later in this runbook writes to the
`COMPREHEND_ENABLED` row again — steps 6 and 7 touch `COMPREHEND_DAILY_BUDGET_USD` only.

The material rate:

```sql
SELECT reason, count(*) FROM item_triage WHERE created_at > <flip> GROUP BY 1;
```

Daily spend from the ledger:

```sql
SELECT date_trunc('day', at), sum(usd) FROM comprehend_spend GROUP BY 1 ORDER BY 1;
```

Record both next to the spec's §4 estimate (~$1.40–2.40/day at 50–86% material; ~$0.56/day
at 20% material — the material rate is the biggest unknown in the spec, and this is what
measures it).

**Also compare the ledger's daily sum against the Anthropic console's daily usage — but
scoped to HAIKU only, not the account total (I4, reviewer finding).** The account total
is confounded and always reads higher than `comprehend_spend`, for reasons that have
nothing to do with `news-brief-eqs`: the daily brief and the weekly summary
(`brief.submit_batch`, `"model": common.MODEL`), signals (`brief._signals_model()`,
`common.SIGNALS_MODEL or common.MODEL`), the claim-verify judge
(`claim_verify._model()`, `common.CLAIM_VERIFY_MODEL or common.MODEL`), and
`scripts/inspect_integration.py` (M6 — a real paid call, outside the ledger and outside
the budget) all draw on the SAME Anthropic account and all default to `common.MODEL`
(Sonnet) — none of that spend is comprehension's, and none of it is in
`comprehend_spend` either, so comparing account totals just measures how much brief/
signals/claim-verify volume there was that week.

Verified by grep (`"model":` across `*.py`, checking every model knob's default in
`common.py`'s `KNOBS`), not assumed: `TRIAGE_MODEL`, `INTEGRATE_MODEL`, `SIGNALS_MODEL`,
and `CLAIM_VERIFY_MODEL` all default to `""` (falling back to `MODEL` = Sonnet), so
**until step 3's row is set, nothing in this codebase calls Haiku, and after it, only
triage does** — narrowing to Haiku isolates comprehension's triage stage cleanly from
everything else on the account.

**With one exception, also found by that grep, worth naming so it isn't
re-discovered:** `brief_memory.py`'s daily standing-claim reconcile call
(`reconcile_ledger`) hardcodes `RECONCILE_MODEL = "claude-haiku-4-5-20251001"` — a real,
paid Haiku call, independent of any settings row, that also never reaches
`comprehend_spend`. It runs once a day (alongside the daily brief), against triage's many
batches an hour, so it is a small, near-constant offset rather than a growing one — but a
Haiku-only comparison should still subtract roughly one reconcile call's tokens per day,
or the residual will look like uncorroborated comprehension spend when it cannot be.

Also account for triage's own retries when reading the Haiku total: `pending_triage`
retries a `failed` item while `attempts < 3` (an item starts at `attempts = 1` on its
first insert), so one stubborn item can cost up to three Haiku calls before
`gave_up_triage` (`attempts >= 3`) retires it — a multiplier on top of one-call-per-item,
not evidence of a leak.

After subtracting the reconcile offset (and accounting for retries), a consistent
shortfall in the ledger — the console's Haiku usage still higher than
`comprehend_spend`'s Haiku rows — is evidence for `news-brief-eqs` (timed-out requests
billed by Anthropic but never debited from the bucket, because a timeout never returns a
`usage` block to record against). Name the bead in whatever you write up, so the
shortfall isn't re-discovered from scratch later. The cleanest fix, if this matters
enough to chase precisely rather than bound: a separate API key or workspace for
comprehension, so the console can scope by key instead of by model.

---

## Alerts you may now see

Three new Telegram alerts exist as of this phase. Each fires once per episode (it keys on
a state row, and clears itself when the underlying condition clears), so seeing one twice
in a row for the same cause would itself be unexpected.

**Capture surge** (`capture.item_surge`, `capture.py`). Fires when an outlet's items in
the last 24h exceed 3× its trailing 7-day daily median **and** that excess is at least 200
items — a quiet outlet's small wobble can't trip it. Message shape:

> `N outlet(s) are capturing far more than usual. Every extra item is also paid for by
> comprehension:`
> `   <outlet>  <recent> items in 24h, daily median <median> over the previous <N> days`

This is the detector that would have caught the 2026-09-16 Reuters surge nine days
earlier. It asks the operator to look at why a source's volume jumped — a new upstream
behavior, a proxy issue, a real news event — because every one of those extra items now
has a comprehension cost attached.

**Comprehension abort** (`comprehend.abort_verdict`, `comprehend.py`). Fires when
comprehension is refusing to run **every** pass — an empty Anthropic balance, a refused
API key, or a model with no entry in `comprehend.PRICES_PER_MTOK`. Message shape:

> `Comprehension is stopping every pass: <advice>`

where `<advice>` is one of:
- billing: *"The Anthropic balance is empty. Top it up; nothing was charged to any item,
  and the next pass after that resumes on its own."*
- auth: *"The API key was refused (401/403). Check ANTHROPIC_API_KEY on the host."*
- unpriced model: *"Model '<model>' has no price in comprehend.PRICES_PER_MTOK, so
  comprehension refuses to spend on it. Add its price, or point the model settings row
  back at a priced model."*

This asks the operator to fix the account or key (billing/auth), or to add a price row
before pointing a settings row at a new model (unpriced) — in every case, nothing was
charged while this alert was live.

**Comprehension budget** (`comprehend.budget_verdict`, `comprehend.py`). Fires once per
UTC day the daily allowance ran out. Message shape:

> `Comprehension's budget ran out today (<date>). Balance $<balance> of $<allowance>/day
> (cap <max_days> days); <untriaged> items untriaged, <awaiting> awaiting integration;
> <stale_today> went stale today and <aged_out> have aged out unintegrated. It resumes as
> the allowance accrues. To spend more, raise the COMPREHEND_DAILY_BUDGET_USD settings
> row.`

This is expected and low-urgency on an ordinary day (it means the bucket did its job);
it asks the operator to raise `COMPREHEND_DAILY_BUDGET_USD` only if the accompanying
`stale`/`aged_out` counts are growing in a way that matters for the KB.

---

## What must not happen

- **Comprehension must not be enabled (step 5) before the old gate has run (step 2).**
  Spec §6.1: the gate cannot see a hole in the corpus, only whether the newest event is
  fresh, so an early flip would hand it an admissible-looking cohort that actually mixes
  the old pipeline, a multi-day gap, and the new one — with no appeal once it verdicts.
- **No threshold changes carried over from the old gate.** Nothing in this runbook
  reopens `CORROBORATION_FLOOR`, the ceiling, or the horizon; those stay exactly as the
  old runbook's step 5 already constrains them.
