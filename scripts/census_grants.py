"""Create the `census_labeller` role and apply its grants (spec 6.1).

Re-runnable host-runbook script: run it after migration 0017 is confirmed in
`schema_migrations`, and again after any restore (roles are cluster-global and
survive DROP SCHEMA, but grants do not). The grant list lives once, in
`census.LABELLER_GRANTS`, and a re-run first revokes everything else the role
holds in schema public, so the result is exactly that list.

Run it with the password loaded from `.env`, never typed on the command line
(a command line lands in shell history):

    set -a; . ./.env; set +a
    py scripts/census_grants.py
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import census  # noqa: E402  (path shim above must run first)
import db  # noqa: E402

PASSWORD_VAR = "CENSUS_LABELLER_PASSWORD"


def main() -> int:
    password = os.environ.get(PASSWORD_VAR, "")
    if not password:
        print(
            f"{PASSWORD_VAR} is empty: set it in .env to the labeller's password "
            "and load .env (see this script's docstring).",
            file=sys.stderr,
        )
        return 2
    with db.connect() as conn:
        census.apply_labeller_grants(conn, password)
    print("grants applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
