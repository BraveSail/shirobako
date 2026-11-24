# mypy: disable-error-code=no-untyped-def
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from curl_cffi.requests import Response
from loguru import logger

from ..errors import TranslationUnavailable
from ..types import ParseError
from ..types.document import Block, TextRun, paragraph
from ..types.x_status import _utf16_boundaries, _web_url, text_runs
from ..utils import http
from ..utils.helpers import UA, to_datetime, to_int

#: 长推文 richtext 的行内样式 → Markdown 标记 (与 ``ArticleRenderer._INLINE_STYLES``
#: 同一套映射; 那个是 Article 的块级渲染, 这里是 note_tweet 正文的区间标记)。
_INLINE_STYLES: dict[str, str] = {
    "Bold": "**",
    "Italic": "*",
    "Strikethrough": "~~",
}


class XTranslationFailure(TranslationUnavailable):
    """Translation failure with safe diagnostic fields, not the upstream error text."""

    def __init__(self, reason: str, message: str, *, stage: str, **details: str | int) -> None:
        super().__init__(message)
        self.stage = stage
        self.reason = reason
        self.details = details


_TRANSLATION_MEDIA_TYPES = {
    "application/json": "json",
    "application/ndjson": "ndjson",
    "application/x-ndjson": "ndjson",
    "application/json-seq": "json_seq",
    "text/event-stream": "sse",
    "text/html": "html",
    "text/plain": "plain",
    "application/octet-stream": "binary",
}


def _translation_mime(response: Response) -> str:
    mime = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    return _TRANSLATION_MEDIA_TYPES.get(mime, "other" if mime else "missing")


def _body_kind(text: str, start: int = 0) -> str:
    """Describe a prefix, not its contents or an inferred framing protocol."""
    for offset in range(start, len(text)):
        if text[offset] != "\ufeff" and not text[offset].isspace():
            break
    else:
        return "empty"
    text = text[offset:offset + 80].lower()
    if text.startswith(("{", "[")):
        return "json_like"
    if text.startswith(("data:", "event:", "id:", "retry:", ":")):
        return "sse_like"
    if text.startswith(("<!doctype html", "<html")):
        return "html_like"
    return "other"


def _x_error_code(payload: object) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {}
    errors = payload.get("errors")
    first_error = errors[0] if isinstance(errors, list) and errors else None
    for item in (payload.get("error"), first_error, payload):
        code = item.get("code") if isinstance(item, dict) else None
        if type(code) is int and code == 131:
            return {"x_code": 131}
    return {}


def _translation_failure(
    response: Response, reason: str, message: str, *, stage: str, **details: str | int,
) -> XTranslationFailure:
    return XTranslationFailure(
        reason, message, stage=stage, http_status=response.status_code, mime=_translation_mime(response),
        body_bytes=len(response.content), **details,
    )


def _translation_http_error(response: Response) -> XTranslationFailure:
    status = response.status_code
    if status == 401:
        reason, message = "http_auth", "X 翻译被拒（HTTP 401），登录态可能已失效"
    elif status == 403:
        reason, message = "http_forbidden", "X 翻译被拒（HTTP 403）"
    elif status == 429:
        reason, message = "http_rate_limited", "X 翻译被限流（HTTP 429）"
    elif status >= 500:
        reason, message = "http_upstream_error", f"X 翻译服务异常（HTTP {status}）"
    else:
        reason, message = "http_error", f"X 翻译请求失败（HTTP {status}）"
    codes: dict[str, int] = {}
    if _translation_mime(response) == "json" and len(response.content) <= 64 * 1024:
        try:
            codes = _x_error_code(response.json())
        except (ValueError, LookupError, RecursionError):
            pass  # 可选的错误码取不到时仍保留 HTTP 失败原因。
    return _translation_failure(response, reason, message, stage="http", **codes)


def _decode_translation(response: Response) -> tuple[str, dict]:
    if not 200 <= response.status_code < 400:
        raise _translation_http_error(response)
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        extra = exc.msg == "Extra data"
        shape = {"tail": _body_kind(exc.doc, exc.pos)} if extra else {"body_kind": _body_kind(exc.doc)}
        raise _translation_failure(
            response, "json_extra_data" if extra else "invalid_json", "X 翻译响应不是单个合法 JSON",
            stage="decode", json_line=exc.lineno, json_col=exc.colno, json_offset=exc.pos, **shape,
        ) from None
    except (UnicodeDecodeError, LookupError):
        raise _translation_failure(response, "invalid_encoding", "X 翻译响应编码无法解读", stage="decode") from None

    if not isinstance(payload, dict):
        raise _translation_failure(response, "unexpected_payload", "X 翻译响应不是对象", stage="payload", field="root")
    # result 是真实响应字段；保留对 value 形态的兼容。
    field = "result"
    value = payload.get(field)
    if not value:
        field = "value"
        value = payload.get(field)
    if payload.get("error") or (payload.get("errors") and not value):
        raise _translation_failure(
            response, "upstream_error", "X 翻译接口返回错误", stage="payload", **_x_error_code(payload),
        )
    if not value:
        raise _translation_failure(response, "missing_translation", "X 没有返回译文（缺 result/value）", stage="payload")
    if not isinstance(value, dict):
        raise _translation_failure(response, "unexpected_payload", "X 翻译结果不是对象", stage="payload", field=field)
    text = str(value.get("text") or "").strip()
    if not text:
        raise _translation_failure(response, "empty_translation", "X 没有返回译文（text 为空）", stage="payload")
    entities = value.get("entities")
    return text, entities if isinstance(entities, dict) else {}


