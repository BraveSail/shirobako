"""threads 帖子话题（``text_post_app_info.tag_header``）→ 作者行末尾的 `` > 話題``。

真实结构（2026-10-20 实测 ``@snowmaple_official/post/DerX8SaGo1a``）::

    "tag_header": {"display_name": "申請審判", "id": "18626374978065524",
                   "is_community": false, "tag_cluster_name": "申請審判",
                   "community_emoji": null}

没有话题时该字段是 ``null``。话题页地址实测 ``/tag/<URL 编码后的话题名>`` 返回 200
（``/tagged/…`` 是 404）。渲染位置跟着 threads 自己的显示走：接在**作者行末尾**。
"""

import asyncio

from parsehub.parsers.parser.threads import TOPIC_PAGE_URL, ThreadsParser, _topic_label
from parsehub.provider_api.threads import ThreadsPost


def _post(**kwargs) -> ThreadsPost:
    base = {"content": "正文", "author_name": "作者", "author_handle": "handle"}
    base.update(kwargs)
    return ThreadsPost(**base)


def _parse(post: ThreadsPost):
    async def _fake_parse(_url):
        return post

    parser = ThreadsParser()
    parser._parse = _fake_parse  # type: ignore[method-assign]
    return asyncio.run(parser._do_parse("https://www.threads.com/@x/post/1"))


# ---------------------------------------------------------------- provider 提取

def test_the_topic_comes_from_the_tag_header():
    raw = {
        "text_post_app_info": {
            "tag_header": {
                "display_name": "申請審判",
                "id": "18626374978065524",
                "is_community": False,
                "tag_cluster_name": "申請審判",
                "community_emoji": None,
            }
        }
    }
    assert ThreadsPost._parse_topic(raw) == "申請審判"


def test_no_tag_header_means_no_topic():
    assert ThreadsPost._parse_topic({}) == ""
    assert ThreadsPost._parse_topic({"text_post_app_info": {"tag_header": None}}) == ""
    assert ThreadsPost._parse_topic({"text_post_app_info": {}}) == ""


# ---------------------------------------------------------------- 片段形态

def test_the_label_is_a_link_to_the_topic_page():
    """话题是**链接**、不加粗（加粗留给作者名）—— 加粗会让人以为它是署名的一部分"""
    label = _topic_label("申請審判")
    assert label == '<a href="https://www.threads.com/tag/%E7%94%B3%E8%AB%8B%E5%AF%A9%E5%88%A4">申請審判</a>'
    assert not label.startswith("**")
    assert TOPIC_PAGE_URL.format(name="x") == "https://www.threads.com/tag/x"


def test_an_empty_topic_yields_no_label():
    assert _topic_label("") == ""


def test_the_topic_name_is_escaped():
    label = _topic_label("a<b>&c")
    assert "a&lt;b&gt;&amp;c" in label
    assert "<b>" not in label


# ---------------------------------------------------------------- 端到端

def test_the_result_carries_the_topic_label():
    """话题落在 ``topic_label`` 上 —— 渲染层接在**作者行末尾**（``… @handle > 話題``）"""
    result = _parse(_post(topic="申請審判"))
    assert result.topic_label == _topic_label("申請審判")
    # 话题**不是**归属行（那是 bgm 的位置：标题与作者之间）
    assert result.origin_line == ""


def test_a_post_without_a_topic_has_no_topic_label():
    assert _parse(_post()).topic_label == ""
