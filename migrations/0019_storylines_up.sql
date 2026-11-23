-- Storylines foundation (plan 2a): the storylines themselves, the proposals an
-- operator accepts or rejects, and the per-article attach decisions. No DELETE
-- from any of these, ever; the down migration refuses while rows exist.
CREATE TABLE storylines (
    id               BIGSERIAL   PRIMARY KEY,
    name             TEXT        NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    description      TEXT        NOT NULL CHECK (length(description) BETWEEN 1 AND 400),
    status           TEXT        NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'dormant', 'retired')),
    pinned           BOOLEAN     NOT NULL DEFAULT FALSE,
    origin           TEXT        NOT NULL CHECK (origin IN ('seed', 'proposal', 'operator', 'burst')),
    last_attached_at TIMESTAMPTZ NULL,
    dormant_since    DATE        NULL,
    retired_on       DATE        NULL,
    merged_into      BIGINT      NULL REFERENCES storylines (id),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((status = 'retired') = (retired_on IS NOT NULL))
);
CREATE UNIQUE INDEX storylines_live_name ON storylines (lower(name)) WHERE status <> 'retired';

CREATE TABLE storyline_proposals (
    id           BIGSERIAL   PRIMARY KEY,
    name         TEXT        NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    description  TEXT        NOT NULL CHECK (length(description) BETWEEN 1 AND 400),
    pinned       BOOLEAN     NOT NULL DEFAULT FALSE,
    origin       TEXT        NOT NULL CHECK (origin IN ('seed', 'proposal', 'burst')),
    proposed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at   TIMESTAMPTZ NULL,
    outcome      TEXT        NULL CHECK (outcome IN ('accepted', 'rejected')),
    storyline_id BIGINT      NULL REFERENCES storylines (id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((outcome IS NULL) = (decided_at IS NULL)),
    CHECK ((outcome = 'accepted') = (storyline_id IS NOT NULL))
);

CREATE TABLE item_attach_decisions (
    item_id      BIGINT      PRIMARY KEY REFERENCES items (id) ON DELETE RESTRICT,
    outcome      TEXT        NOT NULL CHECK (outcome IN ('attached', 'none', 'failed', 'deferred')),
    model        TEXT        NOT NULL,
    attempts     INT         NOT NULL DEFAULT 0 CHECK (attempts >= 0),  -- judged failures only
    defers       INT         NOT NULL DEFAULT 0 CHECK (defers >= 0),    -- responses that judged nothing
    last_problem TEXT        NULL,
    decided_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE item_attachments (
    item_id      BIGINT      NOT NULL REFERENCES items (id) ON DELETE RESTRICT,
    storyline_id BIGINT      NOT NULL REFERENCES storylines (id),
    status       TEXT        NOT NULL DEFAULT 'attached' CHECK (status IN ('attached', 'rejected', 'consumed')),
    update_id    BIGINT      NULL,  -- FK to storyline_updates: plan 2b, 0020
    attached_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (item_id, storyline_id)
);
CREATE INDEX item_attachments_storyline ON item_attachments (storyline_id, attached_at);

ALTER TABLE thesis_evidence
    ADD CONSTRAINT thesis_evidence_storyline_fk FOREIGN KEY (storyline_id) REFERENCES storylines (id);

ALTER TABLE comprehend_spend DROP CONSTRAINT comprehend_spend_stage_check;
ALTER TABLE comprehend_spend ADD CONSTRAINT comprehend_spend_stage_check
    CHECK (stage IN ('triage', 'integration', 'attach', 'seed', 'attach_check'));
