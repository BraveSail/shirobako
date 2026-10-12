"""Threads 投票（poll）: 解析 + 渲染。

用户报：`https://www.threads.com/@snowmaple_official/post/DeY4kZBGnhG` 的投票没处理。

**取证**（真实响应，见 ``fixtures/threads_poll.json``）::

    post["caption_add_on"]["poll"] = {
        "__typename": "XDTPollSticker",
        "tallies": [{"count": 11, "text": "あいはんする"},
                    {"count": 8,  "text": "そうはんする"}],
        "finished": false,
        "viewer_can_vote": false,
        ...
    }

**关键结构**：投票**不在** ``caption.text`` 上（caption 只有作者写的正文），
在 ``caption_add_on.poll`` 里 —— 与长文块（``snippet_attachment_info``）同属
"caption 底下的附加内容"，但键不同。票数（``tallies[].count``）就在
**同一次响应**里，不需要额外请求（与 linux.do 的 ``post["polls"]`` 同一结论）。

**形态**：与 twitter / linux.do 的投票一致（选项 / 票数 / 占比三列）——
表格由渲染层转成服务端的 Table 块。
"""

import asyncio
import json
from pathlib import Path

from parsehub.parsers.parser.threads import ThreadsParser
from parsehub.provider_api.threads import ThreadsPoll, ThreadsPost

FIXTURE = Path(__file__).parent / "fixtures" / "threads_poll.json"


def _real_post() -> ThreadsPost:
    payload = json.loads(FIXTURE.read_text())
    node = payload["data"]["data"]["edges"][0]["node"]["thread_items"][0]["post"]
    return ThreadsPost.from_graphql(node)


def _post(poll=None, **kwargs) -> ThreadsPost:
    base = {"content": "正文", "author_name": "作者", "author_handle": "handle"}
    base.update(kwargs)
    return ThreadsPost(poll=poll, **base)


def _poll(choices, **kwargs) -> ThreadsPoll:
    return ThreadsPoll(choices=choices, **kwargs)


def _render(post: ThreadsPost, reply_to: ThreadsPost | None = None) -> str:
    if reply_to is not None:
        post.reply_to = reply_to

    async def _parse(_url):
        return post

    parser = ThreadsParser()
    parser._parse = _parse  # type: ignore[method-assign]
    return asyncio.run(parser._do_parse("https://www.threads.com/@x/post/1")).content


# ---------------------------------------------------------------- 解析（真实响应）


def test_the_choices_and_counts_come_from_the_caption_add_on():
    """**核心**: 选项与票数来自 ``caption_add_on.poll.tallies``, 与响应原文逐项一致。"""
    poll = _real_post().poll
    assert poll is not None, "投票整条丢失了"
    assert poll.choices == [("あいはんする", 11), ("そうはんする", 8)]
    assert poll.is_finished is False


def test_a_post_without_a_poll_has_none():
    assert _post().poll is None


def test_a_missing_count_is_zero_rather_than_dropping_the_option():
    """票数缺失按 0 算 —— 选项本身仍要显示, 不该整条投票丢掉"""
    poll = ThreadsPost._parse_poll(
        {"caption_add_on": {"poll": {"tallies": [{"text": "A"}]}}}
    )
    assert poll is not None
    assert poll.choices == [("A", 0)]


def test_a_poll_without_options_produces_nothing():
    """没有 tallies（或全空文案）时不产出投票对象, 不抛错"""
    assert ThreadsPost._parse_poll({"caption_add_on": {"poll": {"tallies": []}}}) is None
    assert ThreadsPost._parse_poll({"caption_add_on": {"poll": {"tallies": [{"count": 1}]}}}) is None
    assert ThreadsPost._parse_poll({"caption_add_on": {}}) is None
    assert ThreadsPost._parse_poll({"caption": {"text": "x"}}) is None
    assert ThreadsPost._parse_poll({}) is None


# ---------------------------------------------------------------- 渲染


def test_the_poll_renders_as_a_table():
    """**核心**: 渲染成三列表格（与 twitter / linux.do 的投票同一形态）"""
    text = _render(_post(poll=_real_post().poll))
    assert "| 选项 | 票数 | 占比 |" in text, text
    assert "| --- | --- | --- |" in text, text
    assert "| あいはんする | 11 | 58% |" in text, text
    assert "| そうはんする | 8 | 42% |" in text, text


def test_the_percentage_is_of_the_total():
    """占比按**总票数**算（1/4=25%, 3/4=75%）"""
    text = _render(_post(poll=_poll([("少", 1), ("多", 3)])))
    assert "| 少 | 1 | 25% |" in text, text
    assert "| 多 | 3 | 75% |" in text, text


def test_the_body_is_kept_above_the_table():
    """作者写的正文照旧保留在表格上方"""
    text = _render(_post(poll=_poll([("A", 1)])))
    assert text.startswith("正文"), text
    assert text.index("正文") < text.index("| 选项 |")


def test_a_poll_with_no_votes_does_not_blow_up():
    """一票都没有时不抛错, 占比记 0%"""
    text = _render(_post(poll=_poll([("A", 0), ("B", 0)])))
    assert "| A | 0 | 0% |" in text, text


def test_a_pipe_in_an_option_does_not_break_the_table():
    """选项文案里的 ``|`` 要转义 —— 否则会把列切断"""
    text = _render(_post(poll=_poll([("a|b", 1)])))
    assert r"| a\|b | 1 | 100% |" in text, text


def test_a_post_without_a_poll_has_no_table():
    text = _render(_post())
    assert "| 选项 |" not in text
    assert text.strip() == "正文"


def test_an_empty_choices_list_renders_nothing():
    assert ThreadsParser._build_poll(_post(poll=_poll([]))) == ""


# ---------------------------------------------------------------- 长文块与投票共存


def test_the_poll_sits_between_the_body_and_the_snippet_quote():
    """投票表格排在正文之后、长文引用块之前"""
    text = _render(_post(content="正文", snippet="长文内容", poll=_poll([("A", 1)])))
    assert text.index("正文") < text.index("| 选项 |") < text.index("长文内容"), text


# ---------------------------------------------------------------- 回复里的投票


def test_a_poll_inside_the_replied_post_renders_in_its_own_block():
    """被回复帖带投票时, 表格渲染在**它自己的引用块里**（与 twitter 一致）"""
    reply = ThreadsPost(
        content="回复帖正文", author_name="别人", author_handle="other", poll=_poll([("A", 3), ("B", 1)])
    )
    text = _render(_post(content="主帖"), reply_to=reply)
    lines = text.splitlines()
    table_at = next(i for i, ln in enumerate(lines) if "| 选项 |" in ln)
    assert all(ln.startswith("> ") for ln in lines[table_at : table_at + 4]), lines[table_at : table_at + 4]


def test_the_quoted_poll_table_has_no_italic_wrapper():
    """引用块里的表格行不能包 `<i>` —— 包了服务端就不再当它是表格"""
    reply = ThreadsPost(
        content="回复帖正文", author_name="别人", author_handle="other", poll=_poll([("A", 3), ("B", 1)])
    )
    text = _render(_post(content="主帖"), reply_to=reply)
    lines = text.splitlines()
    table_at = next(i for i, ln in enumerate(lines) if "| 选项 |" in ln)
    for ln in lines[table_at : table_at + 4]:
        assert ln.startswith("> "), ln
        assert "<i>" not in ln, f"表格行被斜体包裹: {ln}"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
