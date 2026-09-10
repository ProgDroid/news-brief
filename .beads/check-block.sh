#!/usr/bin/env sh
# Verify the Beads managed block has not clobbered the local TodoWrite override.
# Run after any `bd setup claude` or beads upgrade.
#
# Why this is a script and not a grep line inside CLAUDE.md: two earlier versions
# were written as commands inside CLAUDE.md itself, and BOTH matched their own
# documentation -- the first on the ban string, the second on the block markers,
# which made sed run to EOF. A probe that lives inside the corpus it probes will
# match itself. Measured 2026-09-10, twice, within five minutes.
#
# Exit: 0 = ok, 1 = a check FAILED, 2 = UNKNOWN (the probe could not answer).

set -u
F="${1:-CLAUDE.md}"
fail=0

# Stop at the FIRST end marker. A stray later marker (in documentation, say)
# must not be able to extend the range to EOF.
block=$(awk '/BEGIN BEADS INTEGRATION/{f=1} f{print} /END BEADS INTEGRATION/{if(f)exit}' "$F")

if [ -z "$block" ]; then
  echo "UNKNOWN: no managed block found in $F -- the probe cannot answer"
  exit 2
fi

if printf '%s\n' "$block" | grep -q "do NOT use TodoWrite"; then
  echo "FAIL: the TodoWrite ban is back inside the managed block"
  fail=1
else
  echo "ok: no TodoWrite ban inside the managed block"
fi

if grep -q "^## Task tracking vs. execution structure" "$F"; then
  echo "ok: the local override section is present"
else
  echo "FAIL: the local override section is missing from $F"
  fail=1
fi

exit "$fail"
