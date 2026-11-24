-- Groups and embeddings (storylines plan 1): pgvector, the vectors, the calls
-- that made them, the calibrations that gate grouping, and the groups. No
-- DELETE from any of these, ever; the down migration refuses while rows exist.

-- Guard: fail with the fix named, not with "extension vector is not available".
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector') THEN
        RAISE EXCEPTION 'pgvector is not installed in this Postgres image: run plan 1 Rollout step 0 (the image swap) first';
    END IF;
END $$;

CREATE EXTENSION IF NOT EXISTS vector;

-- No ANN index, on purpose. Grouping reads a 72 h window (about 2.4k members),
-- so an exact scan takes milliseconds, and an index would pin one dimension
-- while spaces of different dimensions share this table. Plan 2's 30-day reads
-- (about 24k members per query) are measured there; an index is added only if
-- they are slow.
CREATE TABLE item_embeddings (
    item_id    BIGINT      NOT NULL REFERENCES items (id) ON DELETE RESTRICT,
    space      TEXT        NOT NULL,
    text_hash  TEXT        NOT NULL,
    dim        INT         NOT NULL CHECK (dim > 0),
    embedding  vector      NOT NULL CHECK (vector_dims(embedding) = dim),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (item_id, space)
);
CREATE INDEX item_embeddings_space_hash ON item_embeddings (space, text_hash);

-- An item judged unembeddable in a space: an empty text, or an input the
-- provider rejected when sent alone. Assignment passes over it.
CREATE TABLE item_embed_skips (
    item_id    BIGINT      NOT NULL REFERENCES items (id),
    space      TEXT        NOT NULL,
    reason     TEXT        NOT NULL CHECK (length(reason) <= 300),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (item_id, space)
);

CREATE TABLE query_embeddings (
    text_hash  TEXT        NOT NULL,
    space      TEXT        NOT NULL,
    dim        INT         NOT NULL CHECK (dim > 0),
    embedding  vector      NOT NULL CHECK (vector_dims(embedding) = dim),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (text_hash, space)
);

CREATE TABLE embedding_calls (
    id                     BIGSERIAL     PRIMARY KEY,
    provider               TEXT          NOT NULL CHECK (provider IN ('modal', 'openai')),
    space                  TEXT          NOT NULL,
    purpose                TEXT          NOT NULL CHECK (purpose IN ('items', 'queries', 'backfill', 'calibration')),
    inputs                 INT           NOT NULL CHECK (inputs >= 0),
    tokens                 INT           NOT NULL CHECK (tokens >= 0),
    runtime_s              REAL          NULL,
    boot_s                 REAL          NULL,
    cold_start             BOOLEAN       NULL,
    batch                  BOOLEAN       NOT NULL DEFAULT FALSE,
    usd                    NUMERIC(12,6) NOT NULL,
    openai_equivalent_usd  NUMERIC(12,6) NOT NULL,
    outcome                TEXT          NOT NULL CHECK (outcome IN ('ok', 'failed')),
    problem                TEXT          NULL CHECK (length(problem) <= 300),
    created_at             TIMESTAMPTZ   NOT NULL DEFAULT now()
);
CREATE INDEX embedding_calls_created ON embedding_calls (created_at);

-- The latest row per space is the one in force. retrieval_min_sim stays NULL
-- until plan 2 fits it on real document queries.
CREATE TABLE embedding_calibrations (
    id                 BIGSERIAL   PRIMARY KEY,
    space              TEXT        NOT NULL,
    join_sim           REAL        NOT NULL CHECK (join_sim > 0 AND join_sim < 1),
    pair_sim           REAL        NOT NULL CHECK (pair_sim > 0 AND pair_sim < 1),
    title_sim          REAL        NOT NULL CHECK (title_sim > 0 AND title_sim < 1),
    retrieval_min_sim  REAL        NULL CHECK (retrieval_min_sim > 0 AND retrieval_min_sim < 1),
    weighted_precision REAL        NULL,
    weighted_recall    REAL        NULL,
    pairs_labelled     INT         NULL,
    prompt_version     INT         NOT NULL,
    judge_model        TEXT        NULL,
    week_ending        DATE        NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (pair_sim <= join_sim)
);
CREATE INDEX embedding_calibrations_space ON embedding_calibrations (space, id);

-- opened_at is the founding item's created_at, never wall clock. title is the
-- founder's cleaned title (embeddings.clean_title), as members' titles are.
CREATE TABLE item_groups (
    id           BIGSERIAL   PRIMARY KEY,
    space        TEXT        NOT NULL,
    opened_at    TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    title        TEXT        NOT NULL,
    outlet_count INT         NOT NULL CHECK (outlet_count >= 1),
    has_primary  BOOLEAN     NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX item_groups_space_opened ON item_groups (space, opened_at);

-- joined_at is the item's own created_at (capture time), never wall clock:
-- that is what makes as_of cuts consistent. The founder's similarities are NULL.
-- title is the member's cleaned title (embeddings.clean_title: Google's
-- " - Publisher" tail stripped), which the trigram rule compares; a raw tail
-- would make every item from one wire look alike.
CREATE TABLE item_group_members (
    item_id          BIGINT      NOT NULL REFERENCES items (id) ON DELETE RESTRICT,
    space            TEXT        NOT NULL,
    group_id         BIGINT      NOT NULL REFERENCES item_groups (id),
    joined_at        TIMESTAMPTZ NOT NULL,
    title            TEXT        NOT NULL,
    similarity       REAL        NULL,
    title_similarity REAL        NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (item_id, space)
);
CREATE INDEX item_group_members_space_joined ON item_group_members (space, joined_at);
CREATE INDEX item_group_members_group ON item_group_members (group_id);

-- Capture order, which every queue read here walks. It does not shorten the
-- steady-state anti-joins, which visit every item anyway, but it makes a new
-- space (a backfill, a switched model) O(limit) instead of O(items).
CREATE INDEX items_created_at_id ON items (created_at, id);

ALTER TABLE comprehend_spend DROP CONSTRAINT comprehend_spend_stage_check;
ALTER TABLE comprehend_spend ADD CONSTRAINT comprehend_spend_stage_check
    CHECK (stage IN ('triage', 'integration', 'attach', 'seed', 'attach_check', 'calibrate'));
