# Linux.do context-floor photo placement

## phase0 — Reproduce

The recorded response for `https://linux.do/t/topic/3248768/2` contains one image in the first floor, before its paragraph; the second floor contains only text. Its topic tag is `纯水`. The CLI and the document both recognized the original image. The native inline edit put it outside the first-floor quote, after the second-floor text. The renderer looked only in a separate reply bucket even though Linux.do stores its indexed context assets in the main media list.

## phase1 — Capture the regression

Intercept the native `EditInlineBotMessage` request and round-trip its actual TL object. Verify the context photo inside its quote, retained topic tag and footer, and consistent cache rendering. Both a text-only target and a target with its own picture under the same CDN URL failed before the fix.

## phase2 — Resolve the indexed image in its card

Enable the renderer's existing reply-to-main media lookup for Linux.do context floors. The card consumes only the referenced asset, so it is not appended to the trailing gallery. Tag placement, provider output and cache schema remain unchanged. Hiding the quote does not leave its photo behind in the gallery.

## phase3 — Verify and archive

Replay the same response through the CLI and native inline edit. The single photo is back inside the first-floor quote before its paragraph; second-floor text, the original linked tag, and the footer follow. The CLI result is unchanged, and the cold/warm message bytes match. No Telegram message was sent. The targeted suite passed 138 tests. The full bot suite passed 1,068 tests with the same 14 failure signatures as the baseline. Scoped Ruff, Pylint, syntax and diff checks passed; full-repository Ruff diagnostics were unchanged. Keep this record in the signed fix commit.
