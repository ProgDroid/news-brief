# Watch-file event triggers — design (news-brief-8lp)

Date: 2026-11-18. Status: draft for approval, revised after one red-team round
(`docs/superpowers/reviews/2026-11-18-watch-event-triggers-redteam.md`; the user
chose to fold its fixes in and skip a second round). **2026-11-19: scheduled
provisionally BEFORE the validation gate, by user decision** (an unvalidated
watcher beats none; until `news-brief-8lp.1` passes, a quiet night means
"probably nothing", not "verified nothing"). Step 2 (price rows) is
news-brief-vap and is out of scope here except where the file loader is shared.

## Intent

The operator keeps a rebuy watchlist for a T212 ISA in a **private** repo and
pushes it to the host as `DATA_DIR/watch.json`. Some rows are events ("a
ceasefire is formally signed"). The operator wants to be told, with links to
check, when the news says such an event has happened, and then act on it in the
private repo. news-brief already reads the day's news, so it judges the rows.

Success: a real trigger reaches Telegram within a day, naming the row and
carrying links; a quiet night is provably a judged night (`/jobs`), never a
skipped one; and nothing about the watchlist enters this public repo.

## Goals

- Nightly verdict per `kind: "event"` row: `triggered` / `not` / `unclear`,
  each with cited evidence.
- Telegram alert for `triggered`, and for `unclear` **with** evidence, naming
  the row by id + name, with the evidence links.
- **Once per row** (user decision 2026-11-18): a row alerts the first time it
  reaches a given strength and stays quiet after that until its id or trigger
  text changes. `unclear → triggered` alerts again, since the verdict got
  stronger. `triggered → unclear/not` sends nothing.
- A missing, unparseable or unknown-version file, or a window with no captured
  items, is reported: job red in `/jobs` + Telegram. Never a silent skip, never
  a "not triggered".

## Non-goals

- Price rows (`kind: "price"`) — news-brief-vap. This job ignores them, but the
  shared loader validates them so a bad price row is caught on day one.
- Changing the morning brief. The brief prompt and output are untouched.
- Acting on a trigger. Alerts only.
- Reading or storing reasons, sizes or amounts. The file holds none by design.

## Anti-goals (a failure even if it "works")

- A `not` verdict produced from news the judge never saw. Hence no per-row text
  pre-filter: trigram matching is lexical, so "ceasefire signed" would never
  meet "truce agreed" and would come back as a clean `not`.
- `watch.json`, row names or triggers committed to this repo, written into
  tests as real values, or logged at INFO in a way that ends up in committed
  output. Tests use made-up rows like the bead's example.
- An alert link that the model invented. Links come from code, never from the
  model.

## Approaches considered

1. **Judge the finished brief** (post-delivery forced-tool call, like
   signals). Cheapest, but it only sees what the brief chose to cover, and the
   brief and its stored source index carry no URLs. Rejected.
2. **Judge the day's captured items** (titles + URLs from the KB `items`
   table, filled by capture every 30 min). Real links; independent of the
   brief's editorial choices; one extra call. **Picked** (user, 2026-11-18).
3. **Fold the rows into the brief prompt** so the web-searching brief model
   looks for them. Best search, but it entangles structured verdicts with
   free-text generation and changes the brief. Rejected.

Measured input size (host, 2026-11-11..18): 392–1,030 items/day, so all
titles fit one call (~20k input tokens). No cut is needed. If that grows ~10x,
revisit with a per-outlet cap, never a lexical filter.

## Design

### File contract

As written in the bead (version 1; `rows[]` of `id`, `kind`, `name`, plus
`trigger` for events, `signal`/`condition`/`level`/`action` for prices).
The loader has two tiers (revised after red-team objection 3: one bad price
row must not blind event judging, and a new kind in the private generator must
not turn both jobs red):

- **File-level: the whole file is rejected.** That's a missing file, invalid
  JSON, a non-object top level, `version != 1`, `rows` not a list, or duplicate
  ids. These raise `WatchFileError` with a one-line reason, so the job goes red
  and Telegram says why.
