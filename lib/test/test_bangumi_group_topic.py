"""bgm.tv 讨论话题（``/group/topic/<id>`` 与 ``/subject/topic/<id>``）解析。

**只发一层**（用户要求，与 linux.do 的楼层处理同一形态）：

- 链接不带楼层锚点 → 发**主楼**
- 链接带 ``#post_<id>`` 锚点 → 发**那一层**，主楼作为引用块当上下文

（上一版把主楼 + 全部楼层铺出来 —— 45 层的帖子整篇塞满消息，用户要求改掉。）

**两个页面同构但头部不同**（实测）::

    group/topic   : 标题在 ``#pageHeader h1`` 的 ``<br/>`` 之后；归属是小组（/group/<slug>）
    subject/topic : 页面上**两个 h1** —— ``#headerSubject`` 里是条目名，
                    真正的标题在 ``.comment-header h1``；归属是条目（/subject/<id>）

**楼层结构两个页面完全一样**（主楼 ``.postTopic`` / 回复 ``.row_reply`` /
楼中楼 ``.sub_reply_bg``，正文容器 ``.topic_content`` / ``.reply_content`` / ``.cmt_sub_content``）。

fixture ``bangumi_group_topic_472394.html`` 是真实话题（抽过：h1 + 主楼 + 前 5 层）。
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from parsehub.parsers.parser.bangumi import BangumiParser, BangumiParseResult
from parsehub.provider_api.bangumi import BGM_TIMEZONE, BangumiError, BangumiTopic

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "bangumi_group_topic_472394.html"


def _topic(floor_id: str = "") -> BangumiTopic:
    return BangumiTopic._from_html(FIXTURE.read_text(encoding="utf-8"), "472394", floor_id=floor_id)


def _floor(
    pid: str,
    label: str,
    when: str,
    author: str,
    uid: str,
    body: str,
    *,
    cls: str = "light_even row row_reply clearit",
    body_cls: str = "reply_content",
    nested: str = "",
) -> str:
    head = (
        f'<div class="post_actions re_info"><div class="action"><small>'
        f'<a class="floor-anchor" href="#post_{pid}">{label}</a> - {when}'
        f"</small></div></div>"
    )
    return f"""<div class="{cls}" id="post_{pid}">
      {head}
      <a class="avatar" href="/user/{uid}"><span class="avatarNeue"></span></a>
      <div class="inner">
        <strong><a class="l" href="/user/{uid}">{author}</a></strong>
        <div class="{body_cls}">{body}{nested}</div>
      </div>
    </div>"""


def _main(body: str = "主楼正文") -> str:
    return _floor(
        "0", "#1", "2026-10-7 00:19", "楼主", "op", body,
        cls="postTopic light_odd clearit", body_cls="topic_content",
    )


def _group_header(title: str = "话题标题") -> str:
    return f"""<div id="pageHeader"><h1>
      <span><a href="/group/testgrp"><img class="avatar" src="//lain.bgm.tv/g.jpg"/>测试小组</a>
      » <a href="/group/testgrp/forum">讨论</a></span><br/>{title}</h1></div>"""


def _subject_header(subject: str = "無職転生Ⅲ", title: str = "条目话题标题") -> str:
    return f"""<div id="headerSubject"><h1 class="nameSingle">
        <a href="/subject/501963">{subject}</a></h1></div>
      <div class="comment-header"><h1>{title}</h1></div>"""


def _from_html(
    html: str,
    topic_id: str = "1",
    *,
    floor_id: str = "",
    main: str | None = None,
    header: str | None = None,
) -> BangumiTopic:
    """``html`` 是 **comment_list 的内容**；主楼按真实结构放在它**外面**。"""
    page = f"""<html><body><div id="viewEntry">
      {header if header is not None else _group_header()}
      {main if main is not None else _main()}
      <div id="comment_list" class="commentList">{html}</div>
    </div></body></html>"""
    return BangumiTopic._from_html(page, topic_id, floor_id=floor_id)


# ---------------------------------------------------------------- 只发一层

def test_the_opening_post_is_sent_when_there_is_no_anchor():
    """**核心**: 不带锚点 → 只发**主楼**，**不铺楼层**"""
    topic = _topic()
    assert topic.floor_label == "#1"
    assert topic.markdown_content.count("最近视奸") == 1
    # 楼层内容一律不出现（这是本版的核心改动）
    for floor in topic.floors:
        if floor.markdown:
            assert floor.markdown.splitlines()[0] not in topic.markdown_content


def test_the_opening_post_has_no_context_quote():
    """主楼不给自己做引用块"""
    assert not [ln for ln in _topic().markdown_content.splitlines() if ln.startswith("> ")]


def test_an_anchored_floor_is_what_gets_sent():
    """**核心**: 带 ``#post_<id>`` 锚点 → 发**那一层**"""
    topic = _topic("4062141")
    assert topic.floor_label == "#2"
    assert topic.author_name == "大夜宵"
    assert "两年四百+少了" in topic.markdown_content


