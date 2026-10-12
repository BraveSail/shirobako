"""linux.do (Discourse) 解析的离线用例 —— 用真实响应裁剪出的载荷，不联网。"""

import pytest

from parsehub.provider_api.linuxdo import LinuxDoError, LinuxDoTopic


def make_payload(**overrides):
    payload = {
        "id": 2979226,
        "title": "国庆快乐www今天是腿子纯享",
        "fancy_title": "国庆快乐www今天是腿子纯享",
        "posts_count": 28,
        "reply_count": 5,
        "views": 927,
        "like_count": 125,
        "created_at": "2026-10-03T04:30:45.726Z",
        "tags": [
            {"id": 1461, "name": "纯水", "slug": "1461-tag"},
            {"id": 248, "name": "NSFW", "slug": "nsfw"},
        ],
        "details": {"created_by": {"id": 462812, "username": "VerenQwQ", "name": "VerenOwO"}},
        "post_stream": {
            "posts": [
                {
                    "id": 23298920,
                    "post_number": 1,
                    "username": "VerenQwQ",
                    "name": "VerenOwO",
                    "created_at": "2026-10-03T04:30:46.334Z",
                    "reaction_users_count": 75,
                    "cooked": (
                        "<p>杂鱼杂鱼杂鱼</p>\n"
                        "<details>\n<summary>\n总结</summary>\n<div class=\"spoiler\">\n"
                        "<p><div class=\"lightbox-wrapper\">"
                        "<a class=\"lightbox\" "
                        "href=\"https://cdn3.ldstatic.com/original/4X/e/c/c/abc.jpeg\" "
                        "title=\"IMG_7961\">"
                        "<img src=\"https://cdn3.ldstatic.com/optimized/4X/e/c/c/abc_2_375x500.jpeg\" "
                        "width=\"375\" height=\"500\"></a>"
                        "<div class=\"meta\"><span class=\"filename\">IMG_7961</span></div>"
                        "</div></p>\n</div>\n</details>"
                    ),
                }
            ]
        },
    }
    payload.update(overrides)
    return payload


# ── URL 形态 ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://linux.do/t/topic/2979226",
        "https://linux.do/t/2979226",
        "https://linux.do/t/国庆快乐/2979226",
        "https://linux.do/t/some-slug/2979226/12",
        "http://linux.do/t/topic/2979226",
    ],
)
def test_topic_id_is_extracted(url):
    assert LinuxDoTopic.get_topic_id(url) == "2979226"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://linux.do/t/topic/2979226/11", "11"),
        ("https://linux.do/t/2979226/4", "4"),
        ("https://linux.do/t/某话题/2979226/5", "5"),
        ("https://linux.do/t/topic/2979226", ""),
        ("https://linux.do/t/2979226", ""),
    ],
)
def test_floor_number_is_extracted(url, expected):
    """带楼层号的链接要认出楼层（/t/2979226/11 不能被当成 slug=2979226、id=11）"""
    assert LinuxDoTopic.get_topic_id(url) == "2979226"
    assert LinuxDoTopic.get_post_number(url) == expected


def test_floor_url_resolves_the_requested_post():
    """指定楼层时取那一层，不能盲目取第一项（带楼层号的响应不含 1 楼）"""
    payload = make_payload()
    floor_11 = {
        "post_number": 11,
        "username": "hifumi_mizuhara",
        "name": "Hifumi Mizuhara",
        "cooked": '<p>好看</p><img src="https://cdn3.ldstatic.com/original/x.jpeg" width="800" height="600">',
    }
    payload["post_stream"]["posts"] = [
        {"post_number": 6, "username": "a", "cooked": "<p>纯文字</p>"},
        floor_11,
    ]
    topic = LinuxDoTopic._from_payload(payload, "2979226", post_number="11")
    assert topic.author_handle == "hifumi_mizuhara"
    assert len(topic.images) == 1


def test_missing_floor_is_an_error():
    payload = make_payload()
    payload["post_stream"]["posts"] = [{"post_number": 2, "cooked": "<p>x</p>"}]
    with pytest.raises(LinuxDoError):
        LinuxDoTopic._from_payload(payload, "2979226", post_number="99")


