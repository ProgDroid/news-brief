---
name: newsbrief-beads-hygiene
description: "The commit-trailer convention that marks a bead complete, the Stop hook that reads it, and the three probe traps in checking bd state (export population, phantom ids, CRLF)"
metadata: 
  node_type: memory
  type: project
  originSessionId: a1a7f73a-1f3e-48a3-93e0-6dd1c51ba905
  modified: 2026-09-08T17:05:16.515Z
---

Built 2026-09-08 after `news-brief-bqa.12` was committed complete and stayed open for a
day, and `.beads/issues.jsonl` — which is **tracked in git** — was found ten issues behind.
Commit `31cefcc` is titled "refresh a week-stale export", so the second had already
happened before. A stale committed export is a *published* misstatement of issue state.

**The convention already existed and nothing read it.** A bead id **alone on its own
line** in a commit message means "this commit completes it" (`d48b036` ends with
`news-brief-bqa.12`). An id mentioned inline in prose is a *reference*, not a claim —
`be418b3` names three open beads that way. `.claude/hooks/check-beads.sh` (Stop hook)
now reads that distinction.

**It REPORTS beads and only REWRITES the export, and the asymmetry is the finding.**
The same probe that caught bqa.12 also flagged `bqa.11`, whose trailer is simply **wrong**
— commit `338d2bb` is about opaque candidate labels, while bqa.11 is the blocked
"enable COMPREHEND_ENABLED and run the gate". The bead status was right; the commit was
wrong. A signal measured 1-for-2 must never close anything. Regenerating a derived file
is a different risk class: idempotent, reviewable as a diff, decides nothing.
`.beads/hook-ignore` records mis-attributions, because pushed history cannot be rewritten
and **an id that fires every turn is how a hook earns being switched off**.

## Three probe traps, all hit while building this

- **`bd export` omits infrastructure beads by default**, so its line count and the
  `bd stats` total describe **different populations**. 93 vs 103 looked like pure drift
  and partly was not. Never use a count comparison as the drift signal — export to a temp
  file and compare bytes, which is also the command that fixes it.
- **Ids must come from `bd list --json`, never the rendered table.** A regex over the
  table also matches inside TITLES: "Re-extract items ..." produced a phantom bead
  `e-extract`. The JSON is one field per line, so an anchored match on the id line is
  structural rather than a guess about word shapes.
- **`core.autocrlf` is true here and the repo had no `.gitattributes`.** Git would hand a
  fresh clone CRLF, and then (a) `bd export`'s LF output never byte-matches the working
  file, so the hook fires every turn, and (b) **bash dies on a CRLF script** with
  `$'\r': command not found` — a hook failing that way is indistinguishable from one that
  was never wired up. Now pinned: `.beads/issues.jsonl -text` and `*.sh text eol=lf`.

**A Stop hook's bare stdout only reaches the transcript view.** To surface in the UI it
must print `{"systemMessage": "..."}`. Stop fires after *every* turn, not once at session
end, so a report appears where you are already looking rather than as a last gasp.

Verified in both directions with live controls, never by silence: hiding `hook-ignore`
brought bqa.11 back, claiming a bead named it and only it, and a clean repo prints nothing
and exits 0. See [[fix-the-tooling-dont-route-around-it]] and
[[user-runs-concurrent-sessions]] (why the hook rewrites only a generated file).
