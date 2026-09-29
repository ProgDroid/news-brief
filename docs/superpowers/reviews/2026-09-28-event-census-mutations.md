# Event census: pre-registered mutation checks

**Date run:** 2026-09-29, at `9635631` (Task 9's fix round), Task 10 of
`docs/superpowers/plans/2026-09-28-event-census.md`.
**Predictions:** copied unchanged from the Task 10 brief, written before any mutation ran.

## Method

For each row, one mutation was applied to the source, the named test file was run, and the
source was restored:

- **Applying the mutation.** A small harness applied each edit as an exact-text replacement
  and refused any edit whose old text did not match **exactly once**. A zero-match edit would be
  a non-mutation that "passes" silently. M3 edits two files (`census.py` and the up migration).
- **The run.** `py -m pytest <file> -q -rfEs -p no:cacheprovider` against the test Postgres
  (`DATABASE_URL` exported). `-rfEs` is `-rs` plus the failure and error lines, which give the
  failing names. The DB-backed fixtures drop the schema and re-apply every migration per test,
  so a mutated migration is exercised.
- **Restoring.** The original bytes were written back, then `git diff --quiet -- <file>` was
  checked for every mutated file. Exit 0 means byte-identical to `HEAD`.
- **Skips.** A DB-file run with any skip would be UNKNOWN, not a count. There were none.

**Baseline, unmutated, same command:**

| file | result |
|---|---|
| `tests/test_census_metrics.py` | 26 passed, 0 skipped |
| `tests/test_census_access.py` | 22 passed, 0 skipped |
| `tests/test_census_state.py` | 33 passed, 0 skipped |
| `tests/test_census_rules.py` | 17 passed, 0 skipped |
| `tests/test_census_schema.py` | 13 passed, 0 skipped |

## Results

| # | Mutation | File | Predicted failures | Actual (passed / failed / skipped) | Failing tests | Match | Restore (`git diff --quiet`) |
|---|---|---|---|---|---|---|---|
| M1 | `confirms`: drop the different-outlet condition | `test_census_metrics.py` | 1 | 25 / 1 / 0 | `test_confirms_needs_two_outlets_in_one_event` | yes | `census_metrics.py` 0 |
| M2 | `session_valid`: drop `kind = 'session'` | `test_census_access.py` | 1 | 21 / 1 / 0 | `test_link_token_is_not_a_session` | yes | `census.py` 0 |
| M2b | `session_valid`: drop the expiry and revocation conditions | `test_census_access.py` | 2 | 20 / 2 / 0 | `test_revoked_sessions_are_invalid`, `test_session_expires_after_thirty_days` | yes | `census.py` 0 |
| M3 | `save_assignments`: drop `ON CONFLICT … DO NOTHING`, and the `UNIQUE (window_id, tab_id, client_seq, item_id)` constraint from 0017 | `test_census_state.py` | 1 | 32 / 1 / 0 | `test_retried_write_is_ignored` | yes | `census.py` 0, `migrations/0017_census_up.sql` 0 |
| M3b | `latest_assignments`: `ORDER BY item_id, client_seq DESC, id DESC` in place of `item_id, id DESC` | `test_census_state.py` | 2 | 31 / 2 / 0 | `test_later_arrival_wins_across_tabs`, `test_window_assignments_uses_the_same_order` | yes | `census.py` 0 |
| M4 | `draw_precision_sample`: pool every cross-outlet pair of every group, sample `min(n, groups)` pairs from the pool | `test_census_metrics.py` | 2 | 24 / 2 / 0 | `test_precision_sample_is_one_pair_per_group_without_replacement`, `test_precision_sample_draws_groups_at_equal_probability` | yes | `census_metrics.py` 0 |
| M5 | `mde`: `crit = 1.96 + 0.84` in place of the t quantiles | `test_census_metrics.py` | 2 | 24 / 2 / 0 | `test_mde_reproduces_the_spec_table`, `test_mde_uses_t_for_the_window_count` | yes | `census_metrics.py` 0 |
| M6 | `go_no_go`: `mean_m >= 8` → `mean_m > 8` | `test_census_metrics.py` | 1 | 25 / 1 / 0 | `test_go_no_go_thresholds` | yes | `census_metrics.py` 0 |
| M7 | `compute_block`: `span_days < MIN_BLOCK_DAYS` → `< MIN_BLOCK_DAYS - 1` | `test_census_rules.py` | 1 | 16 / 1 / 0 | `test_block_on_2026_09_30_refuses_naming_2026_10_01` | yes | `census.py` 0 |
| M8 | row trigger: `OLD.created_at < v_end` → `<=` | `test_census_schema.py` | 1 | 12 / 1 / 0 | `test_item_at_block_end_is_not_held` | yes | `migrations/0017_census_up.sql` 0 |
| M9 | drop `CREATE TRIGGER census_hold_truncate_trg` from the up script | `test_census_schema.py` | 1 | 12 / 1 / 0 | `test_truncate_items_is_refused_while_census_exists` | yes | `migrations/0017_census_up.sql` 0 |
| M10 | row trigger: `RETURN OLD` → `RETURN NULL` (ruling R4) | `test_census_schema.py` | 4 | 9 / 4 / 0 | `test_cascade_control_without_a_census`, `test_item_at_block_end_is_not_held`, `test_post_block_item_delete_succeeds`, `test_releasing_the_block_lifts_the_hold_except_pinned_items` | yes | `migrations/0017_census_up.sql` 0 |

**12 of 12 match their prediction, in both count and names.** Every restore check exited 0, and
`git status` showed none of `census.py`, `census_metrics.py` or `migrations/` modified afterwards.

## Notes

- Later fix rounds added tests to `test_census_metrics.py` (26 now). None of them failed under
  any mutation, so no count exceeded its prediction.
- The M4 mutation is one concrete reading of "sample pairs uniformly across all groups": a pair
  is drawn uniformly from the pooled cross-outlet pairs, so a six-outlet group (15 pairs)
  dominates a two-outlet group (1 pair). Both pre-registered tests detect it.
- Per-mutation pytest logs were kept in the session's working directory, not in git.
