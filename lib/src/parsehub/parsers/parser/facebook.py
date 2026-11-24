from __future__ import annotations

from pathlib import Path

from ...provider_api.facebook import FacebookAPI
from ...types import (
    DownloadResult,
    ParseError,
    Platform,
    ProgressCallback,
    VideoParseResult,
    VideoRef,
)
from ...types.document import plain
from ..base.base import BaseParser

#: Facebook CDN 对**浏览器 UA** 的大文件请求会限流 (yt-dlp 的 workaround)，
#: 换这个 UA 就能正常下。只影响下载请求, 抓页面仍用普通浏览器 UA。
_DOWNLOAD_USER_AGENT = "facebookexternalhit/1.1"


class FacebookParse(BaseParser):
    __platform__ = Platform.FACEBOOK
    __supported_type__ = ["视频"]
    # watch?v=<id> 里的 v 是定位视频所必需的, 不保留就会退化成 /watch 导致解析失败
    __match__ = r"^(http(s)?://)?.+facebook.com/(watch/?\?v|share/[v,r]|.+/videos/|reel/).*"
    __reserved_parameters__ = ["v"]
    # /share/v/<token> 是短链, 要跟随重定向才能拿到带视频 id 的规范地址
    __redirect_keywords__ = ["/share/"]

    async def _do_parse(self, raw_url: str) -> FacebookVideoParseResult:
        try:
            video = await FacebookAPI(proxy=self.proxy).get_video(raw_url)
        except Exception as e:
            raise ParseError(f"Facebook 解析失败: {e}") from e

        return FacebookVideoParseResult(
            title=video.title,
            content=video.content,
            document=plain(video.content),
            author_name=video.author_name,
            author_url=video.author_url,
            published_at=video.published_at,
            view_count=video.view_count,
            video=VideoRef(
                url=video.url,
                thumb_url=video.thumb_url,
                duration=int(video.duration or 0),
                width=video.width,
                height=video.height,
            ),
        )


class FacebookVideoParseResult(VideoParseResult):
    """下载时带上 Facebook CDN 认的 UA, 其余走项目自研下载器。"""

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
        headers = {"User-Agent": _DOWNLOAD_USER_AGENT}
        return await super()._do_download(
            output_dir=output_dir,
            callback=callback,
            callback_args=callback_args,
            callback_kwargs=callback_kwargs,
            proxy=proxy,
            headers=headers,
            connections=connections,
        )


__all__ = ["FacebookParse", "FacebookVideoParseResult"]
