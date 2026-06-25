# Direct-page temp sources — design

**Date:** 2026-06-25
**Status:** Approved (design)

## Problem

Temp sources (`sources.json` on the deploy volume) are all fetched through
`fetch_rss`. When a source's URL points at an HTML page rather than an RSS/Atom
feed, `feedparser` fails to parse it (`not well-formed (invalid token)`),
yields zero entries, logs a `No entries` warning, and the source silently
contributes nothing to the brief.

This surfaced with a hand-added entry pointing at a BCA Research dashboard
(`https://www.bcaresearch.com/dashboard/us-midterm-election-dashboard`). The
page is a direct, situational analyst source — not a feed. Verified live: the
page returns HTTP 200 and a substantive `<meta name="description">` block
(e.g. "Democrats will win both House and Senate…"), exactly the content the
brief wants. `fetch_web_source` already extracts this; the only gap is that
temp sources can't be routed to it.

A second, related case: the in-code `WEB_SOURCES` list holds one entry,
`BCA Research — Iran Conflict Daily Dashboard`. It is situational (tied to the
Strait of Hormuz crisis) and will stop being relevant once the situation
resolves, unlike the always-on feeds. Because it is baked into the image,
dropping it requires a redeploy. It belongs in temp sources so it can be
removed without a rebuild.

## Goals

1. Let a temp source be marked as a direct page, routed to `fetch_web_source`.
2. Add page sources via the `/addsource` wizard without the silent feed trap.
3. Migrate the Iran dashboard out of in-code `WEB_SOURCES` to a temp source.

## Non-goals

- Deeper page scraping than the existing `fetch_web_source` (meta description
  / first 800 chars). Out of scope; revisit only if quality is insufficient.
- Auto-detecting feed-vs-page by probing the URL. Rejected: a transient fetch
  failure could misclassify a real feed, and a silent feed→page fallback would
  mask dead feeds instead of surfacing them.
- Removing the `WEB_SOURCES` mechanism. Kept as the always-on page baseline.

## Design

### 1. Data model

Temp-source entries gain one optional field, `source_type`, one of:

- `"feed"` (default) — fetched via `fetch_rss` (current behaviour)
- `"page"` — fetched via `fetch_web_source`

`load_temp_sources()` (`brief.py:290`) reads `source_type`, normalizes any
missing/invalid value to `"feed"`, and includes it in the returned dict
alongside `name`/`url`/`category`/`kind`. Every existing entry keeps working
unchanged (absent field → `"feed"`).

Example page entry:

```json
{
  "name": "BCA — US Midterm Election Dashboard",
  "url": "https://www.bcaresearch.com/dashboard/us-midterm-election-dashboard",
  "category": "us",
  "kind": "regional",
  "source_type": "page"
}
```

### 2. Routing

In `mode_submit` (`brief.py:2090-2096`), split temp sources by `source_type`:

- `"feed"` temp sources → appended to `RSS_FEEDS`, fetched via `fetch_rss`
- `"page"` temp sources → appended to `WEB_SOURCES`, fetched via
  `fetch_web_source`

This mirrors the existing baked-baseline-vs-volume split, now for both feeds
and pages.

### 3. Wizard (`/addsource`)

In `_wizard_handle_text` (`brief.py:590`), the `url` step branches:

- **Bare domain** → Google News `site:` feed, `source_type="feed"`, proceed to
  confirm (unchanged — a domain cannot be a page).
- **Full URL** → new wizard step asking "Is this an RSS **feed** or a **page**
  to scrape?" via two inline buttons. The choice sets `source_type`, then
  proceeds to the confirm prompt.

The confirm prompt (`_wizard_confirm_prompt`, ~`brief.py:580`) shows the chosen
`source_type`. The stored entry (via `add_temp_source`) includes it.

This closes the silent trap: pasting a page URL now prompts for its type
instead of producing a broken feed.

### 4. Iran dashboard migration

- Remove the `BCA Research — Iran Conflict Daily Dashboard` entry from the
  in-code `WEB_SOURCES` list (`brief.py:248`), leaving `WEB_SOURCES = []` as the
  always-on page baseline (parallel to `RSS_FEEDS`).
- After deploy, re-add it on the host as a `"page"` temp source via the new
  `/addsource` flow, or hand-edit `sources.json`:

```json
{
  "name": "BCA Research — Iran Conflict Daily Dashboard",
  "url": "https://www.bcaresearch.com/collection/bcas-iran-conflict-daily-dashboard",
  "category": "iran",
  "kind": "regional",
  "source_type": "page"
}
```

### 5. Consistency fix (in scope)

`fetch_web_source` (`brief.py:1189`) currently renders
`### {name} ({category})` with no `[KIND]` tag, unlike `fetch_rss` which
renders `### {name} [{KIND}] ({category})`. Since page sources now carry `kind`
and the brief's forward-tilt weights by kind, add the `[{KIND}]` tag to the
page header so page sources receive the same tilt treatment as feeds. This is a
minor change to the brief's page-section output.

## Testing

- `load_temp_sources`: defaults `source_type` to `feed` when absent; accepts
  `page`; normalizes an invalid value to `feed`.
- Routing: a `page` temp source is fetched via `fetch_web_source`; a `feed`
  temp source via `fetch_rss`.
- Wizard: the full-URL path adds the feed-or-page step and stores the chosen
  type; the confirm prompt shows it.
- `fetch_web_source`: section header includes the `[KIND]` tag.

## Scope / deployment notes

- The host `sources.json` edit (re-adding the Iran dashboard) is a manual step
  on the deploy volume — live state, not committed to the repo.
- No new top-level module is added, so no Dockerfile COPY / workflow allowlist
  changes are required.
- Pre-push gate: `ruff check` + `ruff format --check` + `pytest` (stage all
  reformatted files).
