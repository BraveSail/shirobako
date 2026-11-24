# Rollout: structured ParseHub documents and rich-only Telegram delivery

Runtime revision: `aed57b50f805fc44f25c699e008afa782f6d6657`, including the platform document migration in `b662255`. Scope: the parsing bot only. No production configuration, cache contents, or session files were manually changed.

## phase0 — Release baseline

- [x] Verified the two SSH signatures, clean source tree, expected main branch ancestry, and the running `6bd1414` rollback baseline without source overrides.

## phase1 — Candidate image

- [x] Built from the signed Git archive. Verified its OCI revision, 57 changed runtime file hashes, both removed modules, Kurigram 2.2.26, the workspace import, cache version 15, and the SABR dependency.
- [x] Ran the actual CLI and SDK against saved public X, Facebook, Bangumi, and linux.do responses with networking disabled. The three non-X samples produced byte-identical TL across the fresh and restored cache results. The X replay preserved media references, hashtag links, literal punctuation, and the native footer.

## phase2 — Publish and switch

- [x] Fast-forwarded main; GitHub reported both runtime commits as verified. Preserved the previous image, checked for active downloads and recent requests, and recreated only the bot service. The running container reports the tested revision.

## phase3 — Production checks

- [x] The project's readiness check passed, the bot process owned an established Telegram connection, and the container reported zero restarts and no startup traceback or session error.
- [x] The production parser and download pipeline fetched six original 1920×1080 JPEGs for a public X post. The intercepted SDK payload round-tripped with six native photo blocks, four hashtag links, a source footer, and literal `||` intact.
- [x] No inbound parse request was observed during the post-cutover observation. The probe stopped at SDK serialization: it sent no Telegram message and did not verify client display.
