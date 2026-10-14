import asyncio
import json
import types
from unittest.mock import AsyncMock, patch

import pytest
from pyrogram.parser import Parser

from plugins.helpers import build_caption_by_str, build_metadata_line, build_rich_markdown
from services.cache import CacheEntry, CacheParseResult, PersistentCache


def _time_tag(ts: int, text: str) -> str:
    """页脚时间那段是个 tg-time 实体 (客户端按本地时区渲染), 标签内文字是兜底。"""
    return f'<tg-time unix="{ts}" format="Dt">{text}</tg-time>'


@pytest.mark.parametrize(
    ("author", "versioned", "hit"),
    [("", False, False), ("Author", False, True), ("", True, True), ("Author", True, True)],
)
def test_cache_refreshes_only_legacy_entries_missing_author(author, versioned, hit):
    payload = {
        "parse_result": {
            "title": "T",
            "author_name": author,
            # 当前条目格式带的字段（缺 markdown_content 会被当成旧条目重新解析,
            # 那个判据由 test/test_cached_rich_rendering.py 单独钉住）
            "markdown_content": "",
            "published_at": None,
            "view_count": None,
            "like_count": None,
            "meta_fields": 1,
            "tags": [],
            "quoted_media_count": 0,
            "reply_media_count": 0,
        }
    }
    if versioned:
        payload["author_metadata_version"] = 1
    with patch("services.cache.get_optional", AsyncMock(return_value=json.dumps(payload))):
        result = asyncio.run(PersistentCache().get("https://www.threads.com/@user/post/Abc"))
    assert (result is not None) == hit


def test_cache_refreshes_entries_written_before_metadata_line():
    """没有统计字段的缓存要重新解析, 否则同一链接第二次发送就少了时间/浏览量那行"""
    payload = {"parse_result": {"title": "T", "author_name": "Author", "is_sensitive": False}}
    payload["author_metadata_version"] = 1
    with patch("services.cache.get_optional", AsyncMock(return_value=json.dumps(payload))):
        result = asyncio.run(PersistentCache().get("https://www.threads.com/@user/post/Abc"))
    assert result is None


def test_cache_refreshes_entries_written_before_like_count():
    """没有 like_count 字段的缓存要重新解析, 否则页脚缺点赞那段"""
    payload = {
        "parse_result": {
            "title": "T",
            "author_name": "Author",
            "published_at": None,
            "view_count": None,
            "tags": [],
        },
        "author_metadata_version": 1,
    }
    with patch("services.cache.get_optional", AsyncMock(return_value=json.dumps(payload))):
        result = asyncio.run(PersistentCache().get("https://www.threads.com/@a/post/x"))
    assert result is None


def test_metadata_line_includes_like_count():
    """点赞数按平台给的值渲染, 拿不到就不显示那一段"""
    import datetime

    line = build_metadata_line(
        published_at=datetime.datetime(2026, 10, 3, 11, 0, tzinfo=datetime.UTC),
        view_count=4984,
        like_count=158,
        lang="zh-hans",
        view_label="查看",
        like_label="点赞",
    )
    assert line == f"{_time_tag(1791025200, '2026年10月3日 19:00')} · 4,984 查看 · 158 点赞"


def test_metadata_line_with_like_only():
    """threads 拿不到浏览量但有点赞: 只显示点赞, 不留空占位"""
    line = build_metadata_line(view_count=None, like_count=303, lang="zh-hans", view_label="查看", like_label="点赞")
    assert line == "303 点赞"
    assert " ·  · " not in line


def test_cache_refreshes_entries_written_before_tags():
    """没有 tags 字段的缓存要重新解析, 否则 inline 结果缺 tag 行"""
    payload = {
        "parse_result": {"title": "T", "author_name": "Author", "published_at": None, "view_count": None},
        "author_metadata_version": 1,
    }
    with patch("services.cache.get_optional", AsyncMock(return_value=json.dumps(payload))):
        result = asyncio.run(PersistentCache().get("https://www.pixiv.net/artworks/1"))
    assert result is None


