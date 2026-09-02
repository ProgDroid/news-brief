-- The knowledge base: sixteen tables for the ten objects in section 3.1 of the
-- KB architecture design. Every table ships EMPTY -- nothing reads or writes
-- them until bqa.4. See docs/superpowers/specs/2026-09-02-kb-schema-ddl-design.md.
--
-- These tables carry NO user_id, unlike `sources`. 0003 states the boundary: a
-- source is part of how one person reads the world; what the sources SAY is
-- shared. Source in the KB sense is `outlets` below, NOT `sources` -- an item
-- comes from an outlet, and two readers subscribing to Reuters must not
-- produce two of it.

CREATE TABLE outlets (
    id           BIGSERIAL PRIMARY KEY,
    name         TEXT        NOT NULL,
    kind         TEXT        NOT NULL DEFAULT 'regional'
                 CHECK (kind IN ('wire', 'analyst', 'regional', 'primary')),
    -- NULL means "no vantage claim made", NOT "neutral": calling a source
    -- neutral is a positive editorial claim, as contestable as picking a side.
    perspective  TEXT        NULL
                 CHECK (perspective IS NULL OR perspective IN (
                     'WESTERN', 'CHINESE', 'RUSSIAN', 'IRANIAN', 'ISRAELI',
                     'ARAB', 'UKRAINIAN', 'JAPANESE', 'KOREAN', 'INDIAN')),
    state_funded BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX outlets_name ON outlets (lower(name));

CREATE TABLE items (
    id           BIGSERIAL PRIMARY KEY,
    outlet_id    BIGINT      NOT NULL REFERENCES outlets(id),
    url          TEXT        NOT NULL,
    title        TEXT        NOT NULL,
    body         TEXT        NULL,
    published_at TIMESTAMPTZ NULL,
    content_hash TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Per-outlet, NOT global. 12.3 #15 specified a content-hash dedup key for a
-- capture JSONL with no outlet dimension; promoted unchanged to a shared,
-- outlet-attributed table it would collapse syndicated wire copy and cap any
-- corroboration count at one.
CREATE UNIQUE INDEX items_outlet_hash ON items (outlet_id, content_hash);

CREATE TABLE entities (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT    NOT NULL,
    -- No default: an entity whose type is unknown is a resolution failure, not
    -- a typed row. "the Fed" is an institution, not a company.
    type            TEXT    NOT NULL
                    CHECK (type IN ('country', 'institution', 'company',
                                    'person', 'instrument')),
    aliases         TEXT[]  NOT NULL DEFAULT '{}',
    extractor_model TEXT    NULL,
    prompt_version  INTEGER NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- This does NOT enforce "a company and its equity line are one entity" -- that
-- is an extraction rule for bqa.4. No unique key can express it.
CREATE UNIQUE INDEX entities_name_type ON entities (lower(name), type);

CREATE TABLE entity_instruments (
    id          BIGSERIAL PRIMARY KEY,
    entity_id   BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    symbol      TEXT   NOT NULL,
    market      TEXT   NULL,
    asset_class TEXT   NOT NULL
                CHECK (asset_class IN ('equity', 'index', 'crypto',
                                       'commodity', 'fx')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- symbol first: the consumer is symbol -> entity resolution, which a leading
-- entity_id cannot serve. NULLS NOT DISTINCT because market is nullable and
-- default semantics would let the same mapping insert twice.
CREATE UNIQUE INDEX entity_instruments_symbol
    ON entity_instruments (symbol, market, entity_id) NULLS NOT DISTINCT;

CREATE TABLE events (
    id               BIGSERIAL PRIMARY KEY,
    summary          TEXT        NOT NULL,
    occurred_at      TIMESTAMPTZ NULL,
    -- Three orthogonal fields, spec 3.2. "Trump declared the ceasefire over"
    -- is statement/intended/official; whether the ceasefire IS over is a
    -- separate Claim with its own standing. Conflating them is what the Day 1
    -- brief did.
    type             TEXT        NOT NULL
                     CHECK (type IN ('action', 'statement', 'disclosure')),
    commitment_state TEXT        NOT NULL
                     CHECK (commitment_state IN ('in_force', 'committed',
                                                 'intended', 'proposed')),
    -- Presence IS the superseded state; rule 1 writes it.
    superseded_by    BIGINT      NULL REFERENCES events(id),
    extractor_model  TEXT        NULL,
    prompt_version   INTEGER     NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- NULLS LAST: occurred_at is nullable and DESC defaults to NULLS FIRST, which
-- would put every undated event at the head of rule 1's most-recent scan.
CREATE INDEX events_occurred ON events (occurred_at DESC NULLS LAST);

CREATE TABLE event_entities (
    event_id   BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    entity_id  BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, entity_id)
);
CREATE INDEX event_entities_entity ON event_entities (entity_id);

CREATE TABLE assertions (
    id                  BIGSERIAL PRIMARY KEY,
    item_id             BIGINT      NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    event_id            BIGINT      NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    -- Per assertion, not per event: two sources can assert the same event with
    -- very different weight.
    standing            TEXT        NOT NULL
                        CHECK (standing IN ('verified', 'official', 'reported',
                                            'attributed', 'alleged')),
    -- Nullable, NO default. The only extracted enum with no worked example
    -- anywhere in the parent spec -- severity's exact provenance. NULL means
    -- "not labelled", NOT "independent". news-brief-bqa.8 measures it.
    source_relationship TEXT        NULL
                        CHECK (source_relationship IS NULL OR source_relationship IN
                               ('party', 'aligned', 'independent', 'adversarial')),
    asserted_at         TIMESTAMPTZ NULL,
    extractor_model     TEXT        NULL,
    prompt_version      INTEGER     NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX assertions_item_event ON assertions (item_id, event_id);
