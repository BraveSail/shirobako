"""Render ordinary X statuses as literal captions and native Telegram entities."""

from __future__ import annotations

import re
from collections.abc import Sequence
from copy import copy
from dataclasses import dataclass
from itertools import groupby
from operator import attrgetter
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from parsehub import Platform
from parsehub.types import AnyParseResult, Poll
from parsehub.types.x_status import TextRun, XPost, XTrending
from parsehub.utils.helpers import format_author_label
from pyrogram.enums import MessageEntityType as EntityType
from pyrogram.types import MessageEntity

from i18n import t_
from plugins.helpers import _METADATA_TIMEZONE, build_metadata_line

if TYPE_CHECKING:
    from repo.settings import SettingsConfig

_INLINE_STYLES = {
    "bold": EntityType.BOLD,
    "italic": EntityType.ITALIC,
    "strike": EntityType.STRIKETHROUGH,
}
_CLIPPABLE = {
    EntityType.BOLD,
    EntityType.ITALIC,
    EntityType.UNDERLINE,
    EntityType.STRIKETHROUGH,
    EntityType.SPOILER,
    EntityType.CODE,
    EntityType.PRE,
    EntityType.BLOCKQUOTE,
}
_SEPARATOR = "\n\n"
NOTE_PATTERN = re.compile(r"^==(.*?)==", re.DOTALL | re.MULTILINE)


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


@dataclass(frozen=True, slots=True)
class CaptionNote:
    """Literal user text and Telegram spans relative to its first UTF-16 unit."""

    text: str
    entities: tuple[MessageEntity, ...]


def extract_caption_note(text: str, entities: Sequence[MessageEntity] = ()) -> CaptionNote:
    """Select the first ==…== note from the original message, before any formatting."""
    if (match := NOTE_PATTERN.search(text)) is None:
        return CaptionNote("", ())
    raw_note = match[1]
    note = raw_note.strip()
    if not note or not entities:
        return CaptionNote(note, ())
    left_trim = len(raw_note) - len(raw_note.lstrip())
    start = _utf16_length(text[: match.start(1) + left_trim])
    end = start + _utf16_length(note)
    spans = []
    for source in entities:
        source_end = source.offset + source.length
        lo, hi = max(start, source.offset), min(end, source_end)
        if lo >= hi:
            continue
        # Styles and explicit links can annotate a smaller label; inferred entities
        # like URLs and mentions are tied to their original spelling.
        if source.type not in _CLIPPABLE and source.type is not EntityType.TEXT_LINK and (
            lo != source.offset or hi != source_end
        ):
            continue
        span = copy(source)
        span.offset = lo - start
        span.length = hi - lo
        spans.append(span)
    return CaptionNote(note, tuple(spans))


class _Caption:
    def __init__(self, text: str = "") -> None:
        self.parts: list[str] = [text] if text else []
        self.entities: list[MessageEntity] = []
        self.length = _utf16_length(text)

    def append(self, text: str) -> int:
        start = self.length
        self.parts.append(text)
        self.length += _utf16_length(text)
        return start

    def mark(self, kind: EntityType, start: int, *, url: str | None = None) -> None:
        if start < self.length:
            self.entities.append(MessageEntity(type=kind, offset=start, length=self.length - start, url=url))

    def section(self, child: _Caption, *, separator: str = _SEPARATOR) -> None:
        if not child.length:
            return
        if self.length:
            self.append(separator)
        offset = self.length
        self.parts.extend(child.parts)
        self.length += child.length
        for entity in child.entities:  # Child spans are owned by this composition from here on.
            entity.offset += offset
            self.entities.append(entity)

    def truncate(self, limit: int) -> None:
        if self.length <= limit:
            return
        if limit <= 0:
            self.parts.clear()
            self.entities.clear()
            self.length = 0
            return

        cut = limit - 1  # One UTF-16 unit for the ellipsis.
        for entity in self.entities:
            if entity.type not in _CLIPPABLE and entity.offset < cut < entity.offset + entity.length:
                cut = entity.offset
        encoded = "".join(self.parts).encode("utf-16-le")
        # The input encoded strictly; only the last code unit can now be an unmatched high surrogate.
        prefix = encoded[: 2 * cut].decode("utf-16-le", errors="ignore")
        cut = _utf16_length(prefix)
        self.parts = [prefix, "…"]
        self.length = cut + 1
        self.entities = [entity for entity in self.entities if entity.offset < cut]
        for entity in self.entities:
            end = entity.offset + entity.length
            if entity.type is EntityType.BLOCKQUOTE and cut <= end:
                # Keep the marker inside the quote so the block still ends at a line boundary.
                entity.length = cut + 1 - entity.offset
            elif cut < end:
                entity.length = cut - entity.offset

    def finish(self) -> tuple[str, list[MessageEntity]]:
        self.entities.sort(key=lambda e: (e.offset, -e.length, e.type is not EntityType.BLOCKQUOTE))
        return "".join(self.parts), self.entities


