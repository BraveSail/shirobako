from ...provider_api.zuiyou import MediaType, ZuiYou
from ...types import ImageRef, MultimediaParseResult, Platform, VideoRef
from ...types.document import plain
from ...utils.helpers import image_ext_from_url
from ..base.base import BaseParser


class ZuiYouParser(BaseParser):
    __platform__ = Platform.ZUIYOU
    __supported_type__ = ["视频", "图文"]
    __match__ = r"^(http(s)?://)share.xiaochuankeji.cn/hybrid/share/post\?pid=\d+"
    __reserved_parameters__ = ["pid"]

    async def _do_parse(self, raw_url: str) -> MultimediaParseResult:
        zy = await ZuiYou(self.proxy).parse(raw_url)
        return MultimediaParseResult(
            content=zy.content,
            document=plain(zy.content),
            author_name=zy.author_name,
            media=[
                VideoRef(url=i.url, thumb_url=i.thumb_url)
                if i.type == MediaType.VIDEO
                else ImageRef(url=i.url, thumb_url=i.thumb_url, ext=image_ext_from_url(i.url))
                for i in zy.media
            ],
        )


__all__ = ["ZuiYouParser"]
