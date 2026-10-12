"""twitter 投票（poll）: 解析 + 渲染。

用户报：`https://twitter.com/thsottiaux/status/2107576143285219799` 的投票没处理。

**取证**（真实响应，见 ``fixtures/twitter_poll_card.json``）::

    tweet.full_text = "Vote"            <- 正文只有作者写的这一个词
    node["card"].legacy.name = "poll2choice_text_only"
    binding_values:
      choice1_label = "👌(good day)"        choice1_count = "11805"
      choice2_label = "🫨 (needs a reset)"  choice2_count = "34875"
      end_datetime_utc = "2026-10-07T00:58:03Z"
      counts_are_final = {boolean_value: false}

**关键结构**：投票**不在** ``legacy`` 上（``legacy`` 里没有任何 poll 字段），只在
``node["card"]`` 里 —— 与媒体、标签那两条路都不同，所以以前整条丢掉。

**选项数不固定**：卡片名形如 ``poll{2,3,4}choice_{text_only,image}``，键是
``choice{N}_label`` / ``choice{N}_count`` ⇒ 按 N 递增取，别硬编码 2 个。

**形态（2026-10-12 改造）**：lib 不再拼表格, 只在正文原来放表格的位置放独立一行
占位符 ``{{poll:N}}``, 结构化数据 (选项/票数) 进 ``result.polls`` ——
「选项 / 票数 / 占比」表格由渲染层按用户语言拼装。
"""

import asyncio
import json
from pathlib import Path

from parsehub.parsers.parser.twitter import TwitterParser
from parsehub.provider_api.twitter import Twitter, TwitterPoll, TwitterTweet
from parsehub.types import Poll, PollOption

FIXTURE = Path(__file__).parent / "fixtures" / "twitter_poll_card.json"


def _real_tweet() -> TwitterTweet:
    return Twitter().parse(json.loads(FIXTURE.read_text()))


def _tweet(poll=None, **kwargs) -> TwitterTweet:
    base = {"tweet_id": "1", "full_text": "Vote", "author_name": "作者", "author_handle": "handle"}
    base.update(kwargs)
    return TwitterTweet(**base) if poll is None else TwitterTweet(poll=poll, **base)


def _poll(choices, **kwargs) -> TwitterPoll:
    return TwitterPoll(choices=choices, **kwargs)


def _parse(tweet: TwitterTweet):
    return asyncio.run(TwitterParser.media_parse(tweet))


def _render(tweet: TwitterTweet) -> str:
    return _parse(tweet).content


# ---------------------------------------------------------------- 解析（真实响应）


def test_the_choices_and_counts_come_from_the_card():
    """**核心**: 选项与票数来自卡片, 与响应原文逐项一致。"""
    poll = _real_tweet().poll
    assert poll is not None, "投票整条丢失了"
    assert poll.choices == [("👌(good day)", 11805), ("🫨 (needs a reset)", 34875)]


def test_the_end_time_and_final_flag_are_parsed():
    poll = _real_tweet().poll
    assert poll.end_datetime is not None and poll.end_datetime.year == 2026
    assert poll.is_final is False, "counts_are_final=false, 投票还没结束"


def test_a_tweet_without_a_card_has_no_poll():
    assert _tweet().poll is None


def test_the_option_count_is_not_hardcoded():
    """选项数不固定 —— 3 个、4 个都要取全（卡片名 poll3choice_* / poll4choice_*）"""
    for count in (2, 3, 4):
        card = {
            "legacy": {
                "name": f"poll{count}choice_text_only",
                "binding_values": [
                    *(
                        {"key": f"choice{i}_label", "value": {"string_value": f"选项{i}"}}
                        for i in range(1, count + 1)
                    ),
                    *({"key": f"choice{i}_count", "value": {"string_value": str(i * 10)}} for i in range(1, count + 1)),
                ],
            }
        }
        poll = Twitter._parse_poll({"card": card})
        assert len(poll.choices) == count, count
        assert poll.choices[-1] == (f"选项{count}", count * 10)


def test_a_missing_count_is_zero_rather_than_dropping_the_option():
    """票数缺失按 0 算 —— 选项本身仍要显示, 不该整条投票丢掉"""
    card = {
        "legacy": {
            "name": "poll2choice_text_only",
            "binding_values": [{"key": "choice1_label", "value": {"string_value": "A"}}],
        }
    }
    poll = Twitter._parse_poll({"card": card})
    assert poll.choices == [("A", 0)]


def test_a_card_that_is_not_a_poll_produces_nothing():
    """别的卡片（外链预览等）不该被当成投票"""
    assert Twitter._parse_poll({"card": {"legacy": {"name": "summary_large_image"}}}) is None
    assert Twitter._parse_poll({}) is None


def test_a_poll_card_yields_no_cover_photo():
    """投票卡片的图是选项配图, 不是"外链预览图" —— 不该走封面那条路"""
    node = json.loads(FIXTURE.read_text())
    node = (node.get("data") or {}).get("tweetResult", {}).get("result", {})
    node = node.get("tweet") or node
    assert Twitter._parse_card_photo(node) is None


# ---------------------------------------------------------------- 渲染（占位符 + 结构化数据）


def test_the_poll_becomes_a_placeholder_with_structured_data():
    """**核心**: 正文里放占位符 ``{{poll:0}}``; 选项/票数/占比文案不再由 lib 拼。"""
    result = _parse(_tweet(poll=_real_tweet().poll))
    assert "{{poll:0}}" in result.content, result.content
    assert "| 选项 |" not in result.content, "lib 不该再拼表格"
    assert result.polls == [
        Poll(
            options=[
                PollOption(text="👌(good day)", count=11805),
                PollOption(text="🫨 (needs a reset)", count=34875),
            ],
            total_votes=46680,
        )
    ], result.polls


