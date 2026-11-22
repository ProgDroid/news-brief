-- Dated evidence per thesis key: one row per (key, day, direction, line).
-- storyline_id gets its foreign key in 0019_storylines; until then it is a bare column.
CREATE TABLE thesis_evidence (
    id           BIGSERIAL   PRIMARY KEY,
    thesis_key   TEXT        NOT NULL CHECK (thesis_key <> ''),
    storyline_id BIGINT      NULL,
    on_date      DATE        NOT NULL,
    direction    TEXT        NOT NULL CHECK (direction IN ('supports', 'challenges')),
    line         TEXT        NOT NULL CHECK (length(line) BETWEEN 1 AND 300),
    item_ids     BIGINT[]    NOT NULL DEFAULT '{}',
    source       TEXT        NOT NULL CHECK (source IN ('brief', 'storyline')),
    model        TEXT        NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (source = 'storyline' OR storyline_id IS NULL)
);
CREATE INDEX thesis_evidence_key_date ON thesis_evidence (thesis_key, on_date DESC);
CREATE INDEX thesis_evidence_date_source ON thesis_evidence (on_date, source);
