from ...parsers.base import BaseParser
from ...provider_api.xiaoheihe import XiaoHeiHeAPI, XiaoHeiHeMediaType, XiaoHeiHePost, XiaoHeiHePostType
from ...types import (
    AniRef,
    AnyParseResult,
    ImageParseResult,
    ImageRef,
    MultimediaParseResult,
    ParseError,
    Platform,
    RichTextParseResult,
    VideoParseResult,
    VideoRef,
)
from ...utils.helpers import image_ext_from_url, profile_url


class XiaoHeiHeParser(BaseParser):
    __platform__ = Platform.XIAOHEIHE
    __supported_type__ = ["视频", "图文"]
    __match__ = r"^(http(s)?://)?.+xiaoheihe.cn/(v3|app)/bbs/(app|link).+"
    __redirect_keywords__ = ["api.xiaoheihe"]

    async def _do_parse(self, raw_url: str) -> AnyParseResult:
        xhh = await XiaoHeiHeAPI(proxy=self.proxy).parse(raw_url)
        # 页脚元数据: 平台不提供的项留 None（小黑盒确实没有浏览量）
        # ``author_handle`` 放数字 uid —— 平台没有 @用户名（同抖音/bgm 的约定：
        # 没有用户名时用 uid 顶上，不让 @ 那段整个消失）
        meta = {
            "author_handle": xhh.author_id,
            "author_url": profile_url(Platform.XIAOHEIHE, user_id=xhh.author_id),
            "published_at": xhh.published_at,
            "like_count": xhh.like_count,
            "reply_count": xhh.reply_count,
        }
        match xhh.type:
            case XiaoHeiHePostType.VIDEO:
                return VideoParseResult(
                    video=self.__parse_video(xhh),
                    title=xhh.title,
                    author_name=xhh.author_name,
                    content=xhh.content,
                    **meta,
                )
            case XiaoHeiHePostType.IMAGE:
                media = self.__parse_images(xhh)
                if not media or all(isinstance(m, ImageRef) for m in media):
                    return ImageParseResult(
                        photo=media,
                        title=xhh.title,
                        content=xhh.content,
                        author_name=xhh.author_name,
                        **meta,
                    )
                return MultimediaParseResult(
                    media=media,
                    title=xhh.title,
                    content=xhh.content,
                    author_name=xhh.author_name,
                    **meta,
                )
            case XiaoHeiHePostType.ARTICLE:
                return RichTextParseResult(
                    title=xhh.title,
                    author_name=xhh.author_name,
                    media=self.__parse_images(xhh),
                    markdown_content=xhh.content,
                    **meta,
                )
        raise ParseError("不支持的类型")

    @staticmethod
    def __parse_video(xhh: XiaoHeiHePost) -> VideoRef:
        if not xhh.media:
            raise ParseError("未获取到视频")
        media = xhh.media[0]
        return VideoRef(url=media.url, thumb_url=media.thumb_url)

    @staticmethod
    def __parse_images(xhh: XiaoHeiHePost) -> list[ImageRef | AniRef]:
        images: list[ImageRef | AniRef] = []
        for media in xhh.media or []:
            if media.type == XiaoHeiHeMediaType.IMAGE:
                images.append(
                    ImageRef(
                        url=media.url,
                        width=media.width or 0,
                        height=media.height or 0,
                        ext=image_ext_from_url(media.url),
                    )
                )
            else:
                images.append(
                    AniRef(
                        url=media.url,
                        width=media.width or 0,
                        height=media.height or 0,
                        ext=image_ext_from_url(media.url, default="gif"),
                    )
                )
        return images