def _author(caption: _Caption, post: XPost, *, quote: bool = False) -> None:
    if label := format_author_label(post.author_name, post.author_handle):
        start = caption.append(label)
        if post.author_url:
            caption.mark(EntityType.TEXT_LINK, start, url=post.author_url)
        if not quote:
            caption.append(":")
        caption.mark(EntityType.ITALIC if quote else EntityType.BOLD, start)


def _append_runs(caption: _Caption, runs: Sequence[TextRun], *, italic: bool = False) -> None:
    # Group a link split into runs by style transitions; styles stay inside that one link,
    # with one span per active style on each run (including the quote's default italic).
    for url, group in groupby((run for run in runs if run.text), key=attrgetter("url")):
        link_start = caption.length
        for run in group:
            start = caption.append(run.text)
            for style, kind in _INLINE_STYLES.items():
                if style in run.styles or (italic and kind is EntityType.ITALIC):
                    caption.mark(kind, start)
        if url:
            caption.mark(EntityType.TEXT_LINK, link_start, url=url)


def _poll(poll: Poll, lang: str) -> str:
    """Plain rows rather than a Markdown table, which caption entities cannot express."""
    _t = t_[lang] if lang else t_
    lines = [poll.title] if poll.title else []
    if any(option.count is not None for option in poll.options):
        base = poll.voters or poll.total_votes or sum(option.count or 0 for option in poll.options)
        lines.append(" · ".join((_t("选项"), _t("票数"), _t("占比"))))
        for option in poll.options:
            if option.count is None:
                lines.append(option.text)
            else:
                share = round(option.count * 100 / base) if base else 0
                lines.append(f"{option.text} · {option.count} · {share}%")
    elif poll.options:
        lines.append(_t("选项"))
        lines.extend(option.text for option in poll.options)
        if (total := poll.total_votes) is not None:
            lines.append(_t(f"共 {total:,} 票"))
    return "\n".join(lines)


def _post(post: XPost, polls: Sequence[Poll], lang: str, *, quote: bool = False) -> _Caption:
    caption = _Caption()
    if quote:
        _author(caption, post, quote=True)
    body = _Caption()
    _append_runs(body, post.segments, italic=quote)
    caption.section(body, separator="\n")
    if post.poll_index is not None and 0 <= post.poll_index < len(polls):
        caption.section(_Caption(_poll(polls[post.poll_index], lang)))
    if post.youtube and post.youtube.text:
        card = _Caption()
        _append_runs(card, (post.youtube,), italic=quote)
        caption.section(card, separator="\n")
    if quote:
        caption.mark(EntityType.BLOCKQUOTE, 0)
    return caption