def test_an_anchored_floor_brings_the_opening_post_as_a_quote():
    """**核心**: 分享楼层时把**主楼**做成引用块（与 linux.do 同一形态）"""
    quoted = [ln for ln in _topic("4062141").markdown_content.splitlines() if ln.startswith("> ")]
    assert quoted, "缺主楼引用块"
    assert any("最近视奸" in ln for ln in quoted), "引用块里不是主楼正文"
    assert any("irohard" in ln for ln in quoted), "引用块里没有主楼作者"
    assert any("#1" in ln for ln in quoted), "引用块里的主楼没有楼层号"


def test_the_quote_is_italic_like_every_other_platform():
    """**核心**: bgm 的引用块与其他平台**同形态** —— 作者行在前、**整块斜体**。

    用户报「其他平台都是斜体，这个不是」。根因在这儿手拼 ``"> "`` 前缀造了轮子，
    漏掉了斜体（公共 helper 的形态是每行 ``> <i>…</i>``）。

    块内**不用 markdown 星号**：嵌套引用里会字面显示（实测读到过孤立的 ``"**"``）。
    """
    quoted = [ln for ln in _topic("4062141").markdown_content.splitlines() if ln.startswith("> ")]
    assert quoted, "缺主楼引用块"
    assert all(ln.startswith("> <i>") for ln in quoted), quoted
    assert not any("**" in ln for ln in quoted), quoted


def test_the_quote_and_the_body_are_one_blank_line_apart():
    """**核心**: 引用块与**正文之间只有一个空行** —— 与其他平台同间距。

    ``format_quote_block`` 末尾**自带一个空行**, 直接 ``join`` 会堆成 3 个空行,
    TG 渲染出来行间距就比其他平台大（用户报「感觉比其他平台的大一点」）。
    linux.do 早就 strip 过（见它的注释）, bgm 漏了。
    """
    md = _topic("4062141").markdown_content
    assert "\n\n\n" not in md, f"出现了连续两个空行: {md!r}"
    lines = md.splitlines()
    last_quote = max(i for i, ln in enumerate(lines) if ln.startswith("> "))
    assert lines[last_quote + 1] == "", lines
    assert lines[last_quote + 2] != "", f"引用块与正文之间插了多余空行: {lines}"


def test_the_quote_comes_first_with_no_affiliation_in_the_body():
    """**核心**: 正文里只有「引用块 + 本层正文」，**归属行不在正文里**。

    归属行进 ``origin_line``（渲染层放在标题与作者之间）—— 放正文里会挤到引用块
    前面，把引用块的归位搅乱（踩过两次）。引用块在最前 ⇒ 媒体自然跟在它后面。
    """
    md = _topic("4062141").markdown_content
    lines = md.splitlines()
    assert lines[0].startswith("> <i>"), lines[:2]
    # 归属行（含它的可翻译片段形态）不该出现在正文里
    assert "» 讨论" not in md and "{{t:讨论}}" not in md, "归属行不该出现在正文里"
    last_quote = max(i for i, ln in enumerate(lines) if ln.startswith("> "))
    assert lines[last_quote + 1] == "", lines
    assert lines[last_quote + 2] == "两年四百+少了，很喜欢牛逼火腿一句话，你看点短的不就好了", lines


