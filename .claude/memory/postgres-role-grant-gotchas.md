---
name: postgres-role-grant-gotchas
description: "Measured PG 18.6 behaviour for least-privilege roles and plpgsql guards — CREATEROLE vs ADMIN OPTION, membership direction, column-grant revocation, and why a to_regclass AND-guard still fails"
metadata:
  node_type: memory
  type: reference
  originSessionId: 3643de5b-dee7-49f4-8b2e-34e984499482
  modified: 2026-09-30T16:07:36.633Z
---

All measured on PostgreSQL 18.6 while building the census labeller role (2026-09-29); each one
contradicted a plausible assumption.

- **A CREATEROLE role can only ALTER roles it holds ADMIN OPTION on (PG 16+).** A creator gets it
  automatically. So "have a superuser create the role, then let the app role set its password"
  FAILS at `ALTER ROLE ... PASSWORD` (`permission denied to alter role`), with or without
  CREATEROLE. Either the app role creates the role itself, or a superuser first runs
  `GRANT <role> TO <app role> WITH ADMIN OPTION`.
- **That automatic creator grant is a `pg_auth_members` row in the REVERSE direction.** When
  checking "is role X a member of anything" (over-privilege checks), filter
  `WHERE m.member = X`, never `m.roleid` — otherwise the creator's ADMIN row is misreported as a
  membership.
- **Membership is the privilege hole a per-object check misses.** `GRANT pg_write_all_data TO x`
  or `GRANT <owner role> TO x` (inherits ownership, incl. DROP) leaves every per-table ACL clean.
  Check `pg_auth_members` and role attributes (incl. REPLICATION) alongside the ACLs.
- **A table-level `REVOKE ALL` also clears column-level grants** on that table — no per-column
  revoke needed for an exact-set reset.
- **plpgsql plans a table reference even inside a guarded branch of the same expression:**
  `IF to_regclass('t') IS NOT NULL AND EXISTS (SELECT 1 FROM t) THEN` still raises
  `UndefinedTable` when `t` is missing. Use a nested `IF` (outer on `to_regclass`, inner
  referencing the table).

**How to apply:** for any least-privilege role setup here or elsewhere, write the grant script as
revoke-then-grant, check memberships + attributes + ACLs, and test the creation path against a
role that does NOT yet exist AND one that does. Related: [[event-census-sp0]].
