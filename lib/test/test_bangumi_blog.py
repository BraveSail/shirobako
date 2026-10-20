"""bgm.tv（Bangumi）日志解析。

**为什么抓网页**：官方 API（``api.bgm.tv``，OAS 46 个端点）**没有日志** ——
``/v0/blogs/<id>`` 实测 404、``/blog/<id>.json`` 返回 0 字节。

**BBCode → HTML 的权威映射**来自站内指南 ``bgm.tv/help/bbcode``（那页本身就是渲染器渲染的）；
真实样本里 ``[mask]`` 很罕见（20 条抽样 0 条），所以 mask/下划线/删除线那几条用**按指南构造的
HTML** 钉住，其余用真实页面 fixture 钉住。

fixture：

- ``bangumi_blog_381120.html`` —— 真实日志（作者是**数字 uid**，有标签、有 1 张图）
- ``bangumi_blog_381269.html`` —— 真实日志（作者是**用户名 slug**，无图无标签）
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from parsehub.parsers.parser.bangumi import BangumiParser, BangumiParseResult
from parsehub.provider_api.bangumi import BGM_TIMEZONE, BangumiBlog, BangumiError

FIXTURES = Path(__file__).parent / "fixtures"


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _blog(name: str, blog_id: str) -> BangumiBlog:
    return BangumiBlog._from_html(_html(name), blog_id)


def _parse(entry_html: str, blog_id: str = "1") -> BangumiBlog:
    """按 BBCode 指南的渲染形态**构造**一篇日志（用来钉罕见的标签）。"""
    page = f"""<html><body><div id="viewEntry">
      <div class="author">
        <a class="avatar l" href="/user/tester"><img class="avatar" src="//lain.bgm.tv/u.jpg"/></a>
        <div class="title"><p><a class="avatar l" href="/user/tester">测试者</a></p></div>
      </div>
      <div class="header">
        <h1 class="title">标题</h1>
        <div class="time">2026-10-4 19:13 · 1 分钟阅读</div>
        <div class="tags"></div>
      </div>
      <div id="entry_content" class="content">{entry_html}</div>
    </div></body></html>"""
    return BangumiBlog._from_html(page, blog_id)


# ---------------------------------------------------------------- 真实页面

def test_the_metadata_comes_off_the_real_page():
    """**核心**: 标题 / 作者 / 时间 / 标签都能从真实页面取到"""
    blog = _blog("bangumi_blog_381120.html", "381120")
    assert blog.title == "【学生会也有洞！】ep1观后感"
    assert blog.author_name == "HuangfengXwX"
    assert blog.author_handle == "950407"  # 数字 uid
    # 页面上的时间是**北京时间**（见下面的 test_the_time_is_beijing_time）；阅读时长被剥掉
    assert blog.published_at == datetime(2026, 10, 4, 19, 13, tzinfo=BGM_TIMEZONE)
    assert blog.tags == ["动画"]


def test_the_time_is_beijing_time_not_utc():
    """**核心**: 页面上的时间按**北京时间**解释（用户报「时间好像有问题？多 8 小时」）。

    bgm 的页面只给 ``2026-10-4 19:13``（没有偏移）。按 UTC 解释的话，渲染出来
    （客户端按本地时区显示）会整整晚 8 小时。证据是 ``api.bgm.tv`` 返回的时间戳
    明确带偏移：``{"updated_at":"2026-10-07T01:18:56+08:00"}``。
    """
    blog = _blog("bangumi_blog_381120.html", "381120")
    assert blog.published_at is not None
    assert blog.published_at.utcoffset() == timedelta(hours=8)
    # 换算成 UTC 是 11:13 —— 按 UTC 解释会得到 19:13Z，那才是多 8 小时的来源
    assert blog.published_at.astimezone(UTC) == datetime(2026, 10, 4, 11, 13, tzinfo=UTC)


def test_a_missing_time_is_none_not_an_empty_string():
    """页面没有时间时是 ``None``（不是空串）—— 渲染层按 None 跳过那一段"""
    html = '<html><body><div class="header"><h1 class="title">没有时间</h1></div>'
    html += '<div id="entry_content">正文</div></body></html>'
    assert BangumiBlog._from_html(html, "1").published_at is None


def test_a_slug_author_is_read_too():
    """**核心**: 新用户的标识是**用户名 slug**（``/user/air_chika``）不是数字 ——

    只正则数字的话，新用户的作者名与主页会整个丢失（实测踩到）。
    """
    blog = _blog("bangumi_blog_381269.html", "381269")
    assert blog.author_name == "Air Chika"
    assert blog.author_handle == "air_chika"


def test_the_author_name_is_not_the_avatar_link():
    """作者块里第一个 ``/user/`` 链接是**头像**（里面只有 img）——
    取到它会得到空名字，必须挑那个有文字的
    """
    blog = _blog("bangumi_blog_381120.html", "381120")
    assert blog.author_name  # 非空
    assert blog.author_avatar.startswith("https://")


def test_the_body_carries_the_text():
    blog = _blog("bangumi_blog_381120.html", "381120")
    assert "这集有点怪" in blog.markdown_content
    assert "这集有点怪" in blog.text_content


# ---------------------------------------------------------------- 图片与表情

def test_uploaded_images_become_media_and_leave_a_marker_in_place():
    """**核心**: 上传图抽出去当媒体，正文**原位置**留 ``{{media:N}}`` 标记。

    渲染层把标记换成媒体引用 ⇒ 图出现在**文章原位**。只抽不留标记的话，
    图会全堆到消息末尾（用户报「没有图片原位」）。
    """
    blog = _blog("bangumi_blog_381120.html", "381120")
    assert len(blog.images) == 1
    assert blog.images[0].url == "https://lain.bgm.tv/pic/photo/l/8c/7f/950407_3Dg38.jpg"
    assert "lain.bgm.tv/pic/photo" not in blog.markdown_content
    assert "{{media:0}}" in blog.markdown_content
    # 纯文本兜底里不能留协议标记（那是 lib ↔ 渲染层的东西）
    assert "{{media:" not in blog.text_content


def test_an_image_marker_lands_at_the_original_position():
    """标记夹在**原文那两段之间** —— 不是堆到正文末尾"""
    blog = _parse(
        '段一<br/>\n<br/>\r\n<img class="code" src="//lain.bgm.tv/pic/photo/l/a.jpg"/><br/>\n<br/>\r\n段二'
    )
    before, after = blog.markdown_content.split("{{media:0}}")
    assert "段一" in before, blog.markdown_content
    assert "段二" in after, blog.markdown_content


def test_two_images_get_increasing_indices_in_document_order():
    """多张图的下标按文档顺序递增（与 ``images`` 列表一一对应）"""
    blog = _parse(
        '<img class="code" src="//lain.bgm.tv/pic/photo/l/a.jpg"/>甲'
        '<img class="code" src="//lain.bgm.tv/pic/photo/l/b.jpg"/>乙'
    )
    assert [i.url.split("/")[-1] for i in blog.images] == ["a.jpg", "b.jpg"]
    assert blog.markdown_content.index("{{media:0}}") < blog.markdown_content.index("{{media:1}}")


def test_simplify_without_an_index_still_drops_the_image():
    """没给映射（老调用点）时退回删除 —— 绝不产出对不上下标的标记"""
    from bs4 import BeautifulSoup

    from parsehub.provider_api.bangumi import _simplify

    soup = BeautifulSoup('<div><img class="code" src="//lain.bgm.tv/pic/photo/l/a.jpg"/></div>', "lxml")
    node = soup.find("div")
    _simplify(node)
    assert "{{media:" not in str(node)


def test_a_protocol_relative_image_gets_https():
    """bgm 的图是协议相对的（``//lain.bgm.tv/…``）—— 不补 https 会被当站内路径"""
    assert _blog("bangumi_blog_381120.html", "381120").images[0].url.startswith("https://")


