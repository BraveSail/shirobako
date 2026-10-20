import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Self

from ... import ProgressCallback
from ...provider_api.douyin import DouyinMobileCrawler, DouyinMobileDevice, DouyinWebCrawler
from ...types import (
    AnyParseResult,
    DownloadResult,
    ImageParseResult,
    ImageRef,
    LivePhotoRef,
    ParseError,
    ParseResult,
    Platform,
    RichTextParseResult,
    VideoParseResult,
    VideoRef,
)
from ...utils.helpers import (
    get_author_name,
    image_ext_from_url,
    media_placeholder,
    profile_url,
    to_datetime,
    to_int,
)
from ..base.base import BaseParser


class DouyinParser(BaseParser):
    __platform__ = Platform.DOUYIN
    __supported_type__ = ["视频", "图文", "日常"]
    __match__ = r"^(http(s)?://)?.+douyin.com/(?!share/user|qishui).+"
    __redirect_keywords__ = ["v.douyin", "iesdouyin"]
    __reserved_parameters__ = ["modal_id"]

    async def _do_parse(self, raw_url: str) -> "AnyParseResult":
        result = await self._fetch_api_result(raw_url)

        match result.type:
            case DouyinMediaType.VIDEO:
                return self._build_video_result(result)
            case DouyinMediaType.IMAGE:
                return self._build_image_result(result)
            case DouyinMediaType.ARTICLE:
                return self._build_article_result(result)

    async def _fetch_api_result(self, raw_url: str) -> "DouyinApiResult":
        """获取并解析抖音 API 结果"""
        web_cookie = self.cookie.get_value() or {}

        web_error: ParseError | None = None
        if web_cookie:
            try:
                web_crawler = DouyinWebCrawler(proxy=self.proxy, cookie=web_cookie)
                response = await web_crawler.parse(raw_url)
                return DouyinApiResult.parse(response)
            except ParseError as e:
                web_error = e

        try:
            mobile_device = DouyinMobileDevice.resolve()
            app_crawler = DouyinMobileCrawler(proxy=self.proxy, device=mobile_device)
            response = await app_crawler.parse(raw_url)
            return DouyinApiResult.parse(response)
        except ParseError as e:
            mobile_error = e

        if web_error:
            raise ParseError(f"{web_error}；移动端解析也失败: {mobile_error}") from mobile_error
        raise mobile_error

    @staticmethod
    def _build_video_result(result: "DouyinApiResult") -> VideoParseResult:
        """构建视频解析结果"""
        if result.video is None:
            raise ParseError("抖音解析失败: 未获取到视频")
        return DouyinVideoParseResult(
            title=result.desc,
            author_name=result.author_name,
            # @标识 用数字 uid (sec_uid 太长); 主页地址仍用 sec_uid
            author_handle=result.author_uid,
            author_url=profile_url(Platform.DOUYIN, user_id=result.author_sec_uid),
            video=result.video,
            published_at=result.published_at,
            view_count=result.view_count,
            like_count=result.like_count,
        )

    @staticmethod
    def _build_image_result(result: "DouyinApiResult") -> ImageParseResult:
        """构建图片解析结果"""
        return DouyinImageParseResult(
            title=result.desc,
            author_name=result.author_name,
            author_handle=result.author_uid,
            author_url=profile_url(Platform.DOUYIN, user_id=result.author_sec_uid),
            photo=result.image_list,
            published_at=result.published_at,
            view_count=result.view_count,
            like_count=result.like_count,
        )

    @staticmethod
    def _build_article_result(result: "DouyinApiResult") -> "RichTextParseResult":
        """构建长图文解析结果（正文是平台给的 markdown）。

        标题用 ``article_title``（``desc`` 只是短摘要）。

        **图片要抽成 media**（2026-10-16，用户报「图片丢了」）：平台把图内联在
        markdown 里（``![图片描述](https://...douyinpic.com/...)``），但富文本
        （``InputRichMessage``）**只认 ``tg://photo?id=`` 引用的已上传媒体** ——
        外部 URL 会被渲染层剥掉（``strip_media_markdown`` 防
        ``EXTERNAL_MEDIA_NOT_SUPPORTED``）。不抽出来，图就整批消失。

        抽出后正文**原位置留 ``{{media:N}}`` 标记**（``_extract_markdown_images``）——
        渲染层把它换成第 N 个媒体的占位符，图就出现在文章里，而不是堆到末尾
        （用户报「图片怎么是尾部一堆？不是在文章里？」后改成原位）。

        图片 URL 优先用 ``fe_data.image_list`` 的 ``origin_image_url``（原图）；
        正文里的 ``~tplv-dy-long-article`` URL 是低清 HEIC 缩略。两个列表按**顺序**
        对应（实测 5/5 一致）—— 数量对不上时退回正文 URL，不猜。
        """
        markdown, images = _extract_markdown_images(result.article_markdown)
        images = _resolve_origin_images(images, result.article_images)
        return DouyinArticleParseResult(
            title=result.article_title or result.desc,
            markdown_content=markdown,
            media=[ImageRef(url=url, ext=image_ext_from_url(url) or "jpg") for url in images],
            author_name=result.author_name,
            author_handle=result.author_uid,
            author_url=profile_url(Platform.DOUYIN, user_id=result.author_sec_uid),
            published_at=result.published_at,
            view_count=result.view_count,
            like_count=result.like_count,
        )


