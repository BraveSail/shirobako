from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlparse

from loguru import logger

from ...provider_api.bilibili import BiliAPI, BiliDynamic, BiliImage
from ...types import (
    DownloadResult,
    ImageParseResult,
    ImageRef,
    LivePhotoRef,
    ParseError,
    Platform,
    ProgressCallback,
    VideoParseResult,
    VideoRef,
)
from ...utils.helpers import (
    UA,
    format_author_link,
    format_quote_block,
    get_author_name,
    image_ext_from_url,
    profile_url,
)
from ..base.base import BaseParser


class BiliParse(BaseParser):
    __platform__ = Platform.BILIBILI
    __supported_type__ = ["视频", "动态"]
    __match__ = r"^(http(s)?://)?((((w){3}.|(m).|(t).)?bilibili\.com)/(video|opus|\b\d{18,19}\b)|b23.tv|bili2233.cn).*"
    __reserved_parameters__ = ["p"]
    __redirect_keywords__ = ["b23.tv", "bili2233.cn"]

    @staticmethod
    def _to_refs(images: list[BiliImage] | None) -> list[LivePhotoRef | ImageRef]:
        """BiliImage -> 下载用的 Ref (实况照片带视频)。"""
        refs: list[LivePhotoRef | ImageRef] = []
        for i in images or []:
            if i.live_url:
                refs.append(
                    LivePhotoRef(
                        url=i.url,
                        video_url=i.live_url,
                        width=i.width,
                        height=i.height,
                        ext=image_ext_from_url(i.url),
                    )
                )
            else:
                refs.append(ImageRef(url=i.url, width=i.width, height=i.height, ext=image_ext_from_url(i.url)))
        return refs

    @staticmethod
    def _split_forward_comment(content: str) -> tuple[str, str]:
        """把转发动态的正文拆成 (转发者自己的评论, ``//@`` 段里的原文)。

        转发格式是「转发者的话 + ``//@原作者:被转发的原文``」。后者是**被转发动态的
        正文**, 和引用块里来自 ``orig`` 的东西**不是一回事** —— ``orig`` 是视频时给的是
        视频标题/简介, 这段文字不见于其中 (实测: 丢的是「10月新番《脑洞学生会！》第1话
        已更新！」，而引用块里只有视频标题)。所以不能直接丢, 交给引用块一起渲染。
        """
        if not content:
            return content, ""
        # 第一个 //@ 之前是转发者自己的评论 (多层转发时同样只留最外层)
        parts = re.split(r"\s*\u200b?\s*//@", content, maxsplit=1)
        comment = parts[0].strip()
        if len(parts) == 1:
            return comment, ""
        rest = parts[1]
        # 去掉 "原作者:" 前缀, 只留原文
        for sep in (":", "："):
            if sep in rest:
                rest = rest.split(sep, 1)[1]
                break
        return comment, rest.strip()

    @staticmethod
    def _forward_text_is_covered(extra: str, forward: BiliDynamic) -> bool:
        """``//@`` 段的原文是否已被引用块内容覆盖 (覆盖了就别重复渲染)。"""
        normalize = lambda s: re.sub(r"[\s#《》「」【】:：!！?？。，,.·\u200b]", "", s or "")  # noqa: E731
        target = normalize(extra)
        if not target:
            return True
        return target in normalize(f"{forward.title or ''}{forward.content or ''}")

    @classmethod
    def _render_forward(cls, forward: BiliDynamic, extra_text: str = "") -> str:
        """把被转发的原动态渲染成引用块 (作者带主页链接, 内容取标题或正文)。

        **被转发的是视频时, 标题链到视频页** —— 引用块里只有封面图, 没有视频本身,
        标题不可点就等于看得到标题、进不去视频 (用户明确要求)。

        :param extra_text: 主动态 ``//@`` 段里的**原动态正文**。``orig`` 是视频时它带的是
            视频标题/简介, 这段文字不见于其中 —— 丢掉就是内容丢失 (用户报「为什么丢了
            //@…这些内容」), 所以并进引用块; 与已有内容重复时跳过。
        """
        raw_title = (forward.title or "").strip()
        title = raw_title
        if raw_title and (url := forward.video_url):
            title = f'<a href="{url}">{raw_title}</a>'
        text = (forward.content or "").strip()

        lines: list[str] = []
        if extra_text and not cls._forward_text_is_covered(extra_text, forward):
            lines.append(extra_text)          # 原动态正文在前, 视频信息在后
        if title:
            lines.append(title)
        if text and text != raw_title:
            lines.append(text)
        body = "\n".join(lines)

        if not body and not forward.images:
            return ""
        author = format_author_link(
            forward.author_name or "",
            "",
            profile_url(Platform.BILIBILI, user_id=forward.author_mid),
        )
        # sign_only: 转发的是纯视频/纯图 (无文字) 时也要出引用块 —— 否则
        # quoted_media_count 仍算着它的媒体, 引用块却不存在, 图片会掉进正文
        return format_quote_block(body, author, sign_only=True)

    async def _do_parse(self, raw_url: str) -> BiliVideoParseResult | ImageParseResult:
        if await self.is_dynamic(raw_url):
            dynamic = await self.get_dynamic_info(raw_url)
            content = self.hashtag_handler(dynamic.content or "", dynamic.topics)
            photos: list[LivePhotoRef | ImageRef] = []
            photos.extend(BiliParse._to_refs(dynamic.images))

            # 转发动态: 被转发的原动态渲染成引用块 (文字 + 它自己的媒体)
            quoted_media_count = 0
            has_forward_quote = False
            if forward := dynamic.forward:
                content, forward_text = BiliParse._split_forward_comment(content)
                if forward_text:
                    # ``//@`` 段的原文就是被转发动态的正文, 所以能用它的节点话题
                    forward_text = BiliParse.hashtag_handler(
                        forward_text, dynamic.forward.topics if dynamic.forward else None
                    )
                if quote := BiliParse._render_forward(forward, extra_text=forward_text):
                    content = f"{content}\n\n{quote}" if content else quote
                    has_forward_quote = True
                forward_refs = BiliParse._to_refs(forward.images)
                quoted_media_count = len(forward_refs)
                photos.extend(forward_refs)

            return ImageParseResult(
                title=dynamic.title or "",
                author_name=dynamic.author_name,
                # B 站没有 @用户名, 主页靠 UID: 名字本身会渲染成 space 链接
                author_url=profile_url(Platform.BILIBILI, user_id=dynamic.author_mid),
                content=content,
                photo=photos,
                published_at=dynamic.published_at,
                like_count=dynamic.like_count,
                quoted_media_count=quoted_media_count,
                # 引用块的角色（被转发的原动态在末尾）—— 渲染层据此归位媒体
                quote_roles=["quoted"] if has_forward_quote else [],
            )
        else:
            try:
                return await self.bili_api_parse(raw_url)
            except ParseError:
                raise
            except Exception as e:
                # 这里**没有** yt-dlp 兜底（2026-10-08 移除）—— B 站 API 被风控时不再有后备路径。
                # 所以失败原因要完整带出来: 原实现把它吞成一句"Bilibili 解析失败",
                # 排查时看不到到底是风控、cookie 失效还是接口变了。
                logger.opt(exception=e).warning("Bilibili API 解析失败")
                raise ParseError(f"Bilibili 解析失败: {e}") from e

    @staticmethod
    def _is_bvid(url: str) -> bool:
        if url.lower().startswith("bv"):
            return True
        else:
            return False

    @classmethod
    def match(cls, text: str) -> bool:
        if cls._is_bvid(text):
            return True
        else:
            return super().match(text)

    async def get_raw_url(self, url: str, **kwargs: Any) -> str:
        """获取原始链接"""
        if self._is_bvid(url):
            return f"https://www.bilibili.com/video/{url}"
        else:
            raw_url = await super().get_raw_url(url, **kwargs)
            u = urlparse(raw_url)
            q = urlencode([(k, v) for k, v in parse_qsl(u.query) if (k, v) != ("p", "1")])
            return u._replace(query=q).geturl()

    @staticmethod
    async def is_dynamic(url: str) -> str | None:
        """是动态"""
        if re.search(r"\b\d{18,19}\b", url):
            return url
        return None

    async def get_dynamic_info(self, url: str) -> BiliDynamic:
        try:
            async with BiliAPI(proxy=self.proxy) as bili:
                dynamic_info = await bili.get_dynamic_info(url, cookie=self.cookie.get_value())
        except Exception as e:
            if "风控" in str(e):
                raise ParseError(f"账号风控\n使用的cookie: {self.cookie}") from e
            raise ParseError(str(e)) from e
        else:
            return cast(BiliDynamic, dynamic_info)

    async def bili_api_parse(self, url: str) -> BiliVideoParseResult | ImageParseResult:
        async with BiliAPI(proxy=self.proxy) as bili:
            video_info = await bili.get_video_info(url, cookie=self.cookie.get_value())

            if not (data := video_info.get("data")):
                raise ParseError("获取视频信息失败")

            p = int(parse_qs(urlparse(url).query).get("p", ["1"])[0])
            view = data["View"]

            cid = view["cid"]
            duration = view["duration"]
            dimension = view["dimension"]
            desc = view["desc"]

            if p != 1 and (pages := view.get("pages")):
                if page_info := next((i for i in pages if i["page"] == p), None):
                    cid = page_info["cid"]
                    duration = page_info["duration"]
                    dimension = page_info["dimension"]

            b3, b4 = await bili.get_buvid()
            video_playurl = await bili.get_video_playurl(url, cid, b3, b4)

        durl = video_playurl["data"]["durl"][0]
        # 地址选择（2026-10-13 用户决定）: **优先 bilivideo 副本**。
        #
        # 背景: 此前用 B 站给的 ``durl.url`` 首选（历史上它通常就是 bilivideo）。但 161 上
        # 18 条历史下载统计显示两者的真实差距很大:
        #   - bilivideo.com: 平均 8.99 MB/s, 0 次传输中断
        #   - akamaized.net: 平均 3.42 MB/s, 6 次传输中断（有过主地址连断 4 次才切备用）
        # 而 B 站会**把 akamai 当首选发给某些视频**（同一响应里 bilivideo 只是备用）。
        # 所以这里做的是**挑选**（从响应给的副本里选 bilivideo），不是改写域名 ——
        # 改写域名 = 伪造签名（签名与 host 绑定，实测 403/超时），那条红线不变。
        candidates = [durl["url"], *(durl.get("backup_url") or [])]
        candidates = [u for u in dict.fromkeys(candidates) if isinstance(u, str) and u.strip()]
        preferred = next((u for u in candidates if "bilivideo.com" in urlparse(u).netloc), None)
        if preferred:
            video_url = preferred
            # 其余副本（含可能被降级的原首选) 全部作备用
            backup_urls = tuple(u for u in candidates if u != preferred)
        else:
            # 响应里没有 bilivideo 副本时保持原样（不能凭空造地址）
            video_url = durl["url"]
            backup_urls = tuple(u for u in (durl.get("backup_url") or []) if isinstance(u, str) and u.strip())
        content = desc.strip()
        if content == "-":
            content = ""
        owner = view.get("owner") or {}
        return BiliVideoParseResult(
            title=data["View"]["title"],
            author_name=get_author_name(owner),
            # B 站没有 @用户名, 主页靠 UID: 名字本身会渲染成 space 链接
            author_url=profile_url(Platform.BILIBILI, user_id=owner.get("mid")),
            content=content,
            published_at=view.get("pubdate"),
            view_count=(view.get("stat") or {}).get("view"),
            video=VideoRef(
                url=video_url,
                backup_urls=backup_urls,
                thumb_url=data["View"]["pic"],
                duration=duration,
                width=dimension.get("width", 0),
                height=dimension.get("height", 0),
            ),
        )

    @staticmethod
    def hashtag_handler(desc: str, topics: Sequence[Mapping[str, str]] | None = None) -> str:
        """把 ``#话题#`` 渲染成指向 B 站搜索页的超链接。

        :param topics: **节点给的**话题列表（``BiliDynamic.topics``: name + url）。
            给了就按名字精确链接化、并用节点自带的话题页地址 —— 边界与类型由 B 站判定，
            不必用正则猜（``C# 与 #tag#`` 这种成对 ``#`` 正则匹不出正确的话题）。
            没给（老接口 / 转发的 ``//@`` 段没有节点）时退回正则，行为不变。


        用 HTML ``<a>`` 而不是 markdown 链接: 这条正文既可能落在引用块里 (块内
        markdown 行内语法不解析), 也可能落在正文, HTML 标签两处都有效。

        不额外补空格 (旧实现会换成 `` #话题 ``): 补空格是为了防止行首 ``#`` 被当成
        markdown 标题; 现在行首是 ``<a``, 不存在这个风险, 而补出来的空格会留在
        「《 #话题 》」这种紧贴标点的位置里 (用户可见的瑕疵)。
        """
        if not desc:
            return ""

        # ① 先用**节点给的话题**精确链接化（类型与边界由 B 站判定，且节点自带话题页地址）
        if topics:
            # 长名字优先: 两个话题名互为前缀时，先换长的才不会把短名嵌进去
            named = [t for t in topics if (t or {}).get("name")]
            for topic in sorted(named, key=lambda t: len(t["name"]), reverse=True):
                name = topic["name"]
                url = topic.get("url") or f"https://search.bilibili.com/all?keyword={quote(name)}"
                if url.startswith("//"):
                    # 节点给的是**协议相对**地址（``//search.bilibili.com/…``），
                    # 直接塞进 <a href> 会被当成站内相对路径 —— 补上 https:
                    url = f"https:{url}"
                desc = desc.replace(f"#{name}#", f'<a href="{html.escape(url, quote=True)}">#{html.escape(name)}#</a>')
            return desc

        # ② 兜底：节点拿不到时按 ``#话题#`` 形态链接化
        def _to_link(match: re.Match) -> str:
            # B 站的话题是**左右都有 #** (#话题#), 显示时两侧都保留
            topic = match.group(0).strip("#")
            url = f"https://search.bilibili.com/all?keyword={quote(topic)}"
            return f'<a href="{url}">#{topic}#</a>'

        return re.sub(r"#[^#]+#", _to_link, desc)


class BiliVideoParseResult(VideoParseResult):
    async def _do_download(
        self,
        *,
        output_dir: Path,
        callback: ProgressCallback | None = None,
        callback_args: tuple = (),
        callback_kwargs: dict | None = None,
        proxy: str | None = None,
        headers: dict | None = None,
        connections: int = 4,
    ) -> DownloadResult:
        headers = {"referer": "https://www.bilibili.com", "User-Agent": UA}
        return await super()._do_download(
            output_dir=output_dir,
            callback=callback,
            callback_args=callback_args,
            callback_kwargs=callback_kwargs,
            proxy=proxy,
            headers=headers,
            connections=connections,
        )


__all__ = [
    "BiliParse",
    "BiliVideoParseResult",
]
