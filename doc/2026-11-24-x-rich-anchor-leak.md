# Keep decorative characters out of native X links

## Phase 0 — Reproduce

An X post used `||◤ #08「作戦」◢||` as decoration and did not list `08` as a hashtag. Fallback detection swallowed the punctuation and closing pipes into an anchor. The native rich-text renderer then unescaped the opening pipes before recognizing markup, allowing a false spoiler to split the link. Serializing the cached inline result to Telegram TL reproduced the literal opening and closing tags.

## Phase 1 — Fix the boundaries

- Infer hashtags only from Unicode word characters, combining marks, and internal hyphens. Preserve exact platform-provided tags and existing links.
- Recognize native inline elements before unescaping literal text. Extract captures from the original source so no masking characters reach the output. Decode HTML entities once; do not re-interpret them as markup. Explicit unescaped spoiler spans still work.
- Test direct rendering, fresh inline, and cached inline against the serialized Telegram blocks.

## Phase 2 — Verify

Replayed the original cached post through the final renderer: no literal anchors or unintended spoiler spans, original punctuation unchanged, four native hashtag URLs, six photo blocks, and intact author and footer source links. No cache invalidation or network fetch was needed. The focused suite passed 135 tests. The full bot suite passed 949 tests and matched the same 14 pre-existing failures by test ID and message. Syntax, Ruff F, and Pylint errors-only checks passed. This verifies the serialized output, not delivery to a Telegram client.
