"""Project provider Documents directly into Telegram rich-message blocks."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from copy import copy
from itertools import groupby
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from parsehub import ParseHub
from parsehub.types import Platform
from parsehub.types.document import Block, Document, TextRun
from parsehub.utils.helpers import format_author_label, profile_id_from_url
from pyrogram.types import (
    InputRichBlock,
    InputRichBlockBlockQuotation,
    InputRichBlockCollage,
    InputRichBlockDetails,
    InputRichBlockDivider,
    InputRichBlockFooter,
    InputRichBlockList,
    InputRichBlockListItem,
    InputRichBlockParagraph,
    InputRichBlockPreformatted,
    InputRichBlockSectionHeading,
    InputRichBlockTable,
    RichBlockTableCell,
    RichText,
    RichTextBold,
    RichTextCode,
    RichTextDateTime,
    RichTextItalic,
    RichTextMarked,
    RichTextSpoiler,
    RichTextStrikethrough,
    RichTextSubscript,
    RichTextSuperscript,
    RichTextUnderline,
    RichTextUrl,
)

from i18n import t_
from plugins.helpers import (
    _METADATA_SEPARATOR,
    _METADATA_TIMEZONE,
    SHOW_MORE_VISIBLE_LIMIT,
    TAG_LINE_DISPLAY_BUDGET,
    _display_width,
    _should_fold,
    get_parse_author_name,
    metadata_like_label,
    metadata_reply_label,
    tag_page_url,
)
from plugins.parse.notes import ParseNote
from plugins.parse.rich_blocks import build_poll_blocks
from plugins.parse.x_status import (
    _GALLERY_ITEMS,
    _application_custom_blocks,
    _gallery,
    _main_gallery,
    _preview_offset,
)

if TYPE_CHECKING:
    from repo.settings import SettingsConfig

_MediaSlot = tuple[int | None, str, InputRichBlock]
_Inline = str | RichText | list[str | RichText]
_STYLES = {
    "bold": RichTextBold, "italic": RichTextItalic, "strike": RichTextStrikethrough,
    "underline": RichTextUnderline, "code": RichTextCode, "spoiler": RichTextSpoiler,
    "superscript": RichTextSuperscript, "subscript": RichTextSubscript, "marked": RichTextMarked,
}
_CARD_ROLES = {"reply", "quoted", "trending"}
_MEDIA_KINDS = {"media", "collage"}


def _inline(runs: Sequence[TextRun], lang: str, *, localize: bool = False) -> list[str | RichText]:
    _t = t_[lang] if lang else t_
    inline: list[str | RichText] = []
    for run in runs:
        text = run.text
        if not text:
            continue
        if localize or "localize" in run.styles:
            if text == "讨论":
                text = _t("讨论")
            elif text == "查看原話題":
                text = _t("查看原話題")
        value = RichTextUrl(text, run.url) if run.url else text
        for style in reversed(run.styles):
            if wrapper := _STYLES.get(style):
                value = wrapper(value)
        inline.append(value)
    return inline


class _Media:
    """Track consumed slot positions separately from lookup: a source node may repeat."""

    def __init__(self, slots: Sequence[_MediaSlot], *, fallback: _Media | None = None):
        self.slots = slots
        self.fallback = fallback
        self.used: set[int] = set()
        self.by_index: dict[int, list[int]] = defaultdict(list)
        self.by_url: dict[str, int] = {}
        for position, (index, url, _) in enumerate(slots):
            if index is not None:
                self.by_index[index].append(position)
            if url:
                self.by_url.setdefault(url, position)

    def _index_match(self, node: Block) -> int | None:
        if node.index < 0:
            return None
        exact = next((
            pos for pos in self.by_index.get(node.index, ())
            if not node.url or self.slots[pos][1] == node.url
        ), None)
        return exact if exact is not None else next((
            pos for pos in self.by_index.get(node.index, ()) if not self.slots[pos][1]
        ), None)

    def resolve(self, node: Block) -> list[InputRichBlock]:
        pools = (self, self.fallback) if self.fallback is not None else (self,)
        for match in (_Media._index_match, lambda pool, block: pool.by_url.get(block.url)):
            for pool in pools:
                if (position := match(pool, node)) is not None:
                    index, url, _ = pool.slots[position]
                    # One provider ref can expand into multiple transmitted files.
                    positions = [position] if index is None else [
                        i for i in pool.by_index[index] if pool.slots[i][1] in ("", url)
                    ]
                    pool.used.update(positions)
                    return [pool.slots[i][2] for i in positions]
        return []

    def tail(self) -> list[InputRichBlock]:
        return [block for i, (_, _, block) in enumerate(self.slots) if i not in self.used]


def _consume_media(nodes: Sequence[Block], media: _Media) -> None:
    for node in nodes:
        if node.kind == "media":
            media.resolve(node)
        _consume_media(node.children, media)
        for item in node.items:
            _consume_media(item, media)


def _visible(value) -> str:
    """Count actual display text, not entity targets or closed details' children."""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(filter(None, map(_visible, value)))
    if isinstance(value, InputRichBlockDetails):
        return _visible_inline(value.summary)
    if isinstance(value, InputRichBlockTable):
        return _visible(value.cells)
    if isinstance(value, InputRichBlockList):
        return _visible(value.items)
    if hasattr(value, "blocks"):
        return _visible(value.blocks)
    return _visible_inline(getattr(value, "text", None))