def test_emoji_is_not_treated_as_media():
    """Discourse 的表情是 <img class="emoji">, 20x20 小图不该当媒体发送"""
    cooked = (
        '<p>好看<img src="https://cdn.ldstatic.com/images/emoji/twemoji/x.png?v=15" '
        'title=":enraged_face:" class="emoji" alt=":enraged_face:" width="20" height="20"></p>'
    )
    payload = make_payload()
    payload["post_stream"]["posts"][0]["cooked"] = cooked
    topic = LinuxDoTopic._from_payload(payload, "2979226")
    assert topic.images == []
    # 表情直接从正文里去掉, 不产生外链图片语法也不留下 :name: 形式的文本
    assert topic.markdown_content.strip() == "好看"
    assert "twemoji" not in topic.markdown_content


def test_avatar_is_not_media_and_is_removed_from_the_body():
    """引用回复的头像 (img.avatar) 不是帖子媒体, 正文里也不该出现"""
    cooked = (
        '<aside class="quote no-group" data-username="paomian_1">'
        '<div class="title"><img alt="" width="24" height="24" '
        'src="https://cdn.ldstatic.com/user_avatar/linux.do/paomian_1/48/430070_2.png" '
        'class="avatar"> paomian_1:</div>'
        "<blockquote><p>被引用的内容</p></blockquote></aside>"
        "<p>我的回复</p>"
    )
    payload = make_payload()
    payload["post_stream"]["posts"][0]["cooked"] = cooked
    topic = LinuxDoTopic._from_payload(payload, "2979226")

    assert topic.images == []
    assert "user_avatar" not in topic.markdown_content
    # 引用块与用户名保留
    assert "paomian" in topic.markdown_content
    assert "被引用的内容" in topic.markdown_content
    assert "我的回复" in topic.markdown_content


#: 真实抓到的 poll HTML（2026-10-06 从 ``/t/topic/2985951.json`` 的 cooked 里取）。
#: ⚠️ 这里的人数**恒为 0** —— 它是占位壳，真实数据在同响应的 ``post["polls"]``。
POLL_COOKIE_PLACEHOLDER = (
    "<p>要不然是不是只用过o/的codex，没被a/封过号，a/封号是让我挺恶心的</p>"
    '<div class="poll" data-poll-charttype="bar" data-poll-name="poll" data-poll-public="false"'
    ' data-poll-results="always" data-poll-status="open" data-poll-type="regular">'
    '<div class="poll-container"><ul>'
    '<li data-poll-option-id="d7ffaa1410bc2fd4b0be1de8a1ad3bb3">还在用a/感觉良好</li>'
    '<li data-poll-option-id="2f9cd91f0d73a903a7d4849965241509">没用过a/，感觉o/实在恶心</li>'
    '<li data-poll-option-id="1f35519f11e1df7980d56c002bad370b">即使被a/封过付费号，也觉得o/比a/不当人</li>'
    '<li data-poll-option-id="0520d1c3d9d52393b184ac30d88958e7">一直觉得a/比o/恶心</li>'
    "</ul></div>"
    '<div class="poll-info"><div class="poll-info_counts"><div class="poll-info_counts-count">'
    '<span class="info-number">0</span><span class="info-label">投票人</span>'
    "</div></div></div></div>"
)


#: 真实的结构化 polls 数据（同一次响应的 ``post["polls"]``，2026-10-06 抓）
POLL_STRUCTURED = [
    {
        "id": "40361",
        "name": "poll",
        "type": "regular",
        "status": "open",
        "results": "always",
        "chart_type": "bar",
        "title": None,
        "voters": 222,
        "options": [
            {"id": "d7ffaa1410bc2fd4b0be1de8a1ad3bb3", "html": "还在用a/感觉良好", "votes": 71},
            {"id": "2f9cd91f0d73a903a7d4849965241509", "html": "没用过a/，感觉o/实在恶心", "votes": 31},
            {"id": "1f35519f11e1df7980d56c002bad370b", "html": "即使被a/封过付费号，也觉得o/比a/不当人", "votes": 40},
            {"id": "0520d1c3d9d52393b184ac30d88958e7", "html": "一直觉得a/比o/恶心", "votes": 80},
        ],
    }
]


