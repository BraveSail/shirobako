import json
import shutil
import time
from abc import ABC
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

import aiofiles
from bs4 import BeautifulSoup
from markdown import markdown as md_to_html
from slugify import slugify

from ..config import GlobalConfig
from ..errors import DeleteError, DownloadError
from ..utils.downloader import download
from ..utils.helpers import run_sync, to_datetime, to_int
from .callback import ProgressCallback
from .media_file import AniFile, AnyMediaFile, ImageFile, LivePhotoFile, VideoFile
from .media_ref import AniRef, AnyMediaRef, ImageRef, LivePhotoRef, VideoRef
from .platform import Platform
from .post import PostType


def _media_to_dict(media: AnyMediaRef) -> dict:
    """``MediaRef`` → dict，**剔除只供下载器内部使用的字段**。

    目前只有 ``backup_urls``：它是「主地址那份副本坏掉时换哪一个」的实现细节，
    属于下载策略而非内容描述，不该进序列化结果（缓存条目 / 对外接口）。
    """
    data = asdict(media)
    data.pop("backup_urls", None)
    return data


@dataclass
class PollOption:
    """投票的一个选项。"""

    text: str
    count: int | None = None
    """该选项的票数; None = 平台不给 (如 YouTube 匿名只看得到总票数)。"""


@dataclass
class Poll:
    """投票的结构化数据。

    **lib 只出数据, 不拼文案** —— 正文里对应的位置放独立一行 ``{{poll:N}}``
    (N = 它在 ``result.polls`` 里的索引, 按占位符在正文里的出现顺序从 0 递增),
    「选项/票数/占比」这类表格文案由渲染层按用户语言拼装。
    """

    options: list[PollOption]
    total_votes: int | None = None
    """总票数。"""
    voters: int | None = None
    """参与人数 (仅 linux.do 给)。"""
    title: str = ""
    """投票标题 (仅 linux.do 有)。"""
    is_finished: bool = False
    """投票是否已结束 (仅 twitter/threads 给)。"""


