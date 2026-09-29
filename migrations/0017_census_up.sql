-- Event census: gold-set labelling infrastructure (spec 2026-09-28, section 7).
--
-- Ten tables plus the retention hold. The census freezes a block of history
-- ([block_start, block_end)) and stratified-samples windows out of it for
-- human dual-coding; the hold below exists so the replay this depends on
-- (spec section 14 item 6) always sees the same items production once held,
-- even if a retention job runs against `items` while the census is live.

-- Singleton: the boolean PK with a CHECK forcing it to `true` allows at most
-- one row, following the pattern already documented in spec section 7.
CREATE TABLE census_block (
    id                  BOOLEAN          PRIMARY KEY DEFAULT true CHECK (id),
    block_start         TIMESTAMPTZ      NOT NULL,
    block_end           TIMESTAMPTZ      NOT NULL,
    prepared_at         TIMESTAMPTZ      NOT NULL,
    c439ade_deployed_at TIMESTAMPTZ      NOT NULL,
    gap_split_share     DOUBLE PRECISION NOT NULL,
    gap_pairs           INTEGER          NOT NULL,
    gap_windows         INTEGER          NOT NULL,
    gap_deciles         DOUBLE PRECISION[] NOT NULL,
    gap_band            TEXT             NOT NULL
                        CHECK (gap_band IN ('proceed', 'within_6h_only')),
    go_override_reason  TEXT             NULL
);

-- The full window schedule, persisted once at prepare time so a re-run of the
-- preparation step cannot reshuffle strata already handed to labellers.
CREATE TABLE census_window_order (
    order_no     INTEGER     PRIMARY KEY,
    window_start TIMESTAMPTZ NOT NULL UNIQUE,
    -- `start.hour // 6`, i.e. one of the day's four 6-hour bands (plan Task 3).
    stratum      SMALLINT    NOT NULL CHECK (stratum BETWEEN 0 AND 3)
);

-- Windows the preparation step excluded from the schedule (e.g. a hole in the
-- corpus), kept for the readout rather than silently dropped.
CREATE TABLE census_skipped_windows (
    window_start TIMESTAMPTZ PRIMARY KEY,
    reason       TEXT        NOT NULL
);

CREATE TABLE census_windows (
    id               BIGSERIAL   PRIMARY KEY,
    order_no         INTEGER     NOT NULL,
    window_start     TIMESTAMPTZ NOT NULL,
    pass             SMALLINT    NOT NULL CHECK (pass IN (1, 2)),
    repeat_of        BIGINT      NULL REFERENCES census_windows(id),
    status           TEXT        NOT NULL
                     CHECK (status IN ('prepared', 'open', 'blind_done',
                                       'complete', 'abandoned')),
    abandon_reason   TEXT        NULL,
    -- Counts, not flags (spec 4.1): the number of backlog items excluded from
    -- the window, and the number kept despite a NULL published_at, each
    -- reported per window by the readout.
    backlog_excluded INTEGER     NOT NULL DEFAULT 0 CHECK (backlog_excluded >= 0),
    null_published   INTEGER     NOT NULL DEFAULT 0 CHECK (null_published >= 0),
    opened_at        TIMESTAMPTZ NULL,
    blind_done_at    TIMESTAMPTZ NULL,
    completed_at     TIMESTAMPTZ NULL,
    -- One row per (order_no, pass): the repeat window is pass 2 of the same
    -- order_no, never a second pass-1 row.
    UNIQUE (order_no, pass)
);

-- The membership of a window. `item_id` is RESTRICT, not CASCADE: this is the
-- other half of the retention hold, and the one that outlives the block row
-- itself -- releasing the block (deleting census_block) lifts the trigger,
-- but a window's own items stay pinned until the census that named them is
-- retired and this row goes with it (spec section 7, "Release").
CREATE TABLE census_window_items (
    window_id BIGINT NOT NULL REFERENCES census_windows(id),
    item_id   BIGINT NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
    PRIMARY KEY (window_id, item_id)
);

-- No DEFAULT now() on the timestamp: every writer injects its own clock
-- (plan Global Constraint), so a missing value must fail NOT NULL rather
-- than silently stamp wall-clock time.
CREATE TABLE census_groups (
    id         BIGSERIAL   PRIMARY KEY,
    window_id  BIGINT      NOT NULL REFERENCES census_windows(id),
    created_at TIMESTAMPTZ NOT NULL
);

