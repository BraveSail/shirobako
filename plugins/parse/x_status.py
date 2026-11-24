"""Build Telegram rich-message blocks directly from native X status data."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from parsehub.types import Platform
from parsehub.types.x_status import TextRun, XPost, XTrending
from parsehub.utils.helpers import format_author_label
from pyrogram.types import (
    InputRichBlock,
    InputRichBlockAnimation,
    InputRichBlockBlockQuotation,
    InputRichBlockCollage,
    InputRichBlockDetails,
    InputRichBlockDivider,
    InputRichBlockFooter,
    InputRichBlockParagraph,
    InputRichBlockPhoto,
    InputRichBlockSectionHeading,
    InputRichBlockTable,
    InputRichBlockVideo,
    RichBlockTableCell,
    RichText,
    RichTextBold,
    RichTextCode,
    RichTextDateTime,
    RichTextItalic,
    RichTextStrikethrough,
    RichTextUrl,
)

from i18n import t_
from plugins.helpers import (
    _COLLAGE_FOLD_THRESHOLD,
    _FOLD_PREVIEW_CHARS,
    _FOLD_PREVIEW_LINES,
    _METADATA_SEPARATOR,
    _METADATA_TIMEZONE,
    TAG_LINE_DISPLAY_BUDGET,
    _display_width,
    _poll_field,
    _poll_int,
    _should_fold,
    tag_page_url,
)
from plugins.parse.rich_blocks import SpoilerPhotoBlock, SpoilerVideoBlock

if TYPE_CHECKING:
    from repo.settings import SettingsConfig

_STYLE = {"bold": RichTextBold, "italic": RichTextItalic, "strike": RichTextStrikethrough}
_GALLERY_ITEMS = (
    InputRichBlockPhoto, InputRichBlockVideo, InputRichBlockAnimation, SpoilerPhotoBlock, SpoilerVideoBlock,
)
_VisibleText = str | RichText | list[str | RichText]


def _rich(run: TextRun) -> str | RichText:
    value = RichTextUrl(run.text, run.url) if run.url else run.text
    for style in reversed(run.styles):
        if wrapper := _STYLE.get(style):
            value = wrapper(value)
    return value


def _byline(post: XPost, *, quoted: bool = False) -> InputRichBlockParagraph | None:
    name = post.author_name.strip()
    handle = post.author_handle.strip().lstrip("@").strip()
    label = format_author_label(name, handle)
    if not label:
        return None
    text: _VisibleText = label
    separate_handle = name and handle and name.casefold() != handle.casefold()
    if post.author_url:
        text = RichTextUrl(name if separate_handle else label, post.author_url)
    if not quoted:
        text = RichTextBold(text)
    if post.author_url and separate_handle:
        text = [text, " ", RichTextCode(f"@{handle}")]
    return InputRichBlockParagraph(RichTextItalic(text) if quoted else text)


def _preview_offset(text: str) -> int:
    """Apply the old preview budget to visible text, never to markup/URLs."""
    end = 0
    lines = 0
    for line in text.splitlines(keepends=True):
        if lines >= _FOLD_PREVIEW_LINES or (lines and end + len(line.rstrip("\r\n")) > _FOLD_PREVIEW_CHARS):
            break
        if line.strip():
            lines += 1
        end += len(line)
        if end >= _FOLD_PREVIEW_CHARS:
            return _FOLD_PREVIEW_CHARS
    return end


def _split_runs(runs: Sequence[TextRun], offset: int) -> tuple[list[TextRun], list[TextRun]]:
    before: list[TextRun] = []
    after: list[TextRun] = []
    for run in runs:
        size = len(run.text)
        if offset >= size:
            before.append(run)
        elif offset <= 0 or run.url:
            # A link that straddles the cutoff moves as a single entity to the fold.
            after.append(run)
        else:
            before.append(replace(run, text=run.text[:offset]))
            after.append(replace(run, text=run.text[offset:]))
        offset -= size
    return before, after


def _paragraph(runs: Sequence[TextRun], *, italic: bool = False) -> InputRichBlockParagraph:
    text = [_rich(run) for run in runs if run.text]
    return InputRichBlockParagraph(RichTextItalic(text) if italic else text)


def _body(post: XPost, *, summary: str, fold: bool, quoted: bool) -> list[InputRichBlock]:
    text = "".join(run.text for run in post.segments)
    if not text.strip():
        return []
    if fold and _should_fold(text):
        preview, rest = _split_runs(post.segments, _preview_offset(text))
        if rest:
            blocks: list[InputRichBlock] = []
            if any(run.text for run in preview):
                blocks.append(_paragraph(preview, italic=quoted))
            blocks.append(InputRichBlockDetails(summary, [_paragraph(rest, italic=quoted)]))
            return blocks
    return [_paragraph(post.segments, italic=quoted)]


def _poll(polls: Sequence, index: int | None, lang: str) -> list[InputRichBlock]:
    if index is None or not 0 <= index < len(polls):
        return []
    poll = polls[index]
    options = list(_poll_field(poll, "options") or ())
    if not options:
        return []
    _t = t_[lang] if lang else t_
    counts = [_poll_int(_poll_field(option, "count")) for option in options]
    total_votes = _poll_int(_poll_field(poll, "total_votes"))
    weighted = any(count is not None for count in counts)
    denominator = _poll_int(_poll_field(poll, "voters")) or total_votes or sum(count or 0 for count in counts)
    labels = [_t("选项")]
    if weighted:
        labels.extend((_t("票数"), _t("占比")))
    rows = [[RichBlockTableCell(label, is_header=True) for label in labels]]
    for option, count in zip(options, counts, strict=True):
        cells = [RichBlockTableCell(str(_poll_field(option, "text") or ""))]
        if weighted:
            percent = f"{round(count * 100 / denominator) if denominator else 0}%" if count is not None else None
            cells.extend((RichBlockTableCell(None if count is None else str(count)), RichBlockTableCell(percent)))
        rows.append(cells)
    blocks: list[InputRichBlock] = []
    if total_votes is not None and not weighted:
        head = _t("投票")
        if total_votes:
            total = total_votes
            head += _METADATA_SEPARATOR + _t(f"共 {total:,} 票")
        blocks.append(InputRichBlockParagraph(head))
    blocks.append(InputRichBlockTable(rows))
    return blocks


def _gallery(media: Sequence[InputRichBlock]) -> list[InputRichBlock]:
    """Keep non-media fallbacks in order, but out of Telegram's media-only collage."""
    blocks: list[InputRichBlock] = []
    run: list[InputRichBlock] = []
    for block in media:
        if isinstance(block, _GALLERY_ITEMS):
            run.append(block)
        else:
            blocks.extend([InputRichBlockCollage(run)] if len(run) > 1 else run)
            blocks.append(block)
            run = []
    blocks.extend([InputRichBlockCollage(run)] if len(run) > 1 else run)
    return blocks


