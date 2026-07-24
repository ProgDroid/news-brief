---
name: direct-page-temp-sources
description: "Temp sources can now be direct pages (source_type=page) scraped via fetch_web_source, not just RSS; Iran dashboard migrated off in-code WEB_SOURCES"
metadata: 
  node_type: memory
  type: project
  originSessionId: 3eefcec3-6899-43f7-b31c-3bcd42b64680
---

2026-06-25 BUILT+REVIEWED+PUSHED to main (commits 353906e..5dfa929, 6 commits, 364 tests; pushed e255436..5dfa929 → Docker deploy triggered).

**What:** A temp source (`sources.json` on the deploy volume) can carry
`"source_type": "page"` (default `"feed"`). `load_temp_sources` validates it;
`_split_temp_sources` partitions; `mode_submit` routes `feed`→`fetch_rss`,
`page`→`fetch_web_source` (alongside the now-empty always-on `WEB_SOURCES`
baseline). `fetch_web_source` now renders a `[KIND]` header tag so pages get
the brief's forward-tilt like feeds. The `/addsource` wizard asks
"RSS feed or page to scrape?" for full URLs (bare domains stay Google-News
feeds). Root cause it fixed: a full page URL pasted into a temp source was
parsed as RSS → feedparser "not well-formed" → silent 0 entries.

**Why pages, not Google News:** the trigger was a BCA Research dashboard — a
direct, paywalled-but-meta-rich analyst page. `fetch_web_source` extracts its
`<meta description>` (verified live: substantive content, HTTP 200). The
situational Iran/Hormuz dashboard was hardcoded in `WEB_SOURCES`; moving it to
a temp source lets it be dropped without a redeploy. See
[[live-state-on-deploy-host]].

**PENDING MANUAL HOST STEP (not done yet):** after deploying this code,
re-add BOTH BCA dashboards as `source_type=page` temp sources on the volume
(via `/addsource` → "Page to scrape", or hand-edit `sources.json`):
- Iran Conflict Daily Dashboard — `https://www.bcaresearch.com/collection/bcas-iran-conflict-daily-dashboard` (category `iran`)
- US Midterm Election Dashboard — `https://www.bcaresearch.com/dashboard/us-midterm-election-dashboard` (category `us`)

Spec/plan: `docs/superpowers/specs|plans/2026-06-25-direct-page-temp-sources*`.
