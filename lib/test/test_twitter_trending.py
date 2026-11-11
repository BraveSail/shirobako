"""X 的 trending 卡片（``unified_card`` 的 ``topic_detail``）必须被解析和渲染。

## 这个 bug 的实际形态

用户报「**是个 X 图片，不是 trending 里的图**」。这条推文的正文只是几句感想，
末尾挂了一个 ``https://t.co/…`` ——展开后是 ``https://x.com/i/trending/<id>``。
X 在客户端把它渲染成一张**卡片**（标题 + 摘要 + 配图），配图才是「图」的来源。

旧的解析只从 ``unified_card`` 抽 ``media_entities``（图），**标题与摘要整个丢掉**，
于是发出去只剩一张孤零零的封面图 —— 用户看不到任何 trending 内容。

## 断言取向

- 解析层：``tweet.trending`` 有 title / subtitle / url
- 渲染层：正文里出现标题与摘要，且**裸链被去掉**（卡片已带链接）
- 非 trending 的 unified_card 不受影响（返回 None）
- 正文与 URL 挤在同一行时，**只摘 URL、不吞同行文字**（实测踩过）
"""

from __future__ import annotations

import json

from parsehub.provider_api.twitter import TrendingCard, Twitter, TwitterTweet
from parsehub.parsers.parser.twitter import TwitterParser


def _node_with_trending(
    *,
    title: str = "标题",
    subtitle: str = "摘要",
    url: str = "https://x.com/i/trending/2120353200146411925",
    with_detail: bool = True,
) -> dict:
    card_data: dict = {
        "media_entities": {
            "media_1": {
                "type": "photo",
                "media_url_https": "https://pbs.twimg.com/media/AAA.jpg",
                "original_info": {"width": 100, "height": 100},
            }
        },
        "destination_objects": {"browser_1": {"type": "browser", "data": {"url_data": {"url": url}}}},
        "components": ["media_with_details_horizontal_1"],
    }
    component: dict = {"type": "media_with_details_horizontal", "data": {}}
    if with_detail:
        component["data"]["topic_detail"] = {
            "title": {"content": title},
            "subtitle": {"content": subtitle},
        }
    card_data["component_objects"] = {"media_with_details_horizontal_1": component}
    return {
        "card": {
            "legacy": {
                "name": "unified_card",
                "binding_values": [
                    {"key": "unified_card", "value": {"string_value": json.dumps(card_data)}},
                ],
            }
        }
    }


class TestParseTrending:
    def test_title_subtitle_url_extracted(self) -> None:
        node = _node_with_trending(title="井口由香写真", subtitle="11月9日发售", url="https://x.com/i/trending/999")
        card = Twitter._parse_trending_card(node)
        assert card is not None
        assert card.title == "井口由香写真"
        assert card.subtitle == "11月9日发售"
        assert card.url == "https://x.com/i/trending/999"

    def test_non_trending_unified_card_returns_none(self) -> None:
        """外链预览那类 unified_card 没有 topic_detail，必须零影响。"""
        node = _node_with_trending(with_detail=False)
        assert Twitter._parse_trending_card(node) is None

    def test_plain_card_returns_none(self) -> None:
        node = {"card": {"legacy": {"name": "summary_large_image", "binding_values": []}}}
        assert Twitter._parse_trending_card(node) is None

    def test_broken_json_returns_none(self) -> None:
        node = {
            "card": {
                "legacy": {
                    "name": "unified_card",
                    "binding_values": [{"key": "unified_card", "value": {"string_value": "{not json"}}],
                }
            }
        }
        assert Twitter._parse_trending_card(node) is None


class TestRenderTrending:
    def _tweet(self, url: str = "https://x.com/i/trending/2120353200146411925") -> TwitterTweet:
        return TwitterTweet(
            tweet_id="1",
            full_text="作者感想",
            trending=TrendingCard(title="标题", subtitle="摘要文字", url=url),
        )

    def test_block_has_title_summary_link(self) -> None:
        block = TwitterParser._trending_block(self._tweet())
        assert "标题" in block
        assert "摘要文字" in block
        assert "x.com/i/trending/2120353200146411925" in block
        # 每一行都是引用块成员
        for line in block.splitlines():
            assert line.startswith(">")

    def test_no_trending_returns_empty(self) -> None:
        tweet = TwitterTweet(tweet_id="1", full_text="x")
        assert TwitterParser._trending_block(tweet) == ""

    def test_strip_url_keeps_same_line_text(self) -> None:
        """★ 实测踩过：正文和 URL 挤在同一行，整行过滤会吞掉原文。"""
        tweet = self._tweet()
        body = "第一行\n１日１日を大切に！！！いきたい！ https://t.co/uT2C7ASW9N"
        # needle 在展开后的 URL 上，这里模拟已展开形态
        body_expanded = (
            "第一行\n１日１日を大切に！！！いきたい！ https://x.com/i/trending/2120353200146411925"
        )
        out = TwitterParser._strip_trending_url(body_expanded, tweet)
        assert "いきたい" in out, "同行文字被吞掉了"
        assert "/i/trending/2120353200146411925" not in out
        assert "第一行" in out.upper() or "第一行" in out

    def test_strip_leaves_other_urls_alone(self) -> None:
        tweet = self._tweet()
        body = "看这个 https://example.com/a 和 trending https://x.com/i/trending/2120353200146411925"
        out = TwitterParser._strip_trending_url(body, tweet)
        assert "example.com/a" in out
        assert "/i/trending/2120353200146411925" not in out

    def test_strip_is_noop_without_trending(self) -> None:
        tweet = TwitterTweet(tweet_id="1", full_text="x")
        body = "原样 https://x.com/i/trending/123"
        assert TwitterParser._strip_trending_url(body, tweet) == body