def _main_gallery(media: Sequence[InputRichBlock], lang: str, *, fold: bool) -> list[InputRichBlock]:
    if fold:
        positions = [i for i, block in enumerate(media) if isinstance(block, _GALLERY_ITEMS)]
        if len(positions) > _COLLAGE_FOLD_THRESHOLD:
            cut = positions[_COLLAGE_FOLD_THRESHOLD]
            count = len(positions) - _COLLAGE_FOLD_THRESHOLD
            _t = t_[lang] if lang else t_
            summary = _t(f"🖼 展开其余 {count} 张图片")
            return [*_gallery(media[:cut]), InputRichBlockDetails(summary, _gallery(media[cut:]))]
    return _gallery(media)


def _post_content(
    post: XPost, polls: Sequence, *, summary: str, lang: str, fold: bool, quoted: bool,
) -> list[InputRichBlock]:
    blocks = _body(post, summary=summary, fold=fold, quoted=quoted)
    blocks.extend(_poll(polls, post.poll_index, lang))
    if post.youtube and post.youtube.text:
        blocks.append(_paragraph([post.youtube], italic=True))
    return blocks


def _related(
    post: XPost | None, media: Sequence[InputRichBlock], polls: Sequence, *, summary: str, lang: str, fold: bool,
) -> InputRichBlockBlockQuotation | None:
    blocks: list[InputRichBlock] = []
    if post is not None:
        descendants = _post_content(post, polls, summary=summary, lang=lang, fold=fold, quoted=True)
        if author := _byline(post, quoted=True):
            blocks.append(author)
            if descendants:
                blocks.append(InputRichBlockDivider())
        blocks.extend(descendants)
    if blocks or media:
        return InputRichBlockBlockQuotation([*blocks, *_gallery(media)])
    return None


def _trending(trending: XTrending | None, media: Sequence[InputRichBlock]) -> InputRichBlockBlockQuotation | None:
    blocks: list[InputRichBlock] = []
    if trending and trending.title:
        blocks.append(InputRichBlockParagraph(RichTextBold(trending.title)))
    blocks.extend(_gallery(media))
    if trending and trending.subtitle:
        blocks.append(InputRichBlockParagraph(trending.subtitle))
    if trending and trending.url:
        blocks.append(InputRichBlockParagraph(RichTextUrl("查看原話題", trending.url)))
    return InputRichBlockBlockQuotation(blocks) if blocks else None


