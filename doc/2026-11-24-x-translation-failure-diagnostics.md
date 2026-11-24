# Diagnose X translation failures without logging response contents

## Phase 0 — Identify the missing signal

An HTTP 200 response reached `Response.json()` and failed with `Extra data` on line 2. The bot sent the original post as intended, but the error did not identify the response's declared media type or trailing-data shape. A decode error alone cannot tell whether the extra bytes are JSON, SSE, HTML, or something else.

## Phase 1 — Classify at the failing boundary

Log a warning with `tweet_id`, `lang`, `stage`, `reason`, and `fallback=original`. For HTTP and payload failures add `http_status`, an allowlisted `mime` category and `body_bytes`; JSON failures also include line, column, offset, and the observed prefix class of the body or extra data. A suffix like `tail=json_like` describes the first characters, not a confirmed protocol. Only recognized upstream codes may appear in `x_code`. Transport failures retain their native exception type for library callers. The bot logs the safe type and does not render exception chains, response bodies, or request credentials. Outcome logs use the post ID instead of the original URL.

The request, decoding contract, caching, media handling, and original-text fallback are unchanged. This is not a new NDJSON or SSE translation decoder.

## Phase 2 — Verify

Offline fixtures exercised the real HTTP response decoder, including HTTP 200 with a JSON-like or SSE-like trailer, unexpected media types, timeouts, HTTP errors and missing/empty translation. With Loguru diagnostic output enabled, emitted warnings contained the intended fields and no fixture secret markers. All 82 translation tests passed. Library: 1,251 passed, 1 skipped, 128 subtests passed. Bot: 940 passed, and the same 14 existing failures as the baseline (matched by test ID and failure message). Syntax, Ruff F, and Pylint errors-only checks passed.
