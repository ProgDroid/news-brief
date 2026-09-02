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

CREATE TABLE observations (
    id            BIGSERIAL PRIMARY KEY,
    entity_id     BIGINT      NOT NULL REFERENCES entities(id),
    -- The symbol ACTUALLY queried at the provider, stored beside the result it
    -- produced. Deliberate duplication with entity_instruments: the AVAV_
    -- double-underscore bug was invisible precisely because the queried symbol
    -- was never recorded next to its result.
    symbol        TEXT        NOT NULL,
    -- No default: an observation without a metric is unusable, and a default
    -- would silently mislabel rows rather than reject them.
    metric        TEXT        NOT NULL
                  CHECK (metric IN ('price', 'return', 'yield', 'volume', 'spread')),
    value         NUMERIC     NOT NULL,
    return_window TEXT        NULL,
    observed_at   TIMESTAMPTZ NOT NULL,
    -- NOT extractor_model: these rows are fetched, not extracted. Spec 3.5.
    provider      TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Biconditional, both directions. metric is NOT NULL so neither side can
    -- be NULL and there is no three-valued-logic hole.
    CHECK ((metric = 'return') = (return_window IS NOT NULL))
);
CREATE INDEX observations_entity_observed ON observations (entity_id, observed_at DESC);

CREATE TABLE claims (
    id                 BIGSERIAL PRIMARY KEY,
    -- Preserves the JSON ledger's string identity, format 'c-0001' per
    -- merge_ledger and the r"c-(\d+)$" regex in _max_id_num. merge_ledger
    -- treats an echoed id as authoritative, so a BIGSERIAL alone would
    -- silently renumber every claim the model can cite.
    ledger_id          TEXT    NULL,
    -- Named `claim`, not `text`: that is the ledger's key. Spec 5.1.
    claim              TEXT    NOT NULL,
    topic              TEXT    NULL,
    -- ONE lifecycle in ONE column.
    --
    -- These six ARE brief_memory._VALID_STATUS, and tests/test_kb_schema.py
    -- asserts the two sets equal by reading this CHECK back off pg_constraint.
    -- The reader knew only the first three until news-brief-bqa.9 (spec section
    -- 6 item 1): confirmed/expired/withdrawn coerced back to 'standing', the TTL
    -- deleted them after 7 days, and select_working_set rendered them as live
    -- fact. Adding a value here without adding it there restores that bug, which
    -- is why the equality is a test and not a comment.
    status             TEXT    NOT NULL DEFAULT 'standing'
                       CHECK (status IN ('standing', 'challenged', 'broken',
                                         'confirmed', 'expired', 'withdrawn')),
    origin             TEXT    NOT NULL DEFAULT 'extracted'
                       CHECK (origin IN ('extracted', 'authored')),
    -- Measured DEGENERATE (high 25/25) and shipping anyway, because it is
    -- load-bearing: _ttl_bonus grants 'high' extra retention days and
    -- _severity_rank orders the working-set prefix. Owes a rubric before any
    -- NEW consumer reads it (news-brief-bqa.8).
    severity           TEXT    NOT NULL DEFAULT 'normal'
                       CHECK (severity IN ('low', 'normal', 'high')),
    -- 1..3650 matches _MAX_HORIZON_DAYS. NULL means the horizon could not be
    -- determined and the claim is EXEMPT from rule 4 -- never defaulted.
    horizon_days       INTEGER NULL CHECK (horizon_days IS NULL
                                           OR horizon_days BETWEEN 1 AND 3650),
    resolution_date    DATE    NULL,
    horizon_elapsed    INTEGER NULL,
    falsifier          TEXT    NULL,
    falsifier_kind     TEXT    NULL
                       CHECK (falsifier_kind IS NULL OR falsifier_kind IN
                              ('event_triggered', 'review_required')),
    first_seen         DATE    NOT NULL,
    last_reaffirmed    DATE    NULL,
    restate_count      INTEGER NOT NULL DEFAULT 0,
    source_count       INTEGER NULL,
    driver             TEXT    NULL,
    -- The date the claim LEFT 'standing', in either direction. Named
    -- resolved_on rather than the ledger's broke_on because three of the six
    -- statuses are not breaks; spec 5.1 maps the key across.
    resolved_on        DATE    NULL,
    -- RESTRICT, not CASCADE: deleting the contradicting event must never
    -- silently un-break a claim.
    broken_by_note     TEXT    NULL,
    broken_by_event_id BIGINT  NULL REFERENCES events(id) ON DELETE RESTRICT,
    extractor_model    TEXT    NULL,
    prompt_version     INTEGER NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (status = 'standing' OR resolved_on IS NOT NULL),
    CHECK (horizon_elapsed IS NULL OR resolved_on IS NOT NULL),
    CHECK (status <> 'broken'
           OR num_nonnulls(broken_by_note, broken_by_event_id) >= 1)
);
CREATE UNIQUE INDEX claims_ledger_id ON claims (ledger_id);
-- Rule 4's predicate exactly: a claim that expires LEAVES this index. An
-- earlier draft indexed WHERE status = 'standing' while rule 4 wrote a
-- different column, so an expired claim never left and rule 4 re-fired on it
-- every morning for the life of the row.
CREATE INDEX claims_open_resolution ON claims (resolution_date)
    WHERE status IN ('standing', 'challenged');