def test_new_authorless_cache_is_versioned():
    entry = CacheEntry(parse_result=CacheParseResult())
    payload = entry.model_dump(mode="json")
    assert payload["author_metadata_version"] == 1
    assert "author_metadata_version" in CacheEntry.model_validate(payload).model_fields_set


@pytest.mark.parametrize(
    "author",
    ["Author", "A & B <test>", "\u30d6\u30eb\u30fc\u30a2\u30fc\u30ab\u30a4\u30d6\u516c\u5f0f", "Author \U0001f600"],
)
def test_author_is_bold_and_html_escaped(author):
    caption = build_caption_by_str("", "Body", "https://www.threads.com/@user/post/Abc", author_name=author)
    parsed = asyncio.run(Parser(None).parse(caption))
    assert parsed["message"].startswith(author + ":\n\n")
    length = len((author + ":").encode("utf-16-le")) // 2
    assert any(
        type(e).__name__ == "MessageEntityBold" and e.offset == 0 and e.length == length for e in parsed["entities"]
    )


# ── 统计行 ────────────────────────────────────────────────────


def test_metadata_line_zh_format():
    """中文按 X 的观感: 时间 · 中文日期 · 千分位浏览量"""
    line = build_metadata_line(
        published_at=__import__("datetime").datetime(2026, 10, 3, 11, 0, tzinfo=__import__("datetime").UTC),
        view_count=1455,
        lang="zh-hans",
        view_label="查看",
    )
    assert line == f"{_time_tag(1791025200, '2026年10月3日 19:00')} · 1,455 查看"


def test_metadata_line_uses_24_hour_clock():
    """24 小时制: 兜底文字与 API 的短时间格式 (``t``) 都不带 上午/下午

    真正呈现由客户端本地化 —— 12/24 小时制跟随用户设备设置。
    """
    import datetime

    morning = build_metadata_line(
        published_at=datetime.datetime(2026, 10, 3, 3, 45, tzinfo=datetime.UTC), lang="zh-hans"
    )
    assert morning == _time_tag(1790999100, "2026年10月3日 11:45")

    midnight = build_metadata_line(
        published_at=datetime.datetime(2026, 10, 3, 16, 5, tzinfo=datetime.UTC), lang="zh-hans"
    )
    assert midnight == _time_tag(1791043500, "2026年10月4日 00:05")
    assert "上午" not in morning + midnight
    assert "下午" not in morning + midnight


def test_metadata_line_other_language_uses_numeric_datetime():
    import datetime

    line = build_metadata_line(
        published_at=datetime.datetime(2026, 10, 3, 11, 0, tzinfo=datetime.UTC),
        view_count=1455,
        lang="en-us",
        view_label="views",
    )
    assert line == f"{_time_tag(1791025200, '2026-10-03 19:00')} · 1,455 views"


def test_metadata_line_without_views():
    """threads 拿不到浏览量: 只显示时间, 不能留空占位"""
    import datetime

    line = build_metadata_line(
        published_at=datetime.datetime(2026, 10, 3, 11, 0, tzinfo=datetime.UTC), view_count=None, lang="zh-hans"
    )
    assert line == _time_tag(1791025200, "2026年10月3日 19:00")
    assert " ·  · " not in line


def test_metadata_line_empty_when_nothing_available():
    assert build_metadata_line() == ""


def test_caption_places_metadata_before_source():
    caption = build_caption_by_str(
        "Title", "Body", "https://x.com/u/status/1", author_name="Author", metadata_line="19:00 · 2026年10月3日"
    )
    assert "Body" in caption
    assert caption.index("2026年10月3日") < caption.index("来源")
    assert caption.index("Author") < caption.index("2026年10月3日")


# ── 富文本正文 ────────────────────────────────────────────────


