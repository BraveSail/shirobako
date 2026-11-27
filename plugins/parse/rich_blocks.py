"""富文本 blocks 路径: 敏感内容需要媒体打码时用它替代 markdown 路径。

为什么需要: 官方 Bot API 的 `InputRichBlockPhoto` 只有 photo/caption, **没有 spoiler**,
而 raw 的 `PageBlockPhoto` / `PageBlockVideo` 有 —— 所以「富文本 + 打码」只能自己构造
blocks。markdown 路径 (`InputRichBlockPhoto`) 打不了码, 敏感内容走那条路会泄露。

这里的转换器只处理我们自己产出的富文本正文结构 (标题/引用/列表/代码块/分隔线/页脚/媒体
占位), 行内格式解析 **粗体/斜体/行内代码/链接/删除线**。
"""

from __future__ import annotations

import html
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from pyrogram import raw
from pyrogram.types import (
    InputRichBlockBlockQuotation,
    InputRichBlockCollage,
    InputRichBlockDetails,
    InputRichBlockDivider,
    InputRichBlockExpandableBlockQuotation,
    InputRichBlockFooter,
    InputRichBlockList,
    InputRichBlockListItem,
    InputRichBlockParagraph,
    InputRichBlockPreformatted,
    InputRichBlockSectionHeading,
    InputRichBlockTable,
)
from pyrogram.types.input_content.input_rich_block import (
    InputRichBlock,
    _get_input_document,
    _get_input_photo,
    _write_caption,
)
from pyrogram.types.messages_and_media.rich_block import RichBlockTableCell
from pyrogram.types.messages_and_media.rich_text import (
    RichTextBold,
    RichTextCode,
    RichTextDateTime,
    RichTextItalic,
    RichTextSpoiler,
    RichTextStrikethrough,
    RichTextSubscript,
    RichTextSuperscript,
    RichTextUrl,
)

if TYPE_CHECKING:
    from pyrogram import Client
    from pyrogram.types import InputMediaPhoto, InputMediaVideo

#: 表格行判据与渲染层同源（``plugins.helpers._TABLE_ROW_RE``）——
#: 正文表格、引用块内表格、preserve_linebreaks 的跳过逻辑共用同一个定义。
from plugins.helpers import _LIST_CHECKBOX_RE, _LIST_ITEM_RE, _TABLE_ROW_RE, _markdown_list_items, _poll_display

logger = __import__("loguru").logger.bind(name="RichBlocks")


class SpoilerPhotoBlock(InputRichBlock):
    """等价于 pyrogram 的 InputRichBlockPhoto, 外加 spoiler (打码)。"""

    def __init__(self, photo: InputMediaPhoto, *, spoiler: bool = True, caption=None) -> None:
        super().__init__()
        self.photo = photo
        self.caption = caption
        self.spoiler = spoiler

    async def write(
        self, *, client: Client, chat_id: int | str | None = None, photos: list, documents: list
    ) -> raw.base.PageBlock:
        input_media = await self.photo.write(client=client, chat_id=chat_id)
        input_photo = await _get_input_photo(client, chat_id=chat_id, input_media=input_media)
        photos.append(input_photo)
        return raw.types.PageBlockPhoto(
            photo_id=input_photo.id,
            caption=await _write_caption(client, caption=self.caption),
            spoiler=self.spoiler,
        )


class SpoilerVideoBlock(InputRichBlock):
    """等价于 pyrogram 的 InputRichBlockVideo, 外加 spoiler (打码)。"""

    def __init__(self, video: InputMediaVideo, *, spoiler: bool = True, caption=None) -> None:
        super().__init__()
        self.video = video
        self.caption = caption
        self.spoiler = spoiler

    async def write(
        self, *, client: Client, chat_id: int | str | None = None, photos: list, documents: list
    ) -> raw.base.PageBlock:
        input_media = await self.video.write(client=client, chat_id=chat_id)
        input_document = await _get_input_document(client, chat_id=chat_id, input_media=input_media)
        documents.append(input_document)
        return raw.types.PageBlockVideo(
            video_id=input_document.id,
            caption=await _write_caption(client, caption=self.caption),
            spoiler=self.spoiler,
        )


# ── 行内格式 ──────────────────────────────────────────────────