def test_a_poll_renders_as_a_placeholder_with_structured_data():
    """投票在正文里只留占位符行; 选项/票数/「人参与」文案全部由渲染层本地化拼装。

    以前 poll 就是一段 HTML，转 markdown 后选项与"人数标签"混在一起，
    末尾孤零零一个 `0 / 投票人` 看起来像正文的残句（用户报障就是这个）。
    """
    payload = make_payload()
    payload["post_stream"]["posts"][0]["cooked"] = POLL_COOKIE_PLACEHOLDER
    payload["post_stream"]["posts"][0]["polls"] = POLL_STRUCTURED
    topic = LinuxDoTopic._from_payload(payload, "2979226")
    md = topic.markdown_content

    # 正文段落还在
    assert "没被a/封过号" in md
    # 投票位置是一行占位符, 正文里**没有**任何拼好的投票文案
    assert "{{poll:0}}" in md, md
    assert "📊" not in md, md
    assert "投票" not in md, md
    assert "人参与" not in md, md
    assert "0 人参与" not in md, md
    assert "| 选项 |" not in md, md
    # 结构化数据: 选项/票数来自同一次响应的 post["polls"] (不是 cooked 里那个 0)
    assert len(topic.polls) == 1
    poll = topic.polls[0]
    assert [(o.text, o.count) for o in poll.options] == [
        ("还在用a/感觉良好", 71),
        ("没用过a/，感觉o/实在恶心", 31),
        ("即使被a/封过付费号，也觉得o/比a/不当人", 40),
        ("一直觉得a/比o/恶心", 80),
    ]
    assert poll.voters == 222
    assert poll.total_votes == 222
    assert poll.title == ""


def test_a_poll_without_structured_data_still_carries_its_options():
    """没有结构化 ``polls`` 字段时（老站点）: 占位符照放, 选项文字从 cooked 里取,
    count=None + voters=None —— 渲染层按单列表格处理, **不显示那个假的 0**"""
    payload = make_payload()
    payload["post_stream"]["posts"][0]["cooked"] = POLL_COOKIE_PLACEHOLDER
    topic = LinuxDoTopic._from_payload(payload, "2979226")
    md = topic.markdown_content

    assert "{{poll:0}}" in md, md
    assert "📊" not in md, md
    assert "人参与" not in md, md
    assert "投票人" not in md, md
    # 不泄漏 Discourse 的内部属性
    assert "data-poll" not in md
    assert "info-number" not in md
    # 结构化数据只有选项文字, 没有票数/人数
    assert len(topic.polls) == 1
    poll = topic.polls[0]
    assert [o.text for o in poll.options] == [
        "还在用a/感觉良好",
        "没用过a/，感觉o/实在恶心",
        "即使被a/封过付费号，也觉得o/比a/不当人",
        "一直觉得a/比o/恶心",
    ]
    assert all(o.count is None for o in poll.options)
    assert poll.voters is None and poll.total_votes is None


def test_a_poll_without_options_is_dropped():
    """poll 里没有选项时不要留下空壳 (占位符行也不放)"""
    payload = make_payload()
    payload["post_stream"]["posts"][0]["cooked"] = '<p>正文</p><div class="poll"></div>'
    topic = LinuxDoTopic._from_payload(payload, "2979226")
    assert "{{poll:" not in topic.markdown_content
    assert "正文" in topic.markdown_content
    assert topic.polls == []


def test_the_poll_indexes_follow_the_div_order():
    """**索引纪律**: 多个 poll 时占位符索引 = ``div.poll`` 的 DOM 出现顺序。"""
    cooked = (
        "<p>甲</p>"
        '<div class="poll" data-poll-name="poll"><ul>'
        '<li data-poll-option-id="a">选项甲</li></ul></div>'
        "<p>乙</p>"
        '<div class="poll" data-poll-name="poll2"><ul>'
        '<li data-poll-option-id="b">选项乙</li></ul></div>'
    )
    payload = make_payload()
    payload["post_stream"]["posts"][0]["cooked"] = cooked
    topic = LinuxDoTopic._from_payload(payload, "2979226")
    md = topic.markdown_content

    assert md.index("{{poll:0}}") < md.index("{{poll:1}}"), md
    assert [p.options[0].text for p in topic.polls] == ["选项甲", "选项乙"], topic.polls


