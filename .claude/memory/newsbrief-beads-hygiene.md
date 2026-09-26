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
  **CORRECTION 2026-09-10: (b) does not reproduce under this machine's Git Bash.** A
  deliberately-CRLF script with `set -euo pipefail` ran clean, exit 0, stdout byte-identical
  under `od -c` to its LF twin. Note this bullet only ever said Git *would* hand over CRLF and
  bash *dies* — (b) reads as inferred, not observed, and (a) is the half that was actually
  measured here. Keep the pin (portability, plus (a) is real); stop predicting an execution
  failure on this machine.

**A Stop hook's bare stdout only reaches the transcript view.** To surface in the UI it
must print `{"systemMessage": "..."}`. Stop fires after *every* turn, not once at session
end, so a report appears where you are already looking rather than as a last gasp.

Verified in both directions with live controls, never by silence: hiding `hook-ignore`
brought bqa.11 back, claiming a bead named it and only it, and a clean repo prints nothing
and exits 0. See [[fix-the-tooling-dont-route-around-it]] and
[[user-runs-concurrent-sessions]] (why the hook rewrites only a generated file).


**`bd create --title` is path-converted by Git Bash (2026-09-11):** a title starting `/trade/history …` was stored as `C:/Program Files/Git/trade/history …`; the create echoed the rewritten title back as if I had typed it, and only `bd list` showed it. Prefix `MSYS_NO_PATHCONV=1` on any `bd` call whose title/description starts with `/`. Also: a bead can be FIXED and never closed — `qiz` was fixed in 7f0a904 and blocked `9tq` a day later; when a close is refused for a blocker, read the blocker's code before assuming it is real work.
## `bd update --notes` REPLACES the notes, despite its help text (2026-09-26)

`bd update --help` describes `--notes` as "Additional notes". It **overwrites**. Adding a
status note to `b42.5` silently erased the operator's 2026-09-22 hold reasoning (why the feed
retiming was paused, the half-tick slack bug, the phase split). It was restored only because the
committed `.beads/issues.jsonl` at `HEAD` still held the old text.

**How to apply:** to add to a bead that already has notes, use **`--append-notes`**. Use `--notes`
only on a bead whose notes you have just read and mean to rewrite. After any notes edit, check
that a marker phrase from the OLD text survives. The committed JSONL is the recovery path, so
export and commit after every bead change session.
