from ...provider_api.threads import (
    ThreadsAPI,
    ThreadsAPIError,
    ThreadsMedia,
    ThreadsMediaType,
    ThreadsPoll,
    ThreadsPost,
)
from ...types import AnyMediaRef, ImageRef, MultimediaParseResult, ParseError, Platform, VideoRef
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
        quote = ThreadsParser._build_quote(post)
        # 帖子附带的**长文块**（caption 底下的"阅读更多"内容）渲染成**引用块**,
        # 排在正文之后（用户要求:「这个长文改成引用块」）。走公共 helper ——
        # 与其它平台的引用块同形（整块斜体）。
        snippet_quote = format_quote_block(post.snippet) if post.snippet else ""
        # 帖子附带的**投票**（caption 下方的贴纸）渲染成 markdown 表格,
        # 摆在正文之后、长文引用块之前（与 twitter 的投票同一形态与位置）。
        poll_table = ThreadsParser._build_poll(post)
        # 引用块的角色按出现顺序声明: 被回复帖在前（reply），长文块在后（quoted）——
        # 渲染层据此归位，不靠位置猜。
        roles = (["reply"] if quote else []) + (["quoted"] if snippet_quote else [])
        # 四段按序拼接: 被回复帖引用块 → caption 正文 → 投票表格 → 长文引用块。
        # 每段各自 strip（``format_quote_block`` 末尾自带空行，直接 join 会堆出多余空行）。
        content = "\n\n".join(
            p for p in (quote.strip(), post.content.strip(), poll_table, snippet_quote.strip()) if p
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
        )

    @staticmethod
    def _poll_cell(text: str) -> str:
        """表格单元格转义 —— 选项文案里的 ``|`` 会把列切断。"""
        return text.replace("|", "\\|")

    @staticmethod
    def _build_poll(post: ThreadsPost) -> str:
        """投票 -> markdown 表格, 没有投票时返回空串。

        形态与 twitter / linux.do 的投票一致（选项 / 票数 / 占比三列）——
        表格由渲染层转成服务端的 Table 块。

        占比按**总票数**算; 一票都还没有时占比记 0%, 不抛错。

        选项文案里的 ``|`` 要转义 —— 否则会把表格的列切断。
        """
        poll: ThreadsPoll | None = post.poll
        if not poll or not poll.choices:
            return ""
        total = sum(count for _, count in poll.choices)
        rows = [
            "| 选项 | 票数 | 占比 |",
            "| --- | --- | --- |",
        ]
        rows.extend(
            f"| {ThreadsParser._poll_cell(label)} | {count} | {round(count * 100 / total) if total else 0}% |"
            for label, count in poll.choices
        )
        return "\n".join(rows)

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
    def _build_quote(post: ThreadsPost) -> str:
        """把被回复的帖子渲染成引用块 (排版统一由公共 helper 决定)."""
        reply = post.reply_to
        if not reply:
            return ""
        # 投票跟着**它所属的那条帖**的正文走 —— 被回复帖带投票时,
        # 表格渲染在它自己的引用块里（与 twitter 的处理一致）。
        body = reply.content or ""
        if poll_table := ThreadsParser._build_poll(reply):
            body = f"{body}\n\n{poll_table}"
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
