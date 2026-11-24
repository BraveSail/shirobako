"""Render a source document as literal Telegram text and explicit UTF-16 entities."""

from __future__ import annotations

from collections.abc import Sequence
from copy import copy
from itertools import groupby
from operator import attrgetter
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from parsehub import Platform
from parsehub.types import AnyParseResult, Poll, PollOption, VideoRef
from parsehub.types.document import Block, TextRun
from parsehub.utils.helpers import format_author_label
from pyrogram.enums import MessageEntityType as EntityType
from pyrogram.types import MessageEntity

from i18n import t_
from plugins.helpers import _METADATA_TIMEZONE, build_metadata_line
from plugins.parse.x_caption import _SEPARATOR, CaptionNote, _Caption, _poll, _source, _utf16_length

if TYPE_CHECKING:
    from repo.settings import SettingsConfig

_STYLES = {
    "bold": EntityType.BOLD,
    "italic": EntityType.ITALIC,
    "strike": EntityType.STRIKETHROUGH,
    "underline": EntityType.UNDERLINE,
    "spoiler": EntityType.SPOILER,
}
_PLATFORM_BY_ID = {platform.id: platform for platform in Platform}


def _localize_label(text: str, lang: str) -> str:
    _t = t_[lang] if lang else t_
    if text == "讨论":
        return _t("讨论")
    if text == "查看原話題":
        return _t("查看原話題")
    return text


def _runs(
    runs: Sequence[TextRun], *, bold: bool = False, blockquote: bool = False, lang: str = "", localize: bool = False
) -> _Caption:
    caption = _Caption()
    # A link can span several style runs. Nest the individual styles inside the
    # link, never the other way around or across adjacent links.
    for url, group in groupby((run for run in runs if run.text), key=attrgetter("url")):
        link_start = caption.length
        for run in group:
            text = _localize_label(run.text, lang) if localize or "localize" in run.styles else run.text
            start = caption.append(text)
            if "code" in run.styles and not (url or bold or blockquote):
                caption.mark(EntityType.CODE, start)
                continue
            for style, kind in _STYLES.items():
                if style in run.styles or (bold and style == "bold"):
                    caption.mark(kind, start)
        if url:
            caption.mark(EntityType.TEXT_LINK, link_start, url=url)
    return caption


def _poll_from_cache(value: Poll | dict) -> Poll:
    if isinstance(value, Poll):
        return value
    return Poll(
        options=[PollOption(text=option["text"], count=option.get("count")) for option in value.get("options", ())],
        total_votes=value.get("total_votes"),
        voters=value.get("voters"),
        title=value.get("title", ""),
    )


class _DocumentBody:
    def __init__(self, polls: Sequence[Poll | dict], lang: str) -> None:
        self.polls = polls
        self.lang = lang

    def blocks(self, blocks: Sequence[Block], *, quote: bool = False, in_list: bool = False) -> _Caption:
        caption = _Caption()
        for block in blocks:
            caption.section(self.block(block, quote=quote, in_list=in_list))
        return caption

    def block(self, block: Block, *, quote: bool, in_list: bool) -> _Caption:
        kind = block.kind
        if kind == "media":
            # Keep a real destination when provided; an index alone is not a link.
            return _runs((TextRun("↗", block.url),)) if block.url else _Caption()
        if kind == "poll":
            if 0 <= block.index < len(self.polls):
                return _Caption(_poll(_poll_from_cache(self.polls[block.index]), self.lang))
            return _Caption()
        if kind == "rule":
            return _Caption("────────")
        if kind == "table":
            caption = _Caption()
            for index, row in enumerate(block.rows):
                line = _Caption()
                for col, cell in enumerate(row):
                    if col:
                        line.append(" · ")
                    line.section(
                        _runs(cell, bold=block.header and index == 0, blockquote=quote, lang=self.lang), separator=""
                    )
                caption.section(line, separator="\n")
            return caption
        if kind == "list":
            caption = _Caption()
            for index, item in enumerate(block.items, block.start):
                content = self.blocks(item, quote=quote, in_list=True)
                if content.length:
                    line = _Caption(f"{index}. " if block.ordered else "• ")
                    line.section(content, separator="")
                    caption.section(line, separator="\n")
            return caption
        if kind == "code" and not quote and not any(run.url for run in block.runs):
            caption = _Caption("".join(run.text for run in block.runs))
            if caption.length:
                caption.entities.append(
                    MessageEntity(type=EntityType.PRE, offset=0, length=caption.length, language=block.language)
                )
            return caption
        # A caption quote must start a line and cannot contain another quote.
        # Flatten inner quotes and list-item quotes without dropping their contents.
        native_quote = kind == "quote" and not (quote or in_list)
        quoted = quote or native_quote
        caption = _runs(
            block.runs,
            bold=kind in {"heading", "details"},
            blockquote=quoted,
            lang=self.lang,
            localize=kind == "localize",
        )
        caption.section(self.blocks(block.children, quote=quoted, in_list=in_list))
        if native_quote:
            caption.mark(EntityType.BLOCKQUOTE, 0)
        return caption


