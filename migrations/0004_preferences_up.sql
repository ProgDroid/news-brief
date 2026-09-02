-- Reader overrides: /focus, /mute, /note and the pin set.
--
-- Per user, like sources and for the same reason (spec section 6.2): these are
-- the reading, not the world.
--
-- One row per VALUE rather than a JSON document per user, with `position`
-- carrying the order the reader added them in — the summary and the prompt both
-- render these lists, and a set would reorder them under the reader.

CREATE TABLE preferences (
    id         BIGSERIAL PRIMARY KEY,
    user_id    BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- The keys of the feedback dict verbatim, so there is no translation layer
    -- between what the handlers manipulate and what is stored.
    kind       TEXT        NOT NULL
               CHECK (kind IN ('focus', 'mute', 'notes', 'pin')),
    position   INTEGER     NOT NULL,
    -- NULL is a SENTINEL, not missing data: it means this kind is explicitly set
    -- and empty. That distinction is load-bearing and easy to lose. An absent
    -- `pin` means "the reader has never customised pins" and resolves to
    -- DEFAULT_PINS; an explicitly empty `pin` means "pin nothing". Collapse the
    -- two and /reset stops restoring the defaults, silently — see
    -- brief.resolved_pins, which is the consumer this encoding exists to serve.
    value      TEXT        NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX preferences_user_kind ON preferences (user_id, kind, position);

-- At most one empty-marker per kind: a marker alongside real values would be a
-- contradiction, and two markers would be a duplicate of nothing.
CREATE UNIQUE INDEX preferences_empty_marker
    ON preferences (user_id, kind) WHERE value IS NULL;