#: 时间戳实体: ``<tg-time unix="…" format="…">显示文字</tg-time>``。
#:
#: **markdown 路径由服务端解析这个标签, blocks 路径必须自己认** —— 不认的话它会
#: 原样进 ``InputRichBlockFooter``, 用户看到的是字面的 ``<tg-time unix=…>``
#: (敏感内容走 blocks, 所以只有敏感推文的页脚会这样)。
_TIME_TAG_RE = re.compile(r'<tg-time\s+unix="(\d+)"(?:\s+format="([^"]*)")?\s*>(.*?)</tg-time>', re.S)

_INLINE_PATTERNS: list[tuple[re.Pattern, type]] = [
    # footer 里的来源链接用的是 HTML (markdown 链接语法在 footer 块里不解析)
    (re.compile(r'<a\s+href="([^"]+)"\s*>(.*?)</a>', re.S), RichTextUrl),
    (_TIME_TAG_RE, RichTextDateTime),
    # **footer 块里只有 HTML 标签解析, markdown 的 ** 会字面显示** (实测) ——
    # 阶段标签 (▎上传中...) 与引用块的斜体都写在 footer/正文里用 HTML 标签,
    # 所以 blocks 路径必须认它们 (不认就字面显示 "<b>"/"<i>")
    (re.compile(r"<b>(.*?)</b>", re.S), RichTextBold),
    (re.compile(r"<i>(.*?)</i>", re.S), RichTextItalic),
    # 作者行的 @用户名 = <sub>…</sub> (常规小字; 不再外裹 <code>); 块内引用块的署名也用它们
    (re.compile(r"<sub>(.*?)</sub>", re.S), RichTextSubscript),
    (re.compile(r"<sup>(.*?)</sup>", re.S), RichTextSuperscript),
    (re.compile(r"<code>(.*?)</code>", re.S), RichTextCode),
    (re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)"), RichTextUrl),  # [文字](链接)
    (re.compile(r"\*\*([^*\n]+)\*\*"), RichTextBold),
    (re.compile(r"`([^`\n]+)`"), RichTextCode),
    (re.compile(r"~~([^~\n]+)~~"), RichTextStrikethrough),
    # 行内遮罩: threads 的文字级遮罩会渲染成 `||文字||` (服务端解析成 RichTextSpoiler)。
    # 放在 ~~ 之后 —— 两者不冲突 (`~~` 会先吃掉删除线)。
    (re.compile(r"\|\|([^|\n]+)\|\|"), RichTextSpoiler),
    (re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)"), RichTextItalic),
]


#: ``format_text`` escapes Telegram reserved characters in literal source text.
_MARKDOWN_ESCAPE_RE = re.compile(r"\\([_*\[\]()~`>#+\-=|{}.!])")


def _literal_inline(text: str) -> str:
    return html.unescape(_MARKDOWN_ESCAPE_RE.sub(r"\1", text))


def parse_inline(text: str) -> str | list:
    """Recognize markup before decoding literal text; escapes are never delimiters."""
    if not text:
        return text
    # Preserve match indices. These placeholders never reach a text node.
    markup = _MARKDOWN_ESCAPE_RE.sub("\0\0", text)
    parts: list = []
    pos = 0
    while pos < len(text):
        best = None
        for pattern, cls in _INLINE_PATTERNS:
            m = pattern.search(markup, pos)
            if m and (best is None or m.start() < best[0].start()):
                best = (m, cls)
        if best is None:
            parts.append(_literal_inline(text[pos:]))
            break
        m, cls = best
        if m.start() > pos:
            parts.append(_literal_inline(text[pos : m.start()]))
        groups = [text[start:end] for start, end in m.regs[1:]]
        if cls is RichTextDateTime:
            unix, fmt, label = groups
            stamp = datetime.fromtimestamp(int(unix), tz=UTC)
            parts.append(RichTextDateTime(parse_inline(label), stamp, fmt or None))
        elif cls is RichTextUrl:
            if m.re.pattern.startswith("<a"):
                url, label = groups  # <a href="url">label</a>
            else:
                label, url = groups  # [label](url)
            parts.append(RichTextUrl(parse_inline(label), url=_literal_inline(url)))
        else:
            parts.append(cls(parse_inline(groups[0])))
        pos = m.end()

    if len(parts) == 1 and isinstance(parts[0], str):
        return parts[0]
    return parts


# ── 正文 → blocks ────────────────────────────────────────────

_MEDIA_PLACEHOLDER_RE = re.compile(r"^!\[\]\(tg://(?:photo|video)\?id=([\w-]+)\)$")

_TABLE_SEPARATOR_CELL_RE = re.compile(r":?-{2,}:?")
_DETAILS_TAG_RE = re.compile(r"</?details>")


