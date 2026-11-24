"""User annotations: Telegram input spans are converted directly to rich text."""
from __future__ import annotations

import re
from collections.abc import Sequence
from copy import copy
from dataclasses import dataclass
from itertools import pairwise

from pyrogram.enums import MessageEntityType as EntityType
from pyrogram.types import (
    InputRichBlock,
    InputRichBlockDetails,
    InputRichBlockDivider,
    InputRichBlockFooter,
    InputRichBlockParagraph,
    MessageEntity,
    RichText,
    RichTextBold,
    RichTextCode,
    RichTextCustomEmoji,
    RichTextItalic,
    RichTextMention,
    RichTextSpoiler,
    RichTextStrikethrough,
    RichTextTextMention,
    RichTextUnderline,
    RichTextUrl,
)

_NOTE_PATTERN = re.compile(r"^==(.*?)==", re.DOTALL | re.MULTILINE)
_STYLES = {
    EntityType.BOLD: RichTextBold,
    EntityType.ITALIC: RichTextItalic,
    EntityType.UNDERLINE: RichTextUnderline,
    EntityType.STRIKETHROUGH: RichTextStrikethrough,
    EntityType.SPOILER: RichTextSpoiler,
    EntityType.CODE: RichTextCode,
    EntityType.PRE: RichTextCode,
}
_CLIPPABLE = {*_STYLES, EntityType.TEXT_LINK}


@dataclass(frozen=True, slots=True)
class ParseNote:
    text: str
    entities: tuple[MessageEntity, ...]

    def __bool__(self) -> bool:
        return bool(self.text)


def extract_note(text: str, entities: Sequence[MessageEntity] = ()) -> ParseNote:
    """Select the first ==…== note before formatting, keeping relative UTF-16 spans."""
    if (match := _NOTE_PATTERN.search(text)) is None:
        return ParseNote("", ())
    captured = match[1]
    note = captured.strip()
    if not note or not entities:
        return ParseNote(note, ())
    first = match.start(1) + len(captured) - len(captured.lstrip())
    start = len(text[:first].encode("utf-16-le")) // 2
    end = start + len(note.encode("utf-16-le")) // 2
    spans = []
    for entity in entities:
        entity_end = entity.offset + entity.length
        lo, hi = max(start, entity.offset), min(end, entity_end)
        if lo >= hi or (entity.type not in _CLIPPABLE and (lo != entity.offset or hi != entity_end)):
            continue
        clipped = copy(entity)
        clipped.offset, clipped.length = lo - start, hi - lo
        spans.append(clipped)
    return ParseNote(note, tuple(spans))


def note_block(note: ParseNote) -> InputRichBlockParagraph:
    offsets = {0: 0}
    offset = 0
    for index, char in enumerate(note.text, start=1):
        offset += 2 if ord(char) > 0xFFFF else 1
        offsets[offset] = index
    spans = sorted(
        (
            (offsets[e.offset], offsets[e.offset + e.length], e) for e in note.entities
            if e.length > 0 and e.offset in offsets and e.offset + e.length in offsets
        ),
        key=lambda span: (span[0], -span[1]),
    )
    boundaries = sorted({0, len(note.text), *(edge for lo, hi, _ in spans for edge in (lo, hi))})
    fragments: list[str | RichText] = []
    for start, end in pairwise(boundaries):
        text = note.text[start:end]
        fragment: str | RichText = text
        for lo, hi, entity in reversed(spans):
            if not lo <= start < end <= hi:
                continue
            if entity.type is EntityType.TEXT_LINK and entity.url:
                fragment = RichTextUrl(fragment, entity.url)
            elif entity.type is EntityType.URL:
                fragment = RichTextUrl(fragment, note.text[lo:hi])
            elif entity.type is EntityType.TEXT_MENTION and entity.user is not None:
                fragment = RichTextTextMention(fragment, entity.user)
            elif entity.type is EntityType.MENTION:
                fragment = RichTextMention(fragment, note.text[lo:hi].lstrip("@"))
            elif entity.type is EntityType.CUSTOM_EMOJI and entity.custom_emoji_id:
                fragment = RichTextCustomEmoji(str(entity.custom_emoji_id), text)
            elif styles := _STYLES.get(entity.type):
                fragment = styles(fragment)
        fragments.append(fragment)
    return InputRichBlockParagraph(fragments)


def append_content_blocks(
    blocks: list[InputRichBlock], extra: list[InputRichBlock], spoiler_tag: str,
) -> None:
    """Add material before the footer and inside any manual spoiler fold."""
    if not extra:
        return
    footer = len(blocks)
    if blocks and isinstance(blocks[-1], InputRichBlockFooter):
        footer -= 1
        if footer and isinstance(blocks[footer - 1], InputRichBlockDivider):
            footer -= 1
    if spoiler_tag:
        summary = f"⚠️ {spoiler_tag}"
        for block in reversed(blocks[:footer]):
            if isinstance(block, InputRichBlockDetails) and block.summary in (summary, [summary]):
                block.blocks.extend(extra)
                return
        extra = [InputRichBlockDetails(summary, extra)]
    if footer == 0 and blocks and isinstance(blocks[-1], InputRichBlockFooter):
        extra = [*extra, InputRichBlockDivider()]
    blocks[footer:footer] = extra
