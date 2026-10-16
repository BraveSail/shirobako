"""原位媒体标记（``{{media:N}}``）渲染：lib 把正文图抽走时留标记，渲染层换成媒体占位符。

协议（双端冻结）:

- lib 在图片**原位置**放 ``{{media:N}}``（N = content 媒体列表的下标）；
- bot 在 ``build_rich_markdown`` 里替换成 ``![](tg://photo?id=mN)``；
- **没有媒体的路径**（inline 文字版、处理过程、纯文字兜底）→ 标记删除 + warning
  （**标记字面量绝不泄漏**，与 ``{{poll:N}}`` 同一纪律）；
- 被原位消费的媒体**从末尾追加列表摘掉**，否则同一张图会出现两次。

对照 ``test_poll_render.py`` 的协议测试（同一套模式）。
"""

import types

from plugins.helpers import (
    _consumed_media_indices,
    build_rich_markdown,
    replace_media_placeholders,
)

PH = ["![](tg://photo?id=m0)", "![](tg://photo?id=m1)", "![](tg://photo?id=m2)"]


# ── ① replace_media_placeholders 本体 ──────────────────────────────────


def test_replaces_marker_with_placeholder():
    out = replace_media_placeholders("前文\n\n{{media:1}}\n\n后文", PH)
    assert out == "前文\n\n![](tg://photo?id=m1)\n\n后文"


def test_placeholder_list_empty_deletes_marker():
    """没有媒体的路径 → 标记删除（绝不泄漏字面量）。"""
    out = replace_media_placeholders("前文\n\n{{media:0}}\n\n后文", [])
    assert "{{media" not in out
    assert "前文" in out and "后文" in out


def test_out_of_range_index_deletes_marker():
    out = replace_media_placeholders("{{media:9}}", PH)
    assert "{{media" not in out


def test_marker_inside_quote_keeps_prefix():
    """引用块内的标记 → 占位符留在块内（' > ' 前缀原样保留）。"""
    out = replace_media_placeholders("> {{media:2}}", PH)
    assert out == "> ![](tg://photo?id=m2)"


def test_marker_inline_in_text():
    """标记可以出现在行中（媒体占位符是内联结构）。"""
    out = replace_media_placeholders("文字{{media:0}}文字", PH)
    assert out == "文字![](tg://photo?id=m0)文字"


def test_no_marker_is_noop():
    content = "没有标记的正文"
    assert replace_media_placeholders(content, PH) == content
    assert replace_media_placeholders(content, []) == content


def test_consumed_indices():
    assert _consumed_media_indices("{{media:0}}\n\n{{media:2}}") == {0, 2}
    assert _consumed_media_indices("无标记") == set()


# ── ② build_rich_markdown 集成: 原位渲染 + 末尾不重复 ──────────────────


def _config():
    return types.SimpleNamespace(hide_title=False, hide_desc=False, hide_source=False)


def _parse_result(content, **overrides):
    fields = {
        "title": "标题",
        "content": content,
        "raw_url": "https://www.douyin.com/article/1",
        "author_name": "作者",
        "author_handle": "",
        "author_url": "",
        "published_at": None,
        "view_count": None,
        "like_count": None,
        "tags": [],
        "hashtags": [],
        "platform": None,
        "polls": [],
        "is_sensitive": False,
        "markdown_content": content,
    }
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


def test_media_rendered_in_place_not_appended():
    """图片渲染在正文原位，且**不会**在末尾再出现一次。"""
    md = build_rich_markdown(
        _parse_result("前面\n\n{{media:0}}\n\n后面"),
        config=_config(),
        lang="zh-hans",
        media_placeholders=PH,
    )
    assert "![](tg://photo?id=m0)" in md
    assert md.count("![](tg://photo?id=m0)") == 1
    # 未消费的媒体仍在末尾
    assert md.count("![](tg://photo?id=m1)") == 1
    assert md.count("![](tg://photo?id=m2)") == 1
    # 原位: 占位符在 前面 与 后面 之间
    assert md.index("前面") < md.index("![](tg://photo?id=m0)") < md.index("后面")


def test_all_media_consumed_leaves_nothing_appended():
    """全部图都被原位消费 → 末尾不再追加（也没有空段落残留）。"""
    md = build_rich_markdown(
        _parse_result("A\n\n{{media:0}}\n\n{{media:1}}\n\n{{media:2}}"),
        config=_config(),
        lang="zh-hans",
        media_placeholders=PH,
    )
    assert md.count("![](tg://photo?id=m0)") == 1
    assert md.count("![](tg://photo?id=m1)") == 1
    assert md.count("![](tg://photo?id=m2)") == 1


def test_marker_deleted_when_no_media_passed():
    """不带媒体的路径（如处理过程）→ 标记被删掉，绝不泄漏。"""
    md = build_rich_markdown(
        _parse_result("正文\n\n{{media:0}}\n\n更多"),
        config=_config(),
        lang="zh-hans",
    )
    assert "{{media" not in md


def test_partial_consumption_keeps_rest_at_tail():
    """部分消费: 原位放 0, 其余（1,2）还在末尾。"""
    md = build_rich_markdown(
        _parse_result("正文\n\n{{media:0}}"),
        config=_config(),
        lang="zh-hans",
        media_placeholders=PH,
    )
    assert md.count("![](tg://photo?id=m0)") == 1
    assert "![](tg://photo?id=m1)" in md
    assert "![](tg://photo?id=m2)" in md
