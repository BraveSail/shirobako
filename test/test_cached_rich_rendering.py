"""缓存路径与现场路径必须渲染出**逐字相同**的东西。

用户报障：「https://linux.do/t/topic/2977838/16?u=libc.so.6 缓存丢格式了」

**取证**（同一条内容，两条路径逐字 diff）::

    现场（24 行）: **<a href=".../u/HatsuneMiku">Angel</a>** <code>@HatsuneMiku</code> · #16
                   > <i><a href=".../u/tophnanfong">Only Linux Can Do(OLCD)</a> <code>@tophnanfong</code> · #1</i>
                   >
                   > ---
                   >
                   > <i>国产手机芯片恐成最大赢家？</i>

    缓存（18 行）: **<a href=".../u/HatsuneMiku">Angel</a>** <code>@HatsuneMiku</code>
                   Only Linux Can Do(OLCD) @tophnanfong · #1
                   国产手机芯片恐成最大赢家？

**根因**：`RichTextParseResult.content` 是**派生属性**（= ``plaintext_content``，
由 ``markdown_content`` 经 HTML 转出来的**纯文本**），而 ``rich_cache_entry`` 存的正是
``content`` ⇒ 缓存里躺的是纯文本，富文本渲染拿它当正文（引用块、``<a>`` 链接全塌）。
缓存路径的 ``_RichFields.markdown_content`` 还是写死的 ``""``，所以永远走不到 markdown 分支。

**判据（本文件的核心）**：两条路径的产物**逐字相等**。任何"渲染要用的字段"漏传都会在这里炸，
比逐个字段断言可靠得多。
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

from parsehub.types.platform import Platform
from parsehub.types.result import MultimediaParseResult, RichTextParseResult

from plugins.helpers import build_rich_markdown
from plugins.parse.inline_rich import build_cached_rich_content, rich_cache_entry
from services.cache import FOOTER_META_FIELDS_VERSION, CacheEntry, CacheParseResult, PersistentCache

URL = "https://linux.do/t/topic/2977838/16"
RAW_URL = "https://linux.do/t/topic/2977838/16"

#: 真实形态：引用块（主楼上下文）+ 本层正文 + 楼层号 + 标签实体
_QUOTE_LINE = (
    '> <i><a href="https://linux.do/u/tophnanfong">Only Linux Can Do(OLCD)</a> '
    "<code>@tophnanfong</code> · #1</i>"
)
MARKDOWN = (
    _QUOTE_LINE
    + """
>
> ---
>
> <i>国产手机芯片恐成最大赢家？</i>