def test_the_affiliation_becomes_the_origin_line():
    """**核心**（用户要求）: 归属行走 ``origin_line``，内容是「条目/小组名 » 讨论」+ 链接。

    「讨论」以**可翻译片段占位符**（``{{t:讨论}}``）交给渲染层 —— lib 不认识用户语言，
    替换由 bot 侧 ``replace_translatable_fragments`` 按 lang 完成。
    """
    topic = _topic("4062141")
    assert topic.origin_line == (
        '**<a href="https://bgm.tv/group/fillgrids">补旧番</a>** » {{t:讨论}}'
    ), topic.origin_line
    # 主楼那条路也有归属行（只是没有引用块）
    assert "» {{t:讨论}}" in _topic().origin_line


def test_a_bodyless_floor_keeps_the_quote_in_the_body():
    """**核心**: 纯图楼层（本层没文字）—— 正文就是那个引用块，媒体跟在它后面。

    （以前归属行在正文里，会把引用块挤到末尾 ⇒ 图被插到它前面贴住。）
    """
    main = _main("主楼正文")
    reply = _floor("6", "#2", "2026-10-7 00:24", "甲", "a", "")
    topic = _from_html(reply, floor_id="6", main=main)
    assert topic.markdown_content.splitlines()[0].startswith("> <i>"), topic.markdown_content
    assert "» 讨论" not in topic.markdown_content and "{{t:讨论}}" not in topic.markdown_content


def test_the_opening_post_has_no_stray_blank_lines():
    """主楼那条路（没有引用块）也不该有空行堆积"""
    assert "\n\n\n" not in _topic().markdown_content


def test_the_roles_are_declared_for_the_context_quote():
    """**核心**: 分享楼层时要声明引用块的角色（渲染层据此归位媒体，不看位置）"""
    assert _topic("4062141").quote_roles == ["reply"]
    assert _topic("4062147").quote_roles == ["reply"], "楼中楼也要声明"
    assert _topic().quote_roles == [], "主楼没有引用块，不该声明"
    assert _topic("99999999").quote_roles == [], "锚点失效退回主楼 → 也没有引用块"


def test_the_parser_passes_the_roles_through():
    """provider 声明了还不够 —— parser 层要透传出来（漏了等于没声明）"""
    import asyncio

    from parsehub.provider_api import bangumi as mod

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
            return _Resp(FIXTURE.read_text(encoding="utf-8"))

    original = mod.http.AsyncClient
    mod.http.AsyncClient = _Client()
    try:
        result = asyncio.run(BangumiParser()._do_parse("https://bgm.tv/group/topic/472394#post_4062141"))
    finally:
        mod.http.AsyncClient = original
    assert result.quote_roles == ["reply"]  # 上下文块在正文前 ⇒ reply
    assert result.reply_media_count == 0  # 这个 fixture 里主楼没有图


def test_the_quote_comes_out_of_the_shared_helper():
    """**自检**: 引用块必须由 ``format_quote_block`` 产出，别手拼 ``"> "`` 前缀。

    手写的版本会漂（bgm 就是这样漏掉斜体、与别的平台不一致）。所以直接拿
    公共 helper 的输出与之一行行对 —— 形态一旦分叉这个测试就红。
    """
    from parsehub.utils.helpers import format_quote_block

    quoted = [ln for ln in _topic("4062141").markdown_content.splitlines() if ln.startswith("> ")]
    expected = [ln for ln in format_quote_block("正文第一行", "作者行").splitlines() if ln.strip()]
    # 同一套形态：署名行 + 正文行都是 ``> <i>…</i>``
    for lines in (quoted, expected):
        assert lines and all(ln.startswith("> <i>") and ln.endswith("</i>") for ln in lines), lines


def test_an_anchored_sub_reply_can_be_selected_too():
    """锚点指向**楼中楼**（它也有 ``post_<id>``）"""
    topic = _topic("4062147")
    assert topic.floor_label == "#2-1"
    assert topic.author_name == "irohard"
    assert "经常看到你在新番里评论" in topic.markdown_content