- **Row-level: only that row is skipped.** That's a row without a string
  `id`/`kind`/`name`, an event row without a non-empty `trigger`, a price row
  that breaks the bead's contract (condition in `below|above|above_ma200`,
  numeric `level` unless `above_ma200`, string `signal`), or an **unknown
  `kind`**. The loader returns `(rows, skipped)`, where `skipped` holds
  `(row id or index, reason)`. The remaining rows are judged as normal.
  Skipped rows are reported **every run**, in one Telegram message: the file
  needs fixing, so this deliberately doesn't use the once-per-row rule. The
  job still exits 0, because it judged what it could.
- The file is never renamed or modified (not `_load_json_or`, which
  quarantines). The next push replaces it.
- Zero valid event rows is valid: the job records a clean run and makes no
  model call.

### Where it runs

A new scheduled job **`watch`**: `Schedule("watch", "daily", "06:30", None,
grace_minutes=180)`. It's added to `SCHEDULES`, `MODES` and `JOB_MODES`, so
`run_job` gives it the lock, the `job_runs` row, and the existing crash path.
A raise there means exit 1, `/jobs` red, and `telegram_alert("watch crashed:
WatchFileError: …")`. That is the "say so in /jobs" requirement at no new
cost. 06:30 puts it just after the 06:00 collect, so an alert lands with the
morning brief. It doesn't depend on collect.

### Window

A **fixed trailing 48 h** of items by `created_at`, ending at the run
(revised after red-team objection 1). The once-per-row alert state already
absorbs the overlap, so judging the same items twice costs tokens but never a
second alert. That buys three things:

- A crashed or missed night is covered by the next one, with no bookkeeping.
  The rejected alternative, "since the last finished run", was wrong because
  `job_runs` records a crashed run as `status='finished'` too
  (`supervisor._closing_status`), so the window would have moved past items a
  crash never judged.
- There is no cap that silently drops items.
- A run can be replayed for any date: the window is a pure function of
  `as_of` (see Validation gate).

Zero items in the window raises (`"no captured items in the 48 h to …:
capture may be down"`). It is never judged as `not`.

**Input ceiling:** if the window has more than 5,000 titles after syndication
is collapsed, the job still judges, sends a once-per-episode Telegram warning
(`_alert_once` shape) and logs the count. That is the signal to revisit the
"one call" decision. Each run logs its input count either way.

### Judge input

- Items: `title`, `url` and outlet name (join `outlets`). Identical titles
  across outlets (syndication) are collapsed to one line, keeping each URL for
  evidence. Titles only, no bodies: bodies multiply the input many times, and
  a titles-only judge's doubt resolves to `unclear`, which alerts (see below).
- Each item line carries an **opaque label** `I1..In` assigned per run, never
  the database id (memory `never-give-a-model-raw-database-ids`).
- Rows are given as `id`, `name` and `trigger`.

### The call

- A forced tool call `emit_verdicts` through `_post_messages`, with the
  timeout and attempts **sized for this call** (not the signals defaults;
  memory `shared-helper-carries-first-callers-tuning`).
- `thinking` set explicitly (disabled), and `max_tokens` sized to rows × a
  per-verdict budget. On parse failure, log `stop_reason` first (memory
  `signals-parse-error-is-truncation`).
- Model: a new knob `WATCH_MODEL` (default `""` = follow `MODEL`). It's a
  `KNOBS` entry plus the compose anchor line (memory
  `env-var-needs-compose-passthrough`).
- Schema: `verdicts[]` of `{row_id, verdict: triggered|not|unclear,
  evidence: [label], reason: one sentence}`.

### Validating the answer (code, not the model)

- Every event row must get exactly one verdict. A missing row, an unknown
  `row_id`, or an unknown evidence label makes **that row's** verdict
  `invalid`. An invalid verdict is reported (in the alert batch, as
  "couldn't judge e1: …"), never dropped and never treated as `not`.
- `triggered` with no evidence is downgraded to `unclear` without evidence:
  no alert, but logged.
- Evidence labels map to URLs in code, and each verdict links at most 3.

### Alerting (once per row)

- Strength: `unclear-with-evidence` = 1, `triggered` = 2.
- State: one `runtime_state` key per row, for example `watch.alerted.<row_id>`,
  holding `{trigger_hash, strength}`. An alert is sent when there is no key,
  when the trigger hash differs (the row was edited), or when the new strength
  is above the stored one. Send **before** storing, the `_alert_once` order.
