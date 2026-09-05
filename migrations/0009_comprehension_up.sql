-- Comprehension pipeline (news-brief-bqa.4b). One row per item per triage
-- prompt version: the verdict, why, and how far integration got.
--
-- A table rather than a column on `items` because deriving "has this been
-- processed" from `assertions` works for integrated items but re-triages every
-- REJECTED item forever, and rejects are the majority.
CREATE TABLE item_triage (
    id             BIGSERIAL PRIMARY KEY,
    item_id        BIGINT  NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    verdict        TEXT    NOT NULL
                   CHECK (verdict IN ('material', 'immaterial', 'failed')),
    -- The union rule has two halves and they are different evidence.
    -- Collapsing them to one bit makes it impossible to ask later whether the
    -- tracked half is carrying the pipeline or the topical half is.
    reason         TEXT    NOT NULL
                   CHECK (reason IN ('tracked_entity', 'tracked_claim',
                                     'tracked_story', 'topical', 'sampled',
                                     'none', 'error')),
    -- Biconditional in BOTH directions, in the style of observations' metric
    -- check. Written one-way it would pass a one-way test while permitting an
    -- immaterial row that claims a real reason.
    CHECK ((verdict = 'material') = (reason NOT IN ('none', 'error'))),
    -- NULL means the rules half decided and NO model ran. Same reasoning as
    -- observations.provider being distinct from extractor_model: not every row
    -- is produced by a model, and NOT NULL here would force a lie.
    triage_model             TEXT    NULL,
    -- TWO versions, because there are two prompts on two models behind two
    -- knobs. One column served neither: bumping the integration prompt left
    -- integrated_at set so nothing could re-extract, and bumping the triage
    -- prompt re-paid triage cost on items nobody meant to change.
    triage_prompt_version    INTEGER NOT NULL,
    integrate_prompt_version INTEGER NULL,
    attempts                 INTEGER NOT NULL DEFAULT 1,
    -- Integration gets its own ceiling for the reason triage has one: an item
    -- failing deterministically would re-pay the EXPENSIVE tier every run.
    integrate_attempts       INTEGER NOT NULL DEFAULT 0,
    integrated_at            TIMESTAMPTZ NULL,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX item_triage_item_version
    ON item_triage (item_id, triage_prompt_version);

-- Predicated on the verdict as well as the timestamp: immaterial rows never
-- receive integrated_at, so a partial index on that alone would retain roughly
-- 90% rows the only query against it can never return.
CREATE INDEX item_triage_pending ON item_triage (item_id)
    WHERE verdict = 'material' AND integrated_at IS NULL;
