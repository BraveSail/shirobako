import re
from pathlib import Path
from typing import Union

from parsehub.types.document import plain

from ...provider_api.coolapk import Coolapk
from ...types import (
    AniRef,
    DownloadResult,
    ImageParseResult,
    ImageRef,
    MultimediaParseResult,
    ParseError,
    ParseResult,
    Platform,
    ProgressCallback,
    RichTextParseResult,
)
from ...utils.helpers import image_ext_from_url
from ..base.base import BaseParser


class CoolapkParser(BaseParser):
    __platform__ = Platform.COOLAPK
    __supported_type__ = ["图文"]
    __match__ = r"^(http(s)?://)www.coolapk.com/(feed|picture)/.*"
    __after_clean_parameters__ = ["shareKey", "s"]

    async def _do_parse(
        self, raw_url: str
    ) -> Union["CoolapkImageParseResult", "CoolapkRichTextParseResult", "CoolapkMultimediaParseResult"]:
        try:
            coolapk = await Coolapk.parse(raw_url, proxy=self.proxy)
        except Exception as e:
            raise ParseError(str(e)) from e
        # 动图走 AniRef、静态图走 ImageRef；ext 一律按 URL 上的真实后缀取（平台图片是
        # PNG/WebP 时不取就会被命名成 .jpg，下游按后缀处理媒体会出错）
        media = []
        for i in coolapk.imgs or []:
            ext = image_ext_from_url(i)
            media.append(AniRef(url=i, ext="gif") if ext == "gif" else ImageRef(url=i, ext=ext))
        if coolapk.markdown_content:
            return CoolapkRichTextParseResult(
                title=coolapk.title,
                author_name=coolapk.author_name,
                media=media,
                markdown_content=coolapk.markdown_content,
                document=coolapk.document,
            )
        content = self.hashtag_handler(coolapk.text_content or "")
        if any(isinstance(m, AniRef) for m in media):
            return CoolapkMultimediaParseResult(
                title=coolapk.title,
                author_name=coolapk.author_name,
                media=media,
                content=content,
                document=plain(content),
            )
        return CoolapkImageParseResult(
            title=coolapk.title,
            author_name=coolapk.author_name,
            photo=media,
            content=content,
            document=plain(content),
        )

    @staticmethod
    def hashtag_handler(desc: str) -> str:
        hashtags = re.findall(r" ?#[^#]+# ?", desc)
        for hashtag in hashtags:
            desc = desc.replace(hashtag, f" {hashtag.strip().removesuffix('#')} ")
        return desc


class CoolapkParseResult(ParseResult):
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
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,"
                "*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
            )
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


class CoolapkImageParseResult(ImageParseResult, CoolapkParseResult): ...


class CoolapkMultimediaParseResult(MultimediaParseResult, CoolapkParseResult): ...


class CoolapkRichTextParseResult(RichTextParseResult, CoolapkParseResult): ...


__all__ = ["CoolapkParser", "CoolapkImageParseResult", "CoolapkMultimediaParseResult", "CoolapkRichTextParseResult"]