class Twitter:
    def __init__(self, proxy: str | None = None, cookie: dict | None = None):
        self.proxy = proxy
        self.authorization = (
            "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOu"
            "H5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
        )
        self.cookie = cookie

    async def fetch_tweet(self, url: str) -> TwitterTweet:
        tweet = self.parse(await self._fetch_result(self.get_id_by_url(url)))
        if tweet.reply_to_id:
            tweet.reply_to = await self._fetch_related(tweet.reply_to_id, "被回复推文")
        if tweet.quoted_status_id and tweet.quoted_status is None:
            tweet.quoted_status = await self._fetch_related(tweet.quoted_status_id, "被引用推文")
        # trending 封面不在这里补：匿名客户端取不到（需要登录态），由解析器层用
        # 带 cookie 的客户端补一次，避免这里白发一次无效请求。
        return tweet

    async def _fetch_related(self, tweet_id: str, label: str) -> TwitterTweet | None:
        """补取一条关联推文 (被回复/被引用), 失败只跳过, 不阻断主解析."""
        try:
            return self.parse(await self._fetch_result(tweet_id))
        except Exception as e:
            logger.warning(f"获取{label}失败, 跳过引用: {e}")
            return None

    #: trending 话题页里 ``AiTrendByRestId`` 的 query id（X 会随前端发布变化）。
    _TREND_QUERY_ID = "qrhDEwcC6kB5TIalxd-4XA"
    #: 话题时间线的 ``GenericTimelineById`` query id。
    _TIMELINE_QUERY_ID = "UToImY30uYcerTaw8gu21w"

    async def fetch_trending_cover(self, trend_url: str) -> str:
        """取 trending 话题的**真封面图**。

        这条链路是逆向出来的，值得记清楚为什么绕这一大圈：

        1. ``unified_card`` 的 ``media_entities`` 里那张图是**通用占位图**
           （1600×1600 黑底白 X，media id ``1683353770267234304``）。X 在
           card 里复用它，而真封面（本条样例 640×915 人物照）压根不在这条推文的
           响应里 —— 搜遍全部 API 响应都是 0 命中。
        2. 真封面只出现在话题页 ``/i/trending/<id>``。但页面 HTML 是 SPA 外壳
           （313KB，无图），图是 JS 渲染后才有。
        3. 渲染数据来自 ``GenericTimelineById``，而封面藏在
           ``entries[].itemContent.tweet_results.result.jetfuel_attachment.payload``
           —— 一段 **base64 编码的 JetfuelV2 二进制**。URL 以明文形式嵌在字节流里，
           所以能直接正则抓出来，不需要实现完整的 JetfuelV2 解码器。
        4. 要调 ``GenericTimelineById`` 得先有 ``timelineId``，它由
           ``AiTrendByRestId`` 根据 trendId 给出。

        全程纯 HTTP，不需要浏览器。失败时返回空串（封图是锦上添花，不能拖垮整条解析）。
        """
        trend_id = self._trend_id_from_url(trend_url)
        if not trend_id:
            return ""
        headers = {
            "accept-language": "zh-CN,zh;q=0.9",
            "authorization": self.authorization,
            "content-type": "application/json",
            "user-agent": UA,
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "zh-cn",
            "referer": trend_url,
        }
        cookie = None
        if self.cookie and self.check_cookie():
            headers["x-csrf-token"] = self.cookie.get("ct0", "")
            headers["x-twitter-auth-type"] = "OAuth2Session"
            cookie = self.cookie

        try:
            async with http.AsyncClient(proxy=self.proxy) as client:
                # 1) trendId -> timelineId
                resp = await client.get(
                    f"https://x.com/i/api/graphql/{self._TREND_QUERY_ID}/AiTrendByRestId",
                    params={
                        "variables": json.dumps(
                            {
                                "trendId": trend_id,
                                "includePromotedContent": False,
                                "withVoice": False,
                                "withCommunity": False,
                            }
                        ),
                        "features": "{}",
                    },
                    headers=headers,
                    cookies=cookie,
                )
                resp.raise_for_status()
                page = (
                    (resp.json().get("data") or {})
                    .get("ai_trend_by_rest_id", {})
                    .get("result", {})
                    or {}
                ).get("page") or {}
                timelines = page.get("post_timelines") or []
                if not timelines:
                    return ""
                timeline_id = ((timelines[0].get("post_timeline") or {}).get("id")) or ""
                if not timeline_id:
                    return ""

                # 2) timelineId -> 该话题下的推文条目（含 jetfuel_attachment）
                resp = await client.get(
                    f"https://x.com/i/api/graphql/{self._TIMELINE_QUERY_ID}/GenericTimelineById",
                    params={
                        "variables": json.dumps(
                            {"timelineId": timeline_id, "count": 20, "includePromotedContent": False}
                        ),
                        "features": json.dumps(
                            {
                                "responsive_web_jetfuel_frame": True,
                                "rweb_video_screen_enabled": False,
                                "payments_enabled": False,
                                "verified_phone_label_enabled": False,
                                "creator_subscriptions_tweet_preview_api_enabled": True,
                                "responsive_web_graphql_timeline_navigation_enabled": True,
                                "communities_web_enable_tweet_community_results_fetch": True,
                                "longform_notetweets_consumption_enabled": True,
                                "responsive_web_edit_tweet_api_enabled": True,
                                "view_counts_everywhere_api_enabled": True,
                                "longform_notetweets_rich_text_read_enabled": True,
                                "longform_notetweets_inline_media_enabled": True,
                                "responsive_web_enhance_cards_enabled": False,
                            }
                        ),
                    },
                    headers=headers,
                    cookies=cookie,
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.warning(f"取 trending 封面失败, 跳过封面: {e}")
            return ""

        instructions = (
            (data.get("data") or {}).get("timeline", {}).get("timeline", {}) or {}
        ).get("instructions") or []
        for instruction in instructions:
            for entry in instruction.get("entries") or []:
                result = (
                    ((entry.get("content") or {}).get("itemContent") or {})
                    .get("tweet_results", {})
                    .get("result", {})
                )
                cover = self._cover_from_jetfuel((result.get("jetfuel_attachment") or {}).get("payload"))
                if cover:
                    return cover
        return ""

    @staticmethod
    def _trend_id_from_url(url: str) -> str:
        """从 ``https://x.com/i/trending/<id>`` 里取 trendId。"""
        match = re.search(r"/i/trending/(\d+)", url or "")
        return match.group(1) if match else ""

    @staticmethod
    def _cover_from_jetfuel(payload: str | None) -> str:
        """从 JetfuelV2 payload（base64）里抠出 trending 封面图 URL。

        不实现完整解码器：这段二进制里字符串以明文＋长度前缀存放，直接在整个
        字节流上搜 ``pbs.twimg.com/media/`` 即可。``trending-card`` 作为身份校验，
        避免把普通推文的 jetfuel（回复建议、社区注记等）误当成封面。
        """
        if not payload:
            return ""
        try:
            raw = base64.b64decode(payload + "=" * (-len(payload) % 4), validate=False)
        except Exception:
            return ""
        text = raw.decode("latin-1")
        if "trending-card" not in text:
            return ""
        for match in re.finditer(r"https://pbs\.twimg\.com/media/[A-Za-z0-9_-]+\.(?:jpg|jpeg|png)", text):
            return f"{match.group(0)}?name=orig"
        return ""

    async def _fetch_result(self, tweet_id: str) -> dict:
        headers = {
            "accept-language": "zh-CN,zh;q=0.9",
            "authorization": self.authorization,
            "content-type": "application/json",
            "user-agent": UA,
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "zh-cn",
        }

        cookie = None
        if self.cookie and self.check_cookie():
            headers["x-csrf-token"] = self.cookie.get("ct0", "")
            cookie = self.cookie

        params = {
            "variables": f'{{"tweetId":"{tweet_id}","withComm'
            f'unity":false,"includePromotedContent":false,"withVoice":false}}',
            "features": '{"creator_subscriptions_tweet_preview_api_enabled":true,'
            '"communities_web_enable_tweet_community_results_fetch":true,'
            '"c9s_tweet_anatomy_moderator_badge_enabled":true,"tweetypie_unmention_optimization_enabled":true,'
            '"responsive_web_edit_tweet_api_enabled":true,"graphql_is_translatable_rweb_tweet_is_translatable_enabled"'
            ":true,"
            '"view_counts_everywhere_api_enabled":true,"longform_notetweets_consumption_enabled":true,'
            '"responsive_web_twitter_article_tweet_consumption_enabled":true,"tweet_awards_web_tipping_enabled":false,'
            '"creator_subscriptions_quote_tweet_preview_enabled":false,"freedom_of_speech_not_reach_fetch_enabled"'
            ":true,"
            '"standardized_nudges_misinfo":true,"tweet_with_visibility_results_prefer_gql_limited_actions_policy_enable'
            'd":true,'
            '"tweet_with_visibility_results_prefer_gql_media_interstitial_enabled":false,"rweb_video_timestamps_enabled'
            '":true,'
            '"longform_notetweets_rich_text_read_enabled":true,"longform_notetweets_inline_media_enabled":true,'
            '"rweb_tipjar_consumption_enabled":true,"responsive_web_graphql_exclude_directive_enabled":true,"verified_'
            'phone_label_enabled":false,'
            '"responsive_web_graphql_skip_user_profile_image_extensions_enabled":false,"responsive_web_graphql_timeline'
            '_navigation_enabled":true,'
            '"responsive_web_enhance_cards_enabled":false}',
            "fieldToggles": '{"withArticleRichContentState":true,"withArticlePlainText":false}',
        }

        async with http.AsyncClient(proxy=self.proxy) as client:
            response = await client.get(
                "https://api.twitter.com/graphql/kPLTRmMnzbPTv70___D06w/TweetResultByRestId",
                params=params,
                headers=headers,
                cookies=cookie,
            )
        response.raise_for_status()
        return response.json()

    def parse(self, result: dict) -> TwitterTweet:
        if e := result.get("errors"):
            raise Exception(f"error -1: {e[0]['message']}")

        result = result["data"]["tweetResult"].get("result")
        if not result:
            raise ParseError("error -4: 帖子或用户不存在")

        return self._parse_result(result)

    def _parse_result(self, result: dict) -> TwitterTweet:
        """解析 tweetResult.result 结构 (顶层与内嵌的被引用推文同构)."""
        # 响应有两种形态: `{tweet: {...}}` (包装) 与字段直接铺在 result 上 (平铺)。
        # 字段一律从 node 取 —— 固定取顶层 result 时, 包装形态下长文正文 (note_tweet)、
        # 作者、浏览量、article 会**全部悄悄丢失** (字段根本不在那一层)。
        if tweet := result.get("tweet"):
            node = tweet
            tweet_id = tweet.get("rest_id", {})
            legacy: dict | None = tweet.get("legacy")
        else:
            node = result
            tweet_id = result.get("rest_id", {})
            legacy = result.get("legacy")

        if not legacy:
            if result.get("__typename") == "TweetTombstone":
                raise Exception("error -2: 该推文开启了限制, 匿名用户无法查看")
            raise Exception(f"error -3: {result.get('reason')}")

        author_name = self._extract_author_name(node)
        author_handle = self._extract_author_handle(node)
        reply_to_id = str(legacy.get("in_reply_to_status_id_str") or "")
        quoted_status_id = str(legacy.get("quoted_status_id_str") or "")
        quoted_status = self._parse_quoted(node)
        # 发布时间在 legacy.created_at, 浏览量在 views.count (字符串)
        published_at = legacy.get("created_at")
        view_count = (node.get("views") or {}).get("count")
        # 点赞数在 legacy.favorite_count (匿名请求同样返回)
        like_count = legacy.get("favorite_count")
        # 标签取**平台实体**（服务端已算好边界）—— 正则猜边界会多吃日文标点（见 hashtags 字段说明）
        legacy_entities = legacy.get("entities")
        if not isinstance(legacy_entities, dict):
            legacy_entities = {}
        hashtags = self._extract_hashtags(legacy_entities)

        if article := node.get("article", {}):
            ta = ArticleRenderer(article["article_results"]["result"]).render()
            return TwitterTweet(
                tweet_id=tweet_id,
                article=ta,
                author_name=author_name,
                author_handle=author_handle,
                reply_to_id=reply_to_id,
                quoted_status_id=quoted_status_id,
                quoted_status=quoted_status,
                published_at=published_at,
                view_count=view_count,
                like_count=like_count,
                is_sensitive=bool(legacy.get("possibly_sensitive")),
                hashtags=hashtags,
                poll=self._parse_poll(node),
            )

        note_tweet = node.get("note_tweet") or {}
        note_result = (note_tweet.get("note_tweet_results") or {}).get("result") or {}
        note_entities = note_result.get("entity_set")
        if not isinstance(note_entities, dict):
            note_entities = {}
        note_text = note_result.get("text")
        if isinstance(note_text, str) and note_text:
            original_text = note_text
            entities = note_entities
            richtext = note_result.get("richtext")
        else:
            legacy_text = legacy.get("full_text")
            original_text = legacy_text if isinstance(legacy_text, str) else ""
            entities = legacy_entities
            richtext = None if note_tweet else legacy.get("richtext")
        if not isinstance(richtext, dict):
            richtext = None

        media_list: list[TwitterVideo | TwitterPhoto | TwitterAni] = []
        media_urls: list[str] = []
        for item in self._entity_objects(legacy_entities.get("media")):
            if parsed := self._media_from_dict(item):
                media_list.append(parsed)
                if isinstance(url := item.get("url"), str) and url:
                    media_urls.append(url)

        # Match each text with its own entity set before the compatibility projection
        # inserts Markdown or replaces t.co tokens. Legacy media can also belong to a note.
        segments = text_runs(
            original_text,
            entities,
            richtext_tags=(richtext or {}).get("richtext_tags") or (),
            media_urls=media_urls,
        )

        # 长推文的局部加粗在 richtext_tags 里 (索引指向**未展开**的原文,
        # 必须在短链展开前应用 —— 展开改长度会让区间漂移)。
        full_text = self._apply_richtext(original_text, richtext)
        full_text = self._restore_short_urls(full_text, self._entity_objects(entities.get("urls")))

        # 「统一卡片」(``unified_card``) 里的媒体 —— **视频常常只在这里**。
        # 实测 ``x.com/Apple/status/2104922864910815586``：``legacy`` 上只有 ``entities``
        # （连 ``extended_entities`` 都没有）、一个 media 都没有，视频全在
        # ``card.legacy.binding_values.unified_card`` 这个 **JSON 字符串**的
        # ``media_entities`` 里（``amplify_video``，三档 mp4 + 一个 m3u8）。
        # 以前只读 ``entities.media`` ⇒ 这条推文发出来只剩正文，视频整段丢失
        # （用户报「抓不到视频」）。
        media_list.extend(self._parse_unified_card_media(node))

        if card_photo := self._parse_card_photo(node):
            # 外链卡片的预览图排在推文自带媒体**之后** (与 X 上的显示顺序一致)
            media_list.append(card_photo)

        # 投票的选项与票数在卡片里, legacy 上没有 —— 不取就只剩作者写的那句话
        poll = self._parse_poll(node)

        # 长推文的标签在 note_tweet 的 entity_set 里, 与 legacy 的合并（去重保序）
        hashtags = self._extract_hashtags(legacy_entities, note_entities)

        return TwitterTweet(
            tweet_id=tweet_id,
            full_text=full_text,
            media=media_list or None,
            author_name=author_name,
            author_handle=author_handle,
            reply_to_id=reply_to_id,
            quoted_status_id=quoted_status_id,
            quoted_status=quoted_status,
            published_at=published_at,
            view_count=view_count,
            like_count=like_count,
            is_sensitive=bool(legacy.get("possibly_sensitive")),
            hashtags=hashtags,
            poll=poll,
            trending=self._parse_trending_card(node),
            segments=segments,
        )

    @staticmethod
    def _parse_trending_card(node: dict) -> TrendingCard | None:
        """X 的 trending 卡片 —— 标题/摘要在 ``unified_card`` 的 ``topic_detail`` 里。

        ``unified_card`` 的 ``binding_values`` 里那段 JSON 有两个用途：媒体
        （``media_entities``，已有 ``_parse_unified_card_media`` 在读）和**卡片文案**
        （``component_objects.*.data.topic_detail``）。以前只读前者，于是这条推文
        发出来只剩一张封面图，标题与摘要全丢 —— 用户报「是个 X 图片，不是 trending
        里的图」，因为真正的 trending 内容（标题/摘要/链接）一个字都没渲染。

        不是 trending 的 unified_card（外链预览等）没有 ``topic_detail``，此时返回
        ``None``，行为与以前完全一致。
        """
        card = ((node.get("card") or {}).get("legacy")) or {}
        if (card.get("name") or "") != "unified_card":
            return None
        raw = next(
            (
                (b.get("value") or {}).get("string_value")
                for b in (card.get("binding_values") or [])
                if b.get("key") == "unified_card"
            ),
            None,
        )
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None

        detail = None
        for component in (data.get("component_objects") or {}).values():
            if not isinstance(component, dict):
                continue
            body = component.get("data") or {}
            if isinstance(body.get("topic_detail"), dict):
                detail = body["topic_detail"]
                break
        if detail is None:
            return None

        def content_of(key: str) -> str:
            block = detail.get(key) or {}
            return (block.get("content") or "").strip() if isinstance(block, dict) else ""

        title = content_of("title")
        subtitle = content_of("subtitle")
        if not (title or subtitle):
            return None

        url = ""
        for dest in (data.get("destination_objects") or {}).values():
            if not isinstance(dest, dict):
                continue
            url_data = (dest.get("data") or {}).get("url_data") or {}
            if isinstance(url_data, dict) and url_data.get("url"):
                url = str(url_data["url"])
                break

        return TrendingCard(title=title, subtitle=subtitle, url=url)

    @staticmethod
    def _parse_poll(node: dict) -> TwitterPoll | None:
        """投票 —— 数据在 ``node["card"]`` 里, 不在 ``legacy`` 上。

        X 的投票卡片名形如 ``poll2choice_text_only`` / ``poll3choice_text_only`` /
        ``poll4choice_image``：**选项数不固定**, 所以按 ``choice{N}_label`` 递增取,
        取到没有为止, 而不是硬编码 2 个。

        正文里只有作者自己写的那句话（本例就是 ``Vote``）—— 选项与票数**只能从这里拿**,
        不取就整条丢掉。
        """
        card = ((node.get("card") or {}).get("legacy")) or {}
        if not (card.get("name") or "").startswith("poll"):
            return None

        values = {b.get("key"): (b.get("value") or {}) for b in (card.get("binding_values") or [])}

        def string_of(key: str) -> str | None:
            """取值 —— 字符串字段在 ``string_value`` (布尔字段在 ``boolean_value``)。"""
            value = values.get(key) or {}
            text = value.get("string_value")
            return text if isinstance(text, str) else None

        choices: list[tuple[str, int]] = []
        for index in range(1, 5):
            label = string_of(f"choice{index}_label")
            if label is None:
                break
            # 票数缺失按 0 算: 选项本身仍要显示, 不该整条投票丢掉
            choices.append((label, to_int(string_of(f"choice{index}_count")) or 0))
        if not choices:
            return None

        final = (values.get("counts_are_final") or {}).get("boolean_value")
        return TwitterPoll(
            choices=choices,
            end_datetime=to_datetime(string_of("end_datetime_utc")),
            is_final=bool(final) if isinstance(final, bool) else False,
        )

    @classmethod
    def _media_from_dict(cls, i: dict) -> TwitterPhoto | TwitterVideo | TwitterAni | None:
        """把一份媒体对象转成媒体。

        ``legacy.entities.media`` 与 unified card 的 ``media_entities`` **字段同构**
        （``type`` / ``media_url_https`` / ``video_info`` / ``original_info``），
        所以两条路共用这一个转换 —— 以前是内联在媒体循环里的，接卡片时才发现要复用。

        字段缺失时返回 ``None``（跳过这一项），不让一条坏媒体毁掉整条解析。
        """
        mtype = i.get("type")
        if not mtype:
            return None
        info = i.get("original_info") or {}
        width, height = info.get("width", 0), info.get("height", 0)
        media_url_https = str(i.get("media_url_https") or "")

        if mtype == "photo":
            if not media_url_https:
                return None
            return TwitterPhoto(
                url=cls._build_img_url(media_url_https, "orig"),
                width=width,
                height=height,
                thumb_url=cls._build_img_url(media_url_https, "small"),
            )

        video_info = i.get("video_info") or {}
        url = cls._best_video_url(video_info.get("variants") or [])
        if not url:
            return None
        if mtype == "video":
            return TwitterVideo(
                url=url,
                height=height,
                width=width,
                duration_millis=video_info.get("duration_millis", 0),
                thumb_url=cls._build_img_url(media_url_https, "medium"),
            )
        if mtype == "animated_gif":
            return TwitterAni(
                url=url,
                height=height,
                width=width,
                thumb_url=cls._build_img_url(media_url_https, "small"),
            )
        return None

    @staticmethod
    def _best_video_url(variants: list) -> str:
        """从 variants 里挑最好的直链。

        **优先最高码率的 mp4**：X 的 variants 里 m3u8 排第一，mp4 的**顺序没有保证** ——
        实测这条 unified card 是 ``950k → 2176k → 632k``，**最后一个是码率最低的那档**，
        取 ``variants[-1]`` 等于永远发最糊的。没有 mp4 时回退最后一个变体（HLS）。
        """
        mp4s = [v for v in variants if v.get("content_type") == "video/mp4" and v.get("url")]
        if mp4s:
            best = max(mp4s, key=lambda v: v.get("bitrate") or v.get("bit_rate") or 0)
            return str(best["url"])
        return str(variants[-1]["url"]) if variants else ""

    @classmethod
    def _parse_unified_card_media(cls, node: dict) -> list:
        """「统一卡片」里的媒体 —— 内容是一段 **JSON 字符串**，藏在
        ``card.legacy.binding_values[key=unified_card].value.string_value``。

        结构（``type`` 为 ``video_website`` / ``image_website`` 等）::

            {
              "type": "video_website",
              "component_objects": {"details_1": …, "media_1": …},
              "destination_objects": {"browser_with_docked_media_1": {"data": {"url_data": …}}},
              "media_entities": {"13_<media_id>": {"type": "video", "video_info": …, …}},
              "components": ["media_1", "details_1"]
            }

        媒体在 ``media_entities`` 里（键是 ``media_key``），字段与 legacy 的 media 同构。
        """
        card = ((node.get("card") or {}).get("legacy")) or {}
        if (card.get("name") or "") != "unified_card":
            return []
        raw = next(
            (
                (b.get("value") or {}).get("string_value")
                for b in (card.get("binding_values") or [])
                if b.get("key") == "unified_card"
            ),
            None,
        )
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except ValueError:
            logger.warning("unified_card 不是合法 JSON, 跳过")
            return []
        if not isinstance(data, dict):
            return []

        entities = data.get("media_entities") or {}
        values = entities.values() if isinstance(entities, dict) else entities
        out: list[TwitterPhoto | TwitterVideo | TwitterAni] = []
        # trending 卡片的 media_entities 是**通用占位图**（1600×1600 黑底白 X，
        # media id 1683353770267234304），不是这个话题的封面。真机实测：发出去用户
        # 只看到一个孤零零的 X 图标（报「图片还是 X 那个图标」）。真封面由
        # `Twitter.fetch_trending_cover` 从话题页补取，这里一律跳过占位图。
        is_trending = cls._parse_trending_card(node) is not None
        for i in values:
            if not isinstance(i, dict):
                continue
            if is_trending and (i.get("type") or "") == "photo":
                continue
            if parsed := cls._media_from_dict(i):
                out.append(parsed)
        return out

    @staticmethod
    def _parse_card_photo(node: dict) -> TwitterPhoto | None:
        """外链卡片的预览图 —— 推文贴了链接时, X 会给那个网页生成一张卡片图。

        图由 X 托管在 ``pbs.twimg.com/card_img/...``, 字段在 ``node["card"]`` 里,
        **不在** ``legacy.entities.media`` 那条路上 —— 所以以前整条丢掉:
        正文里只有个链接的推文, 图片一张都发不出来 (用户报「里面有图, 没抓到」)。

        播放器类卡片 (YouTube 等) 跳过 —— 那类链接对应的是**正文里的链接**，
        按用户要求「链接你放那里不管就行了」：链接原样留在正文，不给它配封面，
        也不套引用块格式。收下这张卡片图就等于又替链接做了加工。
        """
        card = ((node.get("card") or {}).get("legacy")) or {}
        # player 类 (YouTube 等) 与 poll 类都不走这里: 前者是正文里的链接,
        # 后者的图是选项配图, 都不是"外链预览图"。
        # ``unified_card`` 也不走: 它的媒体由 `_parse_unified_card_media` 取
        # (那是**推文主体**的媒体, 不是外链预览图), 否则同一张图可能进两次。
        name = card.get("name") or ""
        if "player" in name or name.startswith("poll") or name == "unified_card":
            return None

        values = {b.get("key"): (b.get("value") or {}) for b in (card.get("binding_values") or [])}
        # 同一张图有十几个尺寸变体, 按"最大优先"取 name=orig 的那些
        for key in (
            "photo_image_full_size_original",
            "summary_photo_image_original",
            "thumbnail_image_original",
            "photo_image_full_size_large",
            "summary_photo_image_large",
        ):
            image = (values.get(key) or {}).get("image_value") or {}
            url = image.get("url")
            if not url:
                continue
            thumb = ((values.get("thumbnail_image") or {}).get("image_value") or {}).get("url")
            return TwitterPhoto(
                url=url,
                height=image.get("height", 0) or 0,
                width=image.get("width", 0) or 0,
                thumb_url=thumb,
            )
        return None

    def _parse_quoted(self, result: dict) -> TwitterTweet | None:
        """解析内嵌的被引用推文.

        被引用推文的数据通常就在同一份响应里 (quoted_status_result), 不需要额外请求;
        但 X 会把它降级成 TweetUnavailable (实测匿名请求下稳定如此), 也可能已被删除或受限,
        因此失败一律只跳过 —— 缺失时由 fetch_tweet 按 quoted_status_id 补取一次.
        """
        quoted = (result.get("quoted_status_result") or {}).get("result")
        if not quoted:
            return None
        try:
            return self._parse_result(quoted)
        except Exception as e:
            logger.debug(f"内嵌的被引用推文不可用, 稍后按 ID 补取: {e}")
            return None

    @staticmethod
    def _entity_objects(value: object) -> list[dict]:
        return [item for item in value if isinstance(item, dict)] if isinstance(value, (list, tuple)) else []

    @staticmethod
    def _apply_richtext(text: str, richtext: dict | None) -> str:
        """把 ``richtext_tags`` 的样式区间织成 markdown 标记 (目前只处理 ``Bold``)。

        X 的长推文编辑器允许局部加粗 (网页上显示为粗体段落), 数据在
        ``note_tweet...result.richtext.richtext_tags``::

            [{"from_index": 87, "to_index": 156, "richtext_types": ["Bold"]}, ...]

        ⚠️ **索引是 UTF-16 code unit 偏移, 不是 Python 的 code point 下标** ——
        X 的前端是 JS（字符串按 UTF-16 索引）。文本里出现**非 BMP 字符**
        （emoji 等, 每个在 UTF-16 占 2 个 unit）时, 两者会差出字符数, 标记整体左移。
        实例（``2110672031205511204``）: tag ``[42:54]``, 区间前有 5 个 emoji ⇒
        按 code point 切出 ``'27年1月7日(木)毎週'``（**丢了开头的 20**）,
        按 UTF-16 切才是 ``'2027年1月7日(木)'``。所以必须先用
        :meth:`_utf16_index_to_codepoint` 换算。

        **索引指向 ``text`` 原文** (未经短链展开), 所以本函数必须在
        ``_restore_short_urls`` **之前**调用 —— 展开会改变长度、让区间漂移。

        多个区间按顺序从后往前插入 (从后往前改文本不会让前面的区间偏移)。
        相同位置的标记按 ``_INLINE_STYLES`` 的映射裹上 ``**`` / ``*`` / ``~~``。

        ``richtext_tags`` 缺失 / 为空 / 区间越界时原样返回 (旧响应没有这个字段)。
        """
        tags = Twitter._entity_objects((richtext or {}).get("richtext_tags"))
        if not tags:
            return text

        length = len(text)
        spans: list[tuple[int, int, str]] = []
        for tag in tags:
            start_index, end_index = tag.get("from_index"), tag.get("to_index")
            if start_index is None or end_index is None:
                continue
            try:
                # UTF-16 偏移 -> code point 下标（emoji 在前面时两者不相等）
                start = Twitter._utf16_index_to_codepoint(text, int(start_index))
                end = Twitter._utf16_index_to_codepoint(text, int(end_index))
            except (TypeError, ValueError, OverflowError):
                continue
            if start < 0 or end <= start or start >= length:
                continue  # 越界/空区间 —— 宁可少裹一段, 不能让切片炸掉整条解析
            end = min(end, length)
            types = tag.get("richtext_types")
            if not isinstance(types, (list, tuple)):
                continue
            markers = [_INLINE_STYLES[t] for t in types if isinstance(t, str) and t in _INLINE_STYLES]
            if markers:
                spans.append((start, end, "".join(markers)))

        # 从后往前插入: 后面的插入不会移动前面区间的索引
        for start, end, marker in sorted(spans, key=lambda s: s[0], reverse=True):
            text = f"{text[:start]}{marker}{text[start:end]}{marker}{text[end:]}"
        return text

    @staticmethod
    def _utf16_index_to_codepoint(text: str, utf16_index: int) -> int:
        """把 **UTF-16 code unit 偏移** 换算成 Python 的 code point 下标。

        X 的 ``from_index`` / ``to_index`` 与 JS 的字符串索引一致（UTF-16）,
        而 Python 按 code point 索引 —— 文本里有非 BMP 字符（emoji）时两者不等。

        实现: 逐字符累加它在 UTF-16 下的宽度（非 BMP 算 2, 其余算 1）,
        累加到目标偏移即为对应的 code point 下标。偏移落在某个非 BMP 字符
        中间（只可能出现在区间边界上, 此时按保守处理）时返回该字符的下标。

        偏移超过文本长度时返回 ``len(text)``（调用方会当作越界处理）。
        """
        if utf16_index <= 0:
            return 0
        units = 0
        for index, char in enumerate(text):
            if units >= utf16_index:
                return index
            units += 2 if ord(char) > 0xFFFF else 1
        return len(text)

    @staticmethod
    def _restore_short_urls(text: str, url_entities: list[dict]) -> str:
        """用 entities 中的 expanded_url 还原正文里的 t.co 短链。"""
        for entity in url_entities:
            short_url = entity.get("url")
            expanded_url = entity.get("expanded_url")
            if isinstance(short_url, str) and short_url and isinstance(expanded_url, str) and expanded_url:
                text = text.replace(short_url, expanded_url)
        return text

    @staticmethod
    def _extract_hashtags(*sources: dict) -> list[str]:
        """从实体里取标签名 (不含 ``#``)。

        长推文 (note tweet) 的实体在 ``note_tweet...entity_set.hashtags``, 平铺的在
        ``legacy.entities.hashtags`` —— 两处都看, 去重保序。

        ⚠️ **不用 ``indices``**: 那是指向**原始** ``full_text`` 的位置, 而正文到渲染层时
        已经被加工过 (短链展开、t.co 去掉), 位置会漂。用 ``text`` 按名字匹配更稳。
        """
        out: list[str] = []
        seen: set[str] = set()
        for source in sources:
            for entity in Twitter._entity_objects(source.get("hashtags")):
                name = str(entity.get("text") or "").strip().lstrip("#").strip()
                if name and name not in seen:
                    seen.add(name)
                    out.append(name)
        return out

    @staticmethod
    def _extract_author_name(result: dict) -> str:
        user_result = result.get("core", {}).get("user_results", {}).get("result", {})
        legacy = user_result.get("legacy", {}) if isinstance(user_result, dict) else {}
        return str(legacy.get("name") or legacy.get("screen_name") or "").strip()

    @staticmethod
    def _extract_author_handle(result: dict) -> str:
        user_result = result.get("core", {}).get("user_results", {}).get("result", {})
        legacy = user_result.get("legacy", {}) if isinstance(user_result, dict) else {}
        return str(legacy.get("screen_name") or "").strip()

    @staticmethod
    def _build_img_url(url: str, size: Literal["orig", "large", "medium", "small", "thumb"]):
        p = "&" if "?" in url else "?"
        return f"{url}{p}name={size}"

    @staticmethod
    def get_id_by_url(url: str) -> str:
        # ``article`` 与 ``status`` 是同一种 id 空间（实测 article id 直接查
        # TweetDetail 就返回那条推文，正文在 ``article_results`` 里），
        # 所以两种路径都取最后那段数字。
        match = re.search(r"(?:status|article)/(\d+)", url)
        if not match:
            raise ValueError(f"Invalid tweet url: {url}")
        return match[1]

    def check_cookie(self) -> bool:
        if not self.cookie:
            return False
        if not self.cookie.get("ct0"):
            logger.warning("cookie 缺少必要参数: ct0")
            return False
        if not self.cookie.get("auth_token"):
            logger.warning("cookie 缺少必要参数: auth_token")
            return False
        return True

    #: Grok 翻译端点。**不在 GraphQL 里** —— 从 X 12.44.0 的 APK 反编译得到
    #: （``com.x.http.translation.a`` 上 ``@POST("/2/grok/translation.json")``），
    #: 所以在 web 端的 GraphQL 文档里搜「translate」永远搜不到它。
    #:
    #: 前缀必须是 ``/i/api`` —— X 的内部 API 都挂在这个前缀下。实测：
    #: ``https://x.com/2/...`` → 401（路径不存在，被当匿名），
    #: ``https://x.com/i/api/2/...`` → 500（路由存在、认证已过），
    #: ``https://api.twitter.com/i/api/2/...`` → 404（另一套 host）。
    _TRANSLATE_ENDPOINT = "https://x.com/i/api/2/grok/translation.json"

    async def translate_tweet(self, tweet_id: str, dst_lang: str) -> tuple[str, dict]:
        """用 **X 自己的翻译**（Grok）把一条推文译成 ``dst_lang``。

        **请求只发 id，不发正文** —— 服务端自己知道那条推文的原文，翻译后把
        译文和**重算过的实体偏移**一起返回。所以「能不能保住格式」完全取决于
        用不用 ``entities[*].indices``，而不是我们怎么拆段落。

        请求体只三个字段，字段名来自 APK 里 kotlinx.serialization 的描述符
        （``com.x.ai.grok.a`` 的静态块）：

        ```java
        n1Var.j("id", false);
        n1Var.j("content_type", false);
        n1Var.j("dst_lang", false);
        ```

        **注意是 snake_case**：``GrokTranslateRequest.toString()`` 显示的
        ``contentType`` / ``dstLang`` 是 Kotlin 属性名，不是传输键名，照着它写
        会得到 500/131。

        **``content_type`` 必须是全大写 ``"POST"``** —— 值来自真实调用点
        （``com/x/urt/items/post/translate/grok/q.smali``）：

        ```smali
        new-instance v10, Lcom/x/ai/grok/c;
        const-string v11, "POST"
        invoke-direct {v10, v12, v11, v5}, Lcom/x/ai/grok/c;-><init>(...)
        ```

        实测：``"POST"`` → 200 带译文；``"post"`` / ``"Post"`` → **500 code 131**。
        服务端对大小写敏感，且错值时给的是「内部错误」而不是参数错误 —— 这也是
        为什么之前 8 种组合全 500，看起来像「接口没启用」。

        **``indices`` 是按译文字符（码点）算的** —— 实测一条 157 字符的译文里，
        ``27,43`` 精确切出 ``@maruyono_kamome``，``121,144`` 切出
        ``https://t.co/...``，``146,157`` 切出 ``#FX戦士くるみちゃん``；同一批
        indices 用在 180 字符的**原文**上则全错。所以直接用 Python 的
        ``text[a:b]``，不要按 UTF-16 或字节切。

        Args:
            tweet_id: 推文 id（从 ``get_id_by_url`` 取）。
            dst_lang: 目标语言，BCP-47 小写形式（如 ``zh-cn`` / ``ja`` / ``en``）。

        Returns:
            ``(译文正文, entities)``。``entities`` 形如
            ``{"urls": [{"indices": [a, b], "url": ..., "expanded_url": ...}],
            "user_mentions": [{"indices": [a, b], "screen_name": ...}],
            "hashtags": [{"indices": [a, b], "text": ...}]}``；无实体时是空 dict。

        Raises:
            TranslationUnavailable: 没有登录态、或平台拒绝（401/403/5xx）。翻译
                **必须带登录 cookie** —— 匿名调用一律 401 空 body。
        """
        if not self.check_cookie():
            raise XTranslationFailure("missing_login", "X 翻译需要登录态（auth_token / ct0 缺失）", stage="request")

        cookie = self.cookie or {}
        headers = {
            "authorization": self.authorization,
            "content-type": "application/json",
            "x-csrf-token": str(cookie.get("ct0") or ""),
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
        }
        body = {"id": str(tweet_id), "content_type": "POST", "dst_lang": dst_lang}

        async with http.AsyncClient(proxy=self.proxy) as client:
            response = await client.post(
                self._TRANSLATE_ENDPOINT,
                json=body,
                headers=headers,
                cookies=cookie,
            )

        return _decode_translation(response)


@dataclass
class TwitterPoll:
    """投票 (正文之外的卡片数据)。

    X 把投票放在 ``node["card"]`` 里, **不在** ``legacy`` 上 —— 与媒体、标签那两条路
    都不同, 所以以前整条丢掉: 正文只剩作者写的 ``Vote``, 选项与票数全无。
    """

    choices: list[tuple[str, int]]
    """选项, 每项是 ``(文案, 票数)``, 顺序与 X 上一致。"""
    end_datetime: datetime | None = None
    """投票截止时间 (``end_datetime_utc``)。"""
    is_final: bool = False
    """票数是否为最终结果 (``counts_are_final``), 即投票是否已结束。"""


class TrendingCard:
    """X 的 trending 卡片。

    形态是一张 ``unified_card``：``component_objects.<x>.data.topic_detail`` 里有
    ``title`` / ``subtitle``（各含 ``content``），``destination_objects`` 里有目标链接，
    ``media_entities`` 里是配图。

    **为什么单独建模**：以前只从 ``unified_card`` 抽媒体，标题与摘要整个丢掉 ——
    用户看到的只是一张孤零零的封面图（报「是个 X 图片，不是 trending 里的图」）。
    卡片本身才是这条推文的内容，配图只是它的附图。
    """

    def __init__(
        self, *, title: str = "", subtitle: str = "", url: str = "", cover: str = ""
    ) -> None:
        self.title = title
        """卡片标题 (``topic_detail.title.content``)。"""
        self.subtitle = subtitle
        """卡片摘要 (``topic_detail.subtitle.content``)。"""
        self.url = url
        """卡片指向的地址 (``destination_objects.*.data.url_data.url``)。"""
        self.cover = cover
        """话题封面图 URL。

        **不在推文响应里** —— ``unified_card`` 的 ``media_entities`` 给的是通用
        占位图（1600×1600 黑底白 X），不是这个话题的封面。真封面只出现在话题页
        ``/i/trending/<id>`` 的 ``jetfuel_attachment.payload`` 里（JetfuelV2 二进制，
        base64 编码），由 `Twitter.fetch_trending_cover` 补取。
        """


class TwitterTweet:
    def __init__(
        self,
        tweet_id: str,
        full_text: str = "",
        media: list[TwitterVideo | TwitterPhoto | TwitterAni] | None = None,
        article: TwitterArticle | None = None,
        author_name: str = "",
        author_handle: str = "",
        reply_to_id: str = "",
        reply_to: TwitterTweet | None = None,
        quoted_status_id: str = "",
        quoted_status: TwitterTweet | None = None,
        is_sensitive: bool = False,
        published_at: datetime | None = None,
        view_count: int | None = None,
        like_count: int | None = None,
        hashtags: list[str] | None = None,
        poll: TwitterPoll | None = None,
        trending: TrendingCard | None = None,
        *,
        segments: tuple[TextRun, ...] | None = None,
    ):
        self.tweet_id = tweet_id
        self.full_text = re.sub(r"\s*https://t\.co/[^\s,]+$", "", full_text or "") if media else full_text
        self.segments = segments if segments is not None else ((TextRun(full_text),) if full_text else ())
        self.media = media
        self.article = article
        self.author_name = author_name
        self.author_handle = author_handle
        self.reply_to_id = reply_to_id
        """被回复推文的 ID，空字符串表示不是回复"""
        self.reply_to: TwitterTweet | None = reply_to
        """被回复的推文（由 fetch_tweet 填充，仅在是回复时）"""
        self.quoted_status_id = quoted_status_id
        """被引用推文的 ID，空字符串表示不是引用推文"""
        self.quoted_status: TwitterTweet | None = quoted_status
        """被引用的推文（优先取响应内嵌数据，缺失时由 fetch_tweet 按 ID 补取）"""
        self.is_sensitive = is_sensitive
        """推文是否被标记为敏感内容 (legacy.possibly_sensitive)"""
        self.published_at = to_datetime(published_at)
        """发布时间 (legacy.created_at)"""
        self.view_count = to_int(view_count)
        """浏览量 (views.count)"""
        self.like_count = to_int(like_count)
        """点赞数 (legacy.favorite_count)"""
        self.poll = poll
        """推文附带的投票 (卡片 ``poll*choice*``), 没有则为 ``None``。"""
        self.trending = trending
        """X 的 trending 卡片（``unified_card`` 的 ``topic_detail``），没有则为 ``None``。"""
        self.hashtags = hashtags or []
        """正文里的标签 (不含 ``#``), 来自平台实体 ``entities.hashtags[].text``。

        **平台自己算的边界** —— 比正则可靠: 日文标点 (``」``)、全角符号这些,
        正则要么没枚举到、要么枚举不全, 而实体是服务端切的, 与网页上的 hashtag 链接一致。
        拿不到实体时为空列表, 渲染层退回正则。
        """


@dataclass
class TwitterArticle:
    title: str
    content: str
    media: list[TwitterVideo | TwitterPhoto] | None = None
    blocks: tuple[Block, ...] = ()


@dataclass
class TwitterVideo:
    url: str
    height: int
    width: int
    duration_millis: int
    thumb_url: str | None = None


@dataclass
class TwitterPhoto:
    url: str
    height: int
    width: int
    thumb_url: str | None = None


@dataclass
class TwitterAni:
    url: str
    height: int
    width: int
    thumb_url: str | None = None


class ArticleRenderer:
    """Read DraftJS blocks and UTF-16 ranges once; project legacy Markdown from them."""

    _STYLE = {
        "bold": "bold", "italic": "italic", "strikethrough": "strike",
        "underline": "underline", "code": "code",
    }
    _HEADINGS = {f"header-{name}": i for i, name in enumerate(("one", "two", "three", "four", "five", "six"), 1)}
    _LIST = {"unordered-list-item": False, "ordered-list-item": True}

    def __init__(self, article_data: dict):
        self._data = article_data
        self._media: list[TwitterPhoto | TwitterVideo] = []
        self._media_by_id: dict[str, int] = {}

    def render(self) -> TwitterArticle:
        self._media.clear()
        self._media_by_id.clear()
        state = self._data.get("content_state") or {}
        entity_map = state.get("entityMap") or {}
        if isinstance(entity_map, list):
            entity_map = {str(item["key"]): item["value"] for item in entity_map if isinstance(item, dict)}
        blocks = []
        if cover := self._photo((self._data.get("cover_media") or {}).get("media_info") or {}):
            self._media.append(cover)
            blocks.append(Block("media", index=0, url=cover.url))
        for item in self._data.get("media_entities") or ():
            info = item.get("media_info") or {}
            media = None
            if info.get("__typename") == "ApiImage":
                media = self._photo(info)
            elif info.get("__typename") == "ApiVideo":
                preview = self._photo(info.get("preview_image") or {})
                if url := self._best_mp4_url(info.get("variants") or []):
                    media = TwitterVideo(url, preview.height if preview else 0, preview.width if preview else 0,
                                         info.get("duration_millis") or 0, preview.url if preview else None)
                else:
                    media = preview
            if media:
                if item.get("media_id") is not None:
                    self._media_by_id[str(item["media_id"])] = len(self._media)
                self._media.append(media)

        entries = []
        for raw in state.get("blocks") or ():
            kind = raw.get("type", "unstyled")
            children = self._block(raw, entity_map)
            entries.append((kind, max(0, to_int(raw.get("depth")) or 0), children))
        blocks.extend(self._group_lists(entries))
        legacy = "\n\n".join(self._markdown(block, cover=i == 0 and bool(cover)) for i, block in enumerate(blocks))
        return TwitterArticle(self._data.get("title", ""), legacy, list(self._media) or None, tuple(blocks))

    @staticmethod
    def _photo(info: dict) -> TwitterPhoto | None:
        url = info.get("original_img_url") or ""
        return TwitterPhoto(url, to_int(info.get("original_img_height")) or 0,
                            to_int(info.get("original_img_width")) or 0) if url else None

    @staticmethod
    def _best_mp4_url(variants: list) -> str:
        mp4s = [v for v in variants if v.get("content_type") == "video/mp4" and v.get("url")]
        return str(max(mp4s, key=lambda v: v.get("bit_rate", 0))["url"]) if mp4s else ""

    def _atomic(self, entity: dict) -> tuple[Block, ...]:
        if entity.get("type") == "DIVIDER":
            return (Block("rule"),)
        if entity.get("type") != "MEDIA":
            return ()
        data = entity.get("data") or {}
        blocks = []
        for item in data.get("mediaItems") or ():
            index = self._media_by_id.get(str(item.get("mediaId")))
            if index is not None:
                blocks.append(Block("media", index=index, url=self._media[index].url))
        return tuple(blocks)

    def _block(self, raw: dict, entity_map: dict) -> tuple[Block, ...]:
        text = raw.get("text") or ""
        kind = raw.get("type", "unstyled")
        offsets = _utf16_boundaries(text)
        styles: list[tuple[int, int, str]] = []
        links: list[tuple[int, int, str]] = []
        atomics: dict[int, tuple[int, tuple[Block, ...]]] = {}

        def span(item: dict) -> tuple[int, int] | None:
            start, length = item.get("offset"), item.get("length")
            if (type(start) is int and type(length) is int and length >= 0
                    and start in offsets and start + length in offsets):
                return offsets[start], offsets[start + length]
            return None

        for item in raw.get("inlineStyleRanges") or ():
            if (bounds := span(item)) and (style := self._STYLE.get(str(item.get("style", "")).lower())):
                styles.append((*bounds, style))
        for item in raw.get("entityRanges") or ():
            if not (bounds := span(item)):
                continue
            entity = entity_map.get(str(item.get("key"))) or {}
            if entity.get("type") == "LINK":
                url = _web_url(str((entity.get("data") or {}).get("url") or ""))
                if url:
                    links.append((*bounds, url))
            elif atomic := self._atomic(entity):
                atomics[bounds[0]] = (bounds[1], atomic)
        boundaries = sorted({0, len(text)} | {p for a, b, _ in styles + links for p in (a, b)}
                            | {p for a, (b, _) in atomics.items() for p in (a, b)})
        blocks: list[Block] = []
        runs: list[TextRun] = []

        def flush() -> None:
            if runs:
                payload = tuple(runs)
                if level := self._HEADINGS.get(kind):
                    blocks.append(Block("heading", runs=payload, level=level))
                elif kind == "blockquote":
                    blocks.append(Block("quote", children=(paragraph(payload),)))
                elif kind == "code-block":
                    blocks.append(Block("code", runs=payload))
                else:
                    blocks.append(paragraph(payload))
                runs.clear()

        skip = 0
        for i, start in enumerate(boundaries):
            if start < skip:
                continue
            if start in atomics:
                flush()
                skip, atomic = atomics[start]
                blocks.extend(atomic)
            if i + 1 >= len(boundaries) or start < skip:
                continue
            end = boundaries[i + 1]
            url = next((url for a, b, url in links if a <= start < b), "")
            style_set = tuple(s for s in self._STYLE.values() if any(a <= start < b and t == s for a, b, t in styles))
            value = text[start:end]
            if runs and (runs[-1].url, runs[-1].styles) == (url, style_set):
                value = runs.pop().text + value
            runs.append(TextRun(value, url, style_set))
        flush()
        return tuple(blocks) or (paragraph(()),)

    def _group_lists(self, entries: list[tuple[str, int, tuple[Block, ...]]]) -> tuple[Block, ...]:
        out = []
        i = 0
        while i < len(entries):
            kind, depth, blocks = entries[i]
            if kind not in self._LIST:
                out.extend(blocks)
                i += 1
                continue
            items = []
            while i < len(entries) and entries[i][:2] == (kind, depth):
                item = entries[i][2]
                i += 1
                start = i
                while i < len(entries) and entries[i][0] in self._LIST and entries[i][1] > depth:
                    i += 1
                if start < i:
                    item += self._group_lists(entries[start:i])
                items.append(item)
            out.append(Block("list", items=tuple(items), ordered=self._LIST[kind]))
        return tuple(out)

    def _markdown(self, block: Block, *, cover: bool = False) -> str:
        markers = {"bold": "**", "italic": "*", "strike": "~~", "code": "`"}
        parts = []
        active: tuple[str, ...] = ()
        for run in block.runs:
            desired = tuple(style for style in run.styles if style in markers)
            common = 0
            for old, new in zip(active, desired, strict=False):
                if old != new:
                    break
                common += 1
            parts.extend(markers[style] for style in reversed(active[common:]))
            parts.extend(markers[style] for style in desired[common:])
            parts.append(f"[{run.text}]({run.url})" if run.url else run.text)
            active = desired
        parts.extend(markers[style] for style in reversed(active))
        text = "".join(parts)
        if block.kind == "heading":
            return "#" * block.level + " " + text
        if block.kind == "quote":
            children = "\n\n".join(self._markdown(child) for child in block.children)
            return "\n".join(f"> {line}" for line in children.split("\n"))
        if block.kind == "code":
            return f"```\n{text}\n```"
        if block.kind == "list":
            marker = "1. " if block.ordered else "- "
            return "\n\n".join(marker + "\n  ".join(self._markdown(child) for child in item) for item in block.items)
        if block.kind == "media":
            media = self._media[block.index]
            if isinstance(media, TwitterVideo):
                return f"[![Video]({media.thumb_url})]({block.url})" if media.thumb_url else f"[Video]({block.url})"
            return f"![{'Cover Image' if cover else 'Image'}]({block.url})" + ("\n" if cover else "")
        return "\n---\n" if block.kind == "rule" else text