def _visible_inline(value: _Inline | None) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(map(_visible_inline, value))
    return _visible_inline(value.text) if isinstance(value, RichText) and hasattr(value, "text") else ""


def _split_inline(value: _Inline, offset: int) -> tuple[_Inline | None, _Inline | None]:
    """Divide prose without modifying its styles or slicing a link label in half."""
    if offset <= 0:
        return None, value
    if offset >= len(_visible_inline(value)):
        return value, None
    if isinstance(value, str):
        return value[:offset], value[offset:]
    if isinstance(value, RichTextUrl):
        return None, value
    if isinstance(value, list):
        before, after = [], []
        for part in value:
            left, right = _split_inline(part, offset)
            if left is not None:
                before.append(left)
            if right is not None:
                after.append(right)
            offset -= len(_visible_inline(part))
        return before or None, after or None
    if not isinstance(value, RichText) or not hasattr(value, "text"):
        return None, value
    left, right = _split_inline(value.text, offset)

    def rewrap(text):
        if text is not None:
            piece = copy(value)
            piece.text = text
            return piece
        return None

    return rewrap(left), rewrap(right)


def _fold(blocks: list[InputRichBlock], summary: str) -> list[InputRichBlock]:
    if not _should_fold(text := _visible(blocks)):
        return blocks
    remaining = _preview_offset(text)
    preview: list[InputRichBlock] = []
    for i, block in enumerate(blocks):
        size = len(_visible(block))
        if remaining < size or remaining <= 0:
            tail = blocks[i:]
            if isinstance(block, InputRichBlockParagraph) and remaining > 0:
                before, after = _split_inline(block.text, remaining)
                if before is not None:
                    preview.append(InputRichBlockParagraph(before))
                tail = [InputRichBlockParagraph(after), *blocks[i + 1:]] if after is not None else blocks[i + 1:]
            return [*preview, InputRichBlockDetails(summary, tail)] if tail else preview
        preview.append(block)
        if size:
            remaining -= size + 1  # the block boundary separator in _visible
    return blocks