def test_floor_counts_come_from_the_floor_not_the_topic():
    """指定楼层时, 时间与点赞要用那一层的, 不能用主题级 (那是楼主帖/全话题的)"""
    payload = make_payload(
        created_at="2026-10-02T12:47:06.105Z",
        like_count=730,
        post_stream={
            "posts": [
                {
                    "post_number": 11,
                    "username": "qdd28",
                    "created_at": "2026-10-02T12:49:23.688Z",
                    "reaction_users_count": 17,
                    "cooked": "<p>我的回复</p>",
                }
            ]
        },
    )
    topic = LinuxDoTopic._from_payload(payload, "2977803", post_number="11")
    assert topic.published_at == "2026-10-02T12:49:23.688Z"
    assert topic.like_count == 17


def _floor(post_number, username, cooked, *, name=None, reply_to=None, reply_to_user=None):
    post = {
        "id": 1000 + post_number,
        "post_number": post_number,
        "username": username,
        "name": name or username,
        "created_at": "2026-10-01T10:00:00.000Z",
        "reply_to_post_number": reply_to,
        "reply_to_user": {"username": reply_to_user, "name": reply_to_user} if reply_to_user else None,
        "cooked": cooked,
    }
    return post


def _topic_with(*floors):
    return make_payload(post_stream={"posts": list(floors)})


# ── 上下文引用块 (主楼 / 被回复楼层) ──────────────────────────


def test_floor_author_is_not_replaced_by_the_opening_poster():
    """楼层的 name 字段缺失时, 作者名要用这一层的 username —— 不能回落到楼主。

    用户报过同类问题 (「指定楼层的数据是错的是楼主的」): 时间/点赞修过了,
    作者名这里也踩同一个坑 —— ``created_by`` 是**主题创建者**, 指定楼层时
    拿它会把 3 楼的作者显示成楼主。
    """
    payload = _topic_with(
        _floor(1, "楼主", "<p>主楼</p>", name="楼主显示名"),
        {"id": 99, "post_number": 3, "username": "apparition", "cooked": "<p>三层的话</p>"},  # 没有 name
    )
    topic = LinuxDoTopic._from_payload(payload, "1", post_number="3")
    assert topic.author_handle == "apparition"
    assert topic.author_name == "apparition"      # 不是 "楼主显示名"
    assert topic.author_name != "楼主显示名"


def test_opening_post_still_falls_back_to_created_by():
    """解析主楼本身时, created_by 是合法的回落来源"""
    payload = make_payload(
        post_stream={"posts": [{"id": 1, "post_number": 1, "cooked": "<p>主楼</p>"}]}  # 楼里连 username 都没有
    )
    topic = LinuxDoTopic._from_payload(payload, "2979226")
    assert topic.author_name == "VerenOwO"        # created_by.name
    assert topic.author_handle == "VerenQwQ"      # created_by.username


def test_floor_reply_to_the_topic_quotes_the_opening_post():
    """分享楼层时, 主楼要作为引用块带上 —— 那一层常是在回应主楼

    所有楼层的 ``reply_to_post_number`` 都是 None 时, Discourse 的语义就是
    "回复主题 (主楼)", 所以主楼必须在最上面。
    """
    payload = _topic_with(
        _floor(1, "楼主", "<p>这是主楼的正文</p>"),
        _floor(2, "二层", "<p>二楼说的话</p>"),
        _floor(3, "三层", "<p>回复主题的话</p>"),
    )
    topic = LinuxDoTopic._from_payload(payload, "1", post_number="3")
    md = topic.markdown_content

    assert "这是主楼的正文" in md            # 主楼带上来了
    assert "回复主题的话" in md              # 本层内容也在
    assert md.index("这是主楼的正文") < md.index("回复主题的话")   # 主楼在前
    assert md.count("<blockquote") == 1 or md.count("> <i>") >= 2  # 是引用块形态


