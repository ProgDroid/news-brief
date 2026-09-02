-- Reverse dependency order. Later tasks PREPEND their drops, which keeps the
-- order correct automatically: each task's tables depend only on earlier ones.
DROP TABLE claim_evidence;
DROP TABLE claims;
DROP TABLE observations;
DROP TABLE assertions;
DROP TABLE event_entities;
DROP TABLE events;
DROP TABLE entity_instruments;
DROP TABLE entities;
DROP TABLE items;
DROP TABLE outlets;
DROP FUNCTION claims_freeze_claim_text();
