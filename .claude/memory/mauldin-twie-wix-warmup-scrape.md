---
name: mauldin-twie-wix-warmup-scrape
description: "Mauldin \"The World Isn't Ending\" (Jacob Shapiro) baked-in source + the reusable Wix-warmup-data scrape technique for feedless Wix sites"
metadata: 
  node_type: memory
  type: project
  originSessionId: be6955bb-9a7d-4a26-b93d-67a6144c12f0
---

2026-06-27 BUILT+PUSHED (c50ae8a → origin/main, deploy triggered; 549 tests). Added Jacob Shapiro's weekly **"The World Isn't Ending"** Mauldin column as an always-on baked-in source in `brief.py` (`fetch_mauldin_twie`, tagged `[ANALYST] (GEOPOLITICS)`), wired into `mode_submit`'s `feed_blocks`, fail-safe.

**Why nothing standard worked:** Mauldin is a **Wix SPA with no RSS feed** — feed discovery (`/feed`, `/blog-feed.xml`), Google News `site:`, and `og:description` all fail because a plain `requests.get` sees an empty JS shell. The whole column IS Shapiro (sole author), so "only his articles" = the section path, no per-author filtering.

**The reusable technique — feedless Wix → parse the hydration JSON:** Wix embeds the full structured dataset in a `<script id="wix-warmup-data">` JSON blob for client hydration. That blob is cleaner than rendered HTML: real titles (curly quotes/colons), ISO publish `date`, `metaDescription` summaries, slug links — all under keys named after the collection (here `TheWorldIsntEnding`). KEY GOTCHA: the **section/landing page carries only the collection SCHEMA; you must fetch an ARTICLE page to get the actual records** (which are date-sorted and self-correcting, so any article works). So flow = section page → first `/the-world-isnt-ending/<slug>` link → article page → parse warmup → filter rows needing text title + ISO date + link → newest-first. NOT `fetch_web_source` (that only grabs meta-description/[:800] — useless on Wix).

This is a different mechanism than [[direct-page-temp-sources]] (`source_type=page`) and [[google-news-rss-recipe]] (dead-RSS workaround). Fits the perspective-matrix/source-mining thread in [[external-geo-dashboards-backlog]]. Date field is `{"$date": ISO}` or bare ISO; `_twie_iso_date` normalizes.
