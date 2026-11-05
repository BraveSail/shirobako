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
from datetime import UTC, datetime
from typing import TYPE_CHECKING

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
from plugins.helpers import _TABLE_ROW_RE

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


def _unescape_markdown(text: str) -> str:
    """还原 markdown 转义（``\\#`` → ``#``）。

    为什么需要: ``format_text`` 会给平台原文加反斜杠转义（那是给 **markdown 字符串
    通道**用的 —— 服务端解析 markdown 时会把 ``\\#`` 还原成 ``#``）。但富文本现在
    一律走 **blocks 通道**，那段文本被当成**字面量**塞进 ``RichText`` ——
    没人再做还原，用户就看到字面的 ``\\#C_Chat``。

    转义清单与 ``helpers._TG_RESERVED`` 一致（官方 18 个保留字符）。
    """
    if not text or "\\" not in text:
        return text
    return re.sub(r"\\([_\*\[\]\(\)~`>#\+\-=\|\{\}\.!])", r"\1", text)


def parse_inline(text: str) -> str | list:
    """把一行 markdown 拆成 Richtext 片段 (无格式时直接返回原字符串)。

    只处理正文里常见的几种行内标记; 链接单独处理 url 参数。

    ⚠️ 入口先做 :func:`_unescape_markdown`：这段文本来自 ``format_text``，它给
    平台原文加过 markdown 反斜杠转义。blocks 通道不解转义（那是 markdown 通道的
    事），不还原就会把反斜杠原样发出去。
    """
    if not text:
        return text
    text = _unescape_markdown(text)
    parts: list = []
    pos = 0
    while pos < len(text):
        best = None
        for pattern, cls in _INLINE_PATTERNS:
            m = pattern.search(text, pos)
            if m and (best is None or m.start() < best[0].start()):
                best = (m, cls)
        if best is None:
            parts.append(text[pos:])
            break
        m, cls = best
        if m.start() > pos:
            parts.append(text[pos : m.start()])
        if cls is RichTextDateTime:
            # 组: 1=unix, 2=format(可缺), 3=显示文字。format 缺省时传 None
            # (服务端会按默认形态渲染)
            label = html.unescape(m.group(3))
            stamp = datetime.fromtimestamp(int(m.group(1)), tz=UTC)
            parts.append(RichTextDateTime(parse_inline(label), stamp, m.group(2) or None))
        elif cls is RichTextUrl:
            if m.re.pattern.startswith("<a"):
                url, label = m.group(1), m.group(2)  # <a href="url">label</a>
            else:
                label, url = m.group(1), m.group(2)  # [label](url)
            parts.append(RichTextUrl(parse_inline(html.unescape(label)), url=html.unescape(url)))
        else:
            parts.append(cls(parse_inline(m.group(1))))
        pos = m.end()

    if len(parts) == 1 and isinstance(parts[0], str):
        return html.unescape(parts[0])
    return parts


# ── 正文 → blocks ────────────────────────────────────────────

_MEDIA_PLACEHOLDER_RE = re.compile(r"^!\[\]\(tg://(?:photo|video)\?id=([\w-]+)\)$")

_TABLE_SEPARATOR_CELL_RE = re.compile(r":?-{2,}:?")


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


def _quote_children(quoted: list[str], media_blocks: dict[str, InputRichBlock]) -> list[InputRichBlock]:
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

    for line in quoted:
        stripped = line.strip()
        if stripped in ("---", "***", "___"):
            flush_table()
            flush_text()
            children.append(InputRichBlockDivider())
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
    flush_table()
    flush_text()
    # 首尾多余的分隔线去掉 (块边界不需要线)
    while children and type(children[0]).__name__ == "InputRichBlockDivider":
        children.pop(0)
    while children and type(children[-1]).__name__ == "InputRichBlockDivider":
        children.pop()
    return children


def markdown_to_blocks(markdown: str, *, media_blocks: dict[str, InputRichBlock] | None = None) -> list[InputRichBlock]:
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
            rest = stripped[len("<details>"):]
            same_line = re.fullmatch(r"(.*)</details>\s*", rest, re.S)
            if same_line and "<details>" not in same_line.group(1):
                # 整个折叠块在同一行（``<details><summary>…</summary>x</details>``）
                chunk.append(same_line.group(1))
                i += 1
            else:
                depth = 1 + rest.count("<details>") - rest.count("</details>")
                chunk.append(rest)
                i += 1
                while i < len(lines) and depth > 0:
                    line = lines[i]
                    if depth == 1 and "</details>" in line:
                        chunk.append(line.split("</details>", 1)[0])
                        i += 1
                        depth = 0
                        break
                    chunk.append(line)
                    depth += line.count("<details>") - line.count("</details>")
                    i += 1
            inner_raw = "\n".join(chunk)
            sm = re.search(r"<summary>(.*?)</summary>", inner_raw, re.S)
            summary = sm.group(1).strip() if sm else ""
            body = inner_raw[sm.end() :] if sm else inner_raw
            # 递归: 内部可能是段落/媒体/图集/嵌套折叠 —— 媒体占位符照样要换成打码块
            blocks.append(
                InputRichBlockDetails(
                    parse_inline(summary),
                    markdown_to_blocks(body, media_blocks=media_blocks),
                )
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
                    markdown_to_blocks("\n".join(inner_lines).strip(), media_blocks=media_blocks)
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
            blocks.append(InputRichBlockBlockQuotation(_quote_children(quoted, media_blocks)))
            continue

        # 列表 (连续 - / * / 数字项合并成一个 list)
        if re.match(r"^\s*(?:[-*+]|\d+\.)\s+", line):
            flush_paragraph(paragraph)
            items: list[InputRichBlockListItem] = []
            while i < len(lines) and re.match(r"^\s*(?:[-*+]|\d+\.)\s+", lines[i]):
                item_text = re.sub(r"^\s*(?:[-*+]|\d+\.)\s+", "", lines[i])
                checked: bool | None = None
                if cm := re.match(r"^\[([ xX])\]\s*(.*)$", item_text):
                    checked = cm.group(1).lower() == "x"
                    item_text = cm.group(2)
                items.append(
                    InputRichBlockListItem(
                        [InputRichBlockParagraph(parse_inline(item_text))],
                        has_checkbox=True if checked is not None else None,
                        is_checked=checked,
                    )
                )
                i += 1
            blocks.append(InputRichBlockList(items))
            continue

        paragraph.append(line)
        i += 1

    flush_paragraph(paragraph)
    return blocks