-- B5: one labeller can submit the same (window, item) more than once across
-- tabs/retries; `tab_id` + `client_seq` is the client's own idempotency key,
-- so the same client-generated action is never double-counted.
CREATE TABLE census_assignments (
    id         BIGSERIAL   PRIMARY KEY,
    window_id  BIGINT      NOT NULL REFERENCES census_windows(id),
    item_id    BIGINT      NOT NULL REFERENCES items(id),
    group_id   BIGINT      NULL REFERENCES census_groups(id),
    unsure     BOOLEAN     NOT NULL,
    tab_id     TEXT        NOT NULL,
    client_seq INTEGER     NOT NULL,
    -- No DEFAULT now(): every writer injects its own clock (plan Global
    -- Constraint), and a default would let a forgetful writer stamp
    -- wall-clock time silently.
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (window_id, tab_id, client_seq, item_id)
);

CREATE INDEX census_assignments_window_item_id_desc
    ON census_assignments (window_id, item_id, id DESC);

-- `CHECK (item_a < item_b)` forces one canonical row per unordered pair, so
-- `UNIQUE (kind, item_a, item_b)` cannot be bypassed by swapping the order of
-- the two ids on a second submission.
CREATE TABLE census_adjudications (
    id         BIGSERIAL   PRIMARY KEY,
    window_id  BIGINT      NOT NULL REFERENCES census_windows(id),
    kind       TEXT        NOT NULL CHECK (kind IN ('precision')),
    item_a     BIGINT      NOT NULL REFERENCES items(id),
    item_b     BIGINT      NOT NULL REFERENCES items(id),
    decision   TEXT        NOT NULL CHECK (decision IN ('same', 'different')),
    -- No DEFAULT now(): see census_groups above.
    created_at TIMESTAMPTZ NOT NULL,
    CHECK (item_a < item_b),
    UNIQUE (kind, item_a, item_b)
);

CREATE TABLE census_sessions (
    id           BIGSERIAL   PRIMARY KEY,
    kind         TEXT        NOT NULL CHECK (kind IN ('link', 'session')),
    token_sha256 TEXT        NOT NULL UNIQUE,
    expires_at   TIMESTAMPTZ NULL,
    consumed_at  TIMESTAMPTZ NULL,
    revoked_at   TIMESTAMPTZ NULL
);

-- F4: a plain BIGSERIAL PK, not a natural key -- an event log is read in
-- insertion order and never deduplicated against itself.
CREATE TABLE census_events (
    id        BIGSERIAL   PRIMARY KEY,
    window_id BIGINT      NOT NULL REFERENCES census_windows(id),
    kind      TEXT        NOT NULL
              CHECK (kind IN ('open', 'action', 'heartbeat', 'finish', 'abandon')),
    -- No DEFAULT now(): see census_groups above.
    at        TIMESTAMPTZ NOT NULL
);

-- The retention hold (spec section 7). A naive item delete already fails
-- today via feed_sightings' bare FK; this closes the silent path, a retention
-- job that prunes feed_sightings (and other CASCADE children) first, leaving
-- nothing to stop the item delete itself.
--
-- F2: only the block row and OLD.created_at decide the row trigger's outcome
-- -- a genuinely post-block item always returns OLD and the delete proceeds.
-- F1: plpgsql's own placeholder is `%`, not `%s` (RAISE EXCEPTION is not a
-- format() call).
CREATE OR REPLACE FUNCTION census_hold_row() RETURNS trigger AS $$
DECLARE
    v_end TIMESTAMPTZ;
BEGIN
    SELECT block_end INTO v_end FROM census_block;
    IF v_end IS NOT NULL AND OLD.created_at < v_end THEN
        RAISE EXCEPTION 'items captured before % are held by the event census; '
            'release by deleting the census_block row (see '
            'docs/2026-10-01-host-runbook-census.md)', v_end;
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER census_hold_row_trg
    BEFORE DELETE ON items
    FOR EACH ROW EXECUTE FUNCTION census_hold_row();

-- Row triggers do not fire on TRUNCATE, so the hold needs its own statement
-- trigger; it raises unconditionally while a block exists, since TRUNCATE has
-- no per-row OLD to test against block_end.
CREATE OR REPLACE FUNCTION census_hold_truncate() RETURNS trigger AS $$
DECLARE
    v_end TIMESTAMPTZ;
BEGIN
    SELECT block_end INTO v_end FROM census_block;
    IF v_end IS NOT NULL THEN
        RAISE EXCEPTION 'items captured before % are held by the event census; '
            'release by deleting the census_block row (see '
            'docs/2026-10-01-host-runbook-census.md)', v_end;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER census_hold_truncate_trg
    BEFORE TRUNCATE ON items
    FOR EACH STATEMENT EXECUTE FUNCTION census_hold_truncate();
