from ...provider_api.threads import (
    ThreadsAPI,
    ThreadsAPIError,
    ThreadsMedia,
    ThreadsMediaType,
    ThreadsPost,
)
from ...types import (
    AnyMediaRef,
    ImageRef,
    MultimediaParseResult,
    ParseError,
    Platform,
    Poll,
    PollOption,
    VideoRef,
)
from ...utils.helpers import format_author_link, format_quote_block, image_ext_from_url, profile_url
from ..base.base import BaseParser


class ThreadsParser(BaseParser):
    __platform__ = Platform.THREADS
    __supported_type__ = ["视频", "图文"]
    __match__ = r"^(http(s)?://)?.+threads.com/(@[\w.]+/post|share)/.*"
    __redirect_keywords__ = ["/share/"]

    async def get_raw_url(self, url: str, *, clean_all: bool = False, headers: dict | None = None) -> str:
        return await super().get_raw_url(url, clean_all=clean_all, headers={})

    async def _do_parse(self, raw_url: str) -> "MultimediaParseResult":
        post = await self._parse(raw_url)
        media = ThreadsParser._to_refs(post.media)
        # 被回复帖的媒体也要发 —— 它渲染成回复卡片, 卡片里的图不能丢
        # (用户报「回复的图片丢了」)。顺序与 twitter 一致: 正文媒体在前, 回复媒体在后,
        # 再用 ``reply_media_count`` 告诉渲染层"末尾这几个属于回复卡片"。
        reply_media = ThreadsParser._to_refs(post.reply_to.media if post.reply_to else None)
        media.extend(reply_media)
        # 投票按**占位符出现顺序**收集: 被回复帖(若有) → 主帖 —— 与正文里的顺序一致。
        # 索引纪律: ``result.polls[i]`` 对应正文里第 i 个 ``{{poll:N}}``。
        reply_has_poll = ThreadsParser._has_poll(post.reply_to)
        main_has_poll = ThreadsParser._has_poll(post)
        polls = [
            *([ThreadsParser._to_result_poll(post.reply_to)] if reply_has_poll else []),
            *([ThreadsParser._to_result_poll(post)] if main_has_poll else []),
        ]
        # reply 块在正文最前 ⇒ 有它才有索引 0; 主帖占位符排在正文之后
        quote = ThreadsParser._build_quote(post, poll_index=0 if reply_has_poll else None)
        # 帖子附带的**长文块**（caption 底下的"阅读更多"内容）渲染成**引用块**,
        # 排在正文之后（用户要求:「这个长文改成引用块」）。走公共 helper ——
        # 与其它平台的引用块同形（整块斜体）。
        snippet_quote = format_quote_block(post.snippet) if post.snippet else ""
        # 帖子附带的**投票**（caption 下方的贴纸）在正文里放占位符行,
        # 摆在正文之后、长文引用块之前（与 twitter 的投票同一位置）。
        poll_placeholder = ThreadsParser._poll_placeholder(
            post, 1 if (reply_has_poll and main_has_poll) else (0 if main_has_poll else None)
        )
        # 引用块的角色按出现顺序声明: 被回复帖在前（reply），长文块在后（quoted）——
        # 渲染层据此归位，不靠位置猜。
        roles = (["reply"] if quote else []) + (["quoted"] if snippet_quote else [])
        # 四段按序拼接: 被回复帖引用块 → caption 正文 → 投票占位符 → 长文引用块。
        # 每段各自 strip（``format_quote_block`` 末尾自带空行，直接 join 会堆出多余空行）。
        content = "\n\n".join(
            p for p in (quote.strip(), post.content.strip(), poll_placeholder, snippet_quote.strip()) if p
        )
        return MultimediaParseResult(
            content=content,
            media=media,
            author_name=post.author_name,
            author_handle=post.author_handle,
            author_url=profile_url(Platform.THREADS, post.author_handle),
            published_at=post.published_at,
            view_count=post.view_count,
            like_count=post.like_count,
            reply_media_count=len(reply_media),
            # 引用块的角色（被回复帖在最前）—— 渲染层据此归位媒体, 不再靠位置
            quote_roles=roles,
            polls=polls,
        )

    @staticmethod
    def _has_poll(post: ThreadsPost | None) -> bool:
        """这条帖有没有投票 (有选项才算)。"""
        return bool(post and post.poll and post.poll.choices)

    @staticmethod
    def _poll_placeholder(post: ThreadsPost, index: int | None) -> str:
        """投票 -> 正文里独立一行的占位符 ``{{poll:N}}``, 没有投票 (或索引未知) 时返回空串。

        **lib 只输出占位符**: 「选项/票数/占比」那套文案由渲染层按用户语言拼装
        (``result.polls`` 里带结构化数据)。
        """
        if not ThreadsParser._has_poll(post) or index is None:
            return ""
        return f"{{{{poll:{index}}}}}"

    @staticmethod
    def _to_result_poll(post: ThreadsPost | None) -> Poll:
        """帖子的投票 -> 结构化 ``Poll`` (选项与票数照平台给的原样)。"""
        choices = post.poll.choices if post and post.poll else []
        return Poll(
            options=[PollOption(text=text, count=count) for text, count in choices],
            total_votes=sum(count for _, count in choices),
            is_finished=bool(post.poll.is_finished) if post and post.poll else False,
        )

    @staticmethod
    def _to_refs(raw: "ThreadsMedia | list[ThreadsMedia] | None") -> list[AnyMediaRef]:
        """把 provider 的媒体对象转成结果用的 ref（单条与列表两种形态都吃）。"""
        if not raw:
            return []
        items: list[ThreadsMedia] = raw if isinstance(raw, list) else [raw]
        out: list[AnyMediaRef] = []
        for m in items:
            if m.type == ThreadsMediaType.VIDEO:
                out.append(VideoRef(url=m.url, thumb_url=m.thumb_url, width=m.width, height=m.height))
            elif m.type == ThreadsMediaType.IMAGE:
                out.append(
                    ImageRef(
                        url=m.url,
                        thumb_url=m.url,
                        width=m.width,
                        height=m.height,
                        ext=image_ext_from_url(m.url),
                    )
                )
        return out

    @staticmethod
    def _build_quote(post: ThreadsPost, poll_index: int | None = None) -> str:
        """把被回复的帖子渲染成引用块 (排版统一由公共 helper 决定).

        :param poll_index: 被回复帖的 poll 占位符索引 (``{{poll:N}}``); 由 ``_do_parse``
            按「占位符在正文里的出现顺序」分配 (reply 块在最前 ⇒ 索引 0)。
        """
        reply = post.reply_to
        if not reply:
            return ""
        # 投票跟着**它所属的那条帖**的正文走 —— 被回复帖带投票时,
        # 占位符渲染在它自己的引用块里（与 twitter 的处理一致）。
        body = reply.content or ""
        if placeholder := ThreadsParser._poll_placeholder(reply, poll_index):
            body = f"{body}\n\n{placeholder}"
        return format_quote_block(
            body,
            format_author_link(
                reply.author_name or "",
                reply.author_handle or "",
                profile_url(Platform.THREADS, reply.author_handle or ""),
            ),
        )

    async def _parse(self, url: str) -> ThreadsPost:
        # 公开帖子无需登录即可解析; 登录墙内容 (私密/受限/年龄限制) 才需要 Cookie, 有则带上
        try:
            api = ThreadsAPI(proxy=self.proxy, cookie=self.cookie.get_value() if self.cookie else None)
            return await api.parse(url)
        except ThreadsAPIError as e:
            if not self.cookie:
                raise ParseError("无法获取帖子内容: 该帖子可能位于登录墙内, 请为 threads 平台配置 Cookie") from e
            raise ParseError("无法获取帖子内容(可能为私人或受限内容, 或 Cookie 已失效)") from e
        except ParseError:
            raise
        except Exception as e:
            raise ParseError(f"无法获取帖子内容: {e}") from e


__all__ = ["ThreadsParser"]