def test_floor_reply_to_another_floor_orders_op_then_replied_floor():
    """楼层回复其他楼层: 主楼在最上、被回复的楼层在中间、本层在最后"""
    payload = _topic_with(
        _floor(1, "楼主", "<p>主楼正文</p>"),
        _floor(2, "二层", "<p>二楼说的话</p>"),
        _floor(3, "三层", "<p>回复二楼的话</p>", reply_to=2, reply_to_user="二层"),
    )
    md = LinuxDoTopic._from_payload(payload, "1", post_number="3").markdown_content

    assert md.index("主楼正文") < md.index("二楼说的话") < md.index("回复二楼的话")


def test_opening_post_gets_no_context_quote():
    """解析主楼本身时不该自我引用"""
    payload = _topic_with(_floor(1, "楼主", "<p>主楼正文</p>"), _floor(2, "二层", "<p>二楼</p>"))
    md = LinuxDoTopic._from_payload(payload, "1", post_number="1").markdown_content
    assert md.count("主楼正文") == 1
    assert "二楼" not in md


def test_floor_reply_to_a_floor_does_not_duplicate_the_opening_post():
    """被回复的正是主楼时, 只出现一次 (不重复渲染)"""
    payload = _topic_with(
        _floor(1, "楼主", "<p>主楼正文</p>"),
        _floor(2, "二层", "<p>回复主楼</p>", reply_to=1, reply_to_user="楼主"),
    )
    md = LinuxDoTopic._from_payload(payload, "1", post_number="2").markdown_content
    assert md.count("主楼正文") == 1


def test_image_only_opening_post_still_gets_a_quote_and_its_image():
    """纯图主楼: 没有文字也要出引用块, 它的图片按"引用块媒体"带过去。

    用户指出的: 不能因为"只有图片"就把整层丢掉 —— 那样连在回复谁都不知道。
    """
    payload = _topic_with(
        _floor(1, "楼主", '<p><img src="https://cdn.ldstatic.com/op.png" width="400" height="300"></p>'),
        _floor(3, "三层", "<p>本层的话</p>"),
    )
    topic = LinuxDoTopic._from_payload(payload, "1", post_number="3")

    # 引用块在 (只有署名)
    assert "楼主" in topic.markdown_content
    assert topic.markdown_content.index("楼主") < topic.markdown_content.index("本层的话")
    # 主楼的图被算进"引用块媒体"（块在正文前 ⇒ reply 那一档）, 不在正文媒体里
    assert topic.reply_media_count == 1
    assert len(topic.images) == 1
    assert topic.images[0].url == "https://cdn.ldstatic.com/op.png"


def test_topic_level_like_is_used_when_the_floor_has_none():
    payload = make_payload(like_count=125, post_stream={"posts": [{"post_number": 1, "cooked": "<p>x</p>"}]})
    assert LinuxDoTopic._from_payload(payload, "1").like_count == 125


def test_quote_reply_uses_the_shared_quote_renderer():
    """引用回复要整块斜体、作者行在引用块内、不写"引用"字样 (与 twitter/threads 同一套 helper)"""
    cooked = (
        '<aside class="quote no-group" data-username="paomian_1" data-post="1">'
        '<div class="title"><img class="avatar" src="https://cdn.ldstatic.com/a.png" '
        'width="24" height="24"> paomian_1:</div>'
        "<blockquote><p>被引用的第一段</p><p>被引用的第二段</p></blockquote></aside>"
        "<p>我的回复</p>"
    )
    payload = make_payload()
    payload["post_stream"]["posts"][0]["cooked"] = cooked
    topic = LinuxDoTopic._from_payload(payload, "2979226")
    body = topic.markdown_content

    # ⚠️ 这里测的是**正文内部**的 Discourse 引用 (`aside.quote`) —— 它原地渲染、
    # 没有独立的媒体段, 所以**不声明角色**（只有"分享楼层时补的上下文层"才声明，
    # 见 test_linuxdo_floor_window.py 的分享楼层用例）。
    assert topic.quote_roles == [], topic.quote_roles
    # 作者在引用块内 (带主页链接), 不在块外裸着
    assert '> <i><a href="https://linux.do/u/paomian_1">@paomian_1</a></i>' in body
    # 引用内容整块斜体
    assert "> <i>被引用的第一段</i>" in body
    assert "> <i>被引用的第二段</i>" in body
    # 不写 "引用/回复" 标签字样 (引用内容本身可能含这些字)
    assert "<i>引用" not in body
    assert "<i>回复" not in body
    # 引用块之外的正文仍在, 且不在引用块里
    assert "我的回复" in body
    assert not any(line.startswith("> ") and "我的回复" in line for line in body.splitlines())
    # 头像不残留
    assert "avatar" not in body


