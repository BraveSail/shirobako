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


class XiaoHeiHeRichTextParseResult(RichTextParseResult):
    """小黑盒长文（ARTICLE）。

    ``requires_media_download`` **必须为 True**：正文里的 ``![](https://imgheybox…)``
    会被渲染层 ``rewrite_external_images`` **无条件**改写成上传媒体引用，对不上
    ``media_urls`` 的直接剥掉（见 ``plugins/helpers.py`` 那段的注释：外链图交给
    Telegram 抓会因防盗链/404 整条 400，所以宁可剥）。

    于是「正文里有外链图」并不等于「Telegram 会自己抓」——
    ``RichTextParseResult`` 基类注释里那条旧假设在这里不成立。不下载 ⇒ ``media_urls``
    为空 ⇒ 5 张图全被剥掉，用户看到的就是「怎么没图？」。

    ⚠️ 属性必须加在 **result 类**上：``should_skip_richtext_download`` 读的是
    ``parse_result.requires_media_download``，加在 parser 类上不生效（PTT 踩过）。
    """

    requires_media_download = True


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
                return XiaoHeiHeRichTextParseResult(
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