- Keys for rows no longer in the file are cleared, so a removed row that is
  later re-added alerts again.
- Message, one per alerting row:
  `🔔 Watch e1: Example ceasefire signed. TRIGGERED` + the reason + up to 3
  links (`outlet: title`, linked). It is HTML-escaped and sent with
  `telegram_send_long`.
- **A failed send fails the job.** `telegram_send_long` returns False rather
  than raising, so the job checks the result. On False it stores no state for
  that row and raises after attempting the rest. The job goes red in `/jobs`,
  and the next night, which still sees the same items in its 48 h window,
  sends it again.
- A clean night (nothing alerts) sends nothing. `job_runs` records it, and that
  is the proof it ran.

### Module layout

A new top-level `watch.py` holds the loader (shared with vap), the request
builder, the response validator and the alert-state logic, all pure functions
over plain data. A thin `mode_watch` in `brief.py` wires DB, call, send and
state. The new module needs the Dockerfile `COPY` line and the two workflow
lists, and `tests/test_packaging.py` enforces that (memory
`dockerfile-copy-allowlist`).

### Validation gate: before the schedule is switched on

All the tests below check plumbing. None of them measures the success
criterion: does a titles-only judge actually find a real trigger (red-team
objection 2)? So:

1. The build exposes the judge as a replay command,
   `scripts/watch_replay.py --as-of <date> --rows <made-up rows file>`, run on
   the host as `docker compose run --rm --entrypoint python newsbrief
   scripts/watch_replay.py …` (the image's entrypoint is `python brief.py`,
   so `--entrypoint python` is required). It runs
   the production window, input builder, call and validator for that date, and
   **prints the verdicts instead of alerting or writing state**. It calls the
   production functions with an `as_of` and never reimplements them (memory
   `reconstruction-drifts-from-production`).
2. **Before running anything**, the operator and the agent pick 3–5 past
   events covered by the KB (captured items go back to 2026-09-04), write a
   made-up row for each, plus 2 rows that should be `not` on those dates, and
   **write the expected verdicts into this spec's bead**.
3. The operator runs the replays on the host (the items live there). It
   passes if every expected `triggered` comes back `triggered` or
   `unclear-with-evidence` with a correct link, and no expected-`not` row
   alerts. On a miss, the design is revisited (bodies? a different cut?), not
   the expectations.
4. The `Schedule("watch", …)` entry lands only after the gate passes, as its
   own commit. Until then the job exists, can be run by hand, and is not
   scheduled. No enable-knob is needed.

This is the one **hands-on** step: only the operator can run against the host
DB and judge whether a link is the real event.

### Tests (pre-registered properties)

- Loader: each file-level case raises with its reason. Each row-level case
  skips only that row and names it, and the other rows survive. An unknown
  kind is skipped, not fatal. The file is left byte-identical. Zero event rows
  is valid.
- Window: exactly the 48 h before `as_of`; zero items raises; over 5,000
  titles warns but still judges.
- A failed send stores no state for that row and the job exits non-zero.
- Validator: unknown row id, missing row, unknown label → `invalid`;
  triggered without evidence → downgraded; labels → the right URLs.
- Alert state: first trigger sends; repeat stays silent; edited trigger
  re-sends; `unclear→triggered` re-sends; `triggered→unclear` stays silent; a
  removed row's key is cleared. Send-before-store, so a failing send stores
  nothing.
- The tests patch all three Telegram send channels (memory
  `telegram-send-long-convention`).
- The DB-backed window query runs against real Postgres, and the run count
  must move. A skip is not a pass.

## Left to the builder

- The exact prompt wording.
- The per-verdict token budget and timeout, sized from one measured call.
- The `runtime_state` key format, if one dict key fits that store better.
- Whether `/jobs` gets a one-line verdict summary (nice to have, not required).

## Calls made by the agent (not decided by the user)

06:30 as its own job; the fixed 48 h window; titles only, with syndication
collapsed; two-tier rejection (file-level vs row-level); the 5,000-title
warning ceiling; the `WATCH_MODEL` knob following `MODEL`; at most 3 links per
alert; `triggered` without evidence downgraded to `unclear`; the schedule
landing only after the replay gate.

Red-team points declined: "the loader exposes the watchlist's structure in
public code". The schema is already public in the bead, and no row content is
ever committed.
