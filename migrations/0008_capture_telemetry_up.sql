-- Capture telemetry (news-brief-b42.1).
--
-- These are NOT the knowledge base. `items` records what the world published;
-- these three record which of ONE reader's feeds showed it, when, and whether
-- the poll that failed to show it actually ran. Different object, different
-- lifetime, different table -- which is why `items` gets no feed column.
--
-- See docs/superpowers/specs/2026-09-02-continuous-capture-design.md section 6.

CREATE TABLE capture_runs (
    id              BIGSERIAL PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULL means the pass did not finish: a crash is told from a completed run
    -- by this column, since job_runs.exit_code cannot say which pass it was.
    finished_at     TIMESTAMPTZ NULL,
    -- A disabled pass writes a row too. job_runs has no free-text column
    -- (0001:29-38), so "switched off" and "ran fine" are byte-identical there.
    enabled         BOOLEAN     NOT NULL,
    feeds_total     INTEGER     NOT NULL DEFAULT 0,
    feeds_ok        INTEGER     NOT NULL DEFAULT 0,
    feeds_failed    INTEGER     NOT NULL DEFAULT 0,
    items_seen      INTEGER     NOT NULL DEFAULT 0,
    items_new       INTEGER     NOT NULL DEFAULT 0,
    sources_dropped INTEGER     NOT NULL DEFAULT 0
);

-- One row per feed per pass, success or failure. This table is the DENOMINATOR
-- for roll-off: an item's absence only means "left the window" if a SUCCESSFUL
-- poll followed its last sighting. Without it, a feed 403ing for a day reads as
-- its entire window rolling over at once -- a large, clean, fictitious signal,
-- and the same "a dropped feed looks identical to a quiet one" failure this repo
-- already carries a comment about at brief.py:1849-1853.
CREATE TABLE feed_polls (
    id             BIGSERIAL PRIMARY KEY,
    capture_run_id BIGINT      NOT NULL REFERENCES capture_runs(id),
    source_name    TEXT        NOT NULL,
    polled_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NULL = success. Otherwise the FeedFetch.failure kind, including
    -- 'deadline' for a feed the pass ran out of time to reach.
    failure        TEXT        NULL,
    entries_seen   INTEGER     NOT NULL DEFAULT 0
);
CREATE INDEX feed_polls_source_time ON feed_polls (source_name, polled_at DESC);

CREATE TABLE feed_sightings (
    id            BIGSERIAL PRIMARY KEY,
    -- The FEED, not the outlet: Reuters Markets and Reuters World collapse to
    -- one outlet by design (spec 4), so per-feed measurement can only live here.
    -- Deliberately TEXT and not a FK to `sources`: the baked-in RSS_FEEDS have
    -- no sources row, and a renamed or deleted source must not destroy the
    -- measurement history that justified a poll interval.
    source_name   TEXT        NOT NULL,
    content_hash  TEXT        NOT NULL,
    item_id       BIGINT      NULL REFERENCES items(id),
    position      INTEGER     NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX feed_sightings_source_hash
    ON feed_sightings (source_name, content_hash);