def test_smiles_become_text_and_not_media():
    """**核心**: 表情（``/img/smiles/…``，alt 形如 ``(bgm116)``）是**文字**不是图片。

    判错的话每条带表情的日志都会多出一堆 gif（用户定案：转成文本）。
    """
    blog = _parse('前 <img class="smile" alt="(bgm116)" src="/img/smiles/tv/93.gif"/> 后')
    assert blog.images == []
    assert "(bgm116)" in blog.markdown_content
    assert "smiles" not in blog.markdown_content


def test_a_smile_without_a_class_is_still_a_smile():
    """判据看 **src 路径**，不能只看 class（实测有表情 img 不带 class）"""
    blog = _parse('<img alt="(bgm1)" src="/img/smiles/tv/1.gif"/>')
    assert blog.images == []
    assert "(bgm1)" in blog.markdown_content


def test_a_photo_is_not_mistaken_for_a_smile():
    """反向：真图不带 smile 特征时仍要当媒体"""
    blog = _parse('<img class="code" src="//lain.bgm.tv/pic/photo/l/a.jpg"/>')
    assert len(blog.images) == 1


# ---------------------------------------------------------------- BBCode 形态


def test_mask_becomes_a_telegram_spoiler():
    """**核心**: ``[mask]``（马赛克）→ Telegram 原生剧透 ``||…||``。

    形态取自站内指南：``<span style="background-color:#555; color: #555; border:1px solid #555;">``。
    （``||spoiler||`` 在富文本里能渲染成 RichTextSpoiler —— 真机实测过。）
    """
    blog = _parse('前面<span style="background-color:#555; color: #555; border: 1px solid #555;">剧透内容</span>后面')
    assert "||剧透内容||" in blog.markdown_content
    assert "background-color" not in blog.markdown_content


