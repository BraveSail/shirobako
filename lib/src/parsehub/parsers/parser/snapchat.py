from ...provider_api.snapchat import SnapchatError, fetch_video
from ...types import ParseError, Platform, VideoParseResult, VideoRef
from ...types.document import plain
from ..base.base import BaseParser


class Snapchatarse(BaseParser):
    __platform__ = Platform.SNAPCHAT
    __supported_type__ = ["视频"]
    # 覆盖两种真实形态: ``/@user/spotlight/<id>``、``/@user/<id>``，
    # 以及 yt-dlp 那条 ``/spotlight/<id>``（无 @user）。
    __match__ = (
        r"^(http(s)?://)?(?:www\.)?snapchat\.com/(?:@[a-zA-Z0-9._-]+(?:/spotlight)?/|spotlight/)[a-zA-Z0-9_-]+"
    )

    async def _do_parse(self, raw_url: str) -> VideoParseResult:
        """自研解析：从 spotlight 页面的 ``__NEXT_DATA__`` 取明文直链 + 元数据。

        返回的 :class:`VideoRef.url` 是**明文直链**，基类 ``ParseResult.download()``
        → ``VideoParseResult._do_download`` 会走项目自带下载器（不再调用 yt-dlp）。
        """
        try:
            video = await fetch_video(raw_url, proxy=self.proxy, cookie=self.cookie.get_value())
        except SnapchatError as e:
            raise ParseError(f"Snapchat 解析失败: {e}") from e
        return VideoParseResult(
            title=video.title,
            content=video.description,
            document=plain(video.description),
            author_name=video.author_name,
            author_handle=video.author_handle,
            author_url=video.author_url,
            published_at=video.published_at,
            view_count=video.view_count,
            tags=list(video.hashtags),
            video=VideoRef(
                url=video.url,
                thumb_url=video.thumbnail_url,
                width=video.width,
                height=video.height,
                duration=video.duration,
            ),
        )


__all__ = ["Snapchatarse"]
