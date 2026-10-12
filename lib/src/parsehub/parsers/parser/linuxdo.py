"""linux.do（Discourse 论坛）解析器。"""

from typing import Union

from ...provider_api.linuxdo import LinuxDoError, LinuxDoTopic
from ...types import (
    ImageParseResult,
    ImageRef,
    ParseError,
    Platform,
    Poll,
    PollOption,
    RichTextParseResult,
)
from ...utils.helpers import image_ext_from_url, profile_url
from ..base.base import BaseParser


class LinuxDoRichTextParseResult(RichTextParseResult):
    """linux.do 的图文主题（正文是 markdown）。"""

    # 图片从正文里抽出来放在 media (正文不含外链图), 必须下载后当附件发送
    requires_media_download = True


class LinuxDoImageParseResult(ImageParseResult):
    """linux.do 的纯图主题。"""


class LinuxDoParser(BaseParser):
    __platform__ = Platform.LINUXDO
    __supported_type__ = ["图文"]
    __match__ = r"^(http(s)?://)?linux\.do/t/.*"

    async def _do_parse(self, raw_url: str) -> Union["LinuxDoRichTextParseResult", "LinuxDoImageParseResult"]:
        try:
            topic = await LinuxDoTopic.parse(
                raw_url,
                proxy=self.proxy,
                cookie=self.cookie.get_value() if self.cookie else None,
            )
        except LinuxDoError as e:
            raise ParseError(str(e)) from e
        except Exception as e:
            raise ParseError(f"无法获取话题内容: {e}") from e

        media = [
            ImageRef(url=i.url, thumb_url=i.url, width=i.width, height=i.height, ext=image_ext_from_url(i.url))
            for i in topic.images
        ]
        # 投票: provider 的结构化数据 -> 结果层的 ``Poll`` (顺序 = 正文里
        # ``{{poll:N}}`` 占位符的出现顺序, 原样搬移)
        polls = [
            Poll(
                options=[PollOption(text=o.text, count=o.count) for o in p.options],
                total_votes=p.total_votes,
                voters=p.voters,
                title=p.title,
            )
            for p in topic.polls
        ]
        # 媒体**不进 common**: 两个结果类的参数名不同 (RichText 是 ``media``,
        # Image 是 ``photo``) —— 放进公共字典会让纯图话题那条分支直接 TypeError
        # (既存 bug, pylint E1123 抓到)。
        common = {
            "title": topic.title,
            # 末尾这几张属于上下文引用块 (主楼/被回复楼层) -> 放进引用块内部
            # （块在正文前 ⇒ reply 那一档）
            "reply_media_count": topic.reply_media_count,
            # 引用块的角色 (按出现顺序) —— 渲染层据此归位媒体与位置
            "quote_roles": topic.quote_roles,
            "author_name": topic.author_name,
            "author_handle": topic.author_handle,
            "author_url": profile_url(Platform.LINUXDO, topic.author_handle),
            "is_sensitive": topic.is_sensitive,
            "published_at": topic.published_at,
            "view_count": topic.view_count,
            "like_count": topic.like_count,
            "tags": topic.tags,
            # 投票的结构化数据 (两个结果类都要) —— 渲染层按占位符索引取用
            "polls": polls,
            # 楼层号: 引用块里的其它层早已标了 (` · #N`), 本层不标就不对称
            # (用户报「主楼标楼层号了但是回复没标」)。主楼解析出来就是 ``#1``。
            "position_label": f"#{topic.post_number}" if topic.post_number else "",
        }

        if topic.markdown_content:
            return LinuxDoRichTextParseResult(
                markdown_content=topic.markdown_content,
                media=media,
                **common,  # type: ignore[arg-type]
            )
        return LinuxDoImageParseResult(content=topic.text_content, photo=media, **common)  # type: ignore[arg-type]


__all__ = ["LinuxDoImageParseResult", "LinuxDoParser", "LinuxDoRichTextParseResult"]
