# mypy: disable-error-code=no-untyped-def
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, NamedTuple

from loguru import logger

from ..types import ParseError
from ..utils import http
from ..utils.helpers import UA, to_datetime, to_int

#: 长推文 richtext 的行内样式 → Markdown 标记 (与 ``ArticleRenderer._INLINE_STYLES``
#: 同一套映射; 那个是 Article 的块级渲染, 这里是 note_tweet 正文的区间标记)。
_INLINE_STYLES: dict[str, str] = {
    "Bold": "**",
    "Italic": "*",
    "Strikethrough": "~~",
}


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
        return tweet

    async def _fetch_related(self, tweet_id: str, label: str) -> TwitterTweet | None:
        """补取一条关联推文 (被回复/被引用), 失败只跳过, 不阻断主解析."""
        try:
            return self.parse(await self._fetch_result(tweet_id))
        except Exception as e:
            logger.warning(f"获取{label}失败, 跳过引用: {e}")
            return None

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
        hashtags = self._extract_hashtags(legacy.get("entities") or {})

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

        richtext: dict | None = None
        if note_tweet := node.get("note_tweet"):
            note_result = note_tweet.get("note_tweet_results", {}).get("result", {})
            full_text = note_result.get("text", None)
            richtext = note_result.get("richtext")
            url_entities = (note_result.get("entity_set") or {}).get("urls") or []
            if not full_text:
                full_text = legacy.get("full_text", "")
                richtext = None
                url_entities = legacy["entities"].get("urls", [])
        else:
            full_text = legacy.get("full_text", "")
            richtext = legacy.get("richtext")
            url_entities = legacy["entities"].get("urls", [])

        # 长推文的局部加粗在 richtext_tags 里 (索引指向**未展开**的原文,
        # 必须在短链展开前应用 —— 展开改长度会让区间漂移)。
        full_text = self._apply_richtext(full_text, richtext)
        full_text = self._restore_short_urls(full_text, url_entities)

        media_list: list[TwitterVideo | TwitterPhoto | TwitterAni] = [
            parsed for i in (legacy["entities"].get("media") or []) if (parsed := self._media_from_dict(i))
        ]

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
        hashtags = self._extract_hashtags(
            legacy.get("entities") or {},
            ((node.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result", {}).get("entity_set") or {},
        )

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
        )

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
        for i in values:
            if isinstance(i, dict) and (parsed := cls._media_from_dict(i)):
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
    def _apply_richtext(text: str, richtext: dict | None) -> str:
        """把 ``richtext_tags`` 的样式区间织成 markdown 标记 (目前只处理 ``Bold``)。

        X 的长推文编辑器允许局部加粗 (网页上显示为粗体段落), 数据在
        ``note_tweet...result.richtext.richtext_tags``::

            [{"from_index": 87, "to_index": 156, "richtext_types": ["Bold"]}, ...]

        **索引指向 ``text`` 原文** (未经短链展开), 所以本函数必须在
        ``_restore_short_urls`` **之前**调用 —— 展开会改变长度、让区间漂移。

        只有一个区间、且不嵌套时直接切片; 多个区间按顺序从后往前插入
        (从后往前改文本不会让前面的区间偏移)。相同位置的标记按
        ``_INLINE_STYLES`` 的映射裹上 ``**`` / ``*`` / ``~~``。

        ``richtext_tags`` 缺失 / 为空 / 区间越界时原样返回 (旧响应没有这个字段)。
        """
        tags = (richtext or {}).get("richtext_tags") or []
        if not tags:
            return text

        length = len(text)
        spans: list[tuple[int, int, str]] = []
        for tag in tags:
            try:
                start, end = int(tag.get("from_index")), int(tag.get("to_index"))
            except (TypeError, ValueError):
                continue
            if start < 0 or end <= start or start >= length:
                continue  # 越界/空区间 —— 宁可少裹一段, 不能让切片炸掉整条解析
            end = min(end, length)
            markers = [
                _INLINE_STYLES[t]
                for t in (tag.get("richtext_types") or [])
                if t in _INLINE_STYLES
            ]
            if markers:
                spans.append((start, end, "".join(markers)))

        # 从后往前插入: 后面的插入不会移动前面区间的索引
        for start, end, marker in sorted(spans, key=lambda s: s[0], reverse=True):
            text = f"{text[:start]}{marker}{text[start:end]}{marker}{text[end:]}"
        return text

    @staticmethod
    def _restore_short_urls(text: str, url_entities: list[dict]) -> str:
        """用 entities 中的 expanded_url 还原正文里的 t.co 短链。"""
        for entity in url_entities:
            short_url = entity.get("url")
            expanded_url = entity.get("expanded_url")
            if short_url and expanded_url:
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
            for entity in (source or {}).get("hashtags") or []:
                name = str((entity or {}).get("text") or "").strip().lstrip("#").strip()
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
        match = re.search(r"status/(\d+)", url)
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
    ):
        self.tweet_id = tweet_id
        self.full_text = re.sub(r"\s*https://t\.co/[^\s,]+$", "", full_text or "") if media else full_text
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


