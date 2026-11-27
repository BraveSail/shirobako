from ...provider_api.weixin import WX, WXItemShowType
from ...types import AnyParseResult, ImageParseResult, ImageRef, ParseError, Platform, RichTextParseResult
from ..base.base import BaseParser

_IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "gif", "webp", "bmp", "avif", "heic", "heif"})


class WXArticleParseResult(RichTextParseResult):
    """公众号正文图按 Document 中的位置上传，不由 Telegram 访问微信 CDN。"""

    requires_media_download = True
    follow_media_redirects = False


class WXParser(BaseParser):
    __platform__ = Platform.WEIXIN
    __supported_type__ = ["图文"]
    __match__ = r"^https?://mp\.weixin\.qq\.com/s/"

    @staticmethod
    def _refs(urls: list[str]) -> list[ImageRef]:
        """只接受已知图片后缀，未声明或不认识时保留 jpg 默认值。"""
        refs = []
        for url in urls:
            fmt = url.partition("wx_fmt=")[2].partition("&")[0].lower()
            refs.append(ImageRef(url=url, ext=fmt if fmt in _IMAGE_EXTENSIONS else "jpg"))
        return refs

    async def _do_parse(self, raw_url: str) -> AnyParseResult:
        wx = await WX.parse(raw_url, self.proxy)

        match wx.item_show_type:
            case WXItemShowType.ARTICLE:
                return WXArticleParseResult(
                    title=wx.title,
                    author_name=wx.author_name,
                    media=self._refs(wx.imgs),
                    markdown_content=wx.markdown_content,
                    document=wx.document,
                )
            case WXItemShowType.IMAGE | WXItemShowType.TEXT:
                return ImageParseResult(
                    title=wx.title,
                    author_name=wx.author_name,
                    photo=self._refs(wx.photos),
                    content=wx.text_content,
                    document=wx.document,
                )
            case _:
                raise ParseError(f"不支持的内容类型: {wx.item_show_type!r}")


__all__ = ["WXParser"]
