-- Coordination state: the pending batch id and the Telegram update offset.
--
-- NOT `settings`, despite the similar shape. Settings are configuration a
-- person chooses; these are values processes write to each other while running,
-- and conflating them would put a cache in front of state where staleness
-- causes a double submit (spec section 5.2 lists batch_state separately for the
-- same reason).
--
-- Un-tenanted, like trading (section 6.5): there is one bot, so there is one
-- getUpdates offset, and the pending batch belongs to the deployment.

CREATE TABLE runtime_state (
    key        TEXT        PRIMARY KEY,
    -- JSON-encoded, so a value round-trips as the type it was written as. The
    -- readers care: `tg_offset` is compared and incremented as an int, `date`
    -- as a string, and a TEXT column that stringified everything would make
    -- `state.get("tg_offset", 0) + 1` a silent concatenation.
    value      TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