def build_poll_blocks(polls: Sequence, index: int | None, lang: str) -> list[InputRichBlock]:
    if index is None or not 0 <= index < len(polls) or (display := _poll_display(polls[index], lang)) is None:
        return []
    heading, rows = display
    blocks: list[InputRichBlock] = [InputRichBlockParagraph(heading)] if heading else []
    blocks.append(InputRichBlockTable([
        [RichBlockTableCell(cell, is_header=i == 0) for cell in row] for i, row in enumerate(rows)
    ]))
    return blocks


# Outside an HTML quote boundary, require the exact persisted standalone token.
_LEGACY_POLL_LINE_RE = re.compile(r"(?P<open>> |<blockquote>)?\{\{poll:(?P<index>\d+)\}\}(?P<close></blockquote>)?")


class LegacyPollSlots:
    """Keep the compatibility layout as text, but insert poll blocks only at reserved body slots."""

    def __init__(self, polls: Sequence, lang: str):
        self._polls = polls
        self._lang = lang
        self._prefix = f"Poll{uuid4().hex}x"
        self._slots: dict[str, str] = {}

    def reserve(self, content: str) -> str:
        # The app's layout may escape punctuation and rewrite quotes. The alphanumeric
        # keys survive both and can only be resolved by this render's private map.
        lines = content.split("\n")
        fenced = False
        for i, line in enumerate(lines):
            if line.lstrip(" >\t").startswith("```"):
                fenced = not fenced
            elif not fenced and (match := _LEGACY_POLL_LINE_RE.fullmatch(line)):
                key = f"{self._prefix}{len(self._slots)}"
                self._slots[key] = match["index"]
                lines[i] = f"{match['open'] or ''}{key}{match['close'] or ''}"
        return "\n".join(lines)

    def __contains__(self, token: str) -> bool:
        return token in self._slots

    def render(self, token: str) -> list[InputRichBlock]:
        digits = self._slots[token]
        try:
            index = int(digits)
        except ValueError:
            logger.warning("投票占位符索引无效, 已删除该行")
            return []
        if index >= len(self._polls):
            logger.warning(f"投票占位符索引越界, 已删除该行: index={index}, polls={len(self._polls)}")
            return []
        blocks = build_poll_blocks(self._polls, index, self._lang)
        if not blocks:
            logger.warning(f"投票数据为空, 已删除占位符行: index={index}")
        return blocks


def _table_block(rows: list[str]) -> InputRichBlockTable | None:
    """markdown 表格行组 → ``Table`` 块; 没有 ``|---|`` 分隔行时返回 None。

    与主转换器同源判据: **必须有分隔行**才算表格 —— 正文里孤零零一行 ``| x |``
    不能被误判成表格。单元格里平台侧转义的 ``\\|`` 在这里还原（列分隔早已解析）。
    """
    candidate: list[list[str]] = []
    has_separator = False
    for row in rows:
        cells = [cell.strip().replace("\\|", "|") for cell in row.strip().strip("|").split("|")]
        if all(not cell or _TABLE_SEPARATOR_CELL_RE.fullmatch(cell) for cell in cells):
            has_separator = True
        else:
            candidate.append(cells)
    if not (has_separator and candidate):
        return None
    return InputRichBlockTable(
        [
            [RichBlockTableCell(parse_inline(cell), is_header=(row_index == 0)) for cell in cells]
            for row_index, cells in enumerate(candidate)
        ]
    )


