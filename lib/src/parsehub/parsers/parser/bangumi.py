"""bgm.tv（Bangumi 番组计划）解析器：日志 + 小组话题。

两个页面**不同构**，所以是两条解析路径（选择器、正文容器、楼层结构都不一样），
但共用同一套 BBCode → markdown 转换（见 ``provider_api/bangumi.py``）。

- **日志** ``/blog/<id>``：单篇，标题在 ``.header h1.title``
- **小组话题** ``/group/topic/<id>``：主楼 + 楼层，标题在 ``h1``（``<br/>`` 之后），
  楼层号形如 ``#2`` / ``#2-1``（楼中楼）

条目页（``/subject/<id>``）与小组首页（``/group/<slug>``）都不走这里。
"""

import html

from ...provider_api.bangumi import (
    BangumiBlog,
    BangumiError,
    BangumiIndex,
    BangumiTopic,
)
from ...types import ImageRef, ParseError, Platform, RichTextParseResult
from ...utils.helpers import image_ext_from_url, profile_url
from ..base.base import BaseParser


class BangumiParseResult(RichTextParseResult):
    """bgm.tv 图文（日志 / 小组话题）。"""

    #: 图片**已从正文里抽走**（见 provider），必须下载后当附件发送 ——
    #: 不置位的话流水线会跳过下载，图片就彻底丢了（与 linux.do 同一理由）。
    requires_media_download = True


class BangumiParser(BaseParser):
    __platform__ = Platform.BANGUMI
    __supported_type__ = ["图文"]
    # 锚定 ``blog/<数字>``、``group/topic/<数字>``、``subject/topic/<数字>``、
    # ``rakuen/topic/<归属>/<数字>``、``ep/<数字>``：bgm.tv 与 bangumi.tv 两个域名都有人用。
    # **不接** ``/subject/<id>``（条目页本身，官方 API 覆盖）与 ``/group/<slug>``（小组首页）。
    __match__ = (
        r"^(http(s)?://)?(bgm|bangumi)\.tv/"
        r"(blog/\d+"
        r"|index/\d+"  # 用户编纂的目录/榜单
        r"|(group|subject)/topic/\d+"
        r"|rakuen/topic/(group|subject)/\d+"  # 「超展开」入口，与上面同话题
        r"|ep/\d+)"  # 章节讨论（一整集的吐槽箱）
    )

    async def _do_parse(self, raw_url: str) -> "BangumiParseResult":
        try:
            if "/index/" in raw_url:
                return await self._parse_index(raw_url)
            if "/ep/" in raw_url:
                return await self._parse_topic(raw_url)
            if "/topic/" in raw_url:
                return await self._parse_topic(raw_url)
            return await self._parse_blog(raw_url)
        except BangumiError as e:
            raise ParseError(str(e)) from e
        except Exception as e:  # noqa: BLE001
            raise ParseError(f"无法获取内容: {e}") from e

    async def _parse_blog(self, raw_url: str) -> "BangumiParseResult":
        blog = await BangumiBlog.parse(
            raw_url,
            proxy=self.proxy,
            cookie=self.cookie.get_value() if self.cookie else None,
        )
        return BangumiParseResult(
            title=blog.title,
            markdown_content=blog.markdown_content,
            media=_refs(blog.images),
            author_name=blog.author_name,
            # bgm 没有 @用户名：标识可能是数字 uid（老用户）或 slug（新用户），
            # 主页与日志标签页都用它拼
            author_handle=blog.author_handle,
            # 模板是 ``/user/{id}`` —— 标识要按 **user_id** 传（按 handle 传会拼不出地址，
            # 静默得到空串 ⇒ 作者行不可点）
            author_url=profile_url(Platform.BANGUMI, user_id=blog.author_handle),
            published_at=blog.published_at,
            tags=blog.tags,
        )

    async def _parse_index(self, raw_url: str) -> "BangumiParseResult":
        """bgm 的**目录**（``/index/<id>``）—— 用户编纂的榜单 / 列表。

        形态：标题 + 简介 + 条目列表（名次 + 名称链接 + 角色 / 附加信息）。
        条目可能上百个（实测 409）—— 正文交给渲染层的长文折叠统一收，
        lib 不做截断（截断必须由渲染侧决定，见折叠阈值）。
        """
        index = await BangumiIndex.parse(
            raw_url,
            proxy=self.proxy,
            cookie=self.cookie.get_value() if self.cookie else None,
        )
        return BangumiParseResult(
            title=index.title,
            markdown_content=_render_index_markdown(index),
            author_name=index.author_name,
            author_handle=index.author_handle,
            author_url=profile_url(Platform.BANGUMI, user_id=index.author_handle),
        )

    async def _parse_topic(self, raw_url: str) -> "BangumiParseResult":
        """小组话题 / 条目讨论版 —— **只发一层**（分享的楼层，或主楼）。

        分享楼层时主楼会作为引用块带上（见 provider 的 ``_quote_of``），
        它的图片走 ``reply_media_count`` 那个通道进引用块内部（块在正文前）。
        """
        topic = await BangumiTopic.parse(
            raw_url,
            proxy=self.proxy,
            cookie=self.cookie.get_value() if self.cookie else None,
        )
        return BangumiParseResult(
            title=topic.title,
            markdown_content=topic.markdown_content,
            media=_refs(topic.images),
            author_name=topic.author_name,
            author_handle=topic.author_handle,
            author_url=profile_url(Platform.BANGUMI, user_id=topic.author_handle),
            published_at=topic.published_at,
            # 上下文块在正文**前** ⇒ 角色 ``reply``、媒体走 reply 段
            reply_media_count=topic.reply_media_count,
            quote_roles=topic.quote_roles,
            # 归属行 → 元信息区（标题与作者之间），不进正文
            origin_line=topic.origin_line,
            # 页脚统计: 状态数走 like_count 位（bgm 的表情状态就是它的"点赞"形态,
            # 文案由渲染层按平台给「状态」）；回复数是整帖的
            like_count=topic.state_count,
            reply_count=topic.reply_count,
            # 本层的楼层号（主楼是 ``#1``）—— 通用位置标记机制
            position_label=topic.floor_label,
        )