class _Renderer:
    def __init__(self, lang: str, polls: Sequence, *, fold: bool):
        self.lang = lang
        self.polls = polls
        self.fold = fold
        _t = t_[lang] if lang else t_
        self.summary = _t("展开全文")

    def children(self, nodes: Sequence[Block], media: _Media) -> list[InputRichBlock]:
        return [block for node in nodes for block in self.block(node, media)]

    def block(self, node: Block, media: _Media) -> list[InputRichBlock]:
        kind = node.kind
        if kind == "media":
            return media.resolve(node)
        if kind == "collage":
            return _gallery(self.children(node.children, media))
        if kind == "rule":
            return [InputRichBlockDivider()]
        if kind == "poll":
            return build_poll_blocks(self.polls, node.index, self.lang)
        if kind == "list":
            items = [InputRichBlockListItem(
                self.children(item, media) or [InputRichBlockParagraph("")],
                value=node.start + i if node.ordered else None, type="1" if node.ordered else None,
            ) for i, item in enumerate(node.items)]
            return [InputRichBlockList(items)] if items else []
        if kind == "table":
            rows = [[RichBlockTableCell(_inline(cell, self.lang) or None, is_header=node.header and i == 0)
                     for cell in row] for i, row in enumerate(node.rows)]
            return [InputRichBlockTable(rows)] if rows else []
        text = _inline(node.runs, self.lang, localize=kind == "localize")
        if kind == "heading":
            return [InputRichBlockSectionHeading(text, size=node.level)] if text else []
        if kind == "code":
            return [InputRichBlockPreformatted(text, language=node.language or None)] if text else []
        if kind == "details":
            children = self.children(node.children, media)
            return [InputRichBlockDetails(text or self.summary, children)] if children else []
        if kind == "quote":
            if node.role in _CARD_ROLES:
                return self.card(node, media)
            children = self.children(node.children, media)
            if text:
                children[:0] = [InputRichBlockParagraph(RichTextItalic(text))]
            return [InputRichBlockBlockQuotation(children)] if children else []
        return [InputRichBlockParagraph(text)] if text else []

    def card(self, node: Block, media: _Media) -> list[InputRichBlockBlockQuotation]:
        if node.role == "trending":
            body = self.children(node.children, media)
        else:
            body = []
            for is_media, group in groupby(node.children, key=lambda b: b.kind in _MEDIA_KINDS):
                part = self.children(tuple(group), media)
                if is_media:
                    body.extend(_gallery(part))
                else:
                    part = [InputRichBlockParagraph(RichTextItalic(b.text)) if isinstance(b, InputRichBlockParagraph)
                            else b for b in part]
                    body.extend(_fold(part, self.summary) if self.fold else part)
        if signer := _inline(node.runs, self.lang):
            body[:0] = [InputRichBlockParagraph(RichTextItalic(signer)), *([InputRichBlockDivider()] if body else [])]
        return [InputRichBlockBlockQuotation(body)] if body else []

    def body(self, nodes: Sequence[Block], media: _Media) -> list[InputRichBlock]:
        blocks = self.children(nodes, media)
        return _fold(blocks, self.summary) if self.fold else blocks


def _byline(result, topic: Sequence[TextRun], lang: str) -> InputRichBlockParagraph | None:
    name = get_parse_author_name(result)
    url = getattr(result, "author_url", "") or ""
    handle = str(getattr(result, "author_handle", "") or "").strip().lstrip("@").strip()
    if url and not handle:
        handle = profile_id_from_url(url)
    label = format_author_label(name, handle)
    inline: list[str | RichText] = []
    if label:
        separated = url and name and handle and name.casefold() != handle.casefold()
        inline.append(RichTextBold(RichTextUrl(name if separated else label, url) if url else label))
        if separated:
            inline.extend((" ", RichTextCode(f"@{handle}")))
        if position := getattr(result, "position_label", ""):
            inline.append(f" · {position}")
    if topic_inline := _inline(topic, lang):
        if inline:
            inline.append(" > ")
        inline.extend(topic_inline)
    return InputRichBlockParagraph(inline) if inline else None


def _tags(result, platform: Platform | None) -> InputRichBlockParagraph | None:
    inline: list[str | RichText] = []
    width = 0
    for tag in getattr(result, "tags", None) or ():
        label = f"#{tag}"
        piece = _display_width(label) + 1
        if inline:
            inline.append(" ")
            if width + piece > TAG_LINE_DISPLAY_BUDGET:
                inline.append("…")
                break
        url = tag_page_url(platform, tag, user_id=getattr(result, "author_handle", "") or "")
        inline.append(RichTextUrl(label, url) if url else label)
        width += piece
    return InputRichBlockParagraph(inline) if inline else None


