# Amendment 2 gate run — recorded output (2026-10-02)

**Run under:** `docs/2026-09-25-host-runbook-cost-redesign-phase-1.md` step 2, which runs
`docs/2026-09-22-host-runbook-comprehension-restart.md` step 5 as written.
**Beads:** `news-brief-bqa.11` (the gate run), `news-brief-2r5` (phase-1 epic).
**Command (host):**

```sh
docker compose run --rm --entrypoint python newsbrief scripts/score_comprehension.py \
    --cutover '2026-09-22T18:13:37.571434Z' --horizon-hours 6
```

**Outcome: GATE NOT RESOLVED, NOT MEASURABLE (the corpus is FROZEN).** This is the outcome
written into the runbook's pre-flight section before the run: comprehension has been off since
the cost stop, so the newest event (2026-09-25 10:12 UTC) predates the cohort's end by more
than the 6h horizon. Per both runbooks, nothing appeals this run, and no threshold, horizon or
cutover is adjusted after seeing it. The `li9` continuity line is present: largest gap 169.3h,
from 2026-09-25 10:12 to 2026-10-02 11:30 UTC.

**Step 1 of the same runbook, recorded alongside:** capture's `quote pages dropped` was 0 in
every log line the operator checked. With no quote page stored since 2026-09-26 10:00:16Z, the
flood was stopped by `c439ade`'s feed move (Markets → Business), not by the filter, which has
had nothing to drop.

## Output, verbatim

```
=== Enum variance (spec 8.1) ===
PASS  events.type: n=14523 disclosure=54%, statement=23%, action=23% (>=10%: 3, top 54%)
PASS  events.commitment_state: n=5634 in_force=77%, proposed=11%, intended=9%, committed=3% (>=10%: 2, top 77%)
PASS  assertions.standing: n=19079 official=50%, reported=47%, attributed=2%, alleged=1%, verified=0% (>=10%: 2, top 50%)

=== Per-arm, with `sampled` as the unconfounded control (8.1) ===
A topical arm that varies while sampled does not means the
extractor is being flattered by its sibling's selection.
  events.type:
    [tracked_entity]: n=18091 disclosure=56%, action=23%, statement=21% (>=10%: 3, top 56%)
    [tracked_claim]: no rows
    [tracked_story]: no rows
    [topical]: n=894 action=42%, statement=33%, disclosure=25% (>=10%: 3, top 42%)
    [sampled]: n=94 action=45%, statement=29%, disclosure=27% (>=10%: 3, top 45%)
  events.commitment_state:
    [tracked_entity]: n=6532 in_force=77%, proposed=11%, intended=10%, committed=3% (>=10%: 2, top 77%)
    [tracked_claim]: no rows
    [tracked_story]: no rows
    [topical]: n=752 in_force=79%, proposed=12%, intended=7%, committed=2% (>=10%: 2, top 79%)
    [sampled]: n=42 in_force=74%, proposed=14%, intended=10%, committed=2% (>=10%: 2, top 74%)
  assertions.standing:
    [tracked_entity]: n=18091 official=52%, reported=46%, attributed=2%, alleged=1%, verified=0% (>=10%: 2, top 52%)
    [tracked_claim]: no rows
    [tracked_story]: no rows
    [topical]: n=894 reported=69%, official=28%, attributed=3%, alleged=1%, verified=0% (>=10%: 2, top 69%)
    [sampled]: n=94 reported=88%, official=6%, alleged=3%, verified=2% (>=10%: 1, top 88%)

=== Corroboration, BOTH directions (spec 8.2) ===
cohort [2026-09-22 18:13 UTC, 2026-10-02 11:30 UTC), exposure 6h, last event 2026-09-25 10:12 UTC, largest gap 169.3h (2026-09-25 10:12 to 2026-10-02 11:30 UTC)
NOT MEASURABLE  the corpus is FROZEN: the newest event anywhere in the KB is 2026-09-25 10:12 UTC, which predates the end of the cohort window by more than the 6h horizon. The 8724 events in this window are the residue of a pipeline that stopped, not a sample of a running one, so their rate is a verdict on nothing
                which is not a rate of 0.0

PASS  match-rate corroboration: assertions=19079 events=14523 matched-rate=23.9%
Both directions come from independent sources -- outlet diversity
and the write-path arithmetic above. If they disagree, trust the
disagreement -- a secondary signal contradicting the headline is
the tell that a probe measured the wrong layer.

=== Enum variance by body-depth tier (spec 12.3) ===
  events.type:
    [150-350] statement: 1048
    [150-350] action: 788
    [150-350] disclosure: 261
    [350+] action: 805
    [350+] statement: 694
    [350+] disclosure: 433
    [<150] disclosure: 9606
    [<150] action: 3059
    [<150] statement: 2385
  events.commitment_state:
    [150-350] in_force: 858
    [150-350] proposed: 163
    [150-350] intended: 128
    [150-350] committed: 44
    [350+] in_force: 689
    [350+] proposed: 203
    [350+] intended: 119
    [350+] committed: 32
    [<150] in_force: 4083
    [<150] proposed: 456
    [<150] intended: 442
    [<150] committed: 109
  assertions.standing:
    [150-350] reported: 1305
    [150-350] official: 676
    [150-350] attributed: 91
    [150-350] alleged: 19
    [150-350] verified: 6
    [350+] reported: 1513
    [350+] official: 294
    [350+] alleged: 69
    [350+] attributed: 39
    [350+] verified: 17
    [<150] official: 8612
    [<150] reported: 6200
    [<150] attributed: 178
    [<150] verified: 38
    [<150] alleged: 22

=== standing variance WITHIN each outlet (spec 12.3) ===
A field constant within every outlet is degenerate however
varied it looks overall -- severity's failure in disguise.
  NHK World: 2 distinct over n=407
  Mining.com: 2 distinct over n=111
  Bank of Japan: 2 distinct over n=33
  Observing Japan: 2 distinct over n=29
  U.S. Energy Information Administration: 2 distinct over n=16
  Un-Diplomatic: 2 distinct over n=13
  Yonhap (English): 3 distinct over n=1244
  OilPrice.com: 3 distinct over n=328
  Institute for the Study of War: 3 distinct over n=89
  Sinica Podcast: 3 distinct over n=25
  Marko Papic: 3 distinct over n=12
  Reuters: 4 distinct over n=7911
  IranWire: 4 distinct over n=547
  IRNA: 4 distinct over n=380
  Jacob Shapiro: 4 distinct over n=238
  @LordPos3idon: 4 distinct over n=186
  Chase Taylor: 4 distinct over n=182
  Kyiv Independent: 5 distinct over n=1973
  TASS: 5 distinct over n=1653
  Al Jazeera: 5 distinct over n=1142
  SCMP: 5 distinct over n=1100
  Times of Israel: 5 distinct over n=631
  The Hindu: 5 distinct over n=519
  Meduza: 5 distinct over n=298

=== Triage reason distribution (spec 5.1.1) ===
Interpretable only AFTER entities has accumulated. tracked_story
is expected at ZERO: nothing in production writes `stories`.
  tracked_entity: 19279
  topical: 983
  none: 801
  sampled: 160
  error: 12

GATE NOT RESOLVED: not measurable: corroboration (the corpus is FROZEN: the newest event anywhere in the KB is 2026-09-25 10:12 UTC, which predates the end of the cohort window by more than the 6h horizon. The 8724 events in this window are the residue of a pipeline that stopped, not a sample of a running one, so their rate is a verdict on nothing)
```