class ParseResult(ABC):  # noqa: B024
    """解析结果基类"""

    type: ClassVar[PostType] = PostType.UNKNOWN

    def __init__(
        self,
        title: str = "",
        content: str = "",
        media: Sequence[AnyMediaRef] | AnyMediaRef | None = None,
        platform: Platform | None = None,
        author_name: str = "",
        is_sensitive: bool = False,
        published_at: datetime | None = None,
        view_count: int | None = None,
        like_count: int | None = None,
        author_handle: str = "",
        author_url: str = "",
        tags: Sequence[str] | None = None,
        hashtags: Sequence[str] | None = None,
        quoted_media_count: int = 0,
        reply_media_count: int = 0,
        position_label: str = "",
        topic_label: str = "",
        quote_roles: Sequence[str] | None = None,
        origin_line: str = "",
        reply_count: int | None = None,
        polls: Sequence[Poll] | None = None,
    ):
        """
        :param title: 标题
        :param media: 媒体下载链接
        :param content: 正文 (纯文本)
        :param platform: 平台
        :param is_sensitive: 敏感内容标记 (R18/NSFW), 仅在有平台官方标记时置位
        :param published_at: 发布时间 (带时区), 平台没提供时为 None
        :param view_count: 浏览量/播放量, 平台没提供时为 None
        :param like_count: 点赞数, 平台没提供时为 None
        :param author_handle: 作者的用户名/账号 (不带 @), 平台没提供时为空
        :param author_url: 作者主页地址, 平台没提供或拿不到时为空
        :param tags: 作品标签 (平台提供时才有, 去重且保持原顺序)
        :param hashtags: 正文里的标签名 (**不含 ``#``**, 去重保序) —— 来自**平台实体**
            (如 twitter 的 ``entities.hashtags[].text``)。渲染层拿它做精确链接化, 避免用正则
            猜标签边界 (日文 ``」``、全角标点这类会把边界猜错)。拿不到就为空, 退回正则。
        :param quoted_media_count: ``media`` 末尾有多少个是**被引用内容**的媒体
            (渲染在被引用卡片里, 其余属于正文/被回复内容)
        :param reply_media_count: ``media`` 中, 在 ``quoted_media_count`` 之前的多少个
            属于**被回复内容** (渲染在回复卡片里, 位置在正文之前)
        :param position_label: 这段内容在**源站的位置标记**, 渲染层接在作者行后
            (如 linux.do 的楼层号 ``#4``)。平台没有位置概念的留空即可 ——
            通用层不认识楼层语义, ``#`` 由平台侧给。
        :param topic_label: 这段内容**所属话题**的渲染片段 (已带链接的 HTML, 如
            ``<a href="…">申請審判</a>``)。渲染层接在**作者行末尾** —— threads 的
            话题就是这种形态 (跟着平台自己的显示走)。没有话题概念的平台留空,
            输出与改动前逐字一致。
        :param reply_count: **回复数**（话题/帖子的回复条数）。平台拿不到时留空 ——
            渲染层"拿不到就不显示"，与浏览量/点赞同一条原则。
        :param origin_line: **归属行** —— "这条内容属于哪里" (如 bgm 的
            「小组/条目名 » 讨论」)。渲染层把它放在**标题与作者行之间**的元信息区,
            因此正文里插什么行都不影响引用块的归位。留空则不显示。
        :param quote_roles: 正文里每个引用块的**角色**, 按它们在正文中**出现的顺序**,
            取值 ``"reply"`` (被回复) / ``"quoted"`` (被引用)。

            **为什么需要它**: 引用块在正文里只是 ``> `` 开头的块, 没有身份标记。
            渲染层以前靠**位置**猜 (开头→被回复、末尾→被引用), 于是"谁在正文里插了一行"
            就会让归位漂移 —— bgm 的归属行插在引用块前, 引用块落到末尾, 本层的图就跟
            引用块贴到一起了。

            现在角色由平台显式声明, 位置不再参与判断; 渲染层按角色取对应的媒体通道。
            留空时渲染层**回退**到位置推断 (老缓存与未改的平台照旧)。
        :param polls: **投票的结构化数据**, 顺序 = 正文里 ``{{poll:N}}`` 占位符的出现顺序
            (lib 不拼表格文案; 渲染层按占位符索引取这里的数据再本地化)。
        """
        self.raw_url: str = ""
        self.title = title.strip()
        self.content = content.strip()
        self.media = media
        self.platform = platform
        self.author_name = author_name.strip()
        self.author_handle = author_handle.strip().lstrip("@")
        self.author_url = author_url.strip()
        self.is_sensitive = is_sensitive
        self.published_at = to_datetime(published_at)
        self.view_count = to_int(view_count)
        self.like_count = to_int(like_count)
        self.tags = self._clean_tags(tags)
        self.hashtags = self._clean_tags(hashtags)
        """正文里的标签名 (不含 ``#``), 来自平台实体; 空列表表示拿不到 (渲染层退回正则)"""
        self.quoted_media_count = max(0, to_int(quoted_media_count) or 0)
        self.reply_media_count = max(0, to_int(reply_media_count) or 0)
        self.position_label = (position_label or "").strip()
        self.topic_label = (topic_label or "").strip()
        """内容在源站的位置标记 (如 linux.do 的楼层号 ``#4``); 无此概念的平台为空串"""
        self.quote_roles: list[str] = [str(r).strip() for r in (quote_roles or []) if str(r).strip()]
        """正文里每个引用块的角色 (按出现顺序); 空列表 = 没声明, 渲染层退回位置推断"""
        self.origin_line = (origin_line or "").strip()
        """归属行 (这条内容属于哪里); 渲染层放在标题与作者行之间"""
        self.reply_count = to_int(reply_count)
        """回复数; 平台没提供时为 None (渲染层跳过那一段)"""
        self.polls: list[Poll] = list(polls or [])
        """投票的结构化数据; 顺序 = 正文里 ``{{poll:N}}`` 占位符的出现顺序"""
        self.name = slugify(
            self.title or self.content, allow_unicode=True, max_length=50, lowercase=False
        ).strip() or str(time.time_ns())
        """符合路径命名规范的名称, 可用于目录和文件名"""

    def __repr__(self) -> str:
        media_count = (
            f"[{len(self.media if isinstance(self.media, Sequence) else [self.media])}]" if self.media else None
        )
        return (
            f"{self.__class__.__name__}(platform={self.platform}, title={self.title or ''},"
            f" content={self.content or ''}, author_name={self.author_name or ''}, media={media_count}, "
            f"raw_url={self.raw_url})"
        )

    @staticmethod
    def _clean_tags(tags: Sequence[str] | None) -> list[str]:
        """去空、去重 (保持原顺序), 复用平台给的标签。"""
        seen: set[str] = set()
        result: list[str] = []
        for tag in tags or []:
            text = str(tag).strip()
            if text and text.casefold() not in seen:
                seen.add(text.casefold())
                result.append(text)
        return result

    def to_dict(self) -> dict:
        """转换为字典

        ``backup_urls`` 是**下载期的内部兜底字段**（主地址那份 CDN 副本坏掉时换用的
        备用地址），不参与对外契约：它进 ``to_dict`` 就会写进缓存/接口，把下载器的
        实现细节泄露给消费方，也让序列化快照跟着下载策略变动。
        """
        media: list[dict] | dict | None = None
        if isinstance(self.media, Sequence):
            media = [_media_to_dict(m) for m in self.media]
        elif self.media:
            media = _media_to_dict(self.media)

        return {
            "platform": self.platform.id if self.platform else None,
            "type": self.type.value,
            "title": self.title,
            "content": self.content,
            "author_name": self.author_name,
            "author_handle": self.author_handle,
            "author_url": self.author_url,
            "raw_url": self.raw_url,
            "is_sensitive": self.is_sensitive,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "view_count": self.view_count,
            "like_count": self.like_count,
            "tags": list(self.tags),
            "quoted_media_count": self.quoted_media_count,
            "reply_media_count": self.reply_media_count,
            "media": media,
            "polls": [asdict(p) for p in self.polls],
        }

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
        """
        执行下载
        :param output_dir: 输出的子目录
        :param callback: 下载进度回调函数
        :param callback_args: 回调函数的参数
        :param callback_kwargs: 回调函数的关键字参数
        :param proxy: 代理
        :param headers: 请求头
        :param connections: 多线程下载连接数, 默认为 4
        :return: DownloadResult
        """
        if self.media is None:
            return DownloadResult(output_dir=output_dir, media=[])
        media_list = list(self.media) if isinstance(self.media, Sequence) else [self.media]
        is_single = not isinstance(self.media, Sequence)

        result_list: list[AnyMediaFile] = []

        for i, media in enumerate(media_list):
            dl_progress = None
            dl_progress_args = ()
            dl_progress_kwargs: dict = {}
            if callback and is_single:

                async def _byte_callback(current: int, total: int, *args: Any, **kwargs: Any) -> None:
                    await callback(current, total, "bytes", *args, **kwargs)

                dl_progress = _byte_callback
                dl_progress_args = callback_args
                dl_progress_kwargs = callback_kwargs or {}

            index = i + 1

            try:
                save_path = (
                    output_dir.joinpath(f"{self.name}.{media.ext}")
                    if is_single
                    else output_dir.joinpath(f"{index:03d}_{self.name}.{media.ext}")
                )
                f = await download(
                    media.url,
                    save_path,
                    headers=headers,
                    proxy=proxy,
                    progress=dl_progress,
                    progress_args=dl_progress_args,
                    progress_kwargs=dl_progress_kwargs,
                    connections=connections,
                    backup_urls=getattr(media, "backup_urls", ()),
                )
            except Exception as e:
                shutil.rmtree(output_dir, ignore_errors=True)
                raise DownloadError(f"下载失败: {e}") from e

            mf: AnyMediaFile
            match media:
                case ImageRef():
                    mf = ImageFile(path=f, width=media.width, height=media.height)
                case VideoRef():
                    mf = VideoFile(path=f, width=media.width, height=media.height, duration=media.duration)
                case AniRef():
                    mf = AniFile(path=f, width=media.width, height=media.height, duration=media.duration)
                case LivePhotoRef():
                    mf = LivePhotoFile(path=f, width=media.width, height=media.height, duration=media.duration)
                    if media.video_url:
                        try:
                            save_path = (
                                output_dir.joinpath(f"{self.name}_video.{media.video_ext}")
                                if is_single
                                else output_dir.joinpath(f"{index:03d}_{self.name}_video.{media.video_ext}")
                            )
                            vf = await download(
                                media.video_url,
                                save_path,
                                headers=headers,
                                proxy=proxy,
                                connections=connections,
                            )
                        except Exception as e:
                            shutil.rmtree(output_dir, ignore_errors=True)
                            raise DownloadError(f"LivePhoto 视频下载失败: {e}") from e
                        else:
                            mf.video_path = vf

            result_list.append(mf)

            if callback and not is_single:
                await callback(
                    len(result_list),
                    len(media_list),
                    "count",
                    *callback_args,
                )

        result_media = result_list[0] if is_single else result_list
        return DownloadResult(result_media, output_dir)

    async def download(
        self,
        path: str | Path | None = None,
        *,
        callback: ProgressCallback | None = None,
        callback_args: tuple = (),
        callback_kwargs: dict | None = None,
        proxy: str | None = None,
        save_metadata: bool = False,
        connections: int = 4,
    ) -> "DownloadResult":
        """
        :param path: 保存路径
        :param callback: 下载进度回调函数
        :param callback_args: 下载进度回调函数参数
        :param callback_kwargs: 回调函数的关键字参数
        :param proxy: 代理
        :param save_metadata: 保存解析结果为 metadata.json, 默认为 False
        :param connections: 多线程下载连接数, 默认为 4
        :return: DownloadResult

        Note:
            下载进度回调函数签名::

                async def callback(current: int, total: int, unit: Literal['bytes', 'count'], *args, **kwargs) -> None

            - current: 当前进度值
            - total: 总进度值
            - unit: 进度单位
                - ``bytes``: 字节进度，用于单文件下载时报告已下载/总字节数
                - ``count``: 计数进度，用于多文件下载时报告已完成/总文件数
        """
        save_dir = Path(path) if path else GlobalConfig.default_save_dir
        output_dir = save_dir.joinpath(self.name)
        counter = 2
        while output_dir.exists():
            output_dir = save_dir.joinpath(f"{self.name}_{counter}")
            counter += 1
        output_dir.mkdir(parents=True, exist_ok=True)

        if save_metadata:
            async with aiofiles.open(output_dir.joinpath("metadata.json"), "w", encoding="utf-8") as f:
                await f.write(json.dumps(self.to_dict(), ensure_ascii=False, indent=4))

        try:
            return await self._do_download(
                output_dir=output_dir,
                callback=callback,
                callback_args=callback_args,
                callback_kwargs=callback_kwargs,
                proxy=proxy,
                connections=connections,
            )
        except Exception as e:
            shutil.rmtree(output_dir, ignore_errors=True)
            raise e

    def download_sync(
        self,
        path: str | Path | None = None,
        *,
        callback: ProgressCallback | None = None,
        callback_args: tuple = (),
        callback_kwargs: dict | None = None,
        proxy: str | None = None,
        save_metadata: bool = False,
        connections: int = 4,
    ) -> "DownloadResult":
        """
        :param path: 保存路径
        :param callback: 下载进度回调函数
        :param callback_args: 下载进度回调函数参数
        :param callback_kwargs: 回调函数的关键字参数
        :param proxy: 代理
        :param save_metadata: 保存解析结果为 metadata.json, 默认为 False
        :param connections: 多线程下载连接数, 默认为 4
        :return: DownloadResult

        Note:
            下载进度回调函数签名::

                async def callback(current: int, total: int, unit: Literal['bytes', 'count'], *args) -> None

            - current: 当前进度值
            - total: 总进度值
            - unit: 进度单位
                - ``bytes``: 字节进度，用于单文件下载时报告已下载/总字节数
                - ``count``: 计数进度，用于多文件下载时报告已完成/总文件数
        """
        return run_sync(
            self.download(
                path,
                callback=callback,
                callback_args=callback_args,
                callback_kwargs=callback_kwargs,
                proxy=proxy,
                save_metadata=save_metadata,
                connections=connections,
            )
        )