def test_a_bad_anchor_falls_back_to_the_opening_post():
    """锚点在页面上不存在（楼层被删 / 链接被手改）→ 退回主楼，**不是**解析失败"""
    topic = _topic("99999999")
    assert topic.floor_label == "#1"
    assert "最近视奸" in topic.markdown_content


# ---------------------------------------------------------------- 楼中楼引用父楼

def test_a_sub_reply_quotes_the_floor_it_replies_to():
    """**核心**: 楼中楼引用**父楼**（它实际回复的那层），不是主楼。

    用户定案「引用父楼（#4，它实际回复的那层）」。`#4-1` 在 DOM 上嵌在 `#4` 的正文
    容器里（`div.topic_reply_410733`），所以它回的是 `#4` 的话 —— 拿主楼当上下文
    等于答非所问。
    """
    lines = _topic("4062147").markdown_content.splitlines()
    quoted = "\n".join(ln for ln in lines if ln.startswith("> "))
    assert "大夜宵" in quoted, f"引用块里应是父楼 #2 的作者: {quoted!r}"
    assert "两年四百+少了" in quoted, quoted
    assert "最近视奸" not in quoted, "不该引用主楼"


def test_a_top_level_floor_still_quotes_the_opening_post():
    """一级楼层回复的是主楼 —— 引用块不变"""
    quoted = "\n".join(ln for ln in _topic("4062141").markdown_content.splitlines() if ln.startswith("> "))
    assert "irohard" in quoted and "最近视奸" in quoted, quoted


def test_a_sub_reply_without_a_parent_gets_no_context():
    """**核心**: 父楼不在页面上（被删）→ **不带上下文**（用户：「没有就保持空白」）。

    不能退回主楼：主楼不是它回的那层，引上去等于编造上下文。
    """
    from parsehub.provider_api.bangumi import BangumiFloor

    opening = BangumiFloor(label="#1", dom_id="1", markdown="主楼正文", author_name="楼主")
    orphan = BangumiFloor(
        label="#2-1", dom_id="7", is_sub=True, parent_dom_id="999", markdown="楼中楼", author_name="甲"
    )
    topic = BangumiTopic._build("1", "标题", "小组", "", opening, orphan, [opening, orphan], [], 0)
    assert not [ln for ln in topic.markdown_content.splitlines() if ln.startswith("> ")], topic.markdown_content
    assert "楼中楼" in topic.markdown_content
    assert topic.quote_roles == [], "没有引用块就不该声明角色"


def test_the_parent_is_recorded_for_sub_replies_only():
    floors = {f.label: f for f in _topic().floors}
    assert floors["#2-1"].parent_dom_id == "4062141", "楼中楼要记父楼"
    assert floors["#2"].parent_dom_id == "", "一级楼层没有父楼"
    assert floors["#6-1"].parent_dom_id == floors["#6"].dom_id


def test_the_quote_media_count_covers_the_opening_posts_images():
    """上下文层的图走 ``reply_media_count`` 通道（块在正文前，bot 侧放进引用块内部）"""
    main = _main('主楼正文<img class="code" src="//lain.bgm.tv/pic/photo/l/op.jpg"/>')
    reply = _floor(
        "6", "#2", "2026-10-7 00:24", "甲", "a",
        '<img class="code" src="//lain.bgm.tv/pic/photo/l/r.jpg"/>',
    )
    topic = _from_html(reply, floor_id="6", main=main)
    assert [i.url for i in topic.images] == [
        "https://lain.bgm.tv/pic/photo/l/r.jpg",
        "https://lain.bgm.tv/pic/photo/l/op.jpg",
    ]
    assert topic.reply_media_count == 1, "只有上下文层那张属于引用块"

    # 分享主楼时没有引用块 —— 计数必须是 0（否则图会被切进不存在的卡片）
    assert _from_html(reply, main=main).reply_media_count == 0


# ---------------------------------------------------------------- 两个页面形态

def test_the_group_header_gives_the_group_and_the_title():
    topic = _topic()
    assert topic.title == "真的有人能看过3000+部番吗"
    assert topic.context_name == "补旧番"
    assert topic.context_url == "https://bgm.tv/group/fillgrids"


