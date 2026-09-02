-- Reverse dependency order. Later tasks PREPEND their drops, which keeps the
-- order correct automatically: each task's tables depend only on earlier ones.
DROP TABLE entity_instruments;
DROP TABLE entities;
DROP TABLE items;
DROP TABLE outlets;
