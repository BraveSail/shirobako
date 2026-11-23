# Read complete public Instagram images from the post page

## Phase 0 — Locate the failure

The production parser correctly normalized the Instagram share URL, but the anonymous GraphQL request returned HTTP 401 with `require_login: true`. That response did not establish whether the post was private or missing. A bounded check using the configured session redirected to the Instagram homepage with no data; that did not establish an expired session either. Leave production credentials unchanged.

## Phase 1 — Verify the source

A normal anonymous GET of the public post returned HTTP 200 with the matching image record under `ScheduledServerJS.handle → RelayPrefetchedStreamCache.next → result.data.xig_polaris_media.if_not_gated_logged_out`. This node contained the caption, author, and full-size image candidate. Carousel metadata elsewhere in the page belonged to other posts. Reusing the GraphQL headers and placeholder cookies for the permalink navigation produced HTTP 200 without this record, so those settings must not be applied before the page GET.

## Phase 2 — Implement and test

For `/p/` links, use the anonymous bootstrap GET on the permalink instead of the homepage. Accept only a matching complete single-image record; never return a video cover or carousel preview as a complete result. If no usable image is present, use the existing GraphQL flow and reuse the CSRF cookie from the page when available. If no CSRF cookie was issued, keep the previous homepage bootstrap. The Reel and authenticated GraphQL request paths remain in place. The cache format is unchanged.

`lib/test/test_instagram_post_page.py` uses synthetic HTML and tests the real parser with an offline HTTP transport. It covers request counts, target identity, missing data, full-size candidate selection, fallback behavior, and authenticated error redaction.

## Phase 3 — Verify and publish

The project CLI parsed the original link in an isolated container based on the production image using one post-page GET and no GraphQL request. The download decoded as a single 1440 × 1920 WebP (91,524 bytes). All 20 new tests pass. Library: 1,236 passed, one skipped. Bot: 911 passed with the same 14 failures as the unchanged baseline. Syntax, Ruff, and pylint error checks passed. Deployment is separate from publication.

## Phase 4 — Deploy

On request, built and deployed the signed revision `bb204fc` from tracked files. Verified the changed source hashes and the SABR imports before switching. The previous image remains available for rollback. The bot started and its process established a Telegram connection.

## Phase 5 — Verify production

The production `ParseService` parsed the reported post, and the download decoded as one 1440 × 1920 WebP (91,524 bytes). The same image also passed the saved Linux.do list replay through both fresh and cache-hit inline paths. No test Telegram message was sent.
