"""话题引用块里的封面：``> {{media:N}}`` 必须变成块内的图片子块。

## 用户报的现象

「图片应该在话题里面」—— 封面被当成了**块外的独立媒体**（排在消息末尾），
而他期望它像 X 上的卡片那样**嵌在话题引用块内部**（标题 → 图 → 摘要）。

## 机制

lib 在 `_trending_block` 里把封面写成 ``> {{media:0}}``（带引用前缀 ⇒ 属于块内）。
渲染层 `_quote_children` 遇到媒体占位会去 ``media_blocks`` 取对应块 append 进引用块，
于是 ``InputRichBlockBlockQuotation`` 的子块里出现 ``InputRichBlockPhoto``。

**关键契约**：调用方必须传 ``media_blocks``。不传时占位符被静默丢弃
（对照组：只有两个 Paragraph，图不见），且**不报错** —— 这是最容易漏的一环。
"""

from __future__ import annotations

from pyrogram.types import InputMediaPhoto, InputRichBlockPhoto

from plugins.parse.rich_blocks import markdown_to_blocks

#: 与 lib 侧 `TwitterParser._trending_block` 经 ``replace_media_placeholders``
#: 替换后的真实形态（标题 → 图 → 摘要 → 链接）。⚠️ 这里必须写**替换后**的
#: ``![](tg://photo?id=mN)`` —— ``{{media:N}}`` 是 lib 层标记，进 `markdown_to_blocks`
#: 之前已被渲染层换掉了；直接写 ``{{media:N}}`` 会匹配不上正则、被当普通文字。
TRENDING_MD = """> **井口由香在《周刊花花公子》的写真拍摄中展现了她成熟的魅力。**
>
> ![](tg://photo?id=m0)
> 人气声优井口由香在11月9日发售的《周刊Playboy》第47期中，于雨天酒店拍摄了一组写真。
>
> [查看原話題](https://x.com/i/trending/2120353200146411925)"""


def _photo() -> InputRichBlockPhoto:
    return InputRichBlockPhoto(InputMediaPhoto("AgAC"))


def _quote_children(blocks) -> list:
    for block in blocks:
        if type(block).__name__ == "InputRichBlockBlockQuotation":
            return getattr(block, "blocks", None) or []
    return []


class TestTrendingCoverInQuote:
    def test_placeholder_becomes_photo_inside_quote(self) -> None:
        blocks = markdown_to_blocks(TRENDING_MD, media_blocks={"m0": _photo()})
        children = _quote_children(blocks)
        assert children, "没有解析出引用块"
        assert any(type(c).__name__ == "InputRichBlockPhoto" for c in children), (
            "封面没有成为引用块内的媒体子块"
        )

    def test_photo_sits_between_title_and_subtitle(self) -> None:
        """与 X 卡片同形态：标题在上、图居中、摘要在下。"""
        blocks = markdown_to_blocks(TRENDING_MD, media_blocks={"m0": _photo()})
        children = _quote_children(blocks)
        kinds = [type(c).__name__ for c in children]
        photo_at = kinds.index("InputRichBlockPhoto")
        assert photo_at > 0, "图在摘要之后了"
        assert photo_at < len(kinds) - 1, "图在最后，摘要丢了"

    def test_missing_media_blocks_drops_photo_silently(self) -> None:
        """对照组：不传 media_blocks 时占位符被丢弃 —— 记下这个静默失效点。"""
        blocks = markdown_to_blocks(TRENDING_MD)
        children = _quote_children(blocks)
        assert not any(type(c).__name__ == "InputRichBlockPhoto" for c in children)

    def test_cover_does_not_leak_outside_quote(self) -> None:
        """图不应该出现在引用块**之外**（那正是用户报的“图在话题外面”）。"""
        blocks = markdown_to_blocks(TRENDING_MD, media_blocks={"m0": _photo()})
        outside = [
            b
            for b in blocks
            if type(b).__name__ in ("InputRichBlockPhoto",)
        ]
        assert not outside, "封面漏到引用块外了"

    def test_title_and_link_survive(self) -> None:
        blocks = markdown_to_blocks(TRENDING_MD, media_blocks={"m0": _photo()})
        children = _quote_children(blocks)
        assert len(children) >= 3
