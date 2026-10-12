import html
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse, urlunparse

from ...provider_api.twitter import (
    Twitter,
    TwitterAni,
    TwitterPhoto,
    TwitterTweet,
    TwitterVideo,
)
from ...types import (
    AniRef,
    AnyMediaRef,
    ImageRef,
    MultimediaParseResult,
    ParseError,
    Platform,
    Poll,
    PollOption,
    RichTextParseResult,
    VideoRef,
)
from ...utils.helpers import format_author_link, format_quote_block, image_ext_from_url, profile_url
from ..base.base import BaseParser


class TwitterParser(BaseParser):
    __platform__ = Platform.TWITTER
    __supported_type__ = ["视频", "图文"]
    __match__ = r"^(http(s)?://)?.+(twitter|fixupx|x).com/.*/status/\d+"

    async def _do_parse(self, raw_url: str) -> MultimediaParseResult | RichTextParseResult:
        tweet = await self._parse(raw_url)
        return await self.media_parse(tweet)

    async def get_raw_url(self, url: str, **kwargs: Any) -> str:
        url = await super().get_raw_url(url, **kwargs)
        return str(urlunparse(urlparse(url)._replace(netloc="x.com")))

    async def _parse(self, url: str) -> TwitterTweet:
        x = Twitter(self.proxy, cookie=None)
        try:
            tweet = await x.fetch_tweet(url)
        except Exception as e:
            if any(s in str(e) for s in ("error -2", "error -3")):
                if cookie := self.cookie.get_value():
                    x2 = Twitter(self.proxy, cookie=cookie)
                    try:
                        tweet = await x2.fetch_tweet(url)
                    except Exception as e2:
                        raise ParseError(f"Twitter 账号无权限或已被封禁\n\n使用的 Cookie: {self.cookie}") from e2
                else:
                    raise ParseError(str(e)) from e
            else:
                raise ParseError(str(e)) from e
        return tweet

    @staticmethod
    def _quote_block(source: TwitterTweet, poll_index: int | None = None) -> str:
        """把一条被回复/被引用的推文渲染成引用块 (排版统一由公共 helper 决定).

        :param poll_index: 这条推文的 poll 占位符索引 (``{{poll:N}}``); 由调用方按
            「占位符在正文里的出现顺序」分配 (见 ``_poll_pairs``)。

        ⚠️ ``sign_only=bool(source.media)``: **正文为空但有媒体的推文**（很常见 ——
        推文只有一个媒体短链, 还原后正文是空串）必须照样产出引用块。不产的话
        ``_build_quoted_block`` 返回空 ⇒ 渲染层不知道那几张图属于引用卡片 ⇒
        它们被当成**正文的图**, 与主帖自己的图混进同一个图集
        （用户报「正文图为什么和引用图塞一起」）。

        只有署名行没有正文的引用块是合法的 —— 图由 ``quoted_media_count`` 通道
        放进块内。既没文字也没媒体时仍返回空 (一块孤零零的署名没有意义)。
        """
        # 投票跟着**它所属的那条推文**的正文走 —— 被引用/被回复的推文带投票时,
        # 占位符渲染在它自己的引用块里（与 X 上一致）。
        body = source.full_text or ""
        if placeholder := TwitterParser._poll_placeholder(source, poll_index):
            body = f"{body}\n\n{placeholder}"
        return format_quote_block(
            body,
            format_author_link(
                source.author_name or "",
                source.author_handle or "",
                profile_url(Platform.TWITTER, source.author_handle or ""),
            ),
            sign_only=bool(source.media),
        )

    @staticmethod
    def _has_poll(source: TwitterTweet) -> bool:
        """这条推文有没有投票 (有选项才算)。"""
        return bool(source.poll and source.poll.choices)

    @staticmethod
    def _poll_placeholder(source: TwitterTweet, index: int | None = None) -> str:
        """投票 -> 正文里独立一行的占位符 ``{{poll:N}}``, 没有投票 (或索引未知) 时返回空串。

        **lib 只输出占位符, 不拼表格**: 「选项/票数/占比」那套文案由渲染层按用户
        语言拼装 (``result.polls`` 里带结构化数据)。
        """
        if not TwitterParser._has_poll(source) or index is None:
            return ""
        return f"{{{{poll:{index}}}}}"

    @staticmethod
    def _poll_pairs(tweet: TwitterTweet) -> list[tuple[str, TwitterTweet]]:
        """带投票的推文, **按占位符在正文里的出现顺序**: reply → 主帖 → quoted。

        索引纪律 (lib ↔ 渲染层的契约): ``result.polls[i]`` 对应正文里第 i 个
        ``{{poll:N}}`` 占位符; 这里就是索引的**唯一分配点**。
        """
        pairs: list[tuple[str, TwitterTweet]] = []
        for role, source in (("reply", tweet.reply_to), ("main", tweet), ("quoted", tweet.quoted_status)):
            if source is not None and TwitterParser._has_poll(source):
                pairs.append((role, source))
        return pairs

    @staticmethod
    def _poll_indexes(tweet: TwitterTweet) -> dict[str, int]:
        """每个位置 (``reply`` / ``main`` / ``quoted``) 的占位符索引; 没投票的位置缺席。"""
        return {role: index for index, (role, _source) in enumerate(TwitterParser._poll_pairs(tweet))}

    @staticmethod
    def _to_result_poll(source: TwitterTweet) -> Poll:
        """推文的投票 -> 结构化 ``Poll`` (票数与选项**照平台给的原样**, 不算占比)。"""
        choices = source.poll.choices if source.poll else []
        return Poll(
            options=[PollOption(text=label, count=count) for label, count in choices],
            total_votes=sum(count for _, count in choices),
            is_finished=bool(source.poll.is_final) if source.poll else False,
        )

    @staticmethod
    def _build_quote(tweet: TwitterTweet) -> str:
        """把被回复的推文渲染成 Markdown 引用块, 不是回复或内容为空时返回空串."""
        if not tweet.reply_to:
            return ""
        return TwitterParser._quote_block(tweet.reply_to, TwitterParser._poll_indexes(tweet).get("reply"))

    @staticmethod
    def _build_quoted_block(tweet: TwitterTweet) -> str:
        """把被引用的推文渲染成 Markdown 引用块, 不是引用推文或内容为空时返回空串."""
        if not tweet.quoted_status:
            return ""
        return TwitterParser._quote_block(
            tweet.quoted_status, TwitterParser._poll_indexes(tweet).get("quoted")
        )

    @staticmethod
    def _quote_roles(tweet: TwitterTweet) -> list[str]:
        """正文里引用块的**角色**, 按出现顺序 (``reply`` 在前、``quoted`` 在后)。

        判据与 ``_compose`` **同源**（都看 ``_build_quote`` / ``_build_quoted_block``
        是否非空）—— 两处判据分家的话, roles 会说"有块"而正文里没有, 媒体就配不到。
        """
        roles: list[str] = []
        if TwitterParser._build_quote(tweet):
            roles.append("reply")
        if TwitterParser._build_quoted_block(tweet):
            roles.append("quoted")
        return roles

    @staticmethod
    def _compose(body: str, tweet: TwitterTweet, *, reply_yt: str = "", quoted_yt: str = "") -> str:
        """组装正文: 被回复推文在最前, 被引用推文在最后 (与 X 上的卡片位置一致).

        :param reply_yt: 被回复推文正文里的 YouTube 卡片, 追加进**回复**引用块
        :param quoted_yt: 被引用推文正文里的 YouTube 卡片, 追加进**被引用**引用块
        """
        if placeholder := TwitterParser._poll_placeholder(tweet, TwitterParser._poll_indexes(tweet).get("main")):
            body = f"{body}\n\n{placeholder}"
        reply_block = TwitterParser._append_inside_quote(TwitterParser._build_quote(tweet), reply_yt)
        text = f"{reply_block}{body}"
        # strip: 引用块自带末尾空行（``format_quote_block`` 的块结束约定），
        # 拼接由这里负责，别让它在正文中段多出一个空行。
        quoted_block = TwitterParser._append_inside_quote(
            TwitterParser._build_quoted_block(tweet), quoted_yt
        ).strip()
        return f"{text}\n\n{quoted_block}" if quoted_block else text

    @staticmethod
    def _append_inside_quote(block: str, extra: str) -> str:
        """把内容追加到引用块**内部**。

        ``format_quote_block`` 末尾自带一个空行（块靠它结束），所以必须插在那个空行
        **之前** —— 插在之后就成了独立段落, 媒体会和卡片分家。
        """
        if not block or not extra:
            return block
        return f"{block.rstrip()}\n{extra}\n\n"

    @staticmethod
    async def _youtube_card(text: str | None) -> tuple[str, list[AnyMediaRef]]:
        """把正文里的 YouTube 链接渲染成可点的**卡片行**（标题链到视频）。

        **作用域: 只给被引用 / 被回复的推文用** —— 它们本来就渲染成引用卡片,
        卡片正文里的链接应当是可点的、有标题的。

        ⚠️ **只出文字, 不产出媒体**（2026-10-08 用户纠正）:
        这里曾返回 ``[ImageRef(card.cover_url)]`` —— 于是那张 YouTube 缩略图被当成
        **一条独立的图片**发出去。用户原话:「我之前让弄的是**给视频加封面**, 不是把封面
        再发一遍」。给视频配封面是**另一个机制**: 视频的封面走 ``VideoRef.thumb_url``
        （由 ``plugins/parse/covers.prepare_video_thumbs`` 下载成缩略图,
        见 ``inline_rich`` 的 ``VideoFile`` 分支）。两者别混:
        链接卡片 = 文字行; 视频封面 = ``thumb_url``。**不要把链接的封面塞成独立媒体。**

        ⚠️ **主帖自己的正文不处理**: 它既不是引用也不是回复, 链接原样留着
        (用户原话:「有引用就引用吗, 没引用就不弄, 链接你放那里不管就行了」)。

        只取**第一个**能抓到卡片的链接（一条推文塞多张会喧宾夺主）。

        抓不到 (网络失败/链接不是频道也不是视频) 就返回空 —— 卡片是锦上添花,
        不该让整条解析失败。
        """
        from ...provider_api.youtube import fetch_card, find_youtube_links

        for link in find_youtube_links(text):
            card = await fetch_card(link)
            if card:
                href = html.escape(card.url, quote=True)
                label = html.escape(card.title)
                return f'> <i><a href="{href}">{label}</a></i>', []
        return "", []

    @staticmethod
    def _hashtags(tweet: TwitterTweet) -> list[str]:
        """正文与被引用推文的标签，去重保序。

        被引用推文的标签也要 —— 它渲染成引用卡片，卡片正文里的标签同样要能点
        （渲染层对引用块也走同一套链接化）。
        """
        out = list(tweet.hashtags)
        seen = set(out)
        for other in (tweet.quoted_status, tweet.reply_to):
            for tag in getattr(other, "hashtags", None) or []:
                if tag not in seen:
                    seen.add(tag)
                    out.append(tag)
        return out

    @staticmethod
    def to_media_refs(media_items: Sequence[TwitterPhoto | TwitterVideo | TwitterAni] | None) -> list[AnyMediaRef]:
        """把 provider 的媒体对象转成下载用的 ref (主推与被引用推文共用)。"""
        refs: list[AnyMediaRef] = []
        for m in media_items or []:
            match m:
                case TwitterPhoto():
                    refs.append(
                        ImageRef(
                            url=m.url,
                            ext=image_ext_from_url(m.url),
                            height=m.height,
                            width=m.width,
                            thumb_url=m.thumb_url,
                        )
                    )
                case TwitterVideo():
                    refs.append(
                        VideoRef(
                            url=m.url,
                            height=m.height,
                            width=m.width,
                            duration=int(m.duration_millis / 1000),
                            thumb_url=m.thumb_url,
                        )
                    )
                case TwitterAni():
                    refs.append(AniRef(url=m.url, ext="mp4", height=m.height, width=m.width, thumb_url=m.thumb_url))
        return refs

    @staticmethod
    async def media_parse(tweet: TwitterTweet) -> MultimediaParseResult | RichTextParseResult:
        media: list[AnyMediaRef] = TwitterParser.to_media_refs(tweet.media)

        # 被**引用 / 被回复**推文正文里的 YouTube 链接 -> 引用卡片 (标题可点) + 封面。
        # 只有这两种推文才处理: 它们本来就渲染成引用卡片, 卡片里的链接该有封面。
        # **主帖自己的正文不处理** —— 它既不是引用也不是回复, 链接原样留着。
        reply_yt_quote, reply_yt_media = (
            await TwitterParser._youtube_card(tweet.reply_to.full_text) if tweet.reply_to else ("", [])
        )
        quoted_yt_quote, quoted_yt_media = (
            await TwitterParser._youtube_card(tweet.quoted_status.full_text) if tweet.quoted_status else ("", [])
        )

        # 媒体顺序 = [正文..., 被回复..., 被引用...]（渲染层从末尾往前切两个引用块）。
        # 每档里"卡片自己正文的 YouTube 封面"排在该档媒体的**末尾**。
        reply_media = [
            *TwitterParser.to_media_refs(tweet.reply_to.media if tweet.reply_to else None),
            *reply_yt_media,
        ]
        quoted_media = [
            *TwitterParser.to_media_refs(tweet.quoted_status.media if tweet.quoted_status else None),
            *quoted_yt_media,
        ]
        media.extend(reply_media)
        media.extend(quoted_media)

        # 计数 = 各档**真会渲染出引用块**时的媒体数。判据是数据层有没有那条推文
        # （不是"正文里有没有 `> ` 形态的文字"）。
        quoted_total = len(quoted_media) if (tweet.quoted_status and TwitterParser._build_quoted_block(tweet)) else 0
        reply_total = (
            len(reply_media)
            if (tweet.reply_to and (TwitterParser._build_quote(tweet) or reply_yt_quote))
            else 0
        )
        # 投票: 结构化数据按**占位符出现顺序**收集 (reply → 主帖 → quoted) ——
        # 与 ``_compose`` / ``_quote_block`` 分配索引时用的是同一份 ``_poll_pairs``
        polls = [TwitterParser._to_result_poll(source) for _role, source in TwitterParser._poll_pairs(tweet)]
        if article := tweet.article:
            return RichTextParseResult(
                markdown_content=TwitterParser._compose(
                    article.content, tweet, reply_yt=reply_yt_quote, quoted_yt=quoted_yt_quote
                ),
                title=article.title,
                media=media,
                author_name=tweet.author_name,
                author_handle=tweet.author_handle,
                author_url=profile_url(Platform.TWITTER, tweet.author_handle),
                is_sensitive=tweet.is_sensitive,
                published_at=tweet.published_at,
                view_count=tweet.view_count,
                like_count=tweet.like_count,
                quoted_media_count=quoted_total,
                reply_media_count=reply_total,
                # 引用块的角色（按出现顺序）—— 渲染层据此归位媒体, 不再看位置
                quote_roles=TwitterParser._quote_roles(tweet),
                # 标签走**平台实体**（服务端算好的边界），渲染层据此精确链接化
                hashtags=TwitterParser._hashtags(tweet),
                polls=polls,
            )
        return MultimediaParseResult(
            content=TwitterParser._compose(
                tweet.full_text, tweet, reply_yt=reply_yt_quote, quoted_yt=quoted_yt_quote
            ),
            media=media,
            author_name=tweet.author_name,
            author_handle=tweet.author_handle,
            author_url=profile_url(Platform.TWITTER, tweet.author_handle),
            is_sensitive=tweet.is_sensitive,
            published_at=tweet.published_at,
            view_count=tweet.view_count,
            like_count=tweet.like_count,
            quoted_media_count=quoted_total,
            reply_media_count=reply_total,
            quote_roles=TwitterParser._quote_roles(tweet),
            hashtags=TwitterParser._hashtags(tweet),
            polls=polls,
        )


__all__ = ["TwitterParser"]
