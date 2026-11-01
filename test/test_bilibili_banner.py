"""bilibili 图文动态的板头图（banner）：提到标题上方，用分割线隔开。

banner 在 ``media`` 列表**开头**（``banner_media_count`` 记数量），与
``quoted_media_count`` 从尾部切正好对称。渲染层把它放到最顶、后面跟一道 ``---``。
"""

import pytest

from plugins.helpers import build_rich_markdown


class _Cfg:
    """build_rich_markdown 需要的最小配置面。"""

    hide_title = False
    hide_desc = False
    hide_source = False
    video_cover = True


class _Result:
    """最小结果对象（渲染层只读字段）。"""

    title = "周榜标题"
    content = "正文第一段"
    raw_url = "https://t.bilibili.com/1"
    author_name = "名作之壁吧"
    author_handle = ""
    author_url = "https://space.bilibili.com/2859372"
    platform = None
    tags = []
    hashtags = []
    published_at = None
    view_count = None
    like_count = None
    reply_count = None
    markdown_content = ""
    position_label = ""
    topic_label = ""
    quote_roles = []
    origin_line = ""
    polls = []
    is_sensitive = False
    banner_media_count = 1


def _render(result, ph):
    return build_rich_markdown(
        result, config=_Cfg(), lang="zh-CN", view_label="查看", media_placeholders=ph
    )


def test_banner_sits_above_title_with_divider():
    """板头图在最顶，紧跟一道分割线，然后才是标题。"""
    md = _render(_Result(), ["![](tg://photo?id=m0)", "![](tg://photo?id=m1)"])
    lines = [ln for ln in md.splitlines() if ln.strip()]
    assert lines[0] == "![](tg://photo?id=m0)", "banner 必须在最上面"
    assert lines[1] == "---", "banner 与标题之间要有分割线"
    assert lines[2].startswith("# "), "分割线之后是标题"


def test_banner_marker_is_consumed_from_body_media():
    """banner 占用的那个占位符不能再出现在正文媒体里（否则同一张图发两遍）。"""
    md = _render(_Result(), ["![](tg://photo?id=m0)", "![](tg://photo?id=m1)"])
    assert md.count("tg://photo?id=m0") == 1
    assert md.count("tg://photo?id=m1") == 1


def test_no_banner_leaves_layout_unchanged():
    """没有 banner 的平台/内容：产物与引入这个字段之前逐字一致（标题在最前）。"""

    class NoBanner(_Result):
        banner_media_count = 0

    md = _render(NoBanner(), ["![](tg://photo?id=m0)"])
    assert md.lstrip().startswith("# 周榜标题")
    assert not md.lstrip().startswith("---")


def test_multiple_banner_pictures_become_a_collage():
    """多张 banner（图集式板头）要包成图集，不是竖排单图。"""

    class TwoBanner(_Result):
        banner_media_count = 2

    md = _render(TwoBanner(), ["![](tg://photo?id=m0)", "![](tg://photo?id=m1)"])
    assert "<tg-collage>" in md
    assert md.lstrip().startswith("<tg-collage>")