def _tags(tags: Sequence[str]) -> InputRichBlockParagraph | None:
    inline: list[str | RichText] = []
    width = 0
    for tag in tags:
        label = f"#{tag}"
        piece = _display_width(label) + 1
        if inline:
            inline.append(" ")
            if width + piece > TAG_LINE_DISPLAY_BUDGET:
                inline.append("…")
                break
        inline.append(RichTextUrl(label, tag_page_url(Platform.TWITTER, tag)))
        width += piece
    return InputRichBlockParagraph(inline) if inline else None


def _application_custom_blocks(value: str) -> list[InputRichBlock]:
    """Only the caller's legacy custom annotation enters the markup adapter, never X data."""
    from plugins.parse.rich_blocks import markdown_to_blocks

    return markdown_to_blocks(value)


def _footer(
    result, *, raw_url: str, lang: str, view_label: str, progress: str, hide_source: bool,
) -> InputRichBlockFooter | None:
    _t = t_[lang] if lang else t_
    parts: list[str | RichText] = []
    if progress:
        # The app's format_label() wraps the literal stage text in one bold tag.
        if progress.startswith("<b>▎") and progress.endswith("</b>"):
            parts.append(RichTextBold(progress[3:-4]))
        else:
            parts.append(progress)
    if value := getattr(result, "published_at", None):
        try:
            local = value.astimezone(ZoneInfo(_METADATA_TIMEZONE))
        except ZoneInfoNotFoundError:
            local = value
        if lang.startswith(("zh", "ja")):
            date = f"{local.year}年{local.month}月{local.day}日"
        else:
            date = local.strftime("%Y-%m-%d")
        parts.append(RichTextDateTime(f"{date} {local:%H:%M}", value, "Dt"))
    counters = (("view_count", view_label or _t("查看")), ("like_count", _t("点赞")), ("reply_count", _t("回复")))
    for field, label in counters:
        if (count := getattr(result, field, None)) is not None:
            parts.append(f"{count:,} {label}".strip())
    if raw_url and not hide_source:
        display = Platform.TWITTER.display_name
        label = _t(f"来源（{display}）") if lang else "Source"
        parts.append(RichTextUrl(label, raw_url))
    if not parts:
        return None
    elements = [parts[0]]
    for part in parts[1:]:
        elements.extend((_METADATA_SEPARATOR, part))
    return InputRichBlockFooter(elements)


def build_x_status_blocks(
    result,
    *,
    config: SettingsConfig,
    raw_url: str = "",
    lang: str = "",
    view_label: str = "",
    hide_content: str = "",
    progress: str = "",
    custom_content: str = "",
    main_media: Sequence[InputRichBlock] = (),
    reply_media: Sequence[InputRichBlock] = (),
    quoted_media: Sequence[InputRichBlock] = (),
    trending_media: Sequence[InputRichBlock] = (),
) -> list[InputRichBlock]:
    """Render a typed status; the caller supplies already-resolved media in role order."""
    status = result.x_status
    _t = t_[lang] if lang else t_
    summary = _t("展开全文")
    fold = not hide_content
    polls = getattr(result, "polls", None) or ()
    blocks: list[InputRichBlock] = []
    if not config.hide_title and (title := (getattr(result, "title", None) or "").strip()):
        blocks.append(InputRichBlockSectionHeading(title, size=1))
    if author := _byline(status.main):
        blocks.append(author)
    content: list[InputRichBlock] = []
    quoted = None
    if not config.hide_desc:
        if reply := _related(status.reply, reply_media, polls, summary=summary, lang=lang, fold=fold):
            content.append(reply)
        content.extend(_post_content(status.main, polls, summary=summary, lang=lang, fold=fold, quoted=False))
        if card := _trending(status.trending, trending_media):
            if content and isinstance(content[-1], InputRichBlockBlockQuotation):
                content.append(InputRichBlockDivider())
            content.append(card)
        quoted = _related(status.quoted, quoted_media, polls, summary=summary, lang=lang, fold=fold)
    if custom_content:
        content.extend(_application_custom_blocks(custom_content))
    if gallery := _main_gallery(main_media, lang, fold=fold):
        if content and isinstance(content[-1], InputRichBlockBlockQuotation):
            content.append(InputRichBlockDivider())
        content.extend(gallery)
    if quoted:
        if gallery or (content and isinstance(content[-1], InputRichBlockBlockQuotation)):
            content.append(InputRichBlockDivider())
        content.append(quoted)
    if tags := _tags(getattr(result, "tags", None) or ()):
        content.append(tags)
    if content:
        if blocks:
            blocks.append(InputRichBlockDivider())
        blocks.extend([InputRichBlockDetails(f"⚠️ {hide_content}", content)] if hide_content else content)
    if footer := _footer(
        result, raw_url=raw_url or getattr(result, "raw_url", ""), lang=lang,
        view_label=view_label, progress=progress, hide_source=config.hide_source,
    ):
        if blocks:
            blocks.append(InputRichBlockDivider())
        blocks.append(footer)
    return blocks