#: 目录正文的总字符预算 —— 富文本单条上限 32768，留出标题 / 页脚 / 媒体的余量。
#: 实测某目录（409 个人物 + 7.5K 字简介）原始长度 38262，**整条会被服务端拒掉**，
#: 所以 lib 侧按预算收（末尾 ``…`` 表示还有条目没列出来）。
_INDEX_CHAR_BUDGET = 30000
#: 简介最多占的字符数 —— 目录说明可能很长（有的把整个榜单前 100 名也写进去），
#: 不能让它一个人把条目列表挤没。
_INDEX_SUMMARY_BUDGET = 12000


def _render_index_markdown(index: BangumiIndex) -> str:
    """目录 → markdown：简介 + 分隔线 + 条目列表（每行一条）。

    每行形态：``**名次.** <a href="条目页">名称</a> · 角色 / 角色 · 附加信息``
    （没有名次 / 角色 / 信息的字段自动省略 —— 目录类型不同，这些字段可能缺）。

    ⚠️ 链接一律写 HTML ``<a>``：bgm 的正文会进富文本 markdown，而 markdown 链接
    在引用块 / 折叠块里不解析（项目既有结论，与 ``_BgMarkdownConverter`` 同一理由）。

    ⚠️ **按字符预算截断**：条目可能上百个，加上长简介会顶过富文本单条上限
    （32768）—— 超了不是"发得长了点"，而是**整条消息被服务端拒掉**。
    """
    parts: list[str] = []
    summary = index.summary_markdown.strip()
    if summary:
        if len(summary) > _INDEX_SUMMARY_BUDGET:
            summary = summary[:_INDEX_SUMMARY_BUDGET].rstrip() + "\n\n…"
        parts.append(summary)

    used = len(summary)
    lines: list[str] = []
    for entry in index.entries:
        bits: list[str] = []
        if entry.rank:
            bits.append(f"**{html.escape(entry.rank)}.**")
        bits.append(f'<a href="{html.escape(entry.url, quote=True)}">{html.escape(entry.name)}</a>')
        if entry.jobs:
            bits.append(" / ".join(html.escape(job) for job in entry.jobs))
        if entry.info:
            bits.append(html.escape(entry.info))
        line = " · ".join(bits)
        if used + len(line) + 1 > _INDEX_CHAR_BUDGET:
            lines.append("…")
            break
        lines.append(line)
        used += len(line) + 1

    if lines:
        if parts:
            parts.append("---")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _refs(images: list) -> list[ImageRef]:
    return [
        ImageRef(url=i.url, thumb_url=i.url, width=i.width, height=i.height, ext=image_ext_from_url(i.url))
        for i in images
    ]


__all__ = ["BangumiParseResult", "BangumiParser"]