def _quote_children(
    quoted: list[str], media_blocks: dict[str, InputRichBlock], poll_slots: LegacyPollSlots | None,
) -> list[InputRichBlock]:
    """引用块（``>`` 行）内的子块。

    四种行各有归属: 独立的分隔线 → ``Divider``（署名行与正文之间的那条线,
    与主帖同一形态）; 媒体占位 → 调用方给的媒体块（**以前这里会把它当普通文字,
    切 blocks 时引用块里的图就丢了**）; **markdown 表格行组 → ``Table`` 块**
    （投票等 —— 以前会被合并成段落, poll 表格在敏感内容里散架）; 其余合并成段落。

    没有分隔线、媒体、表格时保持老行为: **整段合成一个段落**，不改动既有渲染。
    """
    has_special = any(
        line.strip() in ("---", "***", "___")
        or _MEDIA_PLACEHOLDER_RE.match(line.strip())
        or _TABLE_ROW_RE.match(line.strip())
        or (poll_slots is not None and line.strip() in poll_slots)
        # 图集标签也是结构行：不认的话整段（含 `<tg-collage>`）会被并成一个
        # 纯文本段落，标签原样显示给用户（真机实测就是这样）
        or line.strip() in ("<tg-collage>", "</tg-collage>")
        for line in quoted
    )
    if not has_special:
        text = "\n".join(quoted).strip()
        return [InputRichBlockParagraph(parse_inline(text))] if text else []

    children: list[InputRichBlock] = []
    buf: list[str] = []
    table_rows: list[str] = []

    def flush_text() -> None:
        text = "\n".join(buf).strip()
        if text:
            children.append(InputRichBlockParagraph(parse_inline(text)))
        buf.clear()

    def flush_table() -> None:
        if not table_rows:
            return
        rows = list(table_rows)
        table_rows.clear()
        if table := _table_block(rows):
            children.append(table)
        else:
            # 像表格但没有分隔行 → 不是表格, 当普通文本 (与主循环的落点一致)
            buf.extend(rows)

    _qidx = 0
    while _qidx < len(quoted):
        line = quoted[_qidx]
        stripped = line.strip()
        if poll_slots is not None and stripped in poll_slots:
            if poll := poll_slots.render(stripped):
                flush_table()
                flush_text()
                children.extend(poll)
        elif stripped in ("---", "***", "___"):
            flush_table()
            flush_text()
            children.append(InputRichBlockDivider())
        elif stripped == "<tg-collage>":
            # 图集：把中间连续的媒体占位收成一个 Collage 块。
            # 不认这对标签的话它们会跟正文一起进 buf，标签原样显示给用户。
            flush_table()
            flush_text()
            collage: list[InputRichBlock] = []
            _qidx += 1
            while _qidx < len(quoted) and quoted[_qidx].strip() != "</tg-collage>":
                inner = quoted[_qidx].strip()
                if m2 := _MEDIA_PLACEHOLDER_RE.match(inner):
                    if (blk := media_blocks.get(m2.group(1))) is not None:
                        collage.append(blk)
                _qidx += 1
            # 此时 _qidx 指向 </tg-collage>（或越界）
            if len(collage) > 1:
                children.append(InputRichBlockCollage(collage))
            else:
                children.extend(collage)
        elif stripped == "</tg-collage>":
            # 没有开标签的孤立闭标签：忽略，绝不当文本
            pass
        elif m := _MEDIA_PLACEHOLDER_RE.match(stripped):
            flush_table()
            flush_text()
            if (block := media_blocks.get(m.group(1))) is not None:
                children.append(block)
        elif _TABLE_ROW_RE.match(stripped):
            flush_text()
            table_rows.append(line)
        else:
            flush_table()
            buf.append(line)
        _qidx += 1
    flush_table()
    flush_text()
    # 首尾多余的分隔线去掉 (块边界不需要线)
    while children and type(children[0]).__name__ == "InputRichBlockDivider":
        children.pop(0)
    while children and type(children[-1]).__name__ == "InputRichBlockDivider":
        children.pop()
    return children


def _list_block(
    lines: list[str], start: int, first: re.Match[str], media_blocks: dict[str, InputRichBlock],
    poll_slots: LegacyPollSlots | None,
) -> tuple[InputRichBlockList, int]:
    """Read siblings at one indentation; give each item's children to the block parser."""
    marker = first[2][-1]
    ranges, end = _markdown_list_items(lines, start, first)
    items: list[InputRichBlockListItem] = []
    for item_start, content_end, match in ranges:
        content_indent = len(match[0].expandtabs(4))
        text = lines[item_start][match.end():].rstrip()
        checked = None
        if checkbox := _LIST_CHECKBOX_RE.match(text):
            checked = checkbox[1].lower() == "x"
            text = text[checkbox.end():]
        item_lines = [text]
        for line in lines[item_start + 1:content_end]:
            body = line.lstrip(" \t")
            leading = line[:len(line) - len(body)].expandtabs(4)
            item_lines.append(leading[content_indent:] + body)
        # The converter emits nested lists on a new, indented line. A number
        # at the start of this item's own text is not a second list marker.
        children = markdown_to_blocks(
            "\n".join(item_lines), media_blocks=media_blocks, poll_slots=poll_slots, _allow_first_list=False,
        )
        items.append(
            InputRichBlockListItem(
                children or [InputRichBlockParagraph("")],
                has_checkbox=True if checked is not None else None,
                is_checked=checked,
                value=int(match[2][:-1]) if marker == "." else None,
                type="1" if marker == "." else None,
            )
        )
    return InputRichBlockList(items), end


