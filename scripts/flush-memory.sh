#!/usr/bin/env bash
# Copy the LIVE memory corpus into the committed one, then stop.
#
# The counterpart to .claude/hooks/hydrate-memory.sh. That hook makes the
# committed corpus readable where the live one is absent; this makes the
# committed corpus current. Run it when memories written this session should
# reach a cloud session or a second machine.
#
# DELIBERATELY MANUAL, AND DELIBERATELY STOPS BEFORE COMMITTING.
#   This repository is PUBLIC. Every memory written here is therefore a
#   publishing candidate, and an automatic flush would make every memory write
#   an unreviewed publication. The 2026-09-04 review of 21 files found no
#   credentials but did find an operational map of the deploy host and a
#   behavioural profile of the author -- exactly the material that wants a human
#   glance rather than a hook. news-brief-iad.
#
#   So: this script writes files and prints a diff summary. It never runs
#   `git add` and never commits. Read `git diff -- .claude/memory/` before you do.
#
# Usage:
#   scripts/flush-memory.sh              # copy, then report
#   scripts/flush-memory.sh --dry-run    # report what would change, copy nothing

set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CORPUS="$REPO/.claude/memory"
MEMORY_ROOT="${HOME}/.claude/projects"
PROJECT=""
DRY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --memory-root) MEMORY_ROOT="${2:?--memory-root needs a value}"; shift 2 ;;
    --project)     PROJECT="${2:?--project needs a value}"; shift 2 ;;
    --dry-run)     DRY=1; shift ;;
    -h|--help)     sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# Derive the project key from cwd when not supplied: /g/a/b -> G--a-b.
# Unlike the hydrate hook, a wrong answer here is NOT safe -- it would read an
# empty or foreign corpus and report "nothing to flush" -- so this one refuses
# rather than guessing.
if [ -z "$PROJECT" ]; then
  drive="$(printf '%s' "$REPO" | sed -n 's:^/\([a-z]\)/.*:\1:p' | tr 'a-z' 'A-Z')"
  rel="$(printf '%s' "$REPO" | sed -n 's:^/[a-z]/::p')"
  if [ -z "$drive" ] || [ -z "$rel" ]; then
    echo "Could not derive the project key from '$REPO'." >&2
    echo "Pass it explicitly: --project <key>   (see $MEMORY_ROOT)" >&2
    exit 1
  fi
  PROJECT="${drive}--$(printf '%s' "$rel" | tr '/ ' '--')"
fi

LIVE="$MEMORY_ROOT/$PROJECT/memory"
if [ ! -d "$LIVE" ]; then
  echo "No live corpus at: $LIVE" >&2
  echo "Nothing to flush. On a machine without one, the committed corpus IS the corpus." >&2
  exit 1
fi
if [ ! -f "$LIVE/MEMORY.md" ]; then
  echo "Refusing: $LIVE has no MEMORY.md, so it is not a corpus." >&2
  exit 1
fi

# What differs, computed BEFORE copying so the report survives the copy.
added=0
changed=0
for src in "$LIVE"/*.md; do
  base="$(basename "$src")"
  if [ ! -f "$CORPUS/$base" ]; then
    added=$((added + 1))
    printf '  NEW      %s\n' "$base"
  elif ! cmp -s "$src" "$CORPUS/$base"; then
    changed=$((changed + 1))
    printf '  CHANGED  %s\n' "$base"
  fi
done

# A file in the committed corpus with no live counterpart is NOT deleted here:
# it may predate this machine, and losing a memory to a sync is worse than
# carrying a stale one. Report it and let a human decide.
for dst in "$CORPUS"/*.md; do
  base="$(basename "$dst")"
  [ -f "$LIVE/$base" ] || printf '  ONLY IN REPO (left alone)  %s\n' "$base"
done

if [ "$added" -eq 0 ] && [ "$changed" -eq 0 ]; then
  echo "Corpora already match: nothing to flush."
  exit 0
fi

if [ "$DRY" -eq 1 ]; then
  echo
  echo "--dry-run: $added new, $changed changed. Nothing was written."
  exit 0
fi

for src in "$LIVE"/*.md; do
  cp "$src" "$CORPUS/$(basename "$src")"
done

echo
echo "Flushed $added new and $changed changed entries into .claude/memory/."
echo
echo "THIS REPOSITORY IS PUBLIC. Nothing has been staged or committed."
echo "Review before you do:"
echo "    git diff -- .claude/memory/"