class VideoParseResult(ParseResult):
    """单个视频"""

    type = PostType.VIDEO

    def __init__(
        self,
        title: str = "",
        video: str | VideoRef | None = None,
        content: str = "",
        author_name: str = "",
        is_sensitive: bool = False,
        published_at: datetime | None = None,
        view_count: int | None = None,
        like_count: int | None = None,
        author_handle: str = "",
        author_url: str = "",
        tags: Sequence[str] | None = None,
        hashtags: Sequence[str] | None = None,
        quoted_media_count: int = 0,
        reply_media_count: int = 0,
        position_label: str = "",
        topic_label: str = "",
        quote_roles: Sequence[str] | None = None,
        origin_line: str = "",
        reply_count: int | None = None,
        polls: Sequence[Poll] | None = None,
    ):
        video = VideoRef(url=video) if isinstance(video, str) else video
        super().__init__(
            title=title,
            media=video,
            content=content,
            author_name=author_name,
            is_sensitive=is_sensitive,
            published_at=published_at,
            view_count=view_count,
            like_count=like_count,
            author_handle=author_handle,
            author_url=author_url,
            tags=tags,
            hashtags=hashtags,
            quoted_media_count=quoted_media_count,
            reply_media_count=reply_media_count,
            position_label=position_label,
            topic_label=topic_label,
            quote_roles=quote_roles,
            origin_line=origin_line,
            reply_count=reply_count,
            polls=polls,
        )


