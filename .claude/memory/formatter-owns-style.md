---
name: formatter-owns-style
description: "In news-brief, ruff (the format-on-save hook) owns whitespace; do not preserve hand-alignment"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 9cfbeb03-c3af-42c3-8610-52f031adb027
---

For the news-brief project (`brief.py`), the user prefers the **formatter to own code style** and places little value on the hand-aligned columns visible in early commits (aligned `=`, single-line `RSS_FEEDS` dicts, aligned dict keys). A PostToolUse `ruff format` hook reflows `brief.py` on every edit and collapses manual alignment — this is accepted and intended.

**Why:** User stated a clear preference for simplicity — "if there's a formatter, use it" — when ruff's reflow conflicted with an earlier "match existing style" request. A format-on-save hook and hand-alignment are fundamentally incompatible (ruff has no preserve-columns mode).

**How to apply:** Don't try to preserve or restore column alignment in `brief.py`. Let ruff format the file; expect large whitespace-only diffs after edits and don't fight them. Keep the section comment banners (`# ── Section ──`) — those survive formatting and are part of the intended style.