CREATE TABLE claim_evidence (
    id             BIGSERIAL PRIMARY KEY,
    claim_id       BIGINT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    -- RESTRICT, not CASCADE: rule 3 is a COUNT over these rows, and a floor an
    -- unrelated delete can silently lower is not a floor.
    event_id       BIGINT NULL REFERENCES events(id) ON DELETE RESTRICT,
    observation_id BIGINT NULL REFERENCES observations(id) ON DELETE RESTRICT,
    span_start     DATE   NULL,
    span_end       DATE   NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(event_id, observation_id) = 1)
);
-- NULLS NOT DISTINCT: without it, (claim, event, NULL) inserts twice and one
-- piece of evidence counts as two. Also serves the claim_id prefix lookup, so
-- no separate FK index is created.
CREATE UNIQUE INDEX claim_evidence_unique
    ON claim_evidence (claim_id, event_id, observation_id) NULLS NOT DISTINCT;

-- Defence in depth. The PRIMARY enforcement is brief_memory._reaffirm, which
-- also catches the case no constraint can see: an unmarked rewrite that keeps
-- status 'standing' while editing the text to match new facts. Three gold-set
-- runs measured the model doing exactly that on every true break it scored
-- standing.
--
-- The predicate reads BOTH tuples. Reading OLD.status alone would permit the
-- single UPDATE that marks a claim broken AND rewrites it -- precisely the
-- 2026-08-29 Patriot mechanism this exists to stop.
-- A terminal status must be a property of the schema, not a belief held by
-- whatever last wrote it -- an interlock the reader can defeat by omission is
-- not an interlock. This clause was the ONLY enforcement until
-- news-brief-bqa.9: _apply_status computed `prior` by coercing the row's own
-- stored status through _coerce_status, whose _VALID_STATUS recognised three of
-- the six values, so a stored 'confirmed'/'expired'/'withdrawn' fell back to
-- _DEFAULT_STATUS = 'standing' before the model's proposed value was even
-- considered. bqa.9 widened the set and gave _apply_status a matching refusal,
-- so this is now the defence in depth it was always described as -- and it
-- still catches any writer that is not brief_memory. 'challenged' is
-- deliberately excluded on both sides: spec 2.3 says a challenge can be
-- answered, so standing -> challenged -> standing must keep working.
CREATE OR REPLACE FUNCTION claims_freeze_claim_text() RETURNS trigger AS $$
BEGIN
    IF OLD.status IN ('broken', 'confirmed', 'expired', 'withdrawn')
       AND NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION 'claim % is %, which is terminal', OLD.id, OLD.status;
    END IF;
    IF (OLD.status <> 'standing' OR NEW.status <> 'standing')
       AND NEW.claim IS DISTINCT FROM OLD.claim THEN
        RAISE EXCEPTION 'claim text is immutable once status leaves standing (id %)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER claims_freeze_claim_text_trg
    BEFORE UPDATE ON claims
    FOR EACH ROW EXECUTE FUNCTION claims_freeze_claim_text();

-- The six tables below are PROVISIONAL (spec 1.2): empty, read by nothing, and
-- reshapeable without ceremony until bqa.4 writes them. theses and
-- thesis_claims have no consumer at all before Epic 6 -- they are here because
-- the schema was scoped to all ten objects, not because anything needs them.

