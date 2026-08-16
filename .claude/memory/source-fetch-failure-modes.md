---
name: source-fetch-failure-modes
description: "RSS source failures triaged by KIND — bot-UA 403s, revoked TLS, and Nitter 429s each need a different fix; don't retry them uniformly"
metadata: 
  node_type: memory
  type: project
  originSessionId: 14a602eb-8f61-49a4-8556-33d4bfaf804e
  modified: 2026-08-09T14:44:15.068Z
---

An `RSS failed <name>: …` warning is three unrelated problems wearing one message.
Triage by kind before doing anything — 2026-08-09 had one of each in a single run,
and only one was retry-shaped. All three fixed in dfff949 (pushed).

**403 = the User-Agent, usually.** `mining.com/feed` returned **403 to
`Mozilla/5.0 (compatible; newsbrief/1.0)` and 200 with 36 items to a browser UA**,
same URL, same second. A self-identifying bot string is increasingly refused outright.
There is now ONE `brief.SOURCE_USER_AGENT` constant (a Chrome string) shared by
`fetch_rss`, `fetch_web_source` and the Mauldin scrape — it used to be duplicated in
three places. **Do not retry a 403**; it is a policy refusal and retrying just
lengthens the submit run.

**429 = Nitter rate-limiting adjacent requests.** The two X feeds sit next to each
other in `RSS_FEEDS`, so one 429'd on most runs and silently never reached the brief —
and a dropped feed is indistinguishable from a quiet one downstream. `fetch_rss` now
retries 429/5xx up to `RSS_MAX_ATTEMPTS` (3), honouring `Retry-After` (capped at 20s)
and **logging the header value**, because Nitter's real window is unknown and the
header is the only place it is stated. Read those log lines before tuning the backoff.

**SSL = check whether the host deserves trust before working around it.**
`presstv.ir` fails TWO independent ways: Windows' cert store reports it **REVOKED**
(`CRYPT_E_REVOKED`) and OpenSSL/certifi — the stack the container actually uses —
fails with `unable to get local issuer certificate`. **Never disable verification for
this**; a revoked cert is a reason to distrust the host. Press TV was REPLACED by
**IRNA** (`https://en.irna.ir/rss`, 30 entries verified), which is also the official
state agency so the `state_funded: True` IRANIAN slot is preserved rather than quietly
downgraded to the independent IranWire feed beside it. **Do not re-add presstv.ir.**
Google News proxying was measured and rejected: `site:presstv.ir` yields **1 item even
with no freshness filter**, far under this project's 3-entry bar — see
[[google-news-rss-recipe]].

**How to apply:** reproduce the failure with `curl` (and with `requests`, which is what
the container uses — the two stacks disagree on TLS) before choosing a fix. Every one
of these three was diagnosed in a single probe each; guessing would have produced a
blanket retry that fixed exactly one of them. Tehran Times and Mehr also tested clean
(30 entries each) if the Iranian slot ever needs a broadcaster's framing rather than a
wire's. Related: [[direct-page-temp-sources]], [[mauldin-twie-wix-warmup-scrape]].