class _Insertion(NamedTuple):
    """待插入原文的 Markdown 标记。"""

    idx: int
    text: str
    kind: str  # "start" | "end" | "atomic"
    length: int = 0


class ArticleRenderer:
    """将 Twitter Article JSON 解析并渲染为 Markdown。"""

    # 行内样式 → Markdown 标记
    _INLINE_STYLES: dict[str, str] = {
        "Bold": "**",
        "Italic": "*",
        "Strikethrough": "~~",
    }

    # 块级类型 → 格式化函数
    _BLOCK_FORMATTERS: dict[str, Callable[[str], str]] = {
        "header-one": lambda t: f"# {t}",
        "header-two": lambda t: f"## {t}",
        "header-three": lambda t: f"### {t}",
        "blockquote": lambda t: "\n".join(f"> {line}" for line in t.split("\n")),
        "ordered-list-item": lambda t: f"1. {t}",
        "unordered-list-item": lambda t: f"- {t}",
    }

    def __init__(self, article_data: dict):
        self._data = article_data
        self._media_dict: dict = {}
        self._media_result: list[TwitterPhoto | TwitterVideo] = []

    # ── 公共入口 ──────────────────────────────

    def render(self) -> TwitterArticle:
        content_state = self._data.get("content_state", {})
        blocks = content_state.get("blocks", [])
        entity_map = {str(item["key"]): item["value"] for item in content_state.get("entityMap", [])}
        title = self._data.get("title", "")

        self._parse_media_entities()
        cover_url = self._data.get("cover_media", {}).get("media_info", {}).get("original_img_url", "")

        md_lines: list[str] = []
        if cover_url:
            md_lines.append(f"![Cover Image]({cover_url})\n")

        for block in blocks:
            md_lines.append(self._render_block(block, entity_map))

        return TwitterArticle(
            title=title,
            content="\n\n".join(md_lines),
            media=self._media_result or None,
        )

    # ── 媒体解析 ──────────────────────────────

    def _parse_media_entities(self) -> None:
        for media in self._data.get("media_entities", []):
            media_id = media.get("media_id")
            media_info = media.get("media_info", {})
            typename = media_info.get("__typename")

            if typename == "ApiImage":
                self._parse_image(media_id, media_info)
            elif typename == "ApiVideo":
                self._parse_video(media_id, media_info)

    def _parse_image(self, media_id, info: dict) -> None:
        url = info.get("original_img_url", "")
        if media_id and url:
            self._media_dict[media_id] = {"type": "image", "url": url}
        self._media_result.append(
            TwitterPhoto(
                url=url,
                height=info.get("original_img_height", 0),
                width=info.get("original_img_width", 0),
            )
        )

    def _parse_video(self, media_id, info: dict) -> None:
        preview = info.get("preview_image", {})
        preview_url = preview.get("original_img_url", "")
        video_url = self._best_mp4_url(info.get("variants", []))

        if media_id and preview_url:
            self._media_dict[media_id] = {
                "type": "video",
                "preview_url": preview_url,
                "video_url": video_url,
            }
        self._media_result.append(
            TwitterVideo(
                url=video_url,
                height=preview.get("original_img_height", 0),
                width=preview.get("original_img_width", 0),
                duration_millis=info.get("duration_millis", 0),
                thumb_url=preview_url,
            )
        )

    @staticmethod
    def _best_mp4_url(variants: list) -> str:
        mp4s = [v for v in variants if v.get("content_type") == "video/mp4"]
        if not mp4s:
            return ""
        return str(max(mp4s, key=lambda v: v.get("bit_rate", 0)).get("url", ""))

    # ── Block 渲染 ────────────────────────────

    def _render_block(self, block: dict, entity_map: dict) -> str:
        b_type = block.get("type", "unstyled")
        text = block.get("text", "")

        insertions = self._collect_inline_styles(block)
        insertions += self._collect_entities(block, entity_map)
        insertions.sort(key=self._insertion_sort_key)

        final_text = self._apply_insertions(text, insertions)
        formatter = self._BLOCK_FORMATTERS.get(b_type)
        return formatter(final_text) if formatter else final_text

    @staticmethod
    def _collect_inline_styles(block: dict) -> list[_Insertion]:
        result: list[_Insertion] = []
        for style in block.get("inlineStyleRanges", []):
            marker = ArticleRenderer._INLINE_STYLES.get(style["style"])
            if not marker:
                continue
            offset, length = style["offset"], style["length"]
            result.append(_Insertion(offset, marker, "start", length))
            result.append(_Insertion(offset + length, marker, "end", length))
        return result

    def _collect_entities(self, block: dict, entity_map: dict) -> list[_Insertion]:
        result: list[_Insertion] = []
        for ent in block.get("entityRanges", []):
            offset, length = ent["offset"], ent["length"]
            ent_data = entity_map.get(str(ent["key"]), {})
            ent_type = ent_data.get("type")

            if ent_type == "LINK":
                url = ent_data.get("data", {}).get("url", "")
                result.append(_Insertion(offset, "[", "start", length))
                result.append(_Insertion(offset + length, f"]({url})", "end", length))

            elif ent_type == "MEDIA":
                md = self._media_entity_to_md(ent_data)
                if md:
                    result.append(_Insertion(offset, md, "atomic", length))

            elif ent_type == "DIVIDER":
                result.append(_Insertion(offset, "\n---\n", "atomic", length))

        return result

    def _media_entity_to_md(self, ent_data: dict) -> str:
        media_items = ent_data.get("data", {}).get("mediaItems", [])
        if not media_items:
            return ""
        obj = self._media_dict.get(media_items[0].get("mediaId"))
        if not obj:
            return ""

        if obj["type"] == "image":
            return f"![Image]({obj['url']})"
        if obj["type"] == "video":
            p, v = obj["preview_url"], obj["video_url"]
            return f"[![Video]({p})]({v})" if v else f"![Video Preview]({p})"
        return ""

    # ── 文本拼装 ──────────────────────────────

    @staticmethod
    def _insertion_sort_key(ins: _Insertion) -> tuple:
        weight = {"end": 1, "atomic": 0, "start": -1}.get(ins.kind, 0)
        return -ins.idx, weight, ins.length

    @staticmethod
    def _apply_insertions(text: str, insertions: list[_Insertion]) -> str:
        chars = list(text)
        for ins in insertions:
            if ins.kind == "atomic" and ins.idx < len(chars):
                chars[ins.idx] = ins.text
            else:
                chars.insert(ins.idx, ins.text)
        return "".join(chars)
