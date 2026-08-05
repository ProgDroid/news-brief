---
name: http-error-body-is-the-diagnosis
description: "A 4xx/5xx is unactionable unless you log resp.text — requests' HTTPError stringifies to only status + URL, and APIs put the offending field name in the body"
metadata: 
  node_type: memory
  type: project
  originSessionId: 0020b8b4-ded9-4def-919b-be8744df2379
  modified: 2026-08-05T22:29:23.045Z
---

When a `requests` call fails, `except Exception as e: log.warning(f"... failed: {e}")` throws away the diagnosis. `HTTPError` stringifies to **status code + URL only** (`400 Client Error: Bad Request for url: …`). The API's explanation — which normally names the offending field — lives in `resp.text`, which the exception never carries.

**Why it matters here:** this cost days on the PolyGram live sleeve. `POST /trade/place` had been 400ing on every order since go-live, and the two log lines available (`400 Client Error` + `place not filled: None`) could not distinguish a wrong field name, a wrong id format, a wrong outcome label, or wrong units — while polygram.ink documents its errors as `{error, message}`, i.e. it was telling us the answer all along. See [[polygram-live-trading-spec]].

**How to apply:** in any `except` around `raise_for_status()`, log `resp.status_code` and `resp.text[:300-400]` (truncate — an HTML error page will otherwise flood the log), and for POST/PUT also log the **request body you sent**, so the venue's complaint can be compared against it side by side. Only safe when no credentials pass through that helper: check the login/auth call isn't routed through the same function first.

The pattern already exists in this repo — copy `common.py`'s Telegram helpers (`log.error(f"Telegram {resp.status_code}: {resp.text[:300]}")`) and `brief.py`'s getUpdates. As of 2026-08-05 only `polygram_live._pg_request` (fixed, 8459093) and those Telegram sites do this; roughly 25 other `raise_for_status()` calls in `trading.py`, `brief.py`, `claim_verify.py`, `brief_memory.py` and `enrichment/providers_bigdata.py` still discard the body — worth fixing at the next failure rather than pre-emptively.

Corollary for WRITE paths specifically: also distinguish "the request was refused" (payload bug) from "the request succeeded but the response didn't parse" (the side effect may have happened anyway — for an order, real money moved and the row is about to be dropped). Those need different log levels and different reactions. Relates to [[fail-closed-needs-status-not-count]] and the global API-response-validation rule (treat unexpected results as suspicious, don't guess params).