def test_the_subject_header_gives_the_subject_and_the_second_h1():
    """**核心**: 条目讨论版的标题是页面**第二个** ``h1`` —— 第一个是条目名。

    直接取 ``h1`` 会把条目名当话题标题（实测就是这个坑）。
    """
    topic = _from_html("", header=_subject_header("無職転生Ⅲ", "我说涩谷亮介身边缺一个平野宏树有没有懂的"))
    assert topic.title == "我说涩谷亮介身边缺一个平野宏树有没有懂的"
    assert topic.context_name == "無職転生Ⅲ"
    assert topic.context_url == "https://bgm.tv/subject/501963"


def test_the_floor_time_is_beijing_time_too():
    """楼层时间与日志同一个坑：页面给 ``2026-10-7 00:24``（无偏移）→ 按 +08:00 解释

    按 UTC 解释的话渲染出来晚 8 小时（用户报「时间好像有问题？多 8 小时」）。
    """
    topic = _topic("4062141")
    assert topic.published_at is not None
    assert topic.published_at.utcoffset() == BGM_TIMEZONE.utcoffset(None)
    # 页面上写的是 10-7 00:24（北京）→ UTC 是前一日的 16:24
    assert topic.published_at.astimezone(UTC) == datetime(2026, 10, 6, 16, 24, tzinfo=UTC)


def test_a_floor_without_a_time_parses_to_none():
    main = _floor(
        "0", "#1", "", "楼主", "op", "没有任何时间", cls="postTopic light_odd clearit", body_cls="topic_content"
    )
    topic = _from_html("", main=main)
    assert topic.published_at is None


def test_the_origin_line_carries_the_affiliation_only():
    """归属行只写「谁家的讨论」（层数不再需要 —— 只发一层）"""
    origin = _topic().origin_line
    assert "补旧番" in origin and "» {{t:讨论}}" in origin
    assert "层" not in origin


# ---------------------------------------------------------------- 楼层解析（内部）

def test_floors_are_parsed_with_dom_ids():
    """``dom_id`` 就是节点的 ``post_<数字>`` 后那串 —— URL 锚点按它定位"""
    by_label = {f.label: f for f in _topic().floors}
    assert by_label["#2"].dom_id == "4062141"
    assert by_label["#2-1"].dom_id == "4062147"


def test_a_sub_reply_body_does_not_contain_the_parent_text():
    """**核心**: 楼中楼摘出来当独立条目 —— 父楼正文里不能混进它的文字。

    ⚠️ 摘用的是 ``decompose()``，它会**清空被摘节点的内容** ⇒ 必须**先收集节点、再逆序解析**
    （正序时父楼先把子楼清掉，轮到子楼就只剩空壳：实测 45 个节点只解析出 38 条）。
    """
    floors = {f.label: f for f in _topic().floors}
    parent, sub = floors["#2"], floors["#2-1"]
    assert "两年四百+少了" in parent.markdown
    assert "经常看到你在新番里评论" not in parent.markdown
    assert "经常看到你在新番里评论" in sub.markdown
    assert "两年四百+少了" not in sub.markdown


def test_sub_replies_are_marked():
    subs = [f for f in _topic().floors if f.is_sub]
    assert [f.label for f in subs] == ["#2-1", "#2-2", "#2-3", "#6-1"]


# ---------------------------------------------------------------- 图片与表情

def test_only_the_sent_floor_keeps_its_images():
    """**核心**: 图片只保留**本层 + 上下文层**的 —— 别的楼层的图一律不发。

    抽图时遍历全页（楼中楼里的图必须提前抽，逆序解析会 decompose 掉那些节点），
    但**发出去时必须收窄**：不收窄的话整页几十层的图都会被当成"本层的图"
    （用户报「把楼里所有图片都发出来了」—— 章节页 214 层一次发了 21 张）。
    """
    nested = _floor(
        "7", "#2-1", "2026-10-7 00:28", "甲", "a",
        '<img class="code" src="//lain.bgm.tv/pic/photo/l/deep.jpg"/>',
        cls="sub_reply_bg clearit", body_cls="cmt_sub_content",
    )
    reply = _floor("6", "#2", "2026-10-7 00:24", "甲", "a",
                   '<img class="code" src="//lain.bgm.tv/pic/photo/l/a.jpg"/>', nested=nested)

    # 发主楼（它自己没图）→ 一张都不带（#2 与它楼中楼的图不是主楼的）
    assert _from_html(reply).images == []

    # 发 #2 层 → 只带它自己那张；楼中楼那张属于**另一层**
    assert [i.url for i in _from_html(reply, floor_id="6").images] == [
        "https://lain.bgm.tv/pic/photo/l/a.jpg"
    ]

    # 发楼中楼 → 它自己那张 + 父楼（上下文）那张
    urls = [i.url for i in _from_html(reply, floor_id="7").images]
    assert urls == [
        "https://lain.bgm.tv/pic/photo/l/deep.jpg",
        "https://lain.bgm.tv/pic/photo/l/a.jpg",
    ], urls