def test_rich_markdown_puts_metadata_and_source_in_footer():
    """统计与来源都在页尾 footer 里, 正文保持原文排版"""
    from datetime import UTC, datetime

    from parsehub.types import MultimediaParseResult, Platform

    result = MultimediaParseResult(content="# 标题\n\n正文**粗体**\n\n- 列表项")
    result.platform = Platform.TWITTER
    result.raw_url = "https://x.com/u/status/1"
    result.author_name = "Author"
    result.published_at = datetime(2026, 10, 3, 11, 0, tzinfo=UTC)
    result.view_count = 1455

    markdown = build_rich_markdown(
        result, config=types.SimpleNamespace(hide_title=False, hide_desc=False, hide_source=False),
        lang="zh-hans", view_label="查看",
    )

    # 作者行: 不加冒号 (用户要求「取消冒号」)
    assert markdown.startswith("**Author**")
    assert "正文**粗体**" in markdown
    assert "- 列表项" in markdown
    assert "---" in markdown
    # 统计与来源在 footer, 且位于正文之后
    footer = markdown.split("<footer>")[1]
    assert _time_tag(1791025200, "2026年10月3日 19:00") in footer
    assert "1,455 查看" in footer
    # footer 里 markdown 链接语法不生效, 必须用 HTML 的 a href
    assert '<a href="https://x.com/u/status/1">来源（Twitter）</a>' in footer
    assert markdown.index("- 列表项") < markdown.index("<footer>")


def test_rich_markdown_author_label_with_handle():
    """作者名带上用户名: "名字 @handle"; 名字与用户名相同时只写 @handle"""
    from parsehub.types import MultimediaParseResult, Platform

    config = types.SimpleNamespace(hide_title=False, hide_desc=False, hide_source=False)
    result = MultimediaParseResult(content="正文")
    result.platform = Platform.PIXIV
    result.raw_url = "https://www.pixiv.net/artworks/1"
    result.author_name = "隣人X"
    result.author_handle = "user_ydyj5227"
    assert build_rich_markdown(result, config=config).startswith("**隣人X @user_ydyj5227**")

    only_handle = MultimediaParseResult(content="正文")
    only_handle.platform = Platform.THREADS
    only_handle.raw_url = "https://www.threads.com/@same/post/x"
    only_handle.author_name = "same"
    only_handle.author_handle = "same"
    assert build_rich_markdown(only_handle, config=config).startswith("**@same**")


def test_rich_markdown_wraps_multiple_media_in_collage():
    """多张媒体必须包在 <tg-collage> 里才是图集, 否则显示成各自独立的图"""
    from parsehub.types import ImageParseResult, Platform

    result = ImageParseResult(content="图集", photo=[])
    result.platform = Platform.PIXIV
    result.raw_url = "https://www.pixiv.net/artworks/1"

    markdown = build_rich_markdown(
        result,
        config=types.SimpleNamespace(hide_title=False, hide_desc=False, hide_source=False),
        media_placeholders=["![](tg://photo?id=m0)", "![](tg://photo?id=m1)"],
    )
    assert "<tg-collage>" in markdown
    assert "</tg-collage>" in markdown
    assert markdown.index("tg://photo?id=m0") < markdown.index("tg://photo?id=m1") < markdown.index("</tg-collage>")


def test_rich_markdown_single_media_is_not_wrapped():
    """单张媒体不需要图集包裹"""
    from parsehub.types import ImageParseResult, Platform

    result = ImageParseResult(content="单图", photo=[])
    result.platform = Platform.PIXIV
    result.raw_url = "https://www.pixiv.net/artworks/1"

    markdown = build_rich_markdown(
        result,
        config=types.SimpleNamespace(hide_title=False, hide_desc=False, hide_source=False),
        media_placeholders=["![](tg://photo?id=m0)"],
    )
    assert "<tg-collage>" not in markdown
    assert "tg://photo?id=m0" in markdown