def _trending(card: XTrending, lang: str) -> _Caption:
    caption = _Caption(card.title)
    caption.mark(EntityType.BOLD, 0)
    caption.section(_Caption(card.subtitle))
    if card.url:
        _t = t_[lang] if lang else t_
        link = _Caption(_t("查看原話題"))
        link.mark(EntityType.TEXT_LINK, 0, url=card.url)
        caption.section(link)
    # The cover is delivered with the media, never as a placeholder in a caption.
    caption.mark(EntityType.BLOCKQUOTE, 0)
    return caption


def _metadata(result: AnyParseResult, lang: str, view_label: str) -> _Caption:
    caption = _Caption()
    if result.published_at is not None:
        local = result.published_at
        try:
            local = local.astimezone(ZoneInfo(_METADATA_TIMEZONE))
        except ZoneInfoNotFoundError:
            pass
        date = (
            f"{local.year}年{local.month}月{local.day}日" if lang.startswith(("zh", "ja")) else local.date().isoformat()
        )
        caption.append(f"{date} {local:%H:%M}")
        caption.entities.append(
            MessageEntity(
                type=EntityType.DATE_TIME,
                offset=0,
                length=caption.length,
                unix_time=int(local.timestamp()),
                date_time_format="Dt",
            )
        )
    if counts := build_metadata_line(
        view_count=result.view_count, like_count=result.like_count, lang=lang, view_label=view_label
    ):
        if caption.length:
            caption.append(" · ")
        caption.append(counts)
    return caption


def _source(url: str, lang: str, max_length: int) -> _Caption:
    caption = _Caption()
    if url:
        _t = t_[lang] if lang else t_
        display = Platform.TWITTER.display_name
        source = _t(f"来源（{display}）")
        if _utf16_length("▎" + source) <= max_length:
            caption.append("▎")
        else:
            source = _t("来源")
        if caption.length + _utf16_length(source) <= max_length:
            caption.mark(EntityType.TEXT_LINK, caption.append(source), url=url)
            caption.mark(EntityType.BOLD, 0)
    return caption


def build_x_status_caption(
    result: AnyParseResult,
    *,
    config: SettingsConfig,
    raw_url: str = "",
    lang: str = "",
    view_label: str = "",
    custom_content: str | CaptionNote = "",
    max_length: int = 1024,
) -> tuple[str, list[MessageEntity]]:
    """Compose the typed status, then budget the complete UTF-16 caption including its footer.

    For legacy results callers must use the legacy renderer. Caption entities have no rich
    details/table/media blocks, so related posts use flat blockquotes and polls use text rows.
    """
    status = result.x_status
    if status is None:
        raise ValueError("build_x_status_caption requires result.x_status")
    if max_length <= 0:
        return "", []

    caption = _Caption()
    _author(caption, status.main)
    if not config.hide_title and result.title:
        title = _Caption(result.title)
        title.mark(EntityType.BOLD, 0)
        caption.section(title)
    if not config.hide_desc:
        if status.reply:
            caption.section(_post(status.reply, result.polls, lang, quote=True))
        caption.section(_post(status.main, result.polls, lang))
        if status.trending:
            caption.section(_trending(status.trending, lang))
        if status.quoted:
            caption.section(_post(status.quoted, result.polls, lang, quote=True))
    if isinstance(custom_content, CaptionNote):
        note = _Caption(custom_content.text)
        note.entities.extend(copy(entity) for entity in custom_content.entities)
        caption.section(note)
    elif custom_content:
        caption.section(_Caption(custom_content))

    source = _source("" if config.hide_source else result.raw_url or raw_url, lang, max_length)
    footer = _Caption()
    metadata = _metadata(result, lang, view_label)
    if metadata.length + (len(_SEPARATOR) + source.length if source.length else 0) <= max_length:
        footer.section(metadata)
    footer.section(source)
    caption.truncate(max_length - (len(_SEPARATOR) + footer.length if footer.length else 0))
    caption.section(footer)
    return caption.finish()