def test_a_mask_spanning_a_line_break_keeps_the_spoiler_intact():
    """剧透是**行内**语法 —— 块内换行会把 ``||`` 破掉，所以换行要压成空格"""
    blog = _parse('<span style="background-color:#555; color:#555;">上半<br/>下半</span>')
    assert "||上半 下半||" in blog.markdown_content


def test_underline_and_strikethrough_are_kept():
    """``[u]`` / ``[s]`` 都是 span，只靠 style 区分"""
    blog = _parse(
        '<span style="text-decoration:underline;">下划线</span>'
        '与<span style="text-decoration:line-through;">删除线</span>'
    )
    assert "<u>下划线</u>" in blog.markdown_content
    assert "<s>删除线</s>" in blog.markdown_content


def test_bold_and_italic_are_plain_tags():
    """``[b]``→``<strong>``、``[i]``→``<em>``，但我们**输出 HTML** ``<b>``/``<i>``。

    用 markdown 的 ``**`` 在**引用块内不解析**（会字面显示星号）—— bgm 自己就会在楼中楼里
    插粗体的「某人 说:」（见 ``test_bangumi_group_topic.py``）。HTML 写法两处都生效。
    """
    blog = _parse("<strong>粗</strong>和<em>斜</em>")
    assert "<b>粗</b>" in blog.markdown_content
    assert "<i>斜</i>" in blog.markdown_content


def test_a_font_weight_span_is_also_bold():
    """编辑器会出 ``<span style="font-weight:bold;">``（指南里没有，但真实日志里有）"""
    blog = _parse('<span style="font-weight:bold;">粗体</span>')
    assert "<b>粗体</b>" in blog.markdown_content


def test_color_and_size_keep_the_text_and_drop_the_style():
    """富文本没有颜色与字号 —— **只保留文字**，别把 style 漏进正文"""
    blog = _parse('<span style="color:red">彩</span><span style="font-size:18pt">大</span>')
    assert "彩" in blog.markdown_content and "大" in blog.markdown_content
    assert "color" not in blog.markdown_content
    assert "font-size" not in blog.markdown_content