def test_rich_markdown_renders_tags():
    """平台给的标签渲染成 #标签 一行, 排在标题下面 (正文之前)"""
    from parsehub.types import ImageParseResult, Platform

    result = ImageParseResult(content="正文", photo=[])
    result.platform = Platform.PIXIV
    result.raw_url = "https://www.pixiv.net/artworks/1"
    result.tags = ["AI画像", "足裏"]

    markdown = build_rich_markdown(
        result, config=types.SimpleNamespace(hide_title=False, hide_desc=False, hide_source=False)
    )
    # 渲染成标签页链接; 链接里的 # 不转义 (行首是 <a 不会是标题, 转义会在复制时露出反斜杠)
    assert '<a href="https://www.pixiv.net/tags/AI%E7%94%BB%E5%83%8F">#AI画像</a>' in markdown
    assert '<a href="https://www.pixiv.net/tags/%E8%B6%B3%E8%A3%8F">#足裏</a>' in markdown
    # 标签排在正文之后
    assert markdown.index("正文") < markdown.index("AI画像")


def test_author_handle_links_to_profile():
    """@handle 渲染成作者主页链接 (富文本里必须用 HTML a 标签)"""
    from parsehub.types import ImageParseResult, Platform

    result = ImageParseResult(content="正文", photo=[])
    result.platform = Platform.PIXIV
    result.raw_url = "https://www.pixiv.net/artworks/1"
    result.author_name = "隣人X"
    result.author_handle = "user_ydyj5227"
    result.author_url = "https://www.pixiv.net/users/123568955"

    markdown = build_rich_markdown(
        result, config=types.SimpleNamespace(hide_title=False, hide_desc=False, hide_source=False)
    )
    # 名字可点, @handle 是等宽下角标 (不再是可点的 Mention)
    assert '<a href="https://www.pixiv.net/users/123568955">隣人X</a>' in markdown
    assert "<code>@user_ydyj5227</code>" in markdown
    assert "隣人X" in markdown


def test_author_without_profile_url_stays_plain():
    """没有主页地址时保持纯文本, 不留空链接"""
    from parsehub.types import ImageParseResult, Platform

    result = ImageParseResult(content="正文", photo=[])
    result.platform = Platform.PIXIV
    result.raw_url = "https://www.pixiv.net/artworks/1"
    result.author_name = "某人"
    result.author_handle = "someone"

    markdown = build_rich_markdown(
        result, config=types.SimpleNamespace(hide_title=False, hide_desc=False, hide_source=False)
    )
    author_line = next(line for line in markdown.splitlines() if "@someone" in line)
    assert "<a href=" not in author_line


def test_media_sits_above_the_quote_card():
    """主推自己的媒体要排在引用卡片之上: 正文 → 标签 → 媒体 → 引用"""
    from parsehub.types import ImageParseResult, Platform

    result = ImageParseResult(content="主推正文", photo=[])
    result.platform = Platform.TWITTER
    result.raw_url = "https://x.com/a/status/1"
    result.content = "主推正文\n\n> 引用 某人 @other：\n> 被引用的内容"

    markdown = build_rich_markdown(
        result,
        config=types.SimpleNamespace(hide_title=False, hide_desc=False, hide_source=False),
        media_placeholders=["![](tg://photo?id=m0)"],
    )
    assert markdown.index("主推正文") < markdown.index("tg://photo?id=m0") < markdown.index("被引用的内容")


def test_reply_quote_stays_above_the_body():
    """回复块仍在正文之前 (只有尾部引用块会后移)"""
    from plugins.helpers import split_trailing_quote

    head, quote = split_trailing_quote("> 回复 a：\n> hi\n\n正文\n\n> 引用 b：\n> yo")
    assert head == "> 回复 a：\n> hi\n\n正文"
    assert quote == "> 引用 b：\n> yo"


def test_split_without_quote_returns_content_untouched():
    from plugins.helpers import split_trailing_quote

    assert split_trailing_quote("纯正文") == ("纯正文", "")
    assert split_trailing_quote("") == ("", "")


