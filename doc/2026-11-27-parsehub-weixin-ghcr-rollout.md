# Weixin article rendering: GHCR rollout

## phase0 — Baseline

The deployed image was pinned to the previous verified GHCR index. The Weixin fix was the only code change to roll out.

## phase1 — Verify source

Reapplied the signed change onto the GHCR-enabled main branch as `57b1b30cbd7f5ac8813f51d6a6abe83c973125bf`. Patch content remained identical, and 40 targeted parser, cache, rendering and image-publishing tests passed.

## phase2 — Verify image

[Docker Image CI run 45930918963](https://github.com/BraveSail/shirobako/actions/runs/45930918963) verified AMD64 and ARM64 and published `ghcr.io/bravesail/shirobako@sha256:190ca9a9167d8cdfaf3db18850e6366507561080dc9f1725de8f0d4e5b861c2a`. GitHub verified the source commit's signature. The digest matched the immutable commit tag; the ARM64 image's five changed runtime source files matched the commit.

## phase3 — Activate

Only the bot service was replaced, after its activity check. The running image matched the published digest; the bot reached readiness with its own established Telegram connection. The previous pinned image was retained for rollback.

## phase4 — Verify production path

The installed image parsed the affected Weixin article, downloaded and decoded 33 images (10 JPEG, 14 PNG, 9 GIF), and produced 33 ordered native Telegram photo blocks and four chapter headings. Cold and reconstructed warm TL messages matched. A separate check confirmed the same container stayed healthy with zero restarts. The probe did not send a Telegram message; client display was not part of this check.

## phase5 — Archive

This record accompanies the tested revision; the documentation commit does not change the deployed image.
