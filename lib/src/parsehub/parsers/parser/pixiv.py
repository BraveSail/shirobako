from pathlib import Path

from ...provider_api.pixiv import REFERER, UA, Pixiv
from ...types import (
    DownloadResult,
    ImageRef,
    MultimediaParseResult,
    Platform,
    ProgressCallback,
)
from ...types.document import plain
from ...utils.helpers import profile_url
from ..base.base import BaseParser


class PixivParseResult(MultimediaParseResult):
    """i.pximg.net 按 UA/Referer 防盗链, 不带 Referer 的下载一律 403"""

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
        headers = {"User-Agent": UA, "Referer": REFERER}
        return await super()._do_download(
            output_dir=output_dir,
            callback=callback,
            callback_args=callback_args,
            callback_kwargs=callback_kwargs,
            proxy=proxy,
            headers=headers,
            connections=connections,
        )


class PixivParser(BaseParser):
    __platform__ = Platform.PIXIV
    __supported_type__ = ["图文"]
    __match__ = (
        r"^(http(s)?://)?.+pixiv\.net/(?:[a-z]{2}(?:-[a-z]{2})?/)?artworks/\d+"
        r"|^(http(s)?://)?.+pixiv\.net/member_illust\.php\?[^#]*illust_id=\d+"
    )
    __reserved_parameters__ = ["illust_id"]

    async def _do_parse(self, raw_url: str) -> MultimediaParseResult:
        illust = await Pixiv(self.proxy, cookie=self.cookie.get_value()).parse(raw_url)
        return PixivParseResult(
            title=illust.title,
            content=illust.description,
            document=plain(illust.description),
            author_name=illust.author_name,
            author_handle=illust.user_account,
            author_url=profile_url(Platform.PIXIV, user_id=illust.author_id),
            tags=illust.tags,
            published_at=illust.create_date,
            view_count=illust.view_count,
            like_count=illust.like_count,
            # pixiv 的 xRestrict > 0 即 R-18, 用作打码标记
            is_sensitive=illust.is_r18,
            media=[
                ImageRef(
                    url=i.url,
                    thumb_url=i.thumb_url,
                    ext=i.ext,
                    width=i.width,
                    height=i.height,
                )
                for i in illust.images
            ],
        )


__all__ = ["PixivParser", "PixivParseResult"]
