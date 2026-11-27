# Restore the full Shirobako functional gate

## phase0 — Reproduce on main

At `3d2989ea78debb1e07b6349b31d080385b36880c`, an isolated Bot run reproduced **1,068 passed and 14 failed**. The same 14 cases were already red at the structured-document migration baseline. The production workflow ran the image tag-promotion unit tests and two architecture-specific smoke checks, but neither full pytest suite. Existing `v1` image indexes carried startup evidence only.

| Historical failure(s) | Classification and evidence | Fix |
| --- | --- | --- |
| `test_golden_parity`: `bgm_index`, `bilibili_opus`, `linuxdo`, `ptt`, `threads`, `tieba`, `x_article`, `x_status` (8) | Obsolete test entry point: `md_segments` and the segment renderer were removed. Import failed before any rendering, even in the correct environment. | Keep the historical JSON, replay native blocks through the actual SDK codec and assert selected historical semantics, ordering, entities, nesting and documented intentional differences. No blanket snapshot regeneration. |
| `test_golden_parity[user_urls]` (1) | Test discovery error: selected a separate URL corpus as a render snapshot; it does not have a snapshot `url` field. | Assert the manifest/fixture boundary and exercise the corpus as URL routing input. |
| `test_media_sits_above_the_quote_card` (1) | Stale fixture: no `quoted` role, so the input described a plain quotation rather than a trailing card. | Declare the quote role and verify body → main media → quote → tags. Native block order is also covered by the golden and native renderer tests. |
| `test_successful_cache_send_reports_sent`, `test_try_send_cached_passes_the_spoiler_flag` (2) | Stale request double lacked `translate`, causing an exception before the sender. The real path additionally evicted a valid shared entry on unrelated failures. | Use `ParseRequest`, the real sender and SDK, and intercept Telegram at its boundary. Only explicitly stale file IDs/references invalidate the entry. The manual fold and platform-sensitive media blur retain distinct semantics. |
| `test_quote_block_placeholder_survives_build_rich_markdown`, `test_cached_render_roundtrip_from_cache_entry` (2) | The literal Markdown `>` assertions are obsolete; the native wire replay also exposed a real defect in supported older cached entries: escaped pipes became paragraphs instead of a poll table. | Preserve poll slots through the legacy renderer and generate the same localized native table in fresh and cached paths. Restore Linux.do poll title/voters and keep quote polls when guest falls back after media rejection. |

The 14 are not missing packages or a live-site outage. CI environment issues are separate: an unlocked workspace can import an incompatible Pyrogram instead of Kurigram; Bot collection needs synthetic settings, tool-dependent tests need FFmpeg/ffprobe, and the old Snapchat live test was skipped without a flag. The gate now runs the Snapchat fetch/parser against its checked-in captured response without contacting the site.

## phase1 — Prove the regressions

Native TL tests fail when the quote loses its table, when locale or literal option text is lost, when cached send evicts on an unrelated error, or when an old startup-only index is allowed to reach tag publication. The functional runner tests also exercise actual passing/failing pytest subtests, xfail/XPASS and empty/skipped results. No skip is added to silence a failure.

## phase2 — Connect the gate to publication

`Dockerfile.deploy` has a separate Python 3.12 functional target with locked workspace dependencies and media tools. `scripts/ci/functional_tests.py` runs Bot and parser pytest as separate processes in fresh state; CI uses `--network none` while retaining the loopback needed for downloader tests. Failure, skip, xfail, XPASS, empty or missing reports fail the gate. The production Docker target remains the default.

Pull requests verify only. Pushes to main and explicit main publishes require success of both full suites and both original native architecture smoke checks. The writer retains archive config digest, architecture and source revision checks, validates the registry index hash, and records `v2` verification. Release events may only promote an already published exact `v2` digest. Previously shipped `v1` SHA tags remain unchanged.

## phase3 — Local verification and signed patch

The ARM64 functional target built from the candidate tree and ran with `--network none`: **1,115 Bot tests and 1,366 parser tests passed**, with zero skipped or expected failures. Scoped Ruff, Pylint errors-only, bytecode compilation and diff checks passed. The tests do not log into Telegram, read production authentication, or change a shared cache. Deployment and GitHub evidence are recorded separately.
