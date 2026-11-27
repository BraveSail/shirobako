# Linux.do context image: GHCR rollout

## phase0 — Baseline

Verified signed `3d2989ea78debb1e07b6349b31d080385b36880c` and the previous pinned image. The only runtime change was the Linux.do renderer fix.

## phase1 — Publish

Fast-forwarded main and verified both outgoing SSH signatures through GitHub. [Docker Image CI run 45937448260](https://github.com/BraveSail/shirobako/actions/runs/45937448260) passed on amd64 and arm64 and published the verified index.

## phase2 — Verify the artifact

The published index matched the commit-specific GHCR tag: `ghcr.io/bravesail/shirobako@sha256:55e1ee163fabb7ddf11ed2748832e90ea28497d7e1fdb721b81e165f8912a541`. The ARM64 image contained the exact renderer bytes and passed a network-isolated replay of the recorded Discourse response.

## phase3 — Activate

Replaced only the bot service after its activity check. The running container matched the verified image and reached readiness with its own Telegram connection. The previous image and pin were retained for rollback.

## phase4 — Verify live artifact and existing cache

The running bot image reproduced the corrected native inline layout. A read-only check of the already cached result reused its existing file ID: the photo is in the first-floor quote, then the second-floor text, linked `#纯水`, and the footer. No cache entry was cleared or changed. The container remained healthy with zero restarts during the check. Telegram delivery and client display were not exercised.

## phase5 — Archive

This documentation does not change the deployed image.
