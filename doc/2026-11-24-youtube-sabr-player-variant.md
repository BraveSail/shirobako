# Resolve SABR with the selected YouTube player script

## Phase 0 — Reproduce

The same watch response contained a SABR URL with an unresolved `n` and selected `b485e332/player_es6.vflset/zh_CN/base.js`. Reconstructing `player_ias.vflset/en_US/base.js` from that ID loaded different code. With the same `n` input, EJS 0.8.0 failed on the reconstructed IAS variant (`addOnDisposeCallback is not a function`) and succeeded on the selected ES6 variant. The previous code forwarded the unresolved URL to the CDN, which returned HTTP 403. See also [yt-dlp #18123](https://github.com/yt-dlp/yt-dlp/issues/18123).

## Phase 1 — Fix the stream preparation

- Preserve the complete player script URL and cache source by URL rather than by ID. Read only trusted YouTube player script paths; do not follow redirects when loading an executable script.
- Prepare SABR streams from either watch or player responses. Keep metadata available when `n` cannot be resolved, but reject the download before spawning SABR. Do not fall back to direct streams for a failed SABR download.
- Offline coverage: correct variant, separate cache entries with reuse, response fallback, ignored commented scripts, and blocking an unresolved download without blocking metadata.

## Phase 2 — Verify

`parsehub d` reproduced the 403 on the old version. On the candidate, the same link reached the existing SABR tool exactly once with no direct streams available. FFprobe measured a 1920×1080 AV1 video and AAC audio, a 45.093152-second MP4, 5,180,203 bytes. Both streams decoded to EOF without errors.

Library: 1,251 passed, 1 skipped, 128 subtests passed. SABR retry tests: 5 passed. Syntax, Ruff F, and Pylint errors-only checks passed.
