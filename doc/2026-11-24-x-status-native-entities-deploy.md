# Production rollout: X status native entities

Only the Shirobako `bot` service is in scope. Roll out signed `6bd1414ac02743868c6dd2e1c963337cd4096ab8`. Do not change settings, session, caches or other services. Do not send probe messages or start a second bot session.

## phase0 — Candidate and rollback baseline
- [x] Evidence: clean candidate, valid SSH signature, remote main at its parent, current service on `3589a2e`, no source `.py` override in Docker diff and sufficient build space. Live Compose service, image and build files checked. Preserve the old image before switching.

## phase1 — Build a candidate without interrupting the bot
- [x] Built `shirobakobot:rev-6bd1414ac027` from the signed commit archive; OCI revision matched and eight source hashes matched the checkout. Isolated image loaded Kurigram 2.2.26, cache version 14, and the SABR dependency. Recorded response replay produced six photo references, four native hashtag links and footer, and preserved literal `||`. Old container remained running.

## phase2 — Publish and switch only the target service
- [x] GitHub main fast-forwarded to `6bd1414`; GitHub verified the signature (`valid`). Deploy checkout fast-forwarded. Immediately before cutover, there were zero incomplete pipelines, new requests, or recently written download files. Retained `shirobakobot:before-x-status-3589a2e`; recreated only the `bot` service with the tested image. Container changed and reported running with the expected immutable image ID and revision.

## phase3 — Production verification and archive
- [x] Verified startup at 2026-11-24 12:44:04 UTC: readiness passed; actual bot process had an ESTABLISHED Telegram socket, no session/boot errors, zero restarts.
- [x] Live production `ParseService` and `ParsePipeline` fetched `https://x.com/toaru_project/status/2124820266370900014` and downloaded six original JPEGs, each 1920×1080. Intercepted at the publishing boundary: six native photo blocks, four hashtag links, native source footer, literal `||` and no leaked anchor markup survived the SDK TL round trip. No Telegram message was sent; client display was not tested.
- Record: `doc/2026-11-24-x-status-native-entities-deploy.md`, signed docs-only follow-up. The production image continues to point to the code revision above.