本层正文（有换行，也有 **粗体**）。
"""
)


class _Config:
    hide_title = False
    hide_desc = False
    hide_source = False


def _rich(**kwargs) -> RichTextParseResult:
    fields = {
        "title": "标题",
        "markdown_content": MARKDOWN,
        "author_name": "Angel",
        "author_handle": "HatsuneMiku",
        "author_url": "https://linux.do/u/HatsuneMiku",
        "position_label": "#16",
        "tags": ["纯水"],
        "hashtags": [],
    }
    fields.update(kwargs)
    result = RichTextParseResult(**fields)
    result.platform = Platform.LINUXDO
    result.raw_url = RAW_URL  # 现场路径从对象上读它（缓存路径从参数读）
    return result


def _render_live(result) -> str:
    """现场路径：直接拿 ParseResult 渲染。"""
    return build_rich_markdown(
        result,
        config=_Config(),
        lang="zh-hans",
        view_label="查看",
        media_placeholders=(),
        quote_media_placeholders=(),
        reply_media_placeholders=(),
    )


def _render_cached(result) -> str:
    """缓存路径：收成条目 → 从条目重建 → 渲染。"""
    entry = rich_cache_entry(result, media=[])
    markdown, _media, _blocks = build_cached_rich_content(
        entry, RAW_URL, lang="zh-hans", config=_Config(), view_label="查看"
    )
    return markdown


# ---------------------------------------------------------------- 核心：逐字相等


def test_a_rich_text_post_renders_identically_from_the_cache():
    """**核心**: 两条路径逐字相等 —— 任何漏传的渲染字段都会在这里炸"""
    result = _rich()
    assert _render_cached(result) == _render_live(result)


def test_the_quoted_block_survives_the_cache():
    """**用户报的那处**: 引用块（`>`、`<a>`、分割线）不能在缓存路径上塌成裸文字"""
    out = _render_cached(_rich())
    assert "> <i><a href=" in out, out
    assert "Only Linux Can Do(OLCD)" in out
    assert "\n>\n" in out, "引用块内的空行（分割线那段）丢了"


def test_the_cache_keeps_the_markdown_source_not_the_plaintext():
    """缓存里存的必须是 **markdown 源**，不是 ``content``（那是转出来的纯文本）"""
    result = _rich()
    entry = rich_cache_entry(result, media=[])
    assert entry.parse_result.markdown_content == MARKDOWN
    # 对照：content 是纯文本（引用块的 `>` 与链接都没了）—— 正是不能存它的原因
    assert ">" in entry.parse_result.markdown_content
    assert ">" not in entry.parse_result.content


def test_the_floor_label_survives_the_cache():
    """楼层号也要经缓存保留（渲染层接在作者行后）"""
    out = _render_cached(_rich())
    assert "· #16" in out, out


def test_the_hashtag_entities_survive_the_cache():
    """标签**实体**要经缓存保留 —— 缺了就退回正则（日文标点处会切错边界）"""
    result = _rich(hashtags=["纯水"])
    entry = rich_cache_entry(result, media=[])
    assert entry.parse_result.hashtags == ["纯水"]

    cached = _render_cached(result)
    assert "纯水" in cached
    assert cached == _render_live(result)


# ---------------------------------------------------------------- 退回路径（不回归）


def test_a_result_without_markdown_falls_back_to_its_content():
    """没有 markdown_content 时退回 content（非 RichText 平台就是这条）"""
    result = MultimediaParseResult(content="正文片段", author_name="作者")
    result.platform = Platform.TWITTER
    result.raw_url = RAW_URL
    entry = rich_cache_entry(result, media=[])
    assert entry.parse_result.markdown_content == ""
    assert _render_cached(result) == _render_live(result)


def test_a_rich_text_result_with_empty_markdown_falls_back():
    """RichText 但 markdown 为空 → 退回 content（与现场路径同一判断）"""
    result = _rich(markdown_content="")
    assert _render_cached(result) == _render_live(result)


def test_the_label_and_hashtags_default_to_empty_for_other_platforms():
    """没有位置概念/标签实体的平台 → 空值，行为与以前一致"""
    result = MultimediaParseResult(content="正文", author_name="作者")
    result.raw_url = RAW_URL
    entry = rich_cache_entry(result, media=[])
    assert entry.parse_result.position_label == ""
    assert entry.parse_result.hashtags == []


# ---------------------------------------------------------------- 旧条目失效


def test_an_entry_without_the_markdown_source_is_a_miss():
    """**核心**: 改动之前写下的条目里躺的是纯文本 —— 必须视为未命中并重新解析。

    只改代码不作废它，用户第二次发同一条链接仍然是塌的。
    """
    payload = {
        "parse_result": {
            "title": "旧条目",
            "content": "纯文本正文",
            "author_name": "作者",
            "published_at": "2026-10-07T00:00:00+00:00",
            "view_count": 1,
            "like_count": 1,
            "tags": [],
            "quoted_media_count": 0,
            "reply_media_count": 0,
        },
        "media": None,
        "rich": True,
    }
    fake = MagicMock()
    fake.get = AsyncMock(return_value=json.dumps(payload))
    fake.delete = AsyncMock(return_value=1)
    from services import redis_client

    with patch.object(redis_client, "get_redis", return_value=fake):
        assert asyncio.run(PersistentCache().get(URL)) is None


def test_a_current_entry_is_a_hit():
    """当前格式的条目照常命中（作废只针对旧格式）"""
    entry = CacheEntry(
        parse_result=CacheParseResult(
            title="新",
            content="纯文本",
            markdown_content="**markdown**",
            author_name="作者",
            published_at="2026-10-07T00:00:00+00:00",
            view_count=1,
            like_count=1,
            meta_fields=FOOTER_META_FIELDS_VERSION,
            tags=[],
            quoted_media_count=0,
            reply_media_count=0,
        ),
        media=None,
        rich=True,
    )
    payload = json.dumps(entry.model_dump(mode="json"), ensure_ascii=False)
    fake = MagicMock()
    fake.get = AsyncMock(return_value=payload)
    from services import redis_client

    with patch.object(redis_client, "get_redis", return_value=fake):
        assert asyncio.run(PersistentCache().get(URL)) is not None


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
