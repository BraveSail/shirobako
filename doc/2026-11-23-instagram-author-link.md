# Preserve Instagram author links

## Phase 0 — Trace the missing link

The public post response had both the author's display name and Instagram username. The parser passed only the name to all three result types, so the shared author renderer had no profile URL and displayed plain text.

## Phase 1 — Reproduce

Add coverage for the public single-image fetch, all three result types, identity fallback, both cache readers, and both inline rich-message builders. The regression tests reproduced missing handle/URL fields, old cache hits, and the missing native author-link entity before the change.

## Phase 2 — Carry the author identity through

Read the display name and username from the same owner record, and build the profile URL with the shared helper. A missing or invalid username remains unlinked. This adds no requests and makes no renderer changes. Both caches write version 13 and consult a shared per-platform minimum: Instagram requires 13, and all other platform minima are unchanged.

## Phase 3 — Verify the payload

Replayed the reported post from the saved public response using a network-disabled copy of the production image. The parser consumed one page response and no GraphQL response. Both fresh and cache-hit inline payloads serialized the unchanged display name as a native link to the actual profile, with the account handle separate. The library suite had 1248 passes and 1 skip; the bot suite had 921 passes and the same 14 failures as the previous baseline. No production state or Telegram messages were changed during verification.

## Phase 4 — Build and verify the release

Built the signed `65bdf80` revision using only tracked files. The four changed runtime files matched Git, the Bun/SABR imports succeeded, and the candidate passed the saved-post replay for both inline paths. Both cache readers retained the existing version requirements for other platforms.

## Phase 5 — Deploy and verify production

Recreated the existing bot service after checking that there were no active media jobs. Kept the previous image available for rollback. The running bot reported revision `65bdf80` and owned an established Telegram connection. A live parse fetched the reported post with one HTTP 200 request. Fresh and cached inline payloads both serialized the original display name as a link to the correct profile and the username separately. Both cache readers rejected old Instagram entries and accepted version 13. No production cache entries were written and no Telegram test message was sent.
