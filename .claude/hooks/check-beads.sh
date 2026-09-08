#!/usr/bin/env bash
# Stop hook: catch the two ways bd state goes quietly stale.
#
# Both failure modes are real and measured in this repo on 2026-09-08:
#
#   1. news-brief-bqa.12 was BUILT and COMMITTED (d48b036, whose message names
#      the bead on its own line) and stayed open for a day. The convention for
#      declaring a bead complete already existed; nothing ever read it.
#   2. .beads/issues.jsonl is TRACKED, and was missing ten issues. Commit
#      31cefcc is titled "refresh a week-stale export", so this had already
#      happened at least once before.
#
# It REPORTS beads and only REWRITES the export. That asymmetry is deliberate:
# the same probe that found bqa.12 also flagged news-brief-bqa.11, whose commit
# trailer was simply wrong -- the bead is blocked, not done. A signal measured
# 1-for-2 must not close anything. Regenerating a derived file is a different
# risk class: idempotent, reviewable as a diff, and it decides nothing.
#
# Exits 0 and prints NOTHING when there is nothing to say, so a clean repo is
# silent. Prints {"systemMessage": ...} otherwise -- bare stdout would only
# reach the transcript view, which nobody opens.

set -uo pipefail

ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." 2>/dev/null && pwd)}"
[ -n "$ROOT" ] && cd "$ROOT" 2>/dev/null || exit 0
[ -d .beads ] || exit 0
command -v bd >/dev/null 2>&1 || exit 0

notes=""
add_note() { [ -n "$notes" ] && notes="$notes  |  $1" || notes="$1"; }

# Ids come from --json, never from the rendered table. A regex over the table
# also matches inside TITLES: "Re-extract items ..." yielded a phantom bead
# `e-extract`. The JSON is pretty-printed one field per line, so an anchored
# match on the id line is structural rather than a guess about word shapes.
bead_ids() {
    bd list --status="$1" --json 2>/dev/null |
        grep -oE '^[[:space:]]*"id": *"[^"]+"' |
        sed 's/.*"\([^"]*\)"$/\1/' | sort -u
}

# --- Which bead ids currently exist and are open.
# Everything below intersects against THIS list, so a bare line in a commit
# that merely looks like an id cannot produce a false report.
open_ids=$(bead_ids open)

# --- 1. A commit declared it complete, but it is still open.
if [ -n "$open_ids" ]; then
    declared=$(git log --format='%B' 2>/dev/null |
        grep -E '^[a-z][a-z0-9-]*-[a-z0-9]+(\.[0-9]+)?$' | sort -u)
    # A trailer can name the WRONG bead, and pushed history cannot be
    # rewritten, so known mis-attributions are recorded rather than tolerated
    # -- an id that fires every turn is how a hook earns being switched off.
    if [ -f .beads/hook-ignore ]; then
        ignored=$(grep -vE '^\s*(#|$)' .beads/hook-ignore | tr -d '\r' | sort -u)
        [ -n "$ignored" ] && declared=$(comm -23 <(printf '%s\n' "$declared") <(printf '%s\n' "$ignored"))
    fi
    if [ -n "$declared" ]; then
        stale=$(comm -12 <(printf '%s\n' "$declared") <(printf '%s\n' "$open_ids"))
        if [ -n "$stale" ]; then
            add_note "beads named as done by a commit but still open: $(echo "$stale" | tr '\n' ' ')"
        fi
    fi
fi

# --- 2. Claimed and never closed. Catches the shape signal 1 cannot see:
# work that was claimed but produced no commit trailer.
inprog=$(bead_ids in_progress)
if [ -n "$inprog" ]; then
    add_note "still in_progress: $(echo "$inprog" | tr '\n' ' ')"
fi

# --- 3. The tracked export drifted from the database.
# Compare against a fresh export, NOT a record count: `bd export` omits
# infrastructure beads by default, so its line count and `bd stats` total
# describe different populations and would disagree even when current.
if [ -f .beads/issues.jsonl ]; then
    tmp=$(mktemp 2>/dev/null) || tmp=""
    if [ -n "$tmp" ]; then
        # -s guards the empty case: a bd failure that still exits 0 would
        # otherwise blank a tracked file, and an empty result is exactly as
        # suspicious as an unexpectedly full one.
        if bd export -o "$tmp" 2>/dev/null && [ -s "$tmp" ]; then
            if ! cmp -s "$tmp" .beads/issues.jsonl; then
                cp "$tmp" .beads/issues.jsonl 2>/dev/null &&
                    add_note "refreshed .beads/issues.jsonl from bd -- review and commit it"
            fi
        fi
        rm -f "$tmp"
    fi
fi

[ -z "$notes" ] && exit 0

# Manual JSON is safe here: every interpolated value is a bead id
# ([a-z0-9.-]) and the fixed text carries no quote or backslash.
printf '{"systemMessage": "bd: %s"}\n' "$notes"