class DouyinParseResult(ParseResult):
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
    ) -> "DownloadResult":
        headers = {
            "Referer": "https://www.douyin.com/",
        }
        return await super()._do_download(
            output_dir=output_dir,
            callback=callback,
            callback_args=callback_args,
            callback_kwargs=callback_kwargs,
            proxy=proxy,
            headers=headers,
            connections=connections,
        )


class DouyinVideoParseResult(DouyinParseResult, VideoParseResult): ...


class DouyinImageParseResult(DouyinParseResult, ImageParseResult): ...


class DouyinArticleParseResult(DouyinParseResult, RichTextParseResult):
    """抖音长图文（``/article/<id>``）。

    正文是平台给的 **markdown**（``article_info.article_content.markdown``）；
    图片从正文抽出走 ``media`` 通道（见 ``_build_article_result``），因此
    ``requires_media_download`` 必须为 True —— 否则流水线跳过下载，图全丢。
    沿用基类的下载链路（``_do_download`` 注 Referer）。
    """

    requires_media_download = True


#: markdown 内联图片：``![alt](url)``（douyin 的 URL 里没有空格，但保留
#: ``[^)\s]+`` 以防将来出现带 title 的写法）
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)[^)]*\)")

#: 原位媒体标记 ``{{media:N}}`` —— 见 ``parsehub.utils.helpers.media_placeholder``
#: （2026-10-19 从本模块提升到公共层：bgm.tv 也要用同一套协议）。

#: douyinpic 资源标识: ``/tos-cn-i-<bucket>/<hash>``。URL 的 host、签名与
#: ``~tplv-<变体>`` 后缀可以不同，这一段是**同一张图**的稳定标识。
_DOUYINPIC_RES_RE = re.compile(r"/tos-cn-i-[^/]+/[0-9a-zA-Z]+")


def _resolve_origin_images(images: list[str], origin_candidates: list[str]) -> list[str]:
    """把正文图片 URL 换成 ``fe_data`` 提供的原图 URL（纯函数）。

    正文 markdown 里的图片是 ``~tplv-dy-long-article`` 低清缩略；``fe_data.image_list``
    给出同一张图的 ``origin_image_url``（原图）。两个集合的 URL **不同源**（host、
    签名、变体全是新的），不能按字符串相等或位置对齐 —— 按**资源标识**
    （``/tos-cn-i-<bucket>/<hash>``）匹配；匹配不到的保留原 URL（宁可用缩略图，
    不能丢图）。
    """
    by_res: dict[str, str] = {}
    for url in origin_candidates:
        if match := _DOUYINPIC_RES_RE.search(url):
            by_res.setdefault(match.group(0), url)

    result: list[str] = []
    for url in images:
        match = _DOUYINPIC_RES_RE.search(url)
        result.append(by_res.get(match.group(0), url) if match else url)
    return result


