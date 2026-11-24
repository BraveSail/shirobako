import html
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, ClassVar
from urllib.parse import urlparse, urlsplit, urlunparse

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
from ...types.document import Block, Document
from ...types.x_document import bind_x_attachments, document_from_x
from ...types.x_status import TextRun, XPost, XStatus, XTrending
from ...utils.helpers import format_author_link, format_quote_block, image_ext_from_url, profile_url
from ..base.base import BaseParser


class TwitterArticleParseResult(RichTextParseResult):
    """twitter 长文（article）。

    **必须声明 ``requires_media_download = True``**：长文的配图被抽成附件放在
    ``media`` 里，正文只留外链 markdown；不下载的话流水线会走「无媒体」分支
    （``processed_list`` 为空），渲染层拿不到 ``media_urls`` 就把正文里的图
    **全部剥掉** —— 用户看到的就是「没图」（实测 6 张全丢）。
    """

    requires_media_download: ClassVar[bool] = True


class TwitterParser(BaseParser):
    __platform__ = Platform.TWITTER
    __supported_type__ = ["视频", "图文"]
    __match__ = r"^(http(s)?://)?.+(twitter|fixupx|x).com/.*/(status|article)/\d+"

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

        # trending 封面与话题时间线**需要登录态**（匿名请求拿不到 timelineId，
        # 只会静默返回空）。首次解析成功时用的是无 cookie 客户端，所以这里补一次：
        # 只有 trending 且缺封面时才走，失败不影响主解析。
        if tweet.trending and not tweet.trending.cover:
            if cookie := self.cookie.get_value():
                x3 = Twitter(self.proxy, cookie=cookie)
                tweet.trending.cover = await x3.fetch_trending_cover(tweet.trending.url)
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
        if trending := TwitterParser._trending_block(tweet):
            # 卡片已带链接，正文那条裸链是同一个地址（X 上渲染成卡片不显示裸链），
            # 去掉避免同一地址出现两次
            body = TwitterParser._strip_trending_url(body, tweet)
            body = f"{body}\n\n{trending}"
        reply_block = TwitterParser._append_inside_quote(TwitterParser._build_quote(tweet), reply_yt)
        text = f"{reply_block}{body}"
        # strip: 引用块自带末尾空行（``format_quote_block`` 的块结束约定），
        # 拼接由这里负责，别让它在正文中段多出一个空行。
        quoted_block = TwitterParser._append_inside_quote(
            TwitterParser._build_quoted_block(tweet), quoted_yt
        ).strip()
        return f"{text}\n\n{quoted_block}" if quoted_block else text

    @staticmethod
    def _trending_block(tweet: TwitterTweet) -> str:
        """trending 卡片渲染成引用块（标题 + 摘要 + 可点链接）。

        为什么是引用块: 它是**被这条推文引用的一块内容**（X 上也是卡片形态），
        不是作者自己写的话。做成引用块既贴合原形态，又能和正文分开。

        链接必须保留 —— 卡片的意义就是指向那个 trending 页；没有链接时只出文案。

        **封面图在块内**（用户：「图片应该在话题里面」）：用 ``{{media:N}}`` 占位符
        表示，渲染层会把这张图塞进引用卡片内部（``attach_quote_media``）。
        占位符紧跟在标题之后、摘要之前 —— 与 X 上卡片的形态一致（大图在上）。
        """
        card = getattr(tweet, "trending", None)
        if card is None:
            return ""
        lines: list[str] = []
        if card.title:
            lines.append(f"> **{card.title}**")
        if card.cover:
            # 作为引用块内的媒体。索引固定为 **0** —— 这是主帖媒体列表里
            # ``media_parse`` 追加的那一张（见那里：``media.append(ImageRef(cover))``
            # 排在主帖媒体末尾，而 trending 帖子主帖没有其它媒体）。
            if lines:
                lines.append(">")
            lines.append("> {{media:0}}")
        if card.subtitle:
            # 摘要可能有多段换行，逐行加引用前缀，保持块连续
            for line in card.subtitle.splitlines():
                lines.append(f"> {line}" if line.strip() else ">")
        if card.url:
            if lines:
                lines.append(">")
            lines.append(f"> [查看原話題]({card.url})")
        return "\n".join(lines)

    @staticmethod
    def _strip_trending_url(body: str, tweet: TwitterTweet) -> str:
        """去掉正文里指向 trending 的那条链接**本身**，保留同一行的其它文字。

        那条链接就是卡片本身的 ``destination``（X 上渲染成卡片，不显示裸链）。
        正文里留着它 + 卡片里再给一次 = 同一地址出现两遍。

        ⚠️ 只删 URL，不要按整行删 —— 实测这条推文的正文把「…いきたい！ですので…」
        和 URL 挤在**同一行**，整行过滤会把末尾那句原文一起吞掉。
        """
        card = getattr(tweet, "trending", None)
        if card is None or not card.url:
            return body
        trending_id = card.url.rstrip("/").rsplit("/", 1)[-1]
        if not trending_id:
            return body
        needle = f"/i/trending/{trending_id}"
        cleaned: list[str] = []
        for line in body.splitlines():
            if needle not in line:
                cleaned.append(line)
                continue
            # 只摘掉那个 token（t.co 短链 / 展开后的 URL 都算），行内其它文字保留
            tokens = [
                tok
                for tok in line.split()
                if not (tok.startswith("http") and (needle in tok or tok.startswith("https://t.co/")))
            ]
            remainder = " ".join(tokens).rstrip()
            if remainder:
                cleaned.append(remainder)
        return "\n".join(cleaned).rstrip()

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
    async def _youtube_link(text: str | None) -> TextRun | None:
        """取正文里首个可用的 YouTube 卡片（仅标题和链接）。

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
                return TextRun(card.title, card.url, ("italic",))
        return None

    @staticmethod
    def _youtube_quote(card: TextRun | None) -> str:
        if card is None:
            return ""
        return f'> <i><a href="{html.escape(card.url, quote=True)}">{html.escape(card.text)}</a></i>'

    @staticmethod
    async def _youtube_card(text: str | None) -> tuple[str, list[AnyMediaRef]]:
        """兼容旧卡片行接口；正文结构不从这里的 HTML 恢复。"""
        return TwitterParser._youtube_quote(await TwitterParser._youtube_link(text)), []

    @staticmethod
    async def _related_youtube(post: TwitterTweet | None) -> TextRun | None:
        if post is None:
            return None
        text = "".join(f" {run.url} " if run.url else run.text for run in post.segments)
        return await TwitterParser._youtube_link(text)

    @staticmethod
    def _trending_path(url: str) -> str:
        try:
            parsed = urlsplit(url)
        except ValueError:
            return ""
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com",
        }:
            return ""
        path = parsed.path.rstrip("/")
        return path if path.startswith("/i/trending/") else ""

    @staticmethod
    def _native_status(
        tweet: TwitterTweet,
        *,
        reply_youtube: TextRun | None,
        quoted_youtube: TextRun | None,
        cover_index: int | None,
    ) -> XStatus:
        indexes = TwitterParser._poll_indexes(tweet)

        def post(
            source: TwitterTweet, segments: tuple[TextRun, ...], role: str, youtube: TextRun | None = None,
        ) -> XPost:
            return XPost(
                segments=segments,
                author_name=source.author_name or "",
                author_handle=source.author_handle or "",
                author_url=profile_url(Platform.TWITTER, source.author_handle or ""),
                poll_index=indexes.get(role),
                youtube=youtube,
            )

        def related(source: TwitterTweet | None, role: str, youtube: TextRun | None) -> XPost | None:
            if source and (source.media or youtube or role in indexes or any(r.text.strip() for r in source.segments)):
                return post(source, source.segments, role, youtube)
            return None

        segments = tweet.segments
        trending = None
        if card := tweet.trending:
            trending = XTrending(title=card.title, subtitle=card.subtitle, url=card.url, cover_index=cover_index)
            if path := TwitterParser._trending_path(card.url):
                # Remove only the matching entity run(s), never an unlinked literal or the rest of its line.
                segments = tuple(run for run in segments if TwitterParser._trending_path(run.url) != path)

        return XStatus(
            main=post(tweet, segments, "main"),
            reply=related(tweet.reply_to, "reply", reply_youtube),
            quoted=related(tweet.quoted_status, "quoted", quoted_youtube),
            trending=trending,
        )

    @staticmethod
    def _document(tweet: TwitterTweet, status: XStatus, media: list[AnyMediaRef],
                  main_end: int, reply_end: int) -> Document:
        def part(source: TwitterTweet | None, start: int, end: int) -> tuple[tuple[Block, ...], tuple[Block, ...]]:
            used: set[int] = set()

            def shifted(block: Block) -> Block:
                if block.kind == "media" and block.index >= 0:
                    index = start + block.index
                    used.add(index)
                    return replace(block, index=index)
                return replace(block, children=tuple(map(shifted, block.children)),
                               items=tuple(tuple(map(shifted, item)) for item in block.items))

            body = tuple(map(shifted, source.article.blocks)) if source and source.article else ()
            attachments = tuple(Block("media", index=i, url=media[i].url) for i in range(start, end) if i not in used)
            return body, attachments

        cover = status.trending.cover_index if status.trending else None
        main_article, main = part(tweet, 0, cover if cover is not None else main_end)
        reply_article, reply = part(tweet.reply_to, main_end, reply_end)
        quoted_article, quoted = part(tweet.quoted_status, reply_end, len(media))

        def signer(source: TwitterTweet) -> XPost:
            return XPost((), source.author_name or "", source.author_handle or "",
                         profile_url(Platform.TWITTER, source.author_handle or ""))

        if (source := tweet.reply_to) and status.reply is None and (reply_article or reply):
            status = replace(status, reply=signer(source))
        if (source := tweet.quoted_status) and status.quoted is None and (quoted_article or quoted):
            status = replace(status, quoted=signer(source))
        return bind_x_attachments(
            document_from_x(status, article=main_article if tweet.article else None),
            main=main, reply=reply, quoted=quoted, trending_url=media[cover].url if cover is not None else "",
            reply_article=reply_article, quoted_article=quoted_article,
        )

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
        # 长文（article）自己的媒体是**独立**的一份 —— 它们不在 ``tweet.media`` 里
        # （实测 article 有 5 个媒体、``tweet.media`` 为 0 个）。不并进来的话正文
        # 里的图全部拿不到（用户报「图片呢」）。article 的正文就是主体，所以排在最前。
        if tweet.article:
            media = [*TwitterParser.to_media_refs(tweet.article.media), *media]

        # trending 卡片的话题封面：**不在** ``tweet.media`` 里（那里的图是 X 的
        # 通用占位图，已在 `_parse_unified_card_media` 里丢掉），由 provider 层
        # 从话题页补取后放在 ``tweet.trending.cover``。排主帖媒体末尾。
        cover_index = None
        if tweet.trending and tweet.trending.cover:
            cover_index = len(media)
            media.append(ImageRef(url=tweet.trending.cover))

        # 每个关联帖只走原有的一次卡片查询流程，同一份标题/链接同时给结构和旧引用行。
        reply_youtube = await TwitterParser._related_youtube(tweet.reply_to)
        quoted_youtube = await TwitterParser._related_youtube(tweet.quoted_status)
        reply_yt_quote = TwitterParser._youtube_quote(reply_youtube)
        quoted_yt_quote = TwitterParser._youtube_quote(quoted_youtube)

        # 媒体顺序 = [正文..., 被回复..., 被引用...]（渲染层从末尾往前切两个引用块）。
        # YouTube 链接卡片只有文字，不占媒体索引。
        def related_media(source: TwitterTweet | None) -> list[AnyMediaRef]:
            if source is None:
                return []
            return [*TwitterParser.to_media_refs(source.article.media if source.article else None),
                    *TwitterParser.to_media_refs(source.media)]

        reply_media = related_media(tweet.reply_to)
        quoted_media = related_media(tweet.quoted_status)
        main_end = len(media)
        media.extend(reply_media)
        reply_end = len(media)
        media.extend(quoted_media)

        # 计数 = 各档**真会渲染出引用块**时的媒体数。判据是数据层有没有那条推文
        # （不是"正文里有没有 `> ` 形态的文字"）。
        quoted_total = (
            len(quoted_media)
            if (tweet.quoted_status and (tweet.quoted_status.article or TwitterParser._build_quoted_block(tweet)))
            else 0
        )
        reply_total = (
            len(reply_media)
            if (tweet.reply_to and (tweet.reply_to.article or TwitterParser._build_quote(tweet) or reply_yt_quote))
            else 0
        )
        # 投票: 结构化数据按**占位符出现顺序**收集 (reply → 主帖 → quoted) ——
        # 与 ``_compose`` / ``_quote_block`` 分配索引时用的是同一份 ``_poll_pairs``
        polls = [TwitterParser._to_result_poll(source) for _role, source in TwitterParser._poll_pairs(tweet)]
        quote_ids: dict[str, str] = {}
        if tweet.reply_to and TwitterParser._build_quote(tweet):
            if rid := (tweet.reply_to.tweet_id or tweet.reply_to_id):
                quote_ids["reply"] = rid
        if tweet.quoted_status and TwitterParser._build_quoted_block(tweet):
            if qid := (tweet.quoted_status.tweet_id or tweet.quoted_status_id):
                quote_ids["quoted"] = qid
        status = TwitterParser._native_status(
            tweet, reply_youtube=reply_youtube, quoted_youtube=quoted_youtube, cover_index=cover_index,
        )
        document = TwitterParser._document(tweet, status, media, main_end, reply_end)
        if article := tweet.article:
            result = TwitterArticleParseResult(
                markdown_content=TwitterParser._compose(
                    article.content, tweet, reply_yt=reply_yt_quote, quoted_yt=quoted_yt_quote
                ),
                title=article.title,
                document=document,
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
                quote_ids=quote_ids,
                # 标签走**平台实体**（服务端算好的边界），渲染层据此精确链接化
                hashtags=TwitterParser._hashtags(tweet),
                polls=polls,
            )
            # The article is a DraftJS document, not the surrounding post teaser.
            # Preserve the translated reply/quote roles without targeting that teaser.
            result.x_status = replace(status, main=replace(status.main, segments=()))
            return result
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
            quote_ids=quote_ids,
            hashtags=TwitterParser._hashtags(tweet),
            polls=polls,
            x_status=status,
            document=document,
        )


__all__ = ["TwitterParser"]
