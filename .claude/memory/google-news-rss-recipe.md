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

## 2026-09-08 — THE VERIFICATION SNIPPET ABOVE IS BROKEN, and it fails silently

`curl -s <url> | grep -oE '<item>' | wc -l` returns **0** for every Google News feed on this
machine. Not because the feed is empty — HTTP 200, 17,698 bytes, valid RSS with the right query
in the channel title. **grep decides the response is BINARY and prints nothing**, and `-o` then
yields no lines. `--compressed` does not help; the body is already decoded.

**Use `grep -a`.** Same command, correct answer: 100.

This is the worst shape of negative result, because a broken probe and a dead feed are
byte-identical: both print `0`. The control that caught it was a feed KNOWN to work (Al Jazeera
native, 25 items) — it returned 25, which proved the pipeline was fine and the Google-specific
result was a lie. Always run the known-good feed alongside.

## 2026-09-08 — the 100 cap is a CAPTURE problem, not a brief problem, and it is about RANKING

Four proxies returned exactly 100 entries on every poll for 4.3 days: Reuters Markets, Reuters
World, Kyiv Independent, Yonhap. Measured windows (twice, stable):

```
          6h     12h    1d    2d   no-when
markets  17-24  75-80  100   100   100
world      40     89   100   100   100
kyiv       61   94-97  100   100   100
yonhap      4     26   100   100   100
```

**What the cap does is narrower than "we lose the tail".** At 30-minute polling a new item enters
near the top and volume alone will not push it past rank 100. The damage is that `when:2d` offers
Google ~370 candidates for 100 **relevance**-ranked slots, and that ranking is not chronological —
items move in and out of view (measured flicker 1.51 on markets). An item never in the top 100 at
any poll instant is lost, **unobservably**. Narrowing the window removes the MECHANISM; it does
not repair a measured loss. Keep that distinction when quoting this.

`no-when` equalled `when:2d` on all four, so the "if no-when ≫ when:2d, go native" diagnostic
above did **not** fire — these paths are rich in fresh items and narrowing is the right lever.

**One URL cannot serve both readers, which is why `capture_url` now exists** (`brief.py`, four
feeds; `capture.capture_sources()` substitutes it into a COPY). The brief fetches at brief time
and takes the newest 25, so a 6h window at 06:00 would hand it the overnight hours and nothing
else. Capture needs it narrow. Windows chosen as the widest measurement leaving 30% headroom:
6h for markets/world/kyiv, 12h for yonhap. **The narrow window REQUIRES frequent polling** — those
8 proxies stay at 30 minutes whatever a roll-off measurement says, which is why the per-feed
interval work treats them as structural rather than measured.
