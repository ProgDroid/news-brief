-- Reverse dependency order. Later tasks PREPEND their drops, which keeps the
-- order correct automatically: each task's tables depend only on earlier ones.
DROP TABLE IF EXISTS links;
DROP TABLE IF EXISTS open_questions;
DROP TABLE IF EXISTS story_members;
DROP TABLE IF EXISTS stories;
DROP TABLE IF EXISTS thesis_claims;
DROP TABLE IF EXISTS theses;
DROP TABLE IF EXISTS claim_evidence;
DROP TABLE IF EXISTS claims;
DROP TABLE IF EXISTS observations;
DROP TABLE IF EXISTS assertions;
DROP TABLE IF EXISTS event_entities;
DROP TABLE IF EXISTS events;
DROP TABLE IF EXISTS entity_instruments;
DROP TABLE IF EXISTS entities;
DROP TABLE IF EXISTS items;
DROP TABLE IF EXISTS outlets;
DROP FUNCTION IF EXISTS claims_freeze_claim_text();