def markdown_to_blocks(
    markdown: str, *, media_blocks: dict[str, InputRichBlock] | None = None,
    poll_slots: LegacyPollSlots | None = None, _allow_first_list: bool = True,
) -> list[InputRichBlock]:
    """把富文本 markdown 转成 blocks 列表。

    只覆盖我们自己生成的语法: 标题 / 段落 / 引用 / 列表 / 代码块 / 分隔线 / 页脚 / 媒体占位。
    媒体占位换成调用方给的 block (图片/视频, 可带 spoiler)。
    """
    media_blocks = media_blocks or {}
    blocks: list[InputRichBlock] = []
    lines = (markdown or "").split("\n")
    i = 0

    def flush_paragraph(buf: list[str]) -> None:
        if buf:
            # **不能 strip 每行**：平台原文常用行首空格做居中/对齐（实测 X 的动画
            # 预告：``◤　　　◥`` 边框内 11 个半角空格的标题）。blocks 通道的段落
            # 文本是**字面量**，服务端不解析 —— 缩进原样保留才对，这正是它比
            # markdown 字符串通道更靠谱的地方（那边行首 4 空格会被当代码块）。
            #
            # 只去掉**首尾空行**（段落分隔用的），中间每一行原样保留。
            while buf and not buf[0].strip():
                buf.pop(0)
            while buf and not buf[-1].strip():
                buf.pop()
            text = "\n".join(buf)
            if text.strip():
                blocks.append(InputRichBlockParagraph(parse_inline(text)))
            buf.clear()

    paragraph: list[str] = []
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 空行结束段落
        if not stripped:
            flush_paragraph(paragraph)
            i += 1
            continue

        if poll_slots is not None and stripped in poll_slots:
            if poll := poll_slots.render(stripped):
                flush_paragraph(paragraph)
                blocks.extend(poll)
            i += 1
            continue

        # 媒体占位
        if m := _MEDIA_PLACEHOLDER_RE.match(stripped):
            flush_paragraph(paragraph)
            block = media_blocks.get(m.group(1))
            if block is not None:
                blocks.append(block)
            i += 1
            continue

        # ── 折叠容器 ──
        # **markdown 路径由服务端解析这两个标签, blocks 路径必须自己认** ——
        # 不认的话标签会原样进段落, 用户看到字面的 <details>, 里面的内容也全散架。
        #
        # ⚠️ **必须按嵌套深度配对**: 折叠块里可以再嵌折叠块（PTT 那种「推文（261 條）」
        # 里面又套一个「展开全文」）。以前只找**第一个** ``</details>``，内层一闭合就
        # 判定结束 ⇒ 内层的闭合标签和外层的整段内容都漏进正文，用户看到字面的
        # ``</details>``。
        if stripped.startswith("<details>"):
            flush_paragraph(paragraph)
            chunk: list[str] = []
            depth = 1
            fragment = line.lstrip()[len("<details>"):]
            while i < len(lines):
                for tag in _DETAILS_TAG_RE.finditer(fragment):
                    depth += 1 if tag[0] == "<details>" else -1
                    if depth == 0:
                        chunk.append(fragment[:tag.start()])
                        # Keep the tail on this line as outer content.
                        lines[i] = fragment[tag.end():]
                        if not lines[i].strip():
                            i += 1
                        break
                if depth == 0:
                    break
                chunk.append(fragment)
                i += 1
                if i < len(lines):
                    fragment = lines[i]
            inner_raw = "\n".join(chunk)
            sm = re.search(r"<summary>(.*?)</summary>", inner_raw, re.S)
            summary = sm.group(1).strip() if sm else ""
            body = inner_raw[sm.end() :] if sm else inner_raw
            # 递归: 内部可能是段落/媒体/图集/嵌套折叠 —— 媒体占位符照样要换成打码块
            blocks.append(
                InputRichBlockDetails(
                    parse_inline(summary),
                    markdown_to_blocks(body, media_blocks=media_blocks, poll_slots=poll_slots),
                ),
            )
            continue

        if stripped.startswith("<blockquote expandable>"):
            flush_paragraph(paragraph)
            one_line = re.match(r"<blockquote expandable>(.*)</blockquote>\s*$", stripped, re.S)
            content = (
                one_line.group(1)
                if one_line
                else stripped[len("<blockquote expandable>") :].rsplit("</blockquote>", 1)[0]
            )
            # 块内换行在源 markdown 里是 <br> (真换行会被并成空格) —— 转成真换行后交给
            # RichText, 服务端才会分行
            for br in ("<br>", "<br/>", "<br />"):
                content = content.replace(br, "\n")
            blocks.append(InputRichBlockExpandableBlockQuotation(parse_inline(content)))
            i += 1
            continue

        # 引用块**容器**: 成员是输入块列表 (与 expandable 只吃 RichText 不同), 所以
        # 「引用块内嵌图 + 长文字用 details 折起」只能走这条 —— 见 helpers.render_folded_quote_card。
        if stripped.startswith("<blockquote>"):
            flush_paragraph(paragraph)
            inner_lines: list[str] = []
            one_line = re.match(r"<blockquote>(.*)</blockquote>\s*$", stripped, re.S)
            if one_line:
                inner_lines.append(one_line.group(1))
                i += 1
            else:
                inner_lines.append(stripped[len("<blockquote>") :])
                i += 1
                while i < len(lines) and "</blockquote>" not in lines[i]:
                    inner_lines.append(lines[i])
                    i += 1
                if i < len(lines):
                    inner_lines.append(lines[i].split("</blockquote>", 1)[0])
                    i += 1
            blocks.append(
                InputRichBlockBlockQuotation(
                    markdown_to_blocks("\n".join(inner_lines).strip(), media_blocks=media_blocks, poll_slots=poll_slots)
                )
            )
            continue

        # 图集: <tg-collage> 包住的若干媒体占位
        if stripped.startswith("<tg-collage>"):
            flush_paragraph(paragraph)
            items: list = []
            i += 1
            while i < len(lines) and "</tg-collage>" not in lines[i]:
                inner = lines[i].strip()
                if m := _MEDIA_PLACEHOLDER_RE.match(inner):
                    block = media_blocks.get(m.group(1))
                    if block is not None:
                        items.append(block)
                i += 1
            i += 1  # 跳过 </tg-collage>
            if len(items) > 1:
                blocks.append(InputRichBlockCollage(items))
            else:
                blocks.extend(items)
            continue

        # markdown 表格 (连续以 | 开头的行) —— 服务端只认 markdown 语法,
        # blocks 路径得自己转成 Table 块 (判据与构造见 _table_block)。
        # `>` 开头的行不是正文表格 —— 它们归下面的引用块分支 (剥前缀后交
        # _quote_children, 那里识别块内表格)。
        if _TABLE_ROW_RE.match(stripped) and not stripped.startswith(">"):
            look = i
            rows: list[str] = []
            while (
                look < len(lines)
                and _TABLE_ROW_RE.match(lines[look].strip())
                and not lines[look].strip().startswith(">")
            ):
                rows.append(lines[look])
                look += 1
            if table := _table_block(rows):
                flush_paragraph(paragraph)
                i = look
                blocks.append(table)
                continue
            # 不是表格 (没有分隔行): 落到下面按普通文本处理

        # 分隔线
        if stripped in ("---", "***", "___"):
            flush_paragraph(paragraph)
            blocks.append(InputRichBlockDivider())
            i += 1
            continue

        # 页脚
        if stripped.startswith("<footer>") and stripped.endswith("</footer>"):
            flush_paragraph(paragraph)
            blocks.append(InputRichBlockFooter(parse_inline(stripped[len("<footer>") : -len("</footer>")])))
            i += 1
            continue

        # 标题
        if hm := re.match(r"^(#{1,6})\s+(.*)$", stripped):
            flush_paragraph(paragraph)
            blocks.append(InputRichBlockSectionHeading(parse_inline(hm.group(2)), len(hm.group(1))))
            i += 1
            continue

        # 代码块
        if stripped.startswith("```"):
            flush_paragraph(paragraph)
            language = stripped[3:].strip() or None
            i += 1
            code: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1  # 跳过结束的 ```
            blocks.append(InputRichBlockPreformatted("\n".join(code), language=language))
            continue

        # 引用块 (连续 > 行合并)
        if stripped.startswith(">"):
            flush_paragraph(paragraph)
            quoted: list[str] = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quoted.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            blocks.append(InputRichBlockBlockQuotation(_quote_children(quoted, media_blocks, poll_slots)))
            continue

        if (i or _allow_first_list) and (item := _LIST_ITEM_RE.match(line)):
            flush_paragraph(paragraph)
            block, i = _list_block(lines, i, item, media_blocks, poll_slots)
            blocks.append(block)
            continue

        paragraph.append(line)
        i += 1

    flush_paragraph(paragraph)
    return blocks