class ImageParseResult(ParseResult):
    """单图 / 多图 / 图集 / 实况照片"""

    type = PostType.IMAGE

    def __init__(
        self,
        title: str = "",
        photo: Sequence[str | ImageRef | AniRef | LivePhotoRef] | None = None,
        content: str = "",
        author_name: str = "",
        is_sensitive: bool = False,
        published_at: datetime | None = None,
        view_count: int | None = None,
        like_count: int | None = None,
        author_handle: str = "",
        author_url: str = "",
        tags: Sequence[str] | None = None,
        hashtags: Sequence[str] | None = None,
        quoted_media_count: int = 0,
        reply_media_count: int = 0,
        position_label: str = "",
        topic_label: str = "",
        quote_roles: Sequence[str] | None = None,
        origin_line: str = "",
        reply_count: int | None = None,
        polls: Sequence[Poll] | None = None,
    ):
        media = [ImageRef(url=p) if isinstance(p, str) else p for p in photo] if photo else None
        super().__init__(
            title=title,
            media=media,
            content=content,
            author_name=author_name,
            is_sensitive=is_sensitive,
            published_at=published_at,
            view_count=view_count,
            like_count=like_count,
            author_handle=author_handle,
            author_url=author_url,
            tags=tags,
            hashtags=hashtags,
            quoted_media_count=quoted_media_count,
            reply_media_count=reply_media_count,
            position_label=position_label,
            topic_label=topic_label,
            quote_roles=quote_roles,
            origin_line=origin_line,
            reply_count=reply_count,
            polls=polls,
        )