CREATE TABLE theses (
    id              BIGSERIAL PRIMARY KEY,
    text            TEXT    NOT NULL,
    -- Ordinal, NO numeric scoring: Bayesian-looking arithmetic over ordinal
    -- judgments manufactures precision the inputs do not contain. Advances
    -- ONLY on RESOLVED supporting claims, never on their count.
    confidence      TEXT    NOT NULL DEFAULT 'speculative'
                    CHECK (confidence IN ('speculative', 'tentative',
                                          'supported', 'established')),
    horizon_days    INTEGER NULL CHECK (horizon_days IS NULL
                                        OR horizon_days BETWEEN 1 AND 3650),
    triggers        TEXT[]  NOT NULL DEFAULT '{}',
    -- Provenance but NOT origin: provenance says which model wrote the row,
    -- which drift detection needs. origin says whether a rule may read it as
    -- evidence, and for a thesis the answer is always no -- so the column
    -- would be uniform, which 12.2 rates worse than missing.
    extractor_model TEXT    NULL,
    prompt_version  INTEGER NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE thesis_claims (
    thesis_id  BIGINT NOT NULL REFERENCES theses(id) ON DELETE CASCADE,
    claim_id   BIGINT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    -- One table, not two: supporting and undermining are the same edge with
    -- opposite sign, and the PK stops a claim being both.
    role       TEXT   NOT NULL CHECK (role IN ('supporting', 'undermining')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (thesis_id, claim_id)
);

CREATE TABLE stories (
    id                   BIGSERIAL PRIMARY KEY,
    name                 TEXT NOT NULL,
    -- Structural stories outlive their parent topics: arms sovereignty
    -- survives the war's end. No hierarchy -- reality does not fit a tree.
    scope                TEXT NOT NULL CHECK (scope IN ('episodic', 'structural')),
    -- NULL last_material_change means "created, no members yet" and reads
    -- 'active'; the state must be defined at the default. Silence is
    -- 'dormant', never 'closed' -- only a review action closes a story.
    state                TEXT NOT NULL DEFAULT 'active'
                         CHECK (state IN ('active', 'dormant', 'closed')),
    last_material_change TIMESTAMPTZ NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX stories_name ON stories (lower(name));

CREATE TABLE story_members (
    id         BIGSERIAL PRIMARY KEY,
    story_id   BIGINT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    -- RESTRICT: 3.5 forbids rebuilding the member list, so a cascade would be
    -- a silent retcon of it -- the Day 3 failure mechanism.
    -- Member STATUS is deliberately NOT a column: it is derivable by join
    -- (superseded_by set, or the claim broken/expired/withdrawn), and a stored
    -- copy could go stale.
    event_id   BIGINT NULL REFERENCES events(id) ON DELETE RESTRICT,
    claim_id   BIGINT NULL REFERENCES claims(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(event_id, claim_id) = 1)
);
CREATE UNIQUE INDEX story_members_unique
    ON story_members (story_id, event_id, claim_id) NULLS NOT DISTINCT;

CREATE TABLE open_questions (
    id         BIGSERIAL PRIMARY KEY,
    story_id   BIGINT NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    text       TEXT   NOT NULL,
    due_date   DATE   NULL,
    status     TEXT   NOT NULL DEFAULT 'open'
               CHECK (status IN ('open', 'answered', 'dropped')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX open_questions_due ON open_questions (due_date) WHERE status = 'open';

CREATE TABLE links (
    id                   BIGSERIAL PRIMARY KEY,
    -- event -> observation only. Event -> event links are out of scope:
    -- nothing in 3.4 or the five rules needs them, and rule 2 ("an Observation
    -- with no explaining Link") is a clean LEFT JOIN under this shape.
    event_id             BIGINT  NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    observation_id       BIGINT  NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
    mechanism            TEXT    NOT NULL,
    -- How long the event keeps explaining the move. Confusing a re_rating
    -- driver with flow produced three contradictory chip verdicts in 72 hours.
    effect_kind          TEXT    NOT NULL
                         CHECK (effect_kind IN ('re_rating', 'risk_premium',
                                                'flow', 'fundamental_revision')),
    expected_persistence TEXT    NOT NULL
                         CHECK (expected_persistence IN ('session', 'days',
                                                         'weeks', 'structural')),
    -- NOT NULL, derived at write time from expected_persistence. A nullable
    -- decay date leaves a link 'unchecked' forever and never in rule 5.
    decay_check_date     DATE    NOT NULL,
    falsifier            TEXT    NULL,
    -- 'unchecked' does NOT become 'decayed' through the passage of time, or
    -- the persistence priors learn from measurements that never happened.
    status               TEXT    NOT NULL DEFAULT 'unchecked'
                         CHECK (status IN ('unchecked', 'active', 'decayed', 'refuted')),
    origin               TEXT    NOT NULL DEFAULT 'extracted'
                         CHECK (origin IN ('extracted', 'authored')),
    extractor_model      TEXT    NULL,
    prompt_version       INTEGER NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX links_observation ON links (observation_id);
CREATE INDEX links_decay_due ON links (decay_check_date) WHERE status = 'unchecked';
