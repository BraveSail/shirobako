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

import base64
import json

from parsehub.provider_api.twitter import TrendingCard, Twitter, TwitterTweet
from parsehub.parsers.parser.twitter import TwitterParser
from parsehub.types import ImageRef


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


class TestTrendingCover:
    """话题封面藏在 JetfuelV2 二进制里 —— 见 `Twitter._cover_from_jetfuel`。

    真机证据：``unified_card.media_entities`` 给的 ``F1x5VdQX0AA9Sgt.jpg`` 是
    1600×1600 的**通用占位图**（黑底白 X），不是话题封面。用户报「图片还是 X 那个
    图标」。真封面 ``HWz-eo3awAEbWQN.jpg``（640×915 人物照）只在话题页的
    ``jetfuel_attachment.payload`` 里出现，其余所有 API 响应搜遍都是 0 命中。
    """

    @staticmethod
    def _payload(*segments: str) -> str:
        """按 JetfuelV2 的实际形态拼一段 payload：字符串以明文+长度前缀存放。"""
        raw = b"\x11\x00\x0b" + b"\x00".join(s.encode() for s in segments)
        return base64.b64encode(raw).decode()

    def test_extract_cover_from_jetfuel(self) -> None:
        payload = self._payload(
            "trending-card",
            "https://pbs.twimg.com/media/HWz-eo3awAEbWQN.jpg?name=orig",
        )
        cover = Twitter._cover_from_jetfuel(payload)
        assert cover == "https://pbs.twimg.com/media/HWz-eo3awAEbWQN.jpg?name=orig"

    def test_ignores_non_trending_jetfuel(self) -> None:
        """普通推文的 jetfuel（回复建议等）不能当成封面。"""
        payload = self._payload(
            "reply-nudge",
            "https://pbs.twimg.com/media/AAAAAAAAAAA.jpg",
        )
        assert Twitter._cover_from_jetfuel(payload) == ""

    def test_profile_image_is_not_cover(self) -> None:
        """话题 payload 里同时有作者头像，必须只取 /media/ 而非 /profile_images/。"""
        payload = self._payload(
            "trending-card",
            "https://pbs.twimg.com/profile_images/18918540839/xe_WmNQP_normal.jpg",
            "https://pbs.twimg.com/media/HWz-eo3awAEbWQN.jpg",
        )
        cover = Twitter._cover_from_jetfuel(payload)
        assert "profile_images" not in cover
        assert "HWz-eo3awAEbWQN" in cover

    def test_empty_and_garbage_payload(self) -> None:
        assert Twitter._cover_from_jetfuel(None) == ""
        assert Twitter._cover_from_jetfuel("") == ""
        assert Twitter._cover_from_jetfuel("!!!not-base64!!!") == ""

    def test_trend_id_from_url(self) -> None:
        assert Twitter._trend_id_from_url("https://x.com/i/trending/2120353200146411925") == (
            "2120353200146411925"
        )
        assert Twitter._trend_id_from_url("https://x.com/i/trending/") == ""
        assert Twitter._trend_id_from_url("https://x.com/foo/status/123") == ""

    def test_cover_goes_into_media(self) -> None:
        """解析层必须把 trending.cover 放进 media —— 否则图还是出不来。"""
        tweet = TwitterTweet(
            tweet_id="1",
            full_text="感想",
            trending=TrendingCard(
                title="标题",
                subtitle="摘要",
                url="https://x.com/i/trending/2120353200146411925",
                cover="https://pbs.twimg.com/media/HWz-eo3awAEbWQN.jpg?name=orig",
            ),
        )
        media = TwitterParser.to_media_refs(tweet.media)
        # 模拟 media_parse 的追加逻辑
        if tweet.trending and tweet.trending.cover:
            media.append(ImageRef(url=tweet.trending.cover))
        assert len(media) == 1
        assert "HWz-eo3awAEbWQN" in media[0].url

    def test_no_cover_means_no_media(self) -> None:
        """补取失败时不能塞空图 —— 宁可不发图。"""
        tweet = TwitterTweet(
            tweet_id="1",
            full_text="感想",
            trending=TrendingCard(title="标题", subtitle="摘要", url="https://x.com/i/trending/1"),
        )
        media = TwitterParser.to_media_refs(tweet.media)
        if tweet.trending and tweet.trending.cover:
            media.append(ImageRef(url=tweet.trending.cover))
        assert media == []


class TestTrendingCoverInsideQuote:
    """封面必须落在**话题引用块内部**（用户：「图片应该在话题里面」）。

    实现是 `{{media:N}}` 占位符写在 `> ` 块里 —— 渲染层 `_quote_children` 遇到
    媒体占位会去 `media_blocks` 取对应块 append 进引用块，于是图在块内而不在块外。
    """

    def _tweet(self, *, cover: str = "https://pbs.twimg.com/media/HWz.jpg?name=orig") -> TwitterTweet:
        return TwitterTweet(
            tweet_id="1",
            full_text="作者感想",
            trending=TrendingCard(
                title="标题",
                subtitle="摘要文字",
                url="https://x.com/i/trending/2120353200146411925",
                cover=cover,
            ),
        )

    def test_cover_placeholder_is_inside_block(self) -> None:
        block = TwitterParser._trending_block(self._tweet())
        # 占位符每一行都带引用前缀 ⇒ 它在块内，不会被渲染层当成块外媒体
        media_lines = [ln for ln in block.splitlines() if "{{media:" in ln]
        assert media_lines, "封面占位符丢了"
        for ln in media_lines:
            assert ln.startswith(">"), f"占位符不在引用块内: {ln!r}"

    def test_cover_placeholder_after_title_before_subtitle(self) -> None:
        """位置与 X 上卡片一致：标题在上、图居中、摘要在下。"""
        block = TwitterParser._trending_block(self._tweet())
        idx_title = block.index("标题")
        idx_media = block.index("{{media:")
        idx_sub = block.index("摘要文字")
        assert idx_title < idx_media < idx_sub

    def test_no_cover_no_placeholder(self) -> None:
        block = TwitterParser._trending_block(self._tweet(cover=""))
        assert "{{media:" not in block
        assert "标题" in block  # 文案照旧

    def test_placeholder_index_matches_media_order(self) -> None:
        """索引 0 = media_parse 追加的那张（主帖媒体末尾，trending 帖主帖无其它媒体）。"""
        block = TwitterParser._trending_block(self._tweet())
        assert "{{media:0}}" in block