class MultimediaParseResult(ParseResult):
    """多视频 / 视频 + 图片 / GIF / 实况照片"""

    type = PostType.MULTIMEDIA

    def __init__(
        self,
        title: str = "",
        media: Sequence[AnyMediaRef] | None = None,
        content: str = "",
        author_name: str = "",
        is_sensitive: bool = False,
        published_at: datetime | None = None,
        view_count: int | None = None,
        like_count: int | None = None,
        author_handle: str = "",
        author_url: str = "",
        tags: Sequence[str] | None = None,
        hashtags: Sequence[str] | None = None,
        quoted_media_count: int = 0,
        reply_media_count: int = 0,
        position_label: str = "",
        topic_label: str = "",
        quote_roles: Sequence[str] | None = None,
        origin_line: str = "",
        reply_count: int | None = None,
        polls: Sequence[Poll] | None = None,
    ):
        super().__init__(
            title=title,
            media=media,
            content=content,
            author_name=author_name,
            is_sensitive=is_sensitive,
            published_at=published_at,
            view_count=view_count,
            like_count=like_count,
            author_handle=author_handle,
            author_url=author_url,
            tags=tags,
            hashtags=hashtags,
            quoted_media_count=quoted_media_count,
            reply_media_count=reply_media_count,
            position_label=position_label,
            topic_label=topic_label,
            quote_roles=quote_roles,
            origin_line=origin_line,
            reply_count=reply_count,
            polls=polls,
        )


