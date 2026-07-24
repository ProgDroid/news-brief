---
name: signals-delimiter-fragility
description: "Why the brief's signals delimiter is @@@SIGNALS@@@ + marker-agnostic parsing; don't add markdown-looking in-band markers to model prompts"
metadata: 
  node_type: memory
  type: project
  originSessionId: ddcce554-d0f6-44e0-a600-c46053e944b5
---

The model drops/restyles markdown-looking in-band delimiters in the brief prompt. The signals delimiter was `---SIGNALS---`; the model kept collapsing it to a bare `---` (word `SIGNALS` gone), so `split_brief_and_signals` returned `no_marker` and dumped the JSON into the delivered brief. Now the prompt asks for `@@@SIGNALS@@@` (told explicitly "literal marker, NOT a divider") and the parser is marker-agnostic: it tries known markers, then falls back to recovering the trailing JSON array directly (`_find_trailing_json_array`).

**Why:** The OUTPUT FORMAT block says "No markdown. No # headers. No asterisks." A `---`-style token reads as a markdown thematic break, so the model "complies" by normalizing it. Any exact in-band marker is a fragile contract with an LLM when other prompt rules fight the marker's appearance.

**How to apply:** When adding a new delimiter to any model prompt in brief.py, avoid markdown-looking tokens (`---`, `===`, `###`, `***`); use an opaque sentinel like `@@@X@@@` and tell the model it's literal. Better, don't rely on exact-match parsing — anchor on structure (e.g. the trailing JSON array) so signals survive a mangled marker. See also [[formatter-owns-style]].