def _extract_markdown_images(markdown: str) -> tuple[str, list[str]]:
    """把 markdown 里的图片抽出来，返回 ``(原位置换成标记的正文, 图片 URL 列表)``（纯函数）。

    为什么必须抽（2026-10-16，用户报「图片丢了」）: 富文本
    （``InputRichMessage``）只认 ``tg://photo?id=`` 引用的**已上传**媒体，外部 URL
    会被渲染层 ``strip_media_markdown`` 剥掉（防 ``EXTERNAL_MEDIA_NOT_SUPPORTED``）。
    图片本体必须走 ``media`` 通道才有得发。

    抽走的位置留 ``{{media:N}}``（N = 本函数返回列表的下标）—— 渲染层
    ``replace_media_placeholders`` 把它换成第 N 个媒体的占位符，图片就渲染在
    **文章原位**（与 ``{{poll:N}}`` 同一套机制）。不留标记的话图全堆到末尾
    （用户报「图片怎么是尾部一堆？不是在文章里？」）。

    只把**图片语法**换成标记，周围文字与空行原样保留。
    """
    urls: list[str] = []
    index_by_url: dict[str, int] = {}

    def _replacement(match: re.Match[str]) -> str:
        url = match.group(1).strip()
        if not url:
            return match.group(0)
        index = index_by_url.get(url)
        if index is None:
            index = len(urls)
            urls.append(url)
            index_by_url[url] = index
        return media_placeholder(index)

    cleaned = _MD_IMAGE_RE.sub(_replacement, markdown or "")
    return cleaned, urls


def remove_video_watermark(url: str) -> str:
    """移除抖音视频水印标识 (playwm -> play)"""
    return url.replace("playwm", "play")


def _p_play_addr(video_data: dict) -> tuple[str | None, int, int]:
    play_addr = video_data["play_addr"]
    url_list = play_addr["url_list"]
    if not url_list:
        return None, 0, 0
    return url_list[0], play_addr.get("width", 0), play_addr.get("height", 0)


def _p_bit_rates(video_data: dict, filter_bytevc1: bool = False) -> tuple[str, int, int]:
    bit_rates: list = video_data.get("bit_rate", [])
    if filter_bytevc1:
        bit_rates = list(filter(lambda i: i.get("is_bytevc1", 0) == 0, bit_rates))
    bit_rates.sort(
        key=lambda x: (
            x.get("play_addr", {}).get("width", 0) * x.get("play_addr", {}).get("height", 0),
            x.get("play_addr", {}).get("data_size", 0),
            x.get("bit_rate", 0),
        ),
        reverse=True,
    )
    best_quality = bit_rates[0]

    play_addr = best_quality.get("play_addr", {})
    url_list = play_addr.get("url_list", [])
    return url_list[0], play_addr.get("width", 0), play_addr.get("height", 0)


def parse_video_info(video_data: dict) -> dict:
    is_app = video_data.get("is_bytevc1", None) is not None  # 只有 app 接口带 is_bytevc1 参数
    if is_app:
        u, w, h = _p_play_addr(video_data)
        if not u:
            u, w, h = _p_bit_rates(video_data, True)
    else:
        u, w, h = _p_bit_rates(video_data)

    video_url = remove_video_watermark(u)

    cover = video_data.get("cover", {})
    thumb_url_list = cover.get("url_list", [])
    thumb_url = thumb_url_list[-1] if thumb_url_list else None

    return {
        "video_url": video_url,
        "thumb_url": thumb_url,
        "duration": video_data.get("duration", 0),
        "width": w,
        "height": h,
    }


class DouyinMediaType(Enum):
    """抖音媒体类型"""

    VIDEO = "video"
    IMAGE = "image"  # 实况图片 + 图片
    ARTICLE = "article"  # 长图文（aweme_type=163），正文是平台给的 markdown


