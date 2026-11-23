---
name: pre-push-privacy-scan
description: "How to check outgoing commits for private portfolio data before pushing this PUBLIC repo — scan EVERY workbench CSV (not just theses/watchlist/triggers), with the control inside the scanned text; known false positives; the author email is already public"
metadata:
  node_type: memory
  type: project
  originSessionId: dd284abf-9374-4918-ac8e-e3648a2b3cca
  modified: 2026-11-22T19:50:57.102Z
---

Before pushing, scan the ADDED lines of the outgoing range (`git diff <origin>..HEAD | grep '^+'`) against the private workbench's `data/*.csv`. Proven recipe (2026-11-22, the qap push):

- **Identifiers:** every cell of a column whose header matches `ticker|symbol|instrument|name|key|^id$|isin|asset`, from **all** CSVs (positions, buys, trades, allocations, decisions, theses, watchlist, event_triggers). Whole-word, case-insensitive.
- **Free text:** 5-word shingles of any cell with 8+ words (thesis text, notes), lowercased substring match.
- **Amounts:** every decimal with 3+ places in any cell (quantities, prices, weights).
- **Credentials/infra:** regexes for `sk-ant-`, `ghp_`, `xox[bp]-`, Telegram bot tokens, `AKIA`, key/secret assignments, IPv4, emails, host names.
- **Controls go INTO the scanned file** (append a control term, a fake key, `10.1.2.3`, `someone@example.org`), never only into the term list.

**Why:** the older one-liner scan read only theses keys, watchlist names and trigger ids, so positions, trades and thesis text were never checked; and its control term sat in the term list, so `matched=0` on a file without it proved nothing (probe-failures #4, 2026-11-22). The full recipe's script lived in a session scratchpad: rewrite it from the bullets above (~60 lines), print counts per source and identifier names only, never matched free-text or amounts.

**How to apply:** expect false positives from short tickers and thesis keys that are also ordinary English or technical words (the case-insensitive match catches the word), and from API-cost decimals in specs that coincide with weight figures. Read every hit in context. **NEVER write the matched terms down anywhere in this repo, this memory included**: the list of "harmless" matches IS a list of the portfolio's tickers and thesis keys, and memory syncs into the public repo (a side agent caught exactly that in this note, 2026-11-22). **The author's email and name are ALREADY public**: the git author field of every commit since the start, plus `owner`/`created_by` in `.beads/issues.jsonl` (202 occurrences before 2026-11-22). Not a new leak when it shows up; switching to a GitHub no-reply address was offered, not done. Related: [[live-state-on-deploy-host]], [[memory-corpus-does-not-travel]].