def test_smiles_stay_text_and_do_not_become_media():
    topic = _from_html(
        _floor("6", "#2", "2026-10-7 00:24", "甲", "a",
               '好笑<img class="smile" alt="(bgm38)" src="/img/smiles/tv/15.gif"/>'),
        floor_id="6",
    )
    assert topic.images == []
    assert "(bgm38)" in topic.markdown_content


def test_the_body_keeps_no_image_tag():
    topic = _from_html(
        _floor("6", "#2", "2026-10-7 00:24", "甲", "a", '<img class="code" src="//lain.bgm.tv/pic/photo/l/a.jpg"/>'),
        floor_id="6",
    )
    assert "lain.bgm.tv/pic/photo" not in topic.markdown_content


# ---------------------------------------------------------------- 边界

def test_a_missing_topic_raises_with_the_sites_wording():
    with pytest.raises(BangumiError) as exc:
        BangumiTopic._from_html("<html><body>呜咕，出错了 数据库中没有查询到该小组话题的信息</body></html>", "9")
    assert "不存在" in str(exc.value)


def test_an_unknown_shape_raises_a_different_message():
    with pytest.raises(BangumiError) as exc:
        BangumiTopic._from_html("<html><body>什么都没有</body></html>", "1")
    assert "改版" in str(exc.value) or "结构" in str(exc.value)


def test_the_ids_come_out_of_the_url():
    assert BangumiTopic.get_id_by_url("https://bgm.tv/group/topic/472394") == "472394"
    assert BangumiTopic.get_id_by_url("https://bgm.tv/subject/topic/41209") == "41209"
    assert BangumiTopic.get_id_by_url("https://bgm.tv/blog/381120") == ""
    assert BangumiTopic.get_floor_id_by_url("https://bgm.tv/group/topic/472394#post_4062141") == "4062141"
    assert BangumiTopic.get_floor_id_by_url("https://bgm.tv/group/topic/472394") == ""


# ---------------------------------------------------------------- 「超展开」入口

def test_the_rakuen_entry_is_read_too():
    """``/rakuen/topic/<归属>/<id>``（超展开列表里的入口）—— 与规范路径**同一个话题**

    实测：``/rakuen/topic/subject/41119`` 与 ``/subject/topic/41119`` 的正文逐块相同、
    楼层号一致；楼层结构（``.postTopic`` / ``.row_reply`` / ``.sub_reply_bg``）也同构。
    """
    assert BangumiTopic.get_id_by_url("https://bgm.tv/rakuen/topic/subject/41119") == "41119"
    assert BangumiTopic.get_id_by_url("https://bgm.tv/rakuen/topic/group/472394") == "472394"
    assert (
        BangumiTopic.get_floor_id_by_url("https://bgm.tv/rakuen/topic/subject/41119#post_410747")
        == "410747"
    )