def _source_footer(url: str, platform: Platform | None, lang: str, max_length: int) -> _Caption:
    if platform is Platform.TWITTER:
        return _source(url, lang, max_length)
    caption = _Caption()
    if not url:
        return caption
    _t = t_[lang] if lang else t_
    display = platform.display_name if platform else ""
    label = _t(f"来源（{display}）") if display else _t("来源")
    if _utf16_length("▎" + label) <= max_length:
        caption.append("▎")
    else:
        label = _t("来源")
    if caption.length + _utf16_length(label) <= max_length:
        caption.mark(EntityType.TEXT_LINK, caption.append(label), url=url)
        caption.mark(EntityType.BOLD, 0)
    return caption


def _metadata(result: AnyParseResult, platform: Platform | None, lang: str, view_label: str) -> _Caption:
    caption = _Caption()
    if (published_at := result.published_at) is not None:
        try:
            published_at = published_at.astimezone(ZoneInfo(_METADATA_TIMEZONE))
        except ZoneInfoNotFoundError:
            pass
        date = (
            f"{published_at.year}年{published_at.month}月{published_at.day}日"
            if lang.startswith(("zh", "ja"))
            else published_at.date().isoformat()
        )
        caption.append(f"{date} {published_at:%H:%M}")
        caption.entities.append(
            MessageEntity(
                type=EntityType.DATE_TIME,
                offset=0,
                length=caption.length,
                unix_time=int(published_at.timestamp()),
                date_time_format="Dt",
            )
        )
    media = getattr(result, "media", None)
    refs = media if isinstance(media, Sequence) else (media,)
    if duration := next((ref.duration for ref in refs if isinstance(ref, VideoRef) and ref.duration > 0), 0):
        hours, seconds = divmod(int(duration), 3600)
        minutes, seconds = divmod(seconds, 60)
        clock = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
        caption.section(_Caption(clock), separator=" · ")
    _t = t_[lang] if lang else t_
    counts = build_metadata_line(
        view_count=result.view_count,
        like_count=result.like_count,
        reply_count=getattr(result, "reply_count", None),
        lang=lang,
        view_label=view_label,
        like_label=_t("状态") if platform is Platform.BANGUMI else "",
        reply_label=_t("评论") if platform is Platform.WEIBO else "",
    )
    caption.section(_Caption(counts), separator=" · ")
    return caption


def _budget_for_prefix(limit: int, tail: _Caption) -> int:
    return limit - (tail.length + len(_SEPARATOR) if tail.length else 0)


def build_document_caption(
    result: AnyParseResult,
    *,
    config: SettingsConfig,
    raw_url: str = "",
    lang: str = "",
    view_label: str = "",
    custom_content: str | CaptionNote = "",
    max_length: int = 1024,
) -> tuple[str, list[MessageEntity]]:
    """Build a UTF-16-budgeted caption; callers must disable parse mode.

    The document owns body order, including reply and quoted posts. Legacy
    content and role fields are not another source of caption text.
    """
    document = result.document
    if document is None:
        raise ValueError("build_document_caption requires result.document")
    if max_length <= 0:
        return "", []

    caption = _Caption()
    author = _Caption()
    if label := format_author_label(result.author_name, result.author_handle):
        start = author.append(label)
        if result.author_url:
            author.mark(EntityType.TEXT_LINK, start, url=result.author_url)
        author.append(":")
        author.mark(EntityType.BOLD, start)
    if position := getattr(result, "position_label", ""):
        author.section(_Caption(position), separator=" ")
    author.section(_runs(document.topic, lang=lang), separator=" › ")
    caption.section(author)
    if result.title and not config.hide_title:
        caption.section(_runs((TextRun(result.title),), bold=True))
    caption.section(_runs(document.origin, lang=lang))
    if not config.hide_desc:
        caption.section(_DocumentBody(result.polls, lang).blocks(document.blocks))

    platform = result.platform if isinstance(result.platform, Platform) else _PLATFORM_BY_ID.get(result.platform)
    url = "" if config.hide_source else getattr(result, "raw_url", "") or raw_url
    source = _source_footer(url, platform, lang, max_length)
    note = _Caption(custom_content.text if isinstance(custom_content, CaptionNote) else custom_content)
    if isinstance(custom_content, CaptionNote):
        note.entities.extend(copy(entity) for entity in custom_content.entities)
    note.truncate(_budget_for_prefix(max_length, source))

    # Preserve the user's note and the source before spending the budget on body text.
    metadata = _metadata(result, platform, lang, view_label)
    footer = _Caption()
    footer.section(note)
    available = _budget_for_prefix(max_length, source)
    if metadata.length <= _budget_for_prefix(available, note):
        footer.section(metadata)
    footer.section(source)
    caption.truncate(_budget_for_prefix(max_length, footer))
    caption.section(footer)
    return caption.finish()