def test_the_total_votes_and_final_flag_are_carried():
    """总票数与"是否已结束"进结构化数据（渲染层的占比要总票数）"""
    result = _parse(_tweet(poll=_poll([("少", 1), ("多", 3)], is_final=True)))
    (poll,) = result.polls
    assert poll.total_votes == 4
    assert poll.is_finished is True
    assert [(o.text, o.count) for o in poll.options] == [("少", 1), ("多", 3)]


def test_the_body_is_kept_above_the_placeholder():
    """作者写的正文照旧保留在投票占位符上方"""
    text = _render(_tweet(poll=_poll([("A", 1)])))
    assert text.startswith("Vote"), text
    assert text.index("Vote") < text.index("{{poll:0}}")


def test_a_poll_with_no_votes_still_yields_the_options():
    """一票都没有时不抛错; 选项照旧带出来 (count 是 0, 不是 None)"""
    result = _parse(_tweet(poll=_poll([("A", 0), ("B", 0)])))
    (poll,) = result.polls
    assert [(o.text, o.count) for o in poll.options] == [("A", 0), ("B", 0)]
    assert poll.total_votes == 0


def test_a_pipe_in_an_option_is_kept_verbatim():
    """选项文案**原样**进结构化数据 —— 表格转义已移到渲染层, lib 不动它"""
    result = _parse(_tweet(poll=_poll([("a|b", 1)])))
    (poll,) = result.polls
    assert poll.options[0].text == "a|b"


def test_a_tweet_without_a_poll_has_no_placeholder():
    result = _parse(_tweet())
    assert "{{poll:" not in result.content
    assert result.polls == []
    assert result.content.strip() == "Vote"


def test_an_empty_choices_list_renders_nothing():
    assert TwitterParser._poll_placeholder(_tweet(poll=_poll([])), 0) == ""
    result = _parse(_tweet(poll=_poll([])))
    assert "{{poll:" not in result.content
    assert result.polls == []


# ---------------------------------------------------------------- 引用/回复里的投票


def test_a_poll_inside_the_quoted_post_renders_in_its_own_block():
    """被引用推文带投票时, 占位符渲染在**它自己的引用块里**（与 X 上一致）"""
    quoted = _tweet(tweet_id="2", full_text="引用里的投票", poll=_poll([("A", 3), ("B", 1)]))
    result = _parse(_tweet(full_text="主帖", quoted_status=quoted))
    lines = result.content.splitlines()
    placeholder_at = next(i for i, ln in enumerate(lines) if "{{poll:" in ln)
    assert lines[placeholder_at] == "> {{poll:0}}", lines[placeholder_at]
    # 结构化数据在 polls[0], 顺序与占位符一致
    assert [(o.text, o.count) for o in result.polls[0].options] == [("A", 3), ("B", 1)]


def test_a_poll_inside_the_replied_post_renders_in_its_own_block():
    reply = _tweet(tweet_id="3", full_text="回复里的投票", poll=_poll([("A", 1)]))
    result = _parse(_tweet(full_text="主帖", reply_to=reply))
    lines = result.content.splitlines()
    placeholder_at = next(i for i, ln in enumerate(lines) if "{{poll:" in ln)
    assert lines[placeholder_at] == "> {{poll:0}}", lines[placeholder_at]
    assert result.polls[0].options[0].text == "A"


def test_the_quoted_poll_placeholder_has_no_italic_wrapper():
    """引用块里的占位符行不能包 `<i>` —— 渲染层要按整行替换, 斜体标记会污染产物。"""
    quoted = _tweet(tweet_id="2", full_text="引用里的投票", poll=_poll([("A", 3), ("B", 1)]))
    result = _parse(_tweet(full_text="主帖", quoted_status=quoted))
    lines = result.content.splitlines()
    placeholder_at = next(i for i, ln in enumerate(lines) if "{{poll:" in ln)
    assert lines[placeholder_at] == "> {{poll:0}}", lines[placeholder_at]
    assert "<i>" not in lines[placeholder_at], f"占位符行被斜体包裹: {lines[placeholder_at]}"
    body_at = next(i for i, ln in enumerate(lines) if "引用里的投票" in ln)
    assert "<i>" in lines[body_at], lines[body_at]


def test_the_indexes_follow_the_placeholder_order():
    """**索引纪律**: polls[i] 对应正文里第 i 个占位符 —— reply → 主帖 → quoted。"""
    reply = _tweet(tweet_id="3", full_text="回复里的投票", poll=_poll([("R", 1)]))
    quoted = _tweet(tweet_id="2", full_text="引用里的投票", poll=_poll([("Q", 2)]))
    result = _parse(_tweet(full_text="主帖", reply_to=reply, quoted_status=quoted, poll=_poll([("M", 3)])))

    assert [p.options[0].text for p in result.polls] == ["R", "M", "Q"], result.polls
    lines = result.content.splitlines()
    order = [ln.strip() for ln in lines if "{{poll:" in ln]
    assert order == ["> {{poll:0}}", "{{poll:1}}", "> {{poll:2}}"], lines


def test_a_poll_only_in_the_reply_still_starts_at_zero():
    """只有 reply 带投票时索引从 0 开始 (不因为主帖没投票而跳号)"""
    reply = _tweet(tweet_id="3", full_text="回复里的投票", poll=_poll([("R", 1)]))
    result = _parse(_tweet(full_text="主帖", reply_to=reply))
    assert [p.options[0].text for p in result.polls] == ["R"]
    assert any(ln.strip() == "> {{poll:0}}" for ln in result.content.splitlines()), result.content


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
