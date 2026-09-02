-- Telegram-managed news sources, per reader.
--
-- These carry user_id because a source is part of how one person reads the
-- world (spec section 6.2). The KB tables that will hold what the sources SAY
-- deliberately do not: the world is shared, the reading of it is personal.

CREATE TABLE sources (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT        NOT NULL,
    url          TEXT        NOT NULL,
    category     TEXT        NOT NULL,
    -- Coerced rather than rejected on read as well, because these values reach
    -- the model as tags and a wrong one is worse than a default one. The CHECK
    -- stops a bad value being WRITTEN; brief.load_temp_sources still coerces on
    -- read, so a row that predates a constraint change degrades to the default
    -- instead of taking down the morning brief.
    kind         TEXT        NOT NULL DEFAULT 'regional'
                 CHECK (kind IN ('wire', 'analyst', 'regional', 'primary')),
    source_type  TEXT        NOT NULL DEFAULT 'feed'
                 CHECK (source_type IN ('feed', 'page')),
    -- Optional and sparse: set only where a national/bloc vantage changes the
    -- read. NULL means "no vantage claim made", NOT "neutral" — there is
    -- deliberately no NEUTRAL value, because calling a source neutral is a
    -- positive editorial claim, as contestable as picking a side.
    perspective  TEXT        NULL
                 CHECK (perspective IS NULL OR perspective IN (
                     'WESTERN', 'CHINESE', 'RUSSIAN', 'IRANIAN', 'ISRAELI',
                     'ARAB', 'UKRAINIAN', 'JAPANESE', 'KOREAN', 'INDIAN')),
    state_funded BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The dedup rule /addsource has always applied, enforced by the store rather
-- than by a read-filter-write in the caller: adding a URL that is already there
-- updates it in place.
CREATE UNIQUE INDEX sources_user_url ON sources (user_id, url);
