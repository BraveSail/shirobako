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

**形态（2026-10-12 改造）**：lib 不再拼表格, 只在正文原来放表格的位置放独立一行
占位符 ``{{poll:N}}``, 结构化数据 (选项/票数) 进 ``result.polls`` ——
「选项 / 票数 / 占比」表格由渲染层按用户语言拼装。
"""

import asyncio
import json
from pathlib import Path

from parsehub.parsers.parser.threads import ThreadsParser
from parsehub.provider_api.threads import ThreadsPoll, ThreadsPost
from parsehub.types import Poll, PollOption

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


def _parse(post: ThreadsPost, reply_to: ThreadsPost | None = None):
    if reply_to is not None:
        post.reply_to = reply_to

    async def _fake_parse(_url):
        return post

    parser = ThreadsParser()
    parser._parse = _fake_parse  # type: ignore[method-assign]
    return asyncio.run(parser._do_parse("https://www.threads.com/@x/post/1"))


def _render(post: ThreadsPost, reply_to: ThreadsPost | None = None) -> str:
    return _parse(post, reply_to).content


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


# ---------------------------------------------------------------- 渲染（占位符 + 结构化数据）


def test_the_poll_becomes_a_placeholder_with_structured_data():
    """**核心**: 正文里放占位符 ``{{poll:0}}``; 表格文案不再由 lib 拼。"""
    result = _parse(_post(poll=_real_post().poll))
    assert "{{poll:0}}" in result.content, result.content
    assert "| 选项 |" not in result.content, "lib 不该再拼表格"
    assert result.polls == [
        Poll(
            options=[PollOption(text="あいはんする", count=11), PollOption(text="そうはんする", count=8)],
            total_votes=19,
        )
    ], result.polls


def test_the_total_votes_are_carried():
    """总票数进结构化数据 —— 渲染层的占比要用它"""
    result = _parse(_post(poll=_poll([("少", 1), ("多", 3)])))
    (poll,) = result.polls
    assert poll.total_votes == 4
    assert [(o.text, o.count) for o in poll.options] == [("少", 1), ("多", 3)]


def test_the_body_is_kept_above_the_placeholder():
    """作者写的正文照旧保留在投票占位符上方"""
    text = _render(_post(poll=_poll([("A", 1)])))
    assert text.startswith("正文"), text
    assert text.index("正文") < text.index("{{poll:0}}")


def test_a_poll_with_no_votes_still_yields_the_options():
    """一票都没有时不抛错; 选项照旧带出来 (count 是 0, 不是 None)"""
    result = _parse(_post(poll=_poll([("A", 0), ("B", 0)])))
    (poll,) = result.polls
    assert [(o.text, o.count) for o in poll.options] == [("A", 0), ("B", 0)]


def test_a_pipe_in_an_option_is_kept_verbatim():
    """选项文案**原样**进结构化数据 —— 表格转义已移到渲染层, lib 不动它"""
    result = _parse(_post(poll=_poll([("a|b", 1)])))
    (poll,) = result.polls
    assert poll.options[0].text == "a|b"


def test_a_post_without_a_poll_has_no_placeholder():
    result = _parse(_post())
    assert "{{poll:" not in result.content
    assert result.polls == []
    assert result.content.strip() == "正文"


def test_an_empty_choices_list_renders_nothing():
    assert ThreadsParser._poll_placeholder(_post(poll=_poll([])), 0) == ""
    result = _parse(_post(poll=_poll([])))
    assert "{{poll:" not in result.content
    assert result.polls == []


# ---------------------------------------------------------------- 长文块与投票共存


def test_the_poll_sits_between_the_body_and_the_snippet_quote():
    """投票占位符排在正文之后、长文引用块之前"""
    text = _render(_post(content="正文", snippet="长文内容", poll=_poll([("A", 1)])))
    assert text.index("正文") < text.index("{{poll:0}}") < text.index("长文内容"), text


# ---------------------------------------------------------------- 回复里的投票


def test_a_poll_inside_the_replied_post_renders_in_its_own_block():
    """被回复帖带投票时, 占位符渲染在**它自己的引用块里**（与 twitter 一致）"""
    reply = ThreadsPost(
        content="回复帖正文", author_name="别人", author_handle="other", poll=_poll([("A", 3), ("B", 1)])
    )
    result = _parse(_post(content="主帖"), reply_to=reply)
    lines = result.content.splitlines()
    placeholder_at = next(i for i, ln in enumerate(lines) if "{{poll:" in ln)
    assert lines[placeholder_at] == "> {{poll:0}}", lines[placeholder_at]
    assert [(o.text, o.count) for o in result.polls[0].options] == [("A", 3), ("B", 1)]


def test_the_quoted_poll_placeholder_has_no_italic_wrapper():
    """引用块里的占位符行不能包 `<i>` —— 渲染层要按整行替换"""
    reply = ThreadsPost(
        content="回复帖正文", author_name="别人", author_handle="other", poll=_poll([("A", 3), ("B", 1)])
    )
    result = _parse(_post(content="主帖"), reply_to=reply)
    line = next(ln for ln in result.content.splitlines() if "{{poll:" in ln)
    assert line == "> {{poll:0}}", line
    assert "<i>" not in line, f"占位符行被斜体包裹: {line}"


def test_the_indexes_follow_the_placeholder_order():
    """**索引纪律**: polls[i] 对应正文里第 i 个占位符 —— reply → 主帖。"""
    reply = ThreadsPost(
        content="回复帖正文", author_name="别人", author_handle="other", poll=_poll([("R", 1)])
    )
    result = _parse(_post(content="主帖", poll=_poll([("M", 2)])), reply_to=reply)

    assert [p.options[0].text for p in result.polls] == ["R", "M"], result.polls
    order = [ln.strip() for ln in result.content.splitlines() if "{{poll:" in ln]
    assert order == ["> {{poll:0}}", "{{poll:1}}"], result.content


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