def test_a_quote_becomes_a_blockquote():
    """``[quote]`` → ``<div class="quote"><q>…</q></div>`` → markdown 引用块"""
    blog = _parse('<div class="quote"><q>被引用的内容</q></div>后面')
    assert any(line.startswith("> ") and "被引用的内容" in line for line in blog.markdown_content.splitlines()), (
        blog.markdown_content
    )


def test_links_are_html_anchors():
    """链接一律写成 HTML ``<a>``：markdown 链接在**引用块内不解析**（项目既有结论）"""
    blog = _parse('<a class="l" href="https://example.com/x" rel="nofollow">示例</a>')
    assert '<a href="https://example.com/x">示例</a>' in blog.markdown_content


def test_line_breaks_do_not_leave_whitespace_only_lines():
    """markdownify 把 ``<br/>`` 转成"行尾两空格 + 换行"，连着两个 br 会留下**只有空格的行**"""
    blog = _parse("第一行<br/><br/>第二行")
    assert not [line for line in blog.markdown_content.splitlines() if line and not line.strip()]


# ---------------------------------------------------------------- 错误场景

def test_a_missing_blog_raises_with_the_sites_own_wording():
    """日志不存在时**仍然是 HTTP 200**，页面写「呜咕，出错了 数据库中没有…」⇒ 判据是"没有 entry_content" """
    with pytest.raises(BangumiError) as exc:
        BangumiBlog._from_html("<html><body>呜咕，出错了 数据库中没有查询到该日志的信息</body></html>", "99999999")
    assert "不存在" in str(exc.value)


def test_an_unknown_page_shape_raises_a_different_message():
    """页面结构变了（改版）要能与"日志不存在"区分开 —— 否则排查时会往错方向查"""
    with pytest.raises(BangumiError) as exc:
        BangumiBlog._from_html("<html><body>完全没有相关标记</body></html>", "1")
    assert "改版" in str(exc.value) or "结构" in str(exc.value)


def test_the_blog_id_comes_out_of_the_url():
    assert BangumiBlog.get_id_by_url("https://bgm.tv/blog/381120") == "381120"
    assert BangumiBlog.get_id_by_url("https://bgm.tv/blog/381120?x=1") == "381120"


# ---------------------------------------------------------------- parser

def test_the_parser_matches_both_domains():
    """bgm.tv 与 bangumi.tv 两个域名都有人用"""
    assert BangumiParser.match("https://bgm.tv/blog/381120")
    assert BangumiParser.match("https://bangumi.tv/blog/381120")


def test_the_parser_does_not_match_other_bgm_paths():
    """条目页与小组首页不该被接走。

    小组话题（``/group/topic/<id>``）**是支持的**（见 ``test_bangumi_group_topic.py``）——
    它有另一条解析路径，因为页面与日志不同构。
    """
    assert BangumiParser.match("https://bgm.tv/group/topic/430000")
    assert not BangumiParser.match("https://bgm.tv/subject/400602")
    assert not BangumiParser.match("https://bgm.tv/group/fillgrids")


def test_the_result_requires_a_media_download():
    """图片已从正文抽走 —— 不置位的话流水线会跳过下载，图片彻底丢"""
    assert BangumiParseResult.requires_media_download is True


def test_the_parser_builds_a_result_from_a_stubbed_page(monkeypatch):
    """端到端（不联网）：页面 → 结果对象"""

    class _Resp:
        status_code = 200

        def __init__(self, text: str):
            self.content = text.encode("utf-8")

        def raise_for_status(self) -> None:
            pass

    class _Client:
        def __call__(self, **_kwargs):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, _url: str, **_kwargs):
            return _Resp(_html("bangumi_blog_381120.html"))

    from parsehub.provider_api import bangumi as mod

    monkeypatch.setattr(mod.http, "AsyncClient", _Client())
    result = asyncio.run(BangumiParser()._do_parse("https://bgm.tv/blog/381120"))

    assert result.title == "【学生会也有洞！】ep1观后感"
    assert result.author_handle == "950407"
    assert len(result.media) == 1
    assert "这集有点怪" in result.markdown_content


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