def test_the_affiliation_comes_out_of_every_entry():
    """归属决定用哪条规范路径 —— ``group`` 与 ``subject`` 的 header 分支不同"""
    cases = {
        "https://bgm.tv/group/topic/472394": ("group", "472394"),
        "https://bgm.tv/subject/topic/41209": ("subject", "41209"),
        "https://bgm.tv/rakuen/topic/group/472394": ("group", "472394"),
        "https://bgm.tv/rakuen/topic/subject/41119": ("subject", "41119"),
        "https://bgm.tv/rakuen/topic/subject/41119#post_410747": ("subject", "41119"),
    }
    for url, expected in cases.items():
        assert BangumiTopic._topic_ref(url) == expected, url
    # 不是话题的路径一律取不到（否则会被 __match__ 之外的东西误接）
    assert BangumiTopic._topic_ref("https://bgm.tv/blog/381120") == ("", "")
    assert BangumiTopic._topic_ref("https://bgm.tv/subject/400602") == ("", "")


def test_a_rakuen_link_is_fetched_from_the_canonical_path():
    """**核心**: rakuen 的入口归一化到规范路径再抓 —— 复用已验证的解析路径。

    rakuen 页面更精简（实测 16KB vs 26KB），归属链指向**条目**而不是小组，
    header 分支与规范页面也不同。归一化后零新增解析逻辑。
    """
    import asyncio

    from parsehub.provider_api import bangumi as mod

    requested: list[str] = []

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

        async def get(self, url: str, **_kwargs):
            requested.append(url)
            return _Resp(FIXTURE.read_text(encoding="utf-8"))

    original = mod.http.AsyncClient
    mod.http.AsyncClient = _Client()
    try:
        for url, wanted in (
            ("https://bgm.tv/rakuen/topic/subject/472394", "https://bgm.tv/subject/topic/472394"),
            ("https://bgm.tv/rakuen/topic/group/472394", "https://bgm.tv/group/topic/472394"),
            ("https://bgm.tv/group/topic/472394", "https://bgm.tv/group/topic/472394"),
        ):
            asyncio.run(BangumiTopic.parse(url))
            assert requested[-1] == wanted, (url, requested[-1])
    finally:
        mod.http.AsyncClient = original


def test_the_rakuen_path_is_matched_and_other_rakuen_pages_are_not():
    """接住三个入口；``/rakuen/topic/privatetopic/…`` 这类不接（实测返回 0 字节）"""
    assert BangumiParser.match("https://bgm.tv/rakuen/topic/subject/41119")
    assert BangumiParser.match("https://bgm.tv/rakuen/topic/group/472394")
    assert BangumiParser.match("https://bgm.tv/rakuen/topic/subject/41119#post_410747")
    assert not BangumiParser.match("https://bgm.tv/rakuen/topic/privatetopic/41119")
    assert not BangumiParser.match("https://bgm.tv/rakuen/")


# ---------------------------------------------------------------- parser

def test_the_parser_matches_both_topic_paths():
    assert BangumiParser.match("https://bgm.tv/group/topic/472394")
    assert BangumiParser.match("https://bgm.tv/subject/topic/41209")
    assert BangumiParser.match("https://bangumi.tv/group/topic/472394")


def test_the_parser_still_matches_blogs_and_refuses_other_paths():
    """**回归**: 加话题不能把日志弄丢；条目页本身与小组首页不该被接走"""
    assert BangumiParser.match("https://bgm.tv/blog/381120")
    assert not BangumiParser.match("https://bgm.tv/subject/400602")
    assert not BangumiParser.match("https://bgm.tv/group/fillgrids")
    assert not BangumiParser.match("https://bgm.tv/group/fillgrids/forum")


def test_the_result_carries_the_floor_label_and_the_quote_count():
    """``position_label`` = 本层楼层号；引用块里的作者行不带 markdown 星号"""

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
            return _Resp(FIXTURE.read_text(encoding="utf-8"))

    from parsehub.provider_api import bangumi as mod

    original = mod.http.AsyncClient
    mod.http.AsyncClient = _Client()
    try:
        result = asyncio.run(BangumiParser()._do_parse("https://bgm.tv/group/topic/472394#post_4062141"))
    finally:
        mod.http.AsyncClient = original

    assert result.position_label == "#2"
    assert result.author_handle == "dayexiao520"
    assert result.author_url == "https://bgm.tv/user/dayexiao520"
    assert BangumiParseResult.requires_media_download is True
    quoted = [ln for ln in result.markdown_content.splitlines() if ln.startswith("> ")]
    assert quoted and all("**" not in ln for ln in quoted)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