@dataclass
class DouyinApiResult:
    """抖音 API 解析结果"""

    type: DouyinMediaType
    video: VideoRef | None = None
    desc: str = ""
    image_list: list[ImageRef | LivePhotoRef] = field(default_factory=list)
    author_name: str = ""
    #: 作者主页标识: 抖音主页要用 ``sec_uid``, 数字 ``uid`` 打不开 (实测)
    author_sec_uid: str = ""
    #: 数字 uid: **仅用于展示** (作者行的 ``@标识``)。
    #: 抖音没有 @用户名, 而 ``sec_uid`` 太长 (60+ 字符) —— 用户选了数字 id 当标识。
    #: 它点不开主页, 主页地址仍由 ``author_sec_uid`` 生成。
    author_uid: str = ""
    published_at: datetime | None = None
    view_count: int | None = None
    like_count: int | None = None
    #: 长文章正文（markdown）。只在 ``type == ARTICLE`` 时有值；图片已内联在
    #: markdown 里（``![alt](https://...douyinpic.com/...)``），由 parser 层抽成
    #: ``media``（平台正文里的 URL 是低清 HEIC 缩略占位，见 ``article_images``）。
    article_markdown: str = ""
    #: 文章标题（``article_info.article_title``）。文章页的 ``desc`` 是短摘要，
    #: 真标题在这里。
    article_title: str = ""
    #: 长图文的图片（``fe_data.image_list`` 里的 ``origin_image_url``，按正文出现顺序）。
    #: 正文 markdown 里的 ``![](…)`` 用的是 ``~tplv-dy-long-article`` 低清缩略（HEIC），
    #: 这个列表给的是**同一批图的原图 URL**（仍是 HEIC，由下载层 ``MediaProcessingUnit``
    #: 转 JPEG —— ``bot.py`` 已注册 ``pillow_heif``）。长度可能与正文图片数不一致
    #: （正文引用不全），parser 层按 URL 对得上才用，对不上就退回正文 URL。
    article_images: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, json_dict: dict) -> Self:
        data = json_dict.get("aweme_detail")
        if not data:
            raise ParseError("抖音解析失败: 未获取到作品详情")

        # 长图文（`/article/<id>`，aweme_type=163）: 正文在
        # `article_info.article_content`（一个**双重编码**的 JSON 字符串，里层含
        # markdown）。⚠️ 不能落进 images/video 分支 —— 那样会得到空结果或误判。
        if article := data.get("article_info"):
            result = cls._parse_article(article, str(data.get("desc") or ""))
        else:
            desc = data.get("desc", "")
            if images := data.get("images"):
                result = cls._parse_images(images, desc)
            elif image_post_info := data.get("image_post_info"):
                result = cls._parse_image_post_info(image_post_info, desc)
            else:
                result = cls._parse_video(data, desc)
        author = data.get("author") or {}
        result.author_name = get_author_name(author)
        result.author_sec_uid = str(author.get("sec_uid") or "")
        result.author_uid = str(author.get("uid") or "")
        # create_time 是 unix 秒; 统计在 statistics 里
        result.published_at = to_datetime(data.get("create_time"))
        stats = data.get("statistics") or {}
        # 点赞: statistics.digg_count (以前完全没取, 用户报"点赞没有")
        result.like_count = to_int(stats.get("digg_count"))
        # 浏览: 移动端接口固定返回 play_count=0 (实测), 而"0 查看"是误导 ——
        # 按项目原则"拿不到就不显示那一段", 所以 0 与缺失一并当作没有
        views = to_int(stats.get("play_count"))
        result.view_count = views or None
        return result

    @classmethod
    def _parse_article(cls, article: dict, desc: str) -> Self:
        """解析长图文（``aweme_type=163``）。

        ``article_content`` 是**双重编码**：值是 JSON 字符串，解出来是
        ``{"long_article_abstract": …, "markdown": …}`` —— 正文（含内联图片）在
        ``markdown`` 里。

        解不出时**不抛**，退回 ``long_article_abstract`` 摘要；两者都没有才报错 ——
        平台格式变动不该让整条解析失败。
        """
        title = str(article.get("article_title") or "").strip()
        markdown = ""
        abstract = ""
        raw = article.get("article_content")
        if isinstance(raw, str) and raw:
            try:
                content = json.loads(raw)
            except ValueError:
                content = {}
            if isinstance(content, dict):
                markdown = str(content.get("markdown") or "")
                abstract = str(content.get("long_article_abstract") or "")
        body = markdown or abstract
        if not body:
            raise ParseError("抖音文章解析失败: article_content 里没有正文")

        # fe_data.image_list: 每张图带多个 URL 变体，正文 markdown 里用的是
        # ``~tplv-dy-long-article``（低清 HEIC 缩略）；``origin_image_url`` 是原图。
        # 顺序与正文出现顺序一致（实测 5/5 对得上）。
        images: list[str] = []
        fe_raw = article.get("fe_data")
        if isinstance(fe_raw, str) and fe_raw:
            try:
                fe = json.loads(fe_raw)
            except ValueError:
                fe = {}
            if isinstance(fe, dict):
                for item in fe.get("image_list") or []:
                    if not isinstance(item, dict):
                        continue
                    url = str(item.get("origin_image_url") or item.get("high_image_url") or "").strip()
                    if url:
                        images.append(url)

        return cls(
            type=DouyinMediaType.ARTICLE,
            desc=desc,
            article_markdown=body,
            article_title=title,
            article_images=images,
        )

    @classmethod
    def _parse_images(cls, images: list[dict], desc: str) -> Self:
        """解析旧版图片格式 (images 字段)

        支持普通图片和实况照片 (LivePhoto)
        """
        has_live_photos = any(img.get("video") for img in images)

        if has_live_photos:
            image_list: list[ImageRef | LivePhotoRef] = []
            for image in images:
                if video := image.get("video"):
                    video_info = parse_video_info(video)
                    image_list.append(
                        LivePhotoRef(
                            url=video_info["thumb_url"],
                            video_url=video_info["video_url"],
                            width=int(video_info["width"]),
                            height=int(video_info["height"]),
                            duration=int(video_info["duration"]) or 3,
                        )
                    )
                else:
                    url_list = image.get("url_list", [])
                    if url_list:
                        image_list.append(
                            ImageRef(
                                url=url_list[-1],
                                height=image.get("height", 0),
                                width=image.get("width", 0),
                                ext=image_ext_from_url(url_list[-1]),
                            )
                        )
        else:
            image_list = [
                ImageRef(
                    url=img["url_list"][-1],
                    height=img.get("height", 0),
                    width=img.get("width", 0),
                    ext=image_ext_from_url(img["url_list"][-1]),
                )
                for img in images
                if img.get("url_list")
            ]

        return cls(
            type=DouyinMediaType.IMAGE,
            desc=desc,
            image_list=image_list,
        )

    @classmethod
    def _parse_image_post_info(cls, image_post_info: dict, desc: str) -> Self:
        """解析新版图片格式 (image_post_info 字段)"""
        images = image_post_info.get("images", [])
        image_list: list[ImageRef | LivePhotoRef] = []

        for image in images:
            display_image = image.get("display_image", {})
            url_list = display_image.get("url_list", [])
            if url_list:
                image_list.append(
                    ImageRef(
                        url=url_list[-1],
                        height=display_image.get("height", 0),
                        width=display_image.get("width", 0),
                        ext=image_ext_from_url(url_list[-1]),
                    )
                )

        return cls(
            type=DouyinMediaType.IMAGE,
            image_list=image_list,
            desc=desc,
        )

    @classmethod
    def _parse_video(cls, data: dict, desc: str) -> Self:
        """解析视频"""
        video_data = data.get("video")
        if not video_data:
            raise ParseError("抖音解析失败: 未获取到视频数据")

        video_info = parse_video_info(video_data)

        return cls(
            type=DouyinMediaType.VIDEO,
            video=VideoRef(
                url=video_info["video_url"],
                thumb_url=video_info["thumb_url"],
                width=video_info["width"],
                height=video_info["height"],
                duration=video_info["duration"],
            ),
            desc=desc,
        )


__all__ = ["DouyinParser"]