class RichTextParseResult(ParseResult):
    """图文混排的文章"""

    type = PostType.RICHTEXT

    #: 正文里已经内嵌了图片的外链, Telegram 会自己去抓, 不需要下载后当附件重发
    #: (流水线的 richtext_skip_download 依赖这个语义)。
    #: 若平台把图片从正文**抽走**当成附件放在 ``media`` 里 (如 linux.do),
    #: 子类要置 True, 否则流水线会跳过下载, 图片就彻底丢了。
    requires_media_download: ClassVar[bool] = False

    def __init__(
        self,
        title: str = "",
        media: Sequence[AnyMediaRef] | None = None,
        markdown_content: str = "",
        author_name: str = "",
        is_sensitive: bool = False,
        published_at: datetime | None = None,
        view_count: int | None = None,
        like_count: int | None = None,
        author_handle: str = "",
        author_url: str = "",
        tags: Sequence[str] | None = None,
        hashtags: Sequence[str] | None = None,
        quoted_media_count: int = 0,
        reply_media_count: int = 0,
        position_label: str = "",
        topic_label: str = "",
        quote_roles: Sequence[str] | None = None,
        origin_line: str = "",
        reply_count: int | None = None,
        polls: Sequence[Poll] | None = None,
    ):
        """
        :param title: 标题
        :param media: 文章中的媒体
        :param markdown_content: markdown 格式正文
        """
        self.markdown_content = markdown_content
        super().__init__(
            title=title,
            media=media,
            content=self.plaintext_content,
            author_name=author_name,
            is_sensitive=is_sensitive,
            published_at=published_at,
            view_count=view_count,
            like_count=like_count,
            author_handle=author_handle,
            author_url=author_url,
            tags=tags,
            hashtags=hashtags,
            quoted_media_count=quoted_media_count,
            reply_media_count=reply_media_count,
            position_label=position_label,
            topic_label=topic_label,
            quote_roles=quote_roles,
            origin_line=origin_line,
            reply_count=reply_count,
            polls=polls,
        )

    def __repr__(self) -> str:
        media_items = self.media if isinstance(self.media, Sequence) else [self.media]
        media_count = f"[{len(media_items)}]" if self.media else None
        return (
            f"{self.__class__.__name__}(title={self.title or ''},"
            f" markdown_content={self.markdown_content or ''}, media={media_count} raw_url={self.raw_url})"
        )

    def to_dict(self) -> dict:
        """转换为字典"""
        data = super().to_dict()
        # 在 "content" 后面插入 "markdown_content"
        result = {}
        for key, value in data.items():
            result[key] = value
            if key == "content":
                result["markdown_content"] = self.markdown_content
        return result

    @property
    def plaintext_content(self) -> str:
        """从 markdown 转换为纯文本"""
        return "".join(BeautifulSoup(md_to_html(self.markdown_content), "lxml").find_all(string=True)).strip()


class DownloadResult:
    def __init__(self, media: AnyMediaFile | Sequence[AnyMediaFile], output_dir: str | Path):
        """
        下载结果
        :param media: 本地媒体路径
        :param output_dir: 输出目录
        """
        self.media = media
        self.output_dir = Path(output_dir).resolve()

    def delete(self) -> None:
        try:
            shutil.rmtree(self.output_dir)
        except Exception as e:
            raise DeleteError(f"目录删除失败: {self.output_dir}") from e

    def __repr__(self) -> str:
        media_count = (
            f"[{len(self.media if isinstance(self.media, Sequence) else [self.media])}]" if self.media else None
        )
        return f"{self.__class__.__name__}(media={media_count}, output_dir={self.output_dir})"


AnyParseResult = VideoParseResult | ImageParseResult | MultimediaParseResult | RichTextParseResult
