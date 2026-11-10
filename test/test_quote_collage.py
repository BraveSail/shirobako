"""引用块里的 <tg-collage> 必须变成 Collage 块，不能当文本漏出去。

## 这个 bug 的实际形态

用户报「**<tg-collage> 原样显示出来了**」。原因是 ``_quote_children`` 判断
「这段要不要按结构拆」时只看三样（分隔线 / 媒体占位 / 表格行），**不认图集标签**。
于是整段引用内容（含标签）被并成一个纯文本段落，标签字面发给用户。

正文里的同一个标签是好的 —— 主循环有图集分支。差别就在引用块走的是
``_quote_children`` 这条独立通道。

## 为什么测试要按「块类型」断言

断言 markdown 里有 ``<tg-collage>`` 毫无意义（它本来就在），要断言的是
**转换之后**：引用块的 children 里出现 ``InputRichBlockCollage``，
且**任何块里都不再有字面标签**。
"""

from __future__ import annotations

from pyrogram.types import (
    InputRichBlockBlockQuotation,
    InputRichBlockCollage,
    InputRichBlockParagraph,
    InputRichBlockPhoto,
)

from plugins.parse.rich_blocks import markdown_to_blocks


def _media_blocks(n: int) -> dict:
    """造 n 个图片块，key 用 ``m0..m(n-1)``（与 tg://photo?id=mN 对应）。"""
    return {
        f"m{i}": InputRichBlockPhoto(f"![](tg://photo?id=m{i})")  # type: ignore[arg-type]
        for i in range(n)
    }


def _all_text(blocks) -> str:
    """递归收集所有块里的文本（用来断言「标签没有漏成文字」）。"""
    out: list[str] = []

    def walk(node) -> None:
        if isinstance(node, str):
            out.append(node)
            return
        if isinstance(node, (list, tuple)):
            for item in node:
                walk(item)
            return
        for attr in ("text", "blocks", "items"):
            value = getattr(node, attr, None)
            if value is not None:
                walk(value)

    walk(blocks)
    return " ".join(out)


MD = """正文开头

> <i>署名行</i>
>
> ---
>
> <i>引用正文</i>
>
> <tg-collage>
>
> ![](tg://photo?id=m0)
> ![](tg://photo?id=m1)
>
> </tg-collage>

正文结尾
"""


class TestCollageInsideQuote:
    def test_becomes_collage_block(self) -> None:
        blocks = markdown_to_blocks(MD, media_blocks=_media_blocks(2))
        quotes = [b for b in blocks if isinstance(b, InputRichBlockBlockQuotation)]
        assert len(quotes) == 1, "引用块应该被识别成一个容器块"

        children = quotes[0].blocks
        collages = [c for c in children if isinstance(c, InputRichBlockCollage)]
        assert len(collages) == 1, (
            f"引用块里应有 1 个 Collage，实际 children={[type(c).__name__ for c in children]}"
        )
        assert len(collages[0].blocks) == 2, "两张图都要在图集里"

    def test_no_literal_tag_leaks(self) -> None:
        """★ 用户报的就是这个：标签字面出现在消息里。"""
        blocks = markdown_to_blocks(MD, media_blocks=_media_blocks(2))
        leaked = _all_text(blocks)
        assert "<tg-collage>" not in leaked, f"开标签漏成了文本: {leaked[:200]!r}"
        assert "</tg-collage>" not in leaked, f"闭标签漏成了文本: {leaked[:200]!r}"

    def test_single_image_is_not_a_collage(self) -> None:
        """单张不成组（与 wrap_collage 同一口径）。"""
        md = """正文

> <tg-collage>
>
> ![](tg://photo?id=m0)
>
> </tg-collage>
"""
        blocks = markdown_to_blocks(md, media_blocks=_media_blocks(1))
        quotes = [b for b in blocks if isinstance(b, InputRichBlockBlockQuotation)]
        assert len(quotes) == 1
        children = quotes[0].blocks
        assert not any(isinstance(c, InputRichBlockCollage) for c in children)
        assert any(isinstance(c, InputRichBlockPhoto) for c in children)

    def test_orphan_close_tag_is_ignored(self) -> None:
        """只有闭标签（结构异常）也绝不能当文本发出去。"""
        md = """正文

> 一些文字
>
> </tg-collage>
"""
        blocks = markdown_to_blocks(md, media_blocks=_media_blocks(0))
        assert "</tg-collage>" not in _all_text(blocks)

    def test_plain_quote_still_becomes_paragraph(self) -> None:
        """没有结构行的普通引用保持老行为（整段一个段落）。"""
        md = """正文

> 第一行
> 第二行
"""
        blocks = markdown_to_blocks(md, media_blocks=_media_blocks(0))
        quotes = [b for b in blocks if isinstance(b, InputRichBlockBlockQuotation)]
        assert len(quotes) == 1
        children = quotes[0].blocks
        assert len(children) == 1
        assert isinstance(children[0], InputRichBlockParagraph)
