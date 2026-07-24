---
name: google-news-rss-recipe
description: "How to build Google News RSS URLs to replace dead publisher feeds (e.g. Reuters), and why site: beats allinurl:+when:"
metadata: 
  node_type: memory
  type: reference
  originSessionId: 11654da7-d94c-4b83-a76e-7bbaac27f89b
---

Reuters (and many outlets) killed public RSS — Reuters in June 2020. Synthesize a feed via Google News search RSS:

`https://news.google.com/rss/search?q=<QUERY>&hl=en-US&gl=US&ceid=US%3Aen`

Build `<QUERY>` (URL-encode it: `:`→`%3A`, `/`→`%2F`, space→`+`):
- **Use `site:reuters.com/markets`** (a section path), NOT `allinurl:`. `site:` is the robust operator — returns ~100 latest items and is stable across repeated calls.
- **`allinurl:` + `when:` is the fragile combo to avoid** — verified `0,0,27` items across three back-to-back calls (HTTP 200, no error). That volatility was the `allinurl:` operator plus rapid-fire rate-limiting, NOT `when:` itself.
- **`when:Nd` with `site:` is stable and a useful freshness guardrail.** Measured stable counts: `when:1d` world=28 / markets=100; `when:2d` world=79 / markets=100. news-brief uses `when:2d+site:reuters.com/...` so a quiet section can't feed the LLM stale headlines as news.
- Recency is a *staleness guardrail*, not a volume control: `fetch_rss` only takes the 5 newest items (`max_items=5`), so feed size (28 vs 100) doesn't change tokens. Substack/Nitter feeds aren't Google News and ignore `when:` (fine — long-form analysis ages well; wire headlines don't).

Always verify a candidate returns items before committing: `curl -s <url> | grep -oE '<item>' | wc -l` (use `grep -o`, not line-count — Google's feed is one line). The first `<title>` is the channel ("Google News"); real items start at the 2nd.

The news-brief `fetch_rss` swallows nothing now — it logs `parsed.status`/`bozo_exception`, so an empty feed shows e.g. `No entries: Reuters World (HTTP 200)` to distinguish dead vs transiently empty. See [[formatter-owns-style]] for the repo's ruff-on-save convention.

**Deep section paths can be stale-only — toggle `when:` to detect it (2026-06-26).** A NARROW sub-path indexes very differently from a top-level section. `site:reuters.com/markets/commodities` returned only **1** item with `when:2d` AND `when:7d`, **6** with `when:30d`, but **100** with NO `when:` filter. That gap is the tell: Google News has ~100 OLD items under that path but almost nothing dated recently → the no-`when:` volume is STALE and defeats the freshness guardrail. Two bad ways to force volume: drop `when:` (stale) or switch to a loose keyword query (`commodities site:reuters.com`=100 fresh but matches the WORD anywhere on the domain, not the section). **Diagnostic rule: if `no-when ≫ when:2d`, the path lacks recent items — don't rescue it, go to a NATIVE feed instead** (here: Mining.com `mining.com/feed/`, 36 native entries, replaced the dead Reuters proxy). Contrast the top-level `/markets` path, which genuinely has 100 fresh items at `when:2d`.

**Region-native feed verification (2026-06-14 sources redesign).** Verified entry counts when adding region-native outlets: native RSS WORKS for Al Jazeera (`aljazeera.com/xml/rss/all.xml`, 25) and SCMP (`scmp.com/rss/91/feed`, 50). Native RSS is DEAD (0 entries) for Kyiv Independent (`kyivindependent.com/feed`) and 38 North (`38north.org/feed/`) → both served via the `site:` proxy instead. **Low-frequency publishers need a wider window:** 38 North and BOJ statements only yielded entries with `when:7d` (not `when:2d`) — a daily-cadence window starves a weekly-cadence source. ISW/Yonhap/NHK use the proxy with `when:2d`. Re-verify with the snippet above if any break.
