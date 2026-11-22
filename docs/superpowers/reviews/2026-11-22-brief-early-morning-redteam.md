# Red team: brief at 04:30 with a real-time fallback (plan 2026-11-22)

Reviewed: `docs/superpowers/plans/2026-11-22-brief-early-morning.md` against spec
`2026-11-21-storylines-design.md` §6.1 and §12, and the code at HEAD `5663ebd`.
Read-only review; nothing was run except greps. Not re-argued: the 04:30 decision, and the
Anthropic cancel / `pause_turn` facts (the one docs check made, on non-streaming limits, found
only the SDK-side 21,333-token check and an idle-connection warning, neither contrary).

## The three objections

### 1. (b) "test_collect_day_key.py must still pass unchanged" is false, and two more tests break that the plan does not name

`tests/test_collect_day_key.py:30` stubs `monkeypatch.setattr(brief, "poll_batch", lambda bid: "RAW")`.
The stub takes exactly one argument. Task 4's `obtain_brief` step 2 calls
`poll_batch(batch_id, max_wait_secs=wait)`, and the default `BRIEF_FALLBACK_AT` is `06:30`
(conftest's `_stubbed_config` resolves every knob to its default), so `wait` is an int and the call
raises `TypeError: <lambda>() got an unexpected keyword argument 'max_wait_secs'`. Nothing in
`mode_collect` catches around the poll (`brief.py:3193`), so the test fails. It is not an edge: it
fails on every run, whatever the wall clock (after 06:30 `wait` is 0, still passed).

The same one-argument stub sits in two tests the plan never mentions:

- `tests/test_commands.py:1524` (`test_collect_finishes_when_the_nudge_fails`)
- `tests/test_trading.py:598` (`test_collect_trading_failure_does_not_duplicate_brief`)

An implementer told the test must pass "unchanged" has two bad options: edit the tests against the
plan, or bend `obtain_brief` (e.g. call `poll_batch(batch_id)` positionally with no wait), which
silently removes the fallback. Fix: in all three, stub `obtain_brief` (or make the stub
`lambda bid, **kw: "RAW"`), and say so in Task 4's file list.

The sentence's "which proves `obtain_brief` takes the batch path on a first-call result" is also
unsupported even if the test passed: the stub answers `"RAW"` on every call, so a test that passes
cannot tell a first-call result from a post-cancel one. The new `obtain_brief` unit tests are what
prove that; drop the claim.

### 2. (a) The fallback does not cover the failure it is most likely to meet: an unhealthy API raises out of collect before the fallback runs

The spec promises "The brief is late, never lost." Two paths lose it:

- **Unguarded polls.** `poll_batch` calls `resp.raise_for_status()` with no handler
  (`brief.py:2903-2904`), and so does `fetch_batch_results` (`brief.py:2984-2985`). Today collect
  polls a batch that ended hours earlier: one GET, one results fetch. Under the plan, collect polls a
  batch submitted 90 minutes (or, with submit's 60-min grace, 30 minutes) earlier for up to 30
  minutes (GETs at about 0, 60, 150, 285, 487 s, ... about 6-7), then up to 600 s more after the
  cancel (about 5 more). Any 5xx, 529, `ConnectionError` or the 15 s `ReadTimeout` on any of those
  propagates out of `obtain_brief` and `mode_collect`: no cancel, no real-time call, and none of
  collect's own alert branch (`brief.py:3262-3266`) runs. The days the batch is slow are the days
  the API is degraded, so the failure is correlated with the condition the fallback exists for.
  The plan's Review Focus lists five cases; none is "a status GET fails". Fix: in `obtain_brief`,
  treat a `requests.RequestException` from either poll as "not ended" (log it with `resp.text`) and
  carry on to the next step; add the test.
- **`max_attempts=1` charges a retry ban to failures that spend nothing.** The plan justifies one
  attempt by "a retry would double a multi-minute spend". That is true of a read timeout, not of a
  429, a 529 or a refused connection, which spend nothing. One 529 at 06:30 returns `None` and the
  day has no brief. This is the project's own `retry-budget-needs-a-verdict` lesson. Fix: retry
  (once, with a short backoff) on 429/529/5xx and connect errors; never on a read timeout.

### 3. (c) The real-time path is never run against the real API before the bead closes, and when it fails it is logged as something else

Every Task 3 and Task 4 test stubs `call` or `run_brief_realtime`. Task 5's first-run check looks
for `brief_path=batch` and the request file, then closes `news-brief-qap`. So the first real
execution of `run_brief_realtime` is the first bad morning. This is the shape of the 2026-06-25
signals regression (`signals-extraction-separate-call-followup.md`): the unit tests stubbed the
`call` seam, so a timeout that was wrong in production was never exercised. Specifically:

- **The 600 s timeout is not derived from anything.** The replayed request has `max_tokens` 16384
  (`brief.py:125`), adaptive thinking and an uncapped server-side web-search loop. Its own comment
  (`brief.py:2870-2872`) sized that budget on the grounds that "the Batch API has no HTTP timeout to
  worry about". Spec §6.1 asks for a timeout "sized to a brief with web search", and the plan's
  Step 3 says the knob comment should explain "why the default is what it is (§6.1)", but §6.1
  gives no number. The plan does not log the call's elapsed time either, so the knob can never be
  retuned from evidence (`a-config-can-be-accepted-and-inert`).
- **Failures are misattributed.** `_post_messages` logs every failure as
  `Signals API attempt {n} failed` (`brief.py:2799`) and re-raises without the response body. A
  fallback brief that fails will read in the host log as a signals failure, against the plan's own
  constraint "Log any HTTP error's `resp.text`". Fix: give `_post_messages` a `label` parameter, or
  have `run_brief_realtime` catch `HTTPError` and log `e.response.text`.
- **In six months:** a `NEWSBRIEF_MODEL` row change to a slower model, or a larger brief, makes 600 s
  too short with no log line saying so, and each fallback day is a full-price call with no brief.

Fix: add a Task 5 step that forces the fallback once on the host (a manual collect with
`BRIEF_FALLBACK_AT` set to a time already passed, against a batch already cancelled, or a one-off
script that replays a saved `brief_request-*.json` through `run_brief_realtime`) and records the
elapsed seconds, stop_reason, continuation count and usage. Close the bead only after that.

## Fixed check: paired facts and what a reader infers

| Plan sentence | The pair | Inferred third claim | Verdict |
|---|---|---|---|
| Task 4 Step 3: "`test_collect_day_key.py` stubs `poll_batch`. It must still pass unchanged, which proves `obtain_brief` takes the batch path on a first-call result." | stubs poll_batch; passes unchanged | (i) it is the only existing test touching this; (ii) passing proves first-call batch path | **Both unsupported, premise false.** Residual: 3 one-argument stubs exist (test_collect_day_key.py:30, test_commands.py:1524, test_trading.py:598); 1 named + 2 unnamed = 3. All 3 raise TypeError. Objection 1. |
| Global Constraints: "`off` disables the fallback (collect waits the full 12 h as today)" (also spec §12) | `off` disables the fallback; the poll default is 43200 s | collect can in fact wait 12 h | **Unsupported, false.** `JOB_MAX_RUNTIME_MINUTES` is 240 (`common.py:319`); `supervisor._enforce_max_runtime` (`supervisor.py:419-453`) SIGTERMs collect at 4 h with an alert "being stopped ... will not be retried". `off` means "wait about 4 h, then be killed". `test_obtain_brief_with_the_fallback_off_waits_the_full_window` asserts 43200 and so enshrines the wrong reading. Reword to "waits until the supervisor's runtime cap". |
| Global Constraints: "A brief is delivered at most once per collect run. A real-time call happens only when the batch yields no succeeded result." | at most one delivery; real time only on no observed success | a fallback day is never billed for both a batch and a real-time call | **Unsupported.** After the 600 s cancel wait, a non-interruptible in-flight request can still complete and be billed at batch price while the real-time call is billed at full price. Spec §6.1's "full price instead of half" (+$0.25-0.35) understates that case (about 1.5x the batch price on top). Delivery is still once, since nobody reads the late result. Cost note only. |
| Task 5 Step 3: "`brief_path=batch`, and `brief_request-<date>.json` exists" | batch path taken; request persisted | the feature is verified and the bead can close | **Unsupported.** Neither fact exercises cancel or the real-time call. Folded into objection 3. |
| Task 1 test: `sum(sleeps) == 100  # 60, then the remaining 40`, `len(gets) == 3` | — | — | Checked: GETs at t=1000, 1060, 1100; sleeps 60 + 40 = 100; 3 GETs. Parts sum. Supported. |
| Task 1 Step 2: "the deadline-passed poll makes 0 GETs" | — | — | Checked against `brief.py:2902` (`while time.time() < deadline`). Supported. |

The two flagged implicatures not folded into an objection (the `off` cap, and the double billing)
are weaker than objections 1-3: the first changes a sentence and one test assertion, the second
is a cost footnote. Neither displaces an objection.

## Smaller findings (not in the three)

- **Task 2's submit test is pseudo-code with no harness to borrow.** "Stub mode_submit's inputs as
  test_collect_day_key.py stubs collect's" points at a stub set that covers none of submit's inputs.
  No existing test drives `mode_submit` (grep for `mode_submit` under `tests/` finds only a docstring
  mention). `mode_submit` reaches `fetch_feed_blocks` (feedparser, urllib), `fetch_mauldin_twie`,
  `fetch_web_source`, `build_chroma_context`, `fetch_portfolio_weights`, `build_market_pulse`,
  `build_enrichment` and more, and the socket-level guard (`conftest.py:216-240`) fails any test that
  attempts a connection, swallowed or not. Expect about 20 stubs. A cheaper test that proves the same
  property: make `submit_batch` take the params dict, so the test can check that `save_brief_request`
  and the POST receive the same object.
- **scheduler.py comments go stale on fallback days.** `backup` at 07:00 is justified as running
  "AFTER the 06:00 collect" (`scheduler.py:59-61`), and `watch` at 06:30 so an alert "lands with the
  morning brief" (`scheduler.py:69-70`). A fallback collect runs to about 06:30 + 10 min cancel wait
  + up to 4 x 600 s, so backup can snapshot before collect's writes. The plan rewrites only the
  submit part of that block. Mention it, or accept it in the comment.
- **The compose anchor line does not seed this host.** `config.import_settings_from_env` writes
  rows only when `settings` is empty (`config.py:233-236`). On the existing deployment an env value
  for either new knob is reported as ignored, not applied. The plan doesn't claim otherwise, but
  Task 5 should say "set it as a settings row" if the operator wants a non-default value.
- `run_brief_realtime`'s usage log should carry a cost estimate at full price beside the batch
  line's `est_batch_cost`, or §11's measurement will price fallback days at batch rates.

## Should red-teaming continue?

**Stop after these fixes.** Every objection here is concrete and local: three test stubs, one
exception guard plus a retry rule, and one host verification step with a log line. None needs a
spec change or re-opens a settled decision. A further round on the revised plan would most likely
produce wording-level findings. Check the fixes in the implementation review instead, and in
particular confirm by running them that the three named collect tests pass.