def test_quote_placeholder_never_leaks():
    """占位符不能在输出里露出来"""
    cooked = (
        '<aside class="quote" data-username="someone"><blockquote><p>x</p></blockquote></aside><p>y</p>'
    )
    payload = make_payload()
    payload["post_stream"]["posts"][0]["cooked"] = cooked
    body = LinuxDoTopic._from_payload(payload, "1").markdown_content
    assert "linuxdo-quote" not in body


def test_text_content_keeps_the_quoted_text():
    """纯文本正文仍应包含被引用的内容 (取自改写前的 DOM)"""
    cooked = (
        '<aside class="quote" data-username="someone"><blockquote><p>被引用</p></blockquote></aside><p>回复</p>'
    )
    payload = make_payload()
    payload["post_stream"]["posts"][0]["cooked"] = cooked
    topic = LinuxDoTopic._from_payload(payload, "1")
    assert "被引用" in topic.text_content
    assert "回复" in topic.text_content
    assert "linuxdo-quote" not in topic.text_content


def test_unsupported_url_is_rejected():
    """用户页/其它站点不能被当成话题解析"""
    for url in ("https://linux.do/u/VerenQwQ", "https://linux.do/latest", "https://example.com/t/topic/1"):
        with pytest.raises(LinuxDoError):
            LinuxDoTopic.get_topic_id(url)


# ── 字段映射 ─────────────────────────────────────────────────


def test_topic_fields_are_mapped():
    topic = LinuxDoTopic._from_payload(make_payload(), "2979226")
    assert topic.topic_id == "2979226"
    assert topic.title == "国庆快乐www今天是腿子纯享"
    assert topic.author_name == "VerenOwO"
    assert topic.author_handle == "VerenQwQ"
    # 时间与点赞取楼层级 (主题级的 created_at/like_count 是楼主帖与全话题的)
    assert topic.published_at == "2026-10-03T04:30:46.334Z"
    assert topic.view_count == 927
    assert topic.like_count == 75
    assert topic.reply_count == 5


def test_tags_and_nsfw_flag():
    """只有平台自己的 NSFW 标签才置敏感位"""
    topic = LinuxDoTopic._from_payload(make_payload(), "2979226")
    assert topic.tags == ["纯水", "NSFW"]
    assert topic.is_sensitive is True


def test_not_sensitive_without_nsfw_tag():
    payload = make_payload(tags=[{"name": "纯水"}])
    assert LinuxDoTopic._from_payload(payload, "2979226").is_sensitive is False


def test_missing_counts_stay_none():
    """平台不给的计数留 None, 展示层整段跳过"""
    payload = make_payload()
    payload.pop("views")
    payload.pop("like_count")
    payload["post_stream"]["posts"][0].pop("reaction_users_count")
    topic = LinuxDoTopic._from_payload(payload, "2979226")
    assert topic.view_count is None
    assert topic.like_count is None


def test_author_falls_back_to_post_author():
    payload = make_payload(details={})
    topic = LinuxDoTopic._from_payload(payload, "2979226")
    assert topic.author_handle == "VerenQwQ"
    assert topic.author_name == "VerenOwO"


# ── 正文与媒体 ───────────────────────────────────────────────


def test_cooked_is_converted_and_cleaned():
    """details 展开、lightbox 包裹去掉, 正文不再是嵌套图片语法"""
    topic = LinuxDoTopic._from_payload(make_payload(), "2979226")
    assert "杂鱼杂鱼杂鱼" in topic.markdown_content
    assert "</details>" not in topic.markdown_content
    assert "lightbox-wrapper" not in topic.markdown_content
    # 图片走 media, 正文里不重复
    assert "![IMG_7961]" not in topic.markdown_content