def _footer(result, raw_url: str, platform: Platform | None, *, lang: str, view_label: str,
            progress: str, hide_source: bool) -> InputRichBlockFooter | None:
    _t = t_[lang] if lang else t_
    parts: list[str | RichText] = []
    if progress:
        # format_label's known app-owned envelope, not general markup parsing.
        stage = progress
        if stage.startswith("<b>▎") and stage.endswith("</b>"):
            stage = RichTextBold(stage[3:-4])
        parts.append(stage)
    if published := getattr(result, "published_at", None):
        try:
            local = published.astimezone(ZoneInfo(_METADATA_TIMEZONE))
        except ZoneInfoNotFoundError:
            local = published
        date = local.strftime("%Y-%m-%d")
        if lang.startswith(("zh", "ja")):
            date = f"{local.year}年{local.month}月{local.day}日"
        parts.append(RichTextDateTime(f"{date} {local:%H:%M}", local, "Dt"))
    counters: tuple[tuple[str, Callable[[], str]], ...] = (
        ("view_count", lambda: view_label or _t("查看")),
        ("like_count", lambda: metadata_like_label(result, _t) or _t("点赞")),
        ("reply_count", lambda: metadata_reply_label(result, _t) or _t("回复")),
    )
    for field, label in counters:
        if (count := getattr(result, field, None)) is not None:
            parts.append(f"{count:,} {label()}".strip())
    if raw_url and not hide_source:
        if platform and lang:
            display = platform.display_name
            label = _t(f"来源（{display}）")
        else:
            label = _t("来源") if lang else "Source"
        parts.append(RichTextUrl(label, raw_url))
    if not parts:
        return None
    inline = [parts[0]]
    for part in parts[1:]:
        inline.extend((_METADATA_SEPARATOR, part))
    return InputRichBlockFooter(inline)


def _with_gap(blocks: list[InputRichBlock], next_blocks: Sequence[InputRichBlock], *, gallery: bool = False) -> None:
    if not next_blocks:
        return
    if blocks:
        previous = blocks[-1]
        adjacent_card = isinstance(previous, InputRichBlockBlockQuotation)
        before_card = isinstance(next_blocks[0], InputRichBlockBlockQuotation)
        if (gallery and adjacent_card) or (before_card and isinstance(
            previous, (InputRichBlockBlockQuotation, InputRichBlockCollage, *_GALLERY_ITEMS)
        )):
            blocks.append(InputRichBlockDivider())
    blocks.extend(next_blocks)


def _cap_visible(blocks: list[InputRichBlock], independent: list[InputRichBlock], summary: str) -> list[InputRichBlock]:
    if len(_visible(blocks)) <= SHOW_MORE_VISIBLE_LIMIT:
        return blocks
    # Keep independent sections at their positions as siblings of the safety folds.
    ids = {id(block) for block in independent}
    return [block for separate, group in groupby(blocks, key=lambda block: id(block) in ids)
            for block in (list(group) if separate else [InputRichBlockDetails(summary, list(group))])]


