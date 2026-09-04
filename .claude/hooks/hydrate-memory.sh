#!/usr/bin/env bash
# SessionStart hook: make this project's memory corpus reachable from a machine
# that does not have the local one.
#
# WHY THIS EXISTS
#   The corpus lives in TWO places. The live one is
#   ~/.claude/projects/<key>/memory/, which the auto-memory system reads and
#   writes and which loads into context automatically. The other is
#   <repo>/.claude/memory/, committed to git.
#
#   Only the first one is local. A cloud session, or a second machine, gets a
#   clone -- so it gets the committed copy and nothing loads it. Everything the
#   project has decided and rejected (the sentiment-sizing null, the retracted
#   fade-the-event rule, the dropped gbrain migration) is invisible there, and
#   expensive to re-derive. news-brief-iad.
#
# WHAT IT DOES
#   When the live corpus is ABSENT, prints the committed MEMORY.md index into
#   the session, plus the path to read an individual entry. Same shape as the
#   aegypt-wiki hook: an index is enough to notice an entry is relevant, and the
#   body is then a plain file read.
#
#   When the live corpus is PRESENT it prints nothing, because the auto-memory
#   system has already loaded it and a second copy is noise.
#
# WHY IT DOES NOT COPY FILES INTO THE LIVE CORPUS
#   That was the first design and it is wrong twice over. The project key is
#   derived from the working directory, and every key on the author's machine is
#   a Windows drive path -- so the form Claude Code uses on a Linux host is
#   UNVERIFIED here. A wrong key writes to a directory nothing reads, silently,
#   on exactly the platform this exists to serve. Copying would also overwrite a
#   live corpus that is normally AHEAD of the committed one, destroying memories
#   to fix a staleness problem.
#
#   Note what that means for the check below: on a host whose path form does not
#   match, detection fails and the corpus is treated as absent, so the index is
#   injected. That is the right answer for a cloud box, so the half that cannot
#   be verified here fails in the safe direction.
#
# FAILS OPEN: every error path exits 0 with no output. A broken hook must never
# block a session from starting.

set -u

CORPUS="$(dirname "$0")/../memory"
MEMORY_ROOT="${HOME}/.claude/projects"
PROJECT=""
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --corpus)      val="${2:-}"; [ -n "$val" ] || exit 0; CORPUS="$val"; shift 2 ;;
    --memory-root) val="${2:-}"; [ -n "$val" ] || exit 0; MEMORY_ROOT="$val"; shift 2 ;;
    --project)     val="${2:-}"; [ -n "$val" ] || exit 0; PROJECT="$val"; shift 2 ;;
    --force)       FORCE=1; shift ;;
    *) shift ;;
  esac
done

INDEX="$CORPUS/MEMORY.md"
[ -f "$INDEX" ] || exit 0

# --- Is the live corpus already loaded? -------------------------------------
# Same derivation as ~/.claude/hooks/memory-index-nudge.sh: /g/a/b -> G--a-b.
# Only used to answer "is it there", never to write, so a miss is safe.
if [ "$FORCE" -eq 0 ]; then
  if [ -z "$PROJECT" ]; then
    cwd="$(pwd)"
    drive="$(printf '%s' "$cwd" | sed -n 's:^/\([a-z]\)/.*:\1:p' | tr 'a-z' 'A-Z')"
    rel="$(printf '%s' "$cwd" | sed -n 's:^/[a-z]/::p')"
    if [ -n "$drive" ] && [ -n "$rel" ]; then
      PROJECT="${drive}--$(printf '%s' "$rel" | tr '/ ' '--')"
    fi
  fi
  if [ -n "$PROJECT" ] && [ -f "$MEMORY_ROOT/$PROJECT/memory/MEMORY.md" ]; then
    exit 0
  fi
fi

# --- Inject ------------------------------------------------------------------
count=$(ls "$CORPUS"/*.md 2>/dev/null | wc -l | tr -d ' ')

echo "# Project memory for news-brief - $count entries, loaded from the repo"
echo
echo "The live memory corpus is NOT present on this machine, so the committed copy"
echo "at .claude/memory/ is being used instead. This is the index; it is the same"
echo "file the auto-memory system loads where that corpus does exist."
echo
echo "HOW TO USE IT"
echo "  - A line below is a POINTER, not the content. If an entry is relevant to"
echo "    the task, READ IT before answering: .claude/memory/<name>.md"
echo "  - These are prior conclusions, several of them decisions to NOT do"
echo "    something. Re-proposing a rejected option is the failure this prevents."
echo "  - Memories written this session land in the auto-memory system, which is"
echo "    NOT this directory. To carry them back, run scripts/flush-memory.sh and"
echo "    commit the result -- the repo is PUBLIC, so review the diff first."
echo
cat "$INDEX"
exit 0