def test_image_uses_original_url_with_dimensions():
    """lightbox 的 href 是原图, img 的宽高用来声明比例"""
    topic = LinuxDoTopic._from_payload(make_payload(), "2979226")
    assert len(topic.images) == 1
    image = topic.images[0]
    assert image.url == "https://cdn3.ldstatic.com/original/4X/e/c/c/abc.jpeg"
    assert (image.width, image.height) == (375, 500)


def test_image_without_lightbox_uses_src():
    cooked = '<p><img src="https://cdn3.ldstatic.com/a.jpeg" width="10" height="20"></p>'
    payload = make_payload()
    payload["post_stream"]["posts"][0]["cooked"] = cooked
    topic = LinuxDoTopic._from_payload(payload, "2979226")
    assert topic.images[0].url == "https://cdn3.ldstatic.com/a.jpeg"


def test_duplicate_images_are_deduped():
    cooked = (
        '<p><img src="https://cdn3.ldstatic.com/a.jpeg" width="1" height="1">'
        '<img src="https://cdn3.ldstatic.com/a.jpeg" width="1" height="1"></p>'
    )
    payload = make_payload()
    payload["post_stream"]["posts"][0]["cooked"] = cooked
    assert len(LinuxDoTopic._from_payload(payload, "2979226").images) == 1


def test_empty_post_stream_is_an_error():
    with pytest.raises(LinuxDoError):
        LinuxDoTopic._from_payload(make_payload(post_stream={"posts": []}), "2979226")


def test_text_content_is_plain():
    topic = LinuxDoTopic._from_payload(make_payload(), "2979226")
    assert "杂鱼杂鱼杂鱼" in topic.text_content
    assert "<p>" not in topic.text_content


def test_the_image_caption_stays_out_of_the_plain_text():
    """图片的说明文字（文件名 / "分辨率 体积"）不能进纯文本正文。

    用户报「正文多了个 image 分辨率体积」—— 那帖是**纯图楼层**（markdown 为空）,
    正文取 ``text_content``; 而 lightbox 的包裹里除了图还有 ``span.filename``
    （文件名）与 ``span.informations``（``746×330 24.3 KB``），清理晚一步就会被抓进来。

    结构取自真实响应（linux.do/t/topic/2987239）。
    """
    cooked = (
        '<p><div class="lightbox-wrapper">'
        '<a class="lightbox" href="https://cdn3.ldstatic.com/original/4X/7/5/b/abc.png" '
        'data-download-href="/uploads/short-url/gN.png?dl=1" title="image" rel="noopener nofollow ugc">'
        '<img src="https://cdn3.ldstatic.com/original/4X/7/5/b/abc.png" alt="image" '
        'data-base62-sha1="gN" width="690" height="305" data-dominant-color="232426">'
        '<div class="meta">'
        '<svg class="fa d-icon d-icon-far-image svg-icon" aria-hidden="true"><use href="#far-image"></use></svg>'
        '<span class="filename">image</span>'
        '<span class="informations">746×330 24.3 KB</span>'
        "</div></a></div></p>"
    )
    payload = make_payload()
    payload["post_stream"]["posts"][0]["cooked"] = cooked
    topic = LinuxDoTopic._from_payload(payload, "2979226")

    # 图照样抽出来了（清的是说明文字, 不是图）
    assert len(topic.images) == 1
    assert topic.images[0].url == "https://cdn3.ldstatic.com/original/4X/7/5/b/abc.png"
    # 说明文字不进正文
    assert "746×330" not in topic.text_content, topic.text_content
    assert "24.3 KB" not in topic.text_content, topic.text_content
    assert "746×330" not in topic.markdown_content, topic.markdown_content


def test_the_lightbox_filename_stays_out_of_the_plain_text():
    """同一个坑的另一半: fixture 的 ``span.filename``（IMG_7961）也不该漏进纯文本"""
    topic = LinuxDoTopic._from_payload(make_payload(), "2979226")
    assert "IMG_7961" not in topic.text_content, topic.text_content
