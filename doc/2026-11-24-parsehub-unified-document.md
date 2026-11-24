# ParseHub: one structured document for all 24 platforms

**Goal:** Keep the unified `ParseHub.parse` API, and unify the rendering input as well. Platform adapters only know the site payload; share author/quote/media layout and serializers. Telegram rich blocks and ordinary message/caption entities remain different wire encodings of one document, not different platform pipelines.

**Baseline:** `2f6a237`; production `6bd1414` (X status only), live parsing/download after rollout; worktree clean. `parsehub platforms --json --compact` lists 24 IDs. Earlier plans `segments-per-platform.md`, `rich-segments-refactor.md` and `x-status-native-entities.md` describe previous attempts; don't re-enable their output-string parser. Do not deploy until separately authorized. Do not add origin requests or expose credentials.

## phase0 — inventory and acceptance baseline
- [x] `doc/format-adaptations.md` records all 24 CLI platform IDs, source shape, formatting output, and fixture locations; AST scan covers 75 distinct hit lines (not an exhaustive behavior proof). At HEAD `2f6a237`, lib 1290 passed / 1 skipped, bot 1022 passed / 14 failed, Ruff had 37 diagnostics. Baseline XML and diagnostics stored in scratch; no changes to production.

## phase1 — contract and source adapters, no switch yet
- [x] `types/document.py` provides the immutable shared document, runs, blocks and single source-DOM adapter. `ParseResult` and built-in result class constructors take `document`; public JSON unchanged, lib cache reconstructs it, persistent cache model accepts the same DTO. The former X `TextRun` is the exact same class. Test-first observed missing module; 45 related tests / 26 subtests pass including existing X, old entries, and explicit persistent JSON round trip; changed contract/fixture Ruff and diff checks pass.

## phase2 — platform production sources
- [x] All 24 registered adapters now produce `Document` from source DOM, source Markdown, upstream JSON/entities, or literal text, including X Article and Bilibili comment/opus. Public field compatibility preserved. Parent verified 68 source/contract tests and producer inventory; no new fetches. Existing fixture coverage does not include a captured X Article response (tests build its actual DraftJS schema). No Telegram/client claim at this phase.

## phase3 — one layout for all platform documents
- [x] Native renderers `plugins/parse/document_render.py` and `document_caption.py` now serve one Document contract at direct send, progress, inline preview/edit, guest, raw/zip/GIF, and persistent-cache replay. `#t` updates only the display document. Document cache v15 pairs actual rendered media leaves with stable source ref indices, preserving repeats and failed positions; compatible earlier entries retain their old readers and file IDs. Parent verified 116 targeted bot tests after a RED/GREEN duplicate-media sender regression, plus the saved X original response through real CLI and TL serialization (6 source photo references, 4 native links, no anchor leak).

## phase4 — clean up, verify and archive
- Independent review found an Article-linked `#t` regression (only the old string was translated). Add the X source roles to Article results and restore them from parsed-result cache so explicitly translatable quoted posts render in both cold and warm documents. Do not replace a full DraftJS article with its teaser translation. Re-run full tests and baseline lint after this correction.
- [x] Reviewed the new source and delivery boundary. Existing compatibility code is still reachable for pre-migration cache entries and public exports, so it was retained without using it for fresh platform bodies. Fixed and independently re-tested the Article quote-translation issue. Full library: 1,358 passed, 1 skipped, 128 subtests passed. Full bot: 1,067 passed; the same 14 baseline failures by test ID and failure reason. Ruff has no added diagnostics; Pylint errors-only, syntax and diff checks pass.
- [x] Ran `parsehub parse` with original X, Facebook, Bangumi and linux.do responses replayed at the network boundary, then serialized with Kurigram. Facebook, Bangumi and linux.do cold and cache readback payloads were byte-identical with legacy renderers disabled in the probe. X kept all 6 media references, 4 explicit hashtag links and the literal punctuation. This did not send a Telegram message or verify client display.
- Archive in `doc/2026-11-24-parsehub-unified-document.md` and include in the English SSH-signed code commit. No push or deployment authorized for this change.