def test_tags_with_middle_dot_are_linked_not_bare_hashtags():
    """含 ``・`` 的长标签必须走链接。

    裸 hashtag 时 Telegram 遇到 ``・`` 就停止解析, 只染蓝到中点之前,
    看起来就是标签被截断 (实测 ``#アリサ・ミハイロヴナ・九条`` -> ``#アリサ``)。
    """
    from parsehub.types import ImageParseResult, Platform

    result = ImageParseResult(content="正文", photo=[])
    result.platform = Platform.PIXIV
    result.raw_url = "https://www.pixiv.net/artworks/1"
    result.tags = ["アリサ・ミハイロヴナ・九条"]

    markdown = build_rich_markdown(
        result, config=types.SimpleNamespace(hide_title=False, hide_desc=False, hide_source=False)
    )
    assert "<a href=" in markdown
    assert "アリサ・ミハイロヴナ・九条</a>" in markdown


def test_tags_fall_back_to_plain_text_without_tag_page():
    """平台没有标签页时退回纯文本 #标签"""
    from parsehub.types import ImageParseResult, Platform

    result = ImageParseResult(content="正文", photo=[])
    result.platform = Platform.COOLAPK
    result.raw_url = "https://www.coolapk.com/feed/1"
    result.tags = ["手机"]

    markdown = build_rich_markdown(
        result, config=types.SimpleNamespace(hide_title=False, hide_desc=False, hide_source=False)
    )
    assert "\\#手机" in markdown
    tag_line = next(line for line in markdown.splitlines() if "手机" in line)
    assert "<a href" not in tag_line


def test_tag_page_url_encodes_japanese():
    from parsehub.types import Platform

    from plugins.helpers import tag_page_url

    assert tag_page_url(Platform.PIXIV, "周防有希") == "https://www.pixiv.net/tags/%E5%91%A8%E9%98%B2%E6%9C%89%E5%B8%8C"
    assert tag_page_url(Platform.XHS, "x") == "" or "xiaohongshu" in tag_page_url(Platform.XHS, "x")
    assert tag_page_url(None, "x") == ""


def test_rich_markdown_places_media_between_body_and_footer():
    from parsehub.types import ImageParseResult, Platform

    result = ImageParseResult(content="图集", photo=[])
    result.platform = Platform.PIXIV
    result.raw_url = "https://www.pixiv.net/artworks/1"

    markdown = build_rich_markdown(
        result, config=types.SimpleNamespace(hide_title=False, hide_desc=False, hide_source=False),
        media_placeholders=["![](tg://photo?id=m0)", "![](tg://photo?id=m1)"],
    )
    assert markdown.index("图集") < markdown.index("tg://photo?id=m0") < markdown.index("tg://photo?id=m1")
    assert markdown.index("tg://photo?id=m1") < markdown.index("<footer>")


def test_rich_markdown_omits_missing_metadata():
    from parsehub.types import MultimediaParseResult, Platform

    result = MultimediaParseResult(content="无统计数据")
    result.platform = Platform.THREADS
    result.raw_url = "https://www.threads.com/@u/post/x"

    markdown = build_rich_markdown(
        result, config=types.SimpleNamespace(hide_title=False, hide_desc=False, hide_source=False),
        lang="zh-hans", view_label="查看",
    )
    assert "<footer>" in markdown
    assert "查看" not in markdown
    assert " ·  · " not in markdown


def test_rich_markdown_respects_hide_source():
    from parsehub.types import MultimediaParseResult, Platform

    result = MultimediaParseResult(content="正文")
    result.platform = Platform.TWITTER
    result.raw_url = "https://x.com/u/status/1"

    markdown = build_rich_markdown(
        result, config=types.SimpleNamespace(hide_title=False, hide_desc=False, hide_source=True)
    )
    assert "来源" not in markdown


def test_caption_without_metadata_is_unchanged():
    caption = build_caption_by_str("Title", "Body", "https://x.com/u/status/1", author_name="Author")
    assert "·" not in caption
