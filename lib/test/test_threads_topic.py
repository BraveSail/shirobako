"""threads 帖子话题（``text_post_app_info.tag_header``）→ 归属行（标题与作者之间）。

真实结构（2026-10-20 实测 ``@snowmaple_official/post/DerX8SaGo1a``）::

    "tag_header": {"display_name": "申請審判", "id": "18626374978065524",
                   "is_community": false, "tag_cluster_name": "申請審判",
                   "community_emoji": null}

没有话题时该字段是 ``null``。话题页地址实测 ``/tag/<URL 编码后的话题名>`` 返回 200
（``/tagged/…`` 是 404）。
"""

import asyncio

from parsehub.parsers.parser.threads import TOPIC_PAGE_URL, ThreadsParser, _topic_origin_line
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


# ---------------------------------------------------------------- 归属行格式

def test_the_origin_line_links_to_the_topic_page():
    """形态与 bgm 归属行同形（加粗 + 链接），URL 走 ``/tag/<编码后的话题名>``"""
    line = _topic_origin_line("申請審判")
    assert line == '**<a href="https://www.threads.com/tag/%E7%94%B3%E8%AB%8B%E5%AF%A9%E5%88%A4">申請審判</a>**'
    assert TOPIC_PAGE_URL.format(name="x") == "https://www.threads.com/tag/x"


def test_an_empty_topic_yields_no_origin_line():
    assert _topic_origin_line("") == ""


def test_the_topic_name_is_escaped():
    line = _topic_origin_line("a<b>&c")
    assert "a&lt;b&gt;&amp;c" in line
    assert "<b>" not in line


# ---------------------------------------------------------------- 端到端

def test_the_result_carries_the_topic_as_the_origin_line():
    """话题落在 ``origin_line`` 上 —— 渲染层把它放在标题与作者之间（与 bgm 同一位置）"""
    result = _parse(_post(topic="申請審判"))
    assert result.origin_line == _topic_origin_line("申請審判")


def test_a_post_without_a_topic_has_no_origin_line():
    assert _parse(_post()).origin_line == ""