def build_document_blocks(
    result,
    *,
    config: SettingsConfig,
    raw_url: str = "",
    lang: str = "",
    view_label: str = "",
    hide_content: str = "",
    progress: str = "",
    custom_content: str | ParseNote = "",
    main_media: Sequence[_MediaSlot] = (),
    reply_media: Sequence[_MediaSlot] = (),
    quoted_media: Sequence[_MediaSlot] = (),
) -> list[InputRichBlock]:
    """Render typed platform content with the caller's already-resolved media slots."""
    document = getattr(result, "document", None) or Document()
    raw_url = raw_url or getattr(result, "raw_url", "") or ""
    platform = getattr(result, "platform", None)
    if isinstance(platform, str):
        platform = next((value for value in Platform if value.id == platform), None)
    platform = platform or (ParseHub().get_platform(raw_url) if raw_url else None)
    render = _Renderer(lang, getattr(result, "polls", None) or (), fold=not hide_content)
    main = _Media(main_media)
    # Bilibili comment chains and Linux.do context floors also address media in main.
    # Resolve those references in the card so they cannot fall into the trailing gallery.
    shared_reply_media = main if platform in {Platform.BILIBILI, Platform.LINUXDO} else None
    buckets = {"reply": _Media(reply_media, fallback=shared_reply_media),
               "quoted": _Media(quoted_media)}
    banners: list[InputRichBlock] = []
    flow: list[Block] = []
    front: list[Block] = []
    back: list[Block] = []
    for node in document.blocks:
        if node.role == "banner" and node.kind in _MEDIA_KINDS:
            banners.extend(render.block(node, main))
        elif node.kind == "quote" and node.role in {"reply", "quoted"}:
            (front if node.role == "reply" else back).append(node)
        else:
            flow.append(node)
    # Providers append explicit attachment nodes at the end; PTT's nodes, including
    # trailing images, are actual source positions rather than an attachment gallery.
    attachment_start = len(flow)
    while (platform != Platform.PTT and attachment_start
           and flow[attachment_start - 1].kind in _MEDIA_KINDS and flow[attachment_start - 1].role != "attached_video"):
        attachment_start -= 1
    source_body, attachments = flow[:attachment_start], flow[attachment_start:]

    def cards(nodes: list[Block], role: str) -> list[InputRichBlockBlockQuotation]:
        bucket = buckets[role]
        if config.hide_desc:
            _consume_media(nodes, bucket)  # a hidden card can own explicitly addressed main media
            return []
        rendered = [card for node in nodes for card in render.card(node, bucket)]
        if nodes and (tail := _gallery(bucket.tail())):
            if rendered:
                rendered[0].blocks.extend(tail)
            else:
                rendered.append(InputRichBlockBlockQuotation(tail))
        return rendered

    content: list[InputRichBlock] = []
    independent: list[InputRichBlock] = []
    front_cards = cards(front, "reply")
    back_cards = cards(back, "quoted")
    for card in front_cards:
        _with_gap(content, [card])
    # Explicit details and cards are boundaries, not content of a surrounding
    # automatic prose fold. Arbitrary source quotations remain in the body.
    def section(node: Block) -> str:
        if (node.kind == "collage" or node.role == "attachment"
                or (platform == Platform.YOUTUBE and node.kind == "media" and node.role != "attached_video")):
            return "gallery"
        if node.kind == "details" or (node.kind == "quote" and node.role == "trending"):
            return "separate"
        return "body"

    def gallery_items(nodes: Sequence[Block]) -> list[InputRichBlock]:
        return [block for node in nodes for block in (
            render.children(node.children, main) if node.kind == "collage" else render.block(node, main)
        )]

    for kind, group in groupby(source_body, key=section):
        nodes = tuple(group)
        if kind == "gallery":
            _with_gap(content, _main_gallery(gallery_items(nodes), lang, fold=not hide_content), gallery=True)
        elif config.hide_desc:
            _consume_media(nodes, main)  # consume inline slots without appending them
        elif kind == "separate":
            for node in nodes:
                blocks = render.block(node, main)
                if node.kind == "details":
                    independent.extend(blocks)
                _with_gap(content, blocks)
        else:
            content.extend(render.body(nodes, main))
    if custom_content:
        content.extend(_application_custom_blocks(custom_content))
    tail = gallery_items(attachments)
    tail.extend(main.tail())
    if not config.hide_desc:
        if not front:
            tail.extend(buckets["reply"].tail())
        if not back:
            tail.extend(buckets["quoted"].tail())
    gallery = _main_gallery(tail, lang, fold=not hide_content)
    _with_gap(content, gallery, gallery=True)
    for card in back_cards:
        if gallery and content[-1] is gallery[-1]:
            content.append(InputRichBlockDivider())
        _with_gap(content, [card])
    if tags := _tags(result, platform):
        content.append(tags)

    blocks: list[InputRichBlock] = []
    if not config.hide_title and (title := (getattr(result, "title", "") or "").strip()):
        blocks.append(InputRichBlockSectionHeading(title, size=1))
    if origin := _inline(document.origin, lang):
        blocks.append(InputRichBlockParagraph(origin))
    author = _byline(result, document.topic, lang)
    if author:
        blocks.append(author)
    banner = _gallery(banners)
    if hide_content:
        if banner or content:
            if author or origin:
                blocks.append(InputRichBlockDivider())
            blocks.append(InputRichBlockDetails(f"⚠️ {hide_content}", [*banner, *content]))
    else:
        if author is not None or origin:
            if content:
                blocks.append(InputRichBlockDivider())
        if banner:
            blocks[:0] = [*banner, *([InputRichBlockDivider()] if blocks else [])]
        blocks.extend(content)
        blocks = _cap_visible(blocks, independent, render.summary)
    if footer := _footer(result, raw_url, platform, lang=lang, view_label=view_label, progress=progress,
                         hide_source=config.hide_source):
        if blocks:
            blocks.append(InputRichBlockDivider())
        blocks.append(footer)
    return blocks
