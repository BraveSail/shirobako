# Preserve lists in rich messages

## Phase 0 — Reproduce

Replay a saved Discourse response containing a five-item `<ul>` through the bot's fresh and cached inline rendering paths. The response and provider Markdown retained all five items. The Telegram payload contained plain paragraphs with literal asterisks: text escaping ran before list construction, and the automatic preview boundary crossed the list.

## Phase 1 — Add regression tests

`test/test_rich_markdown_lists.py` exercises anonymized HTML through the real renderers and round-trips the serialized Telegram payload. It checks unordered lists, numbering, nesting, continuation paragraphs, checkboxes, fold boundaries, and literal markers in prose or code. The original code reproduced the missing list in both inline paths.

## Phase 2 — Preserve structure

`plugins/helpers.py` preserves list prefixes in structured Markdown bodies and uses the same item boundaries as `plugins/parse/rich_blocks.py` when cutting the preview. The block builder emits native Telegram lists rather than replacing asterisks with bullet glyphs. A list that crosses the preview boundary moves into the folded body intact. Provider output and cache schema are unchanged.

## Phase 3 — Verify

The original response was replayed in a network-disabled copy of the production image. Fresh and cache-hit payloads each contain one native list with the five items in their original order and no literal bullet markers. This checks serialization, not desktop or mobile display. The 43 targeted tests pass. The full bot suite has 911 passing tests and the same 14 failures as the pre-change baseline (897 passing). The library suite has 1216 passing tests and one skip.
