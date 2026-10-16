import asyncio
import os
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from functools import partial
from itertools import batched
from typing import TYPE_CHECKING, Any, BinaryIO, cast

from easy_ai18n import PreLocaleSelector
from parsehub.types import AniFile, AniRef, AnyMediaRef, AnyParseResult, ImageFile, LivePhotoFile, VideoFile
from pyrogram import Client, enums
from pyrogram.errors import FloodWait, Forbidden, SlowmodeWait, WebpageCurlFailed, WebpageMediaEmpty
from pyrogram.types import (
    InlineKeyboardButton as Ikb,
)
from pyrogram.types import (
    InlineKeyboardMarkup as Ikm,
)
from pyrogram.types import (
    InputMediaAnimation,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    InputRichMessage,
    LinkPreviewOptions,
    Message,
)

from core import bs
from log import logger
from plugins.helpers import (
    build_caption,
    build_rich_markdown,
    format_label,
    markdown_needs_blocks,
)
from plugins.parse.cache import cache_media_from_message
from plugins.parse.covers import prepare_video_thumbs
from plugins.parse.inline_rich import (
    build_cached_rich_content,
    build_rich_media,
    cached_rich_message,
    extract_cache_media,
    rich_cache_entry,
)
from plugins.parse.rich_blocks import markdown_to_blocks
from repo.settings import SettingsConfig

if TYPE_CHECKING:
    # 仅为类型标注: reporters 反向依赖本模块的 MessageSender, 运行时不能互相导入
    from plugins.parse.reporters import MessageStatusReporter
from services import (
    CacheEntry,
    CacheMedia,
    CacheMediaType,
    PipelineResult,
    StatusReporter,
    persistent_cache,
)
from services.media import ProcessedMedia, resolve_media_info
from utils.helpers import pack_dir_to_tar_gz, to_list

logger = logger.bind(name="ParseSender")

#: Telegram 媒体 caption 上限 1024, 留余量给元数据行。
#: 只有**以媒体 caption 发送**的路径需要: send_raw / send_zip。
#: 富文本正文没有这个限制, 靠折叠收起 (见 helpers.format_text)。
_CAPTION_MAX_LENGTH = 1000

MAX_RETRIES = 5
GIF_ONLY_SKIP_DOWNLOAD_COUNT_THRESHOLD = 5


type ReplyMediaGroupItem = InputMediaPhoto | InputMediaVideo | InputMediaDocument
type PathType = str | os.PathLike[str]


@dataclass(frozen=True, slots=True)
class MessageSender:
    cli: Client
    msg: Message
    config: SettingsConfig
    delete_after_seconds: float | None = None

    def delete_after(self, seconds: float | int | None) -> "MessageSender":
        if not seconds:
            return self
        return replace(self, delete_after_seconds=float(seconds))

    def _delete_later(self, sent: Message | Sequence[Message]) -> None:
        if self.delete_after_seconds is None:
            return

        messages = to_list(sent)

        async def fn() -> None:
            await asyncio.sleep(self.delete_after_seconds or 0)
            for message in messages:
                try:
                    await message.delete()
                except Exception as e:
                    logger.debug(
                        f"定时删除消息失败: chat_id={message.chat and message.chat.id}, msg_id={message.id}, error={e}"
                    )

        asyncio.get_running_loop().create_task(fn())

    async def _send_and_schedule_delete[T](self, send_coro_fn: Callable[[], Awaitable[T]]) -> T:
        sent = await self._send(send_coro_fn)
        if isinstance(sent, Message) or isinstance(sent, list):
            self._delete_later(sent)
        return sent

    @staticmethod
    async def _send[T](send_coro_fn: Callable[[], Awaitable[T]]) -> T:
        for attempt in range(MAX_RETRIES):
            try:
                return await send_coro_fn()
            except (FloodWait, SlowmodeWait) as e:
                if attempt < MAX_RETRIES - 1:
                    wait_seconds = e.value if isinstance(e.value, int | float) else 0.5
                    logger.warning(f"{e.ID} 重试 ({attempt + 1}/{MAX_RETRIES})，等待 {wait_seconds}s")
                    await asyncio.sleep(float(wait_seconds))
                else:
                    raise
            except Forbidden as e:
                logger.warning(f"消息发送失败, Bot 无权限: {e}")
                break
            except Exception as e:
                logger.warning(f"消息发送失败: {e}")
                break
            await asyncio.sleep(0.5)
        raise RuntimeError("消息发送失败")

    async def chat_action(self, action: enums.ChatAction) -> None:
        await self.msg.reply_chat_action(action)

    async def typing(self) -> None:
        await self.chat_action(enums.ChatAction.TYPING)

    async def upload_document(self) -> None:
        await self.chat_action(enums.ChatAction.UPLOAD_DOCUMENT)

    async def upload_photo(self) -> None:
        await self.chat_action(enums.ChatAction.UPLOAD_PHOTO)

    async def upload_video(self) -> None:
        await self.chat_action(enums.ChatAction.UPLOAD_VIDEO)

    async def text(
        self,
        text: str,
        *,
        link_preview_options: LinkPreviewOptions | None = None,
        reply_markup: Ikm | None = None,
    ) -> Message:
        """发一条文本消息。**默认关闭链接预览**。

        文本里带链接时 Telegram 会挂一张 link preview 卡片, 而卡片在正文之外 ——
        内容即使被折叠遮住, 卡片上的封面图照样看得见 (用户报「不然还是能看到图」)。
        这个项目的文本消息要么是提示、要么是解析结果, **没有一处需要预览**,
        所以默认值取安全的一侧: 忘了传也不会泄露。要预览得显式传 options。
        """
        # 显式判 None (不用 `or`): 调用方传什么都不该被覆盖
        if link_preview_options is None:
            link_preview_options = LinkPreviewOptions(is_disabled=True)
        return cast(
            Message,
            await self._send_and_schedule_delete(
                partial(
                    self.msg.reply if self.config.reply_msg else self.msg.answer,
                    text,
                    link_preview_options=link_preview_options,
                    reply_markup=reply_markup,
                )
            ),
        )

    async def text_no_preview(self, text: str, *, reply_markup: Ikm | None = None) -> Message:
        return await self.text(
            text,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            reply_markup=reply_markup,
        )


    async def document(
        self,
        document: PathType | BinaryIO,
        *,
        caption: str | None = None,
        force_document: bool | None = None,
        use_reply_policy: bool = True,
    ) -> Message:
        return cast(
            Message,
            await self._send_and_schedule_delete(
                partial(
                    (self.msg.reply_document if self.config.reply_msg else self.msg.answer_document)
                    if use_reply_policy
                    else self.msg.reply_document,
                    document,
                    caption=caption or "",
                    force_document=force_document,
                )
            ),
        )

    def reply_to(self, msg: Message) -> "MessageSender":
        return replace(self, msg=msg)

    async def force_document(
        self,
        document: PathType | BinaryIO,
        *,
        caption: str | None = None,
        use_reply_policy: bool = True,
    ) -> Message:
        return await self.document(
            document,
            caption=caption,
            force_document=True,
            use_reply_policy=use_reply_policy,
        )

    async def photo(
        self, photo: PathType | BinaryIO, *, caption: str | None = None, has_spoiler: bool = False
    ) -> Message:
        return cast(
            Message,
            await self._send_and_schedule_delete(
                partial(
                    self.msg.reply_photo if self.config.reply_msg else self.msg.answer_photo,
                    photo,
                    caption=caption or "",
                    has_spoiler=has_spoiler,
                )
            ),
        )

    async def video(
        self,
        video: PathType | BinaryIO,
        *,
        caption: str | None = None,
        video_cover: PathType | BinaryIO | None = None,
        duration: int | None = None,
        width: int | None = None,
        height: int | None = None,
        supports_streaming: bool | None = None,
        has_spoiler: bool = False,
    ) -> Message:
        return cast(
            Message,
            await self._send_and_schedule_delete(
                partial(
                    self.msg.reply_video if self.config.reply_msg else self.msg.answer_video,
                    video,
                    caption=caption or "",
                    has_spoiler=has_spoiler,
                    video_cover=video_cover,
                    duration=duration or 0,
                    width=width or 0,
                    height=height or 0,
                    supports_streaming=True if supports_streaming is None else supports_streaming,
                )
            ),
        )

    async def streaming_video(
        self,
        video: PathType | BinaryIO,
        *,
        caption: str | None = None,
        video_cover: PathType | BinaryIO | None = None,
        duration: int | None = None,
        width: int | None = None,
        height: int | None = None,
        has_spoiler: bool = False,
    ) -> Message:
        return await self.video(
            video,
            caption=caption,
            video_cover=video_cover,
            duration=duration,
            width=width,
            height=height,
            supports_streaming=True,
            has_spoiler=has_spoiler,
        )

    async def streaming_video_with_cover_fallback(
        self,
        video: PathType | BinaryIO,
        *,
        caption: str | None = None,
        video_cover: PathType | BinaryIO | None = None,
        duration: int | None = None,
        width: int | None = None,
        height: int | None = None,
        has_spoiler: bool = False,
    ) -> Message:
        try:
            return await self.streaming_video(
                video,
                caption=caption,
                video_cover=video_cover,
                duration=duration,
                width=width,
                height=height,
                has_spoiler=has_spoiler,
            )
        except (WebpageCurlFailed, WebpageMediaEmpty):
            logger.warning("Tg 获取封面失败, 移除封面上传")
            return await self.streaming_video(
                video,
                caption=caption,
                duration=duration,
                width=width,
                height=height,
                has_spoiler=has_spoiler,
            )

    async def animation(
        self, animation: PathType | BinaryIO, *, caption: str | None = None, has_spoiler: bool = False
    ) -> Message:
        return cast(
            Message,
            await self._send_and_schedule_delete(
                partial(
                    self.msg.reply_animation if self.config.reply_msg else self.msg.answer_animation,
                    animation,
                    caption=caption or "",
                    has_spoiler=has_spoiler,
                )
            ),
        )

    async def media_group(self, media: list[ReplyMediaGroupItem]) -> list[Message]:
        return await self._send_and_schedule_delete(
            partial(
                self.msg.reply_media_group if self.config.reply_msg else self.msg.answer_media_group,
                media=cast(Any, media),
            )
        )

    async def rich_message(
        self,
        rich_message: InputRichMessage,
        *,
        reply_markup: Ikm | None = None,
    ) -> Message:
        return cast(
            Message,
            await self._send_and_schedule_delete(
                partial(
                    self.msg.reply_rich if self.config.reply_msg else self.msg.answer_rich,
                    rich_message=rich_message,
                    reply_markup=reply_markup,
                )
            ),
        )


async def send_raw(
    sender: MessageSender,
    result: PipelineResult,
    reporter: StatusReporter,
    *,
    _t: PreLocaleSelector,
    custom_content: str = "",
) -> None:
    """Raw 模式：将文件以原始文档形式上传。"""
    logger.debug("Raw 模式, 直接上传文件")
    await reporter.report_result(result.parse_result, _t("上 传 中..."))
    try:
        caption = build_caption(
            result.parse_result,
            config=sender.config,
            custom_content=custom_content,
            lang=_t.locale,
            view_label=_t("查看"),
            # 原始文件以媒体 caption 形式发送, Telegram 上限 1024
            max_length=_CAPTION_MAX_LENGTH,
        )
        docs: list[InputMediaDocument] = []
        gifs = []
        livephoto_videos: dict[int, InputMediaDocument] = {}

        for processed in result.processed_list:
            file_paths = processed.output_paths or [processed.source.path]
            file_path = file_paths[0]
            doc = InputMediaDocument(media=str(file_path))
            if isinstance(processed.source, AniFile):
                gifs.append(doc)
            elif isinstance(processed.source, LivePhotoFile):
                docs.append(doc)
                livephoto_videos[len(docs) - 1] = InputMediaDocument(media=str(processed.source.video_path))
            else:
                docs.append(doc)

        if len(docs + gifs) == 1:
            all_docs = docs + gifs
            await sender.upload_document()
            sent_msg = await sender.force_document(media_input(all_docs[0].media), caption=caption)
            if livephoto_videos and sent_msg:
                await sender.reply_to(sent_msg).force_document(
                    media_input(livephoto_videos[0].media),
                    use_reply_policy=False,
                )
        else:
            msgs: list[Message] = []
            for batch in batched(docs, 10):
                await sender.upload_document()
                mg = await sender.media_group(list(batch))
                msgs.extend(mg)
            if livephoto_videos:
                for idx, media_doc in livephoto_videos.items():
                    await sender.upload_document()
                    await sender.reply_to(msgs[idx]).force_document(
                        media_input(media_doc.media),
                        use_reply_policy=False,
                    )
            if gifs:
                await sender.text(
                    format_label(_t("GIF 下载链接")),
                    reply_markup=build_gif_button(to_list(result.parse_result.media)),
                )
            await sender.text_no_preview(caption)

    except Exception as e:
        logger.opt(exception=e).debug("详细堆栈")
        logger.error(f"Raw 模式上传失败: {e}")
        await reporter.report_error(_t("上传"), e)
        return
    else:
        await reporter.dismiss()
    finally:
        result.cleanup()


async def send_zip(
    sender: MessageSender,
    result: PipelineResult,
    reporter: StatusReporter,
    *,
    _t: PreLocaleSelector,
    custom_content: str = "",
) -> None:
    logger.debug("Zip 模式, 开始打包")
    await reporter.report_result(result.parse_result, _t("打 包 中..."))
    try:
        caption = build_caption(
            result.parse_result,
            config=sender.config,
            custom_content=custom_content,
            lang=_t.locale,
            view_label=_t("查看"),
            # 打包成一条消息时带 caption, Telegram 上限 1024, 必须留余量
            max_length=_CAPTION_MAX_LENGTH,
        )
        if result.output_dir is None:
            raise ValueError("缺少打包目录")
        pack_path = await asyncio.to_thread(pack_dir_to_tar_gz, result.output_dir)
    except Exception as e:
        logger.opt(exception=e).debug("详细堆栈")
        logger.error(f"打包失败: {e}")
        await reporter.report_error(_t("打包"), Exception("..."))
        return
    finally:
        result.cleanup()

    await reporter.report_result(result.parse_result, _t("上 传 中..."))
    try:
        await sender.upload_document()
        await sender.document(str(pack_path), caption=caption)
    except Exception as e:
        logger.opt(exception=e).debug("详细堆栈")
        logger.error(f"上传失败: {e}")
        await reporter.report_error(_t("上传"), e)
        return
    else:
        await reporter.dismiss()
    finally:
        if not bs.debug_skip_cleanup:
            logger.debug("清理压缩包")
            os.remove(pack_path)


async def send_cached(
    sender: MessageSender,
    entry: CacheEntry,
    url: str,
    *,
    custom_content: str = "",
    _t: PreLocaleSelector | None = None,
    spoiler_tag: str = "",
) -> None:
    """从 file_id 缓存直接发送（富文本）: file_id 复用, 跳过解析/下载/转码/上传。

    :param spoiler_tag: 用户这次要求遮住内容 (``/s``) —— 缓存命中也得照遮,
        否则第二次发同一链接时打码会失效 (缓存存的是解析字段, 排版现做)。
    """
    logger.debug(f"缓存发送: media={entry.media}")
    lang = _t.locale if _t else ""
    view_label = _t("查看") if _t else ""
    # 缓存路径同样走富文本排版 (与直发共用 build_cached_rich_content):
    # 不再拼老 caption —— 否则同一链接第二次发送会变成另一种格式
    markdown, media, media_blocks = build_cached_rich_content(
        entry,
        url,
        lang=lang,
        config=sender.config,
        view_label=view_label,
        custom_content=custom_content,
        spoiler_tag=spoiler_tag,
    )
    # 敏感内容走 blocks 路径才打得了码 (以前这里直接用 markdown, 缓存命中就没遮罩)
    await sender.rich_message(
        rich_message=cached_rich_message(
            markdown, media, media_blocks, sensitive=bool(entry.parse_result.is_sensitive)
        )
    )


def media_input(media: PathType | BinaryIO | None) -> PathType | BinaryIO:
    return cast(PathType | BinaryIO, media)


async def send_rich_media(
    sender: MessageSender,
    parse_result: AnyParseResult,
    processed_list: list[ProcessedMedia],
    *,
    _t: PreLocaleSelector,
    custom_content: str = "",
    raw_url: str = "",
    reporter: "MessageStatusReporter | None" = None,
    spoiler_tag: str = "",
) -> bool:
    """以富文本 (rich message) 发送解析结果: 正文保留原文格式, 统计与来源进页尾。

    :param spoiler_tag: 用户手动要求遮住内容 (链接后跟 ``/s``) —— 正文折成
        ``<details><summary>⚠️</summary>``, 不留预览。

    给了 ``reporter`` 且它已经发过状态消息时, 结果会**编辑进那条状态消息**
    (整个流程只留一条消息, 没有删除记录、没有多余临时消息); 否则新建一条。

    发送后把服务端返回的媒体 file_id 写回缓存 —— 下次同一链接零上传直发,
    inline 也能直接带媒体 (不用二次编辑)。
    """
    media_refs = to_list(parse_result.media)
    # 视频封面要本地文件: 富文本的 document 只带 id, 远端 cover 会被丢掉
    video_thumbs = (
        await prepare_video_thumbs(media_refs, platform=parse_result.platform)
        if sender.config.video_cover
        else {}
    )
    media, placeholders, media_blocks, quoted_placeholders, reply_placeholders, media_urls = build_rich_media(
        media_refs,
        processed_list,
        video_cover=sender.config.video_cover,
        is_sensitive=parse_result.is_sensitive,
        video_thumbs=video_thumbs,
        quoted_media_count=getattr(parse_result, "quoted_media_count", 0),
        reply_media_count=getattr(parse_result, "reply_media_count", 0),
    )
    markdown = build_rich_markdown(
        parse_result,
        config=sender.config,
        lang=_t.locale,
        view_label=_t("查看"),
        custom_content=custom_content,
        media_placeholders=placeholders,
        quote_media_placeholders=quoted_placeholders,
        reply_media_placeholders=reply_placeholders,
        media_urls=media_urls,
        hide_content=spoiler_tag,
    )
    if (parse_result.is_sensitive and media_blocks) or markdown_needs_blocks(markdown):
        # 两种情况只能走 blocks:
        #  1. 敏感内容的媒体必须打码 —— 官方 API 的富文本媒体块没有 spoiler 字段,
        #     只能自己构造 raw blocks (PageBlockPhoto/Video 带 spoiler);
        #  2. 折叠的引用卡片带媒体 —— 图要留在引用块内 (markdown 的 `>` 块做不到)。
        blocks = markdown_to_blocks(markdown, media_blocks=media_blocks)
        logger.debug(f"富文本(blocks): media={len(media_blocks)}, blocks={len(blocks)}")
        rich = InputRichMessage(blocks=blocks)
    else:
        logger.debug(f"富文本: media={len(media)}, markdown_len={len(markdown)}")
        rich = InputRichMessage(markdown=markdown, media=media or None)

    message = await _post_rich(sender, rich, reporter=reporter)

    if raw_url and media:
        cached_media = extract_cache_media(getattr(message, "rich_message", None))
        # ⚠️ **全有才写**（2026-10-08）: 服务端返回的结构里若有一项没认出来（嵌套在引用块里、
        # 或某个分支拿不到 file_id），条目里就会少一项。而下面那两个计数是**按数量**推的 ——
        # 少一项时 `quoted_items` 仍会算成 1，于是缓存命中时把**正文的媒体**放进引用卡片、
        # 正文反而没图（用户报「缓存正文又没图了」）。
        # 宁可这次不写（下次重新解析 + 上传，只是贵一点），也不要写一条**位置错**的缓存。
        expected = len(media)
        if len(cached_media) == expected:
            # 缓存里媒体是平铺的: 末尾 N 项属于被引用块、其前 M 项属于被回复块
            # (用占位符数量推, 两者一一对应)
            quoted_items = min(len(quoted_placeholders), len(cached_media))
            reply_items = min(len(reply_placeholders), len(cached_media) - quoted_items)
            await persistent_cache.set(
                raw_url,
                rich_cache_entry(
                    parse_result,
                    cached_media,
                    quoted_media_count=quoted_items,
                    reply_media_count=reply_items,
                    # 投票要进缓存 —— 否则"第二次发同一条链接"占位符渲染不出来
                    polls=getattr(parse_result, "polls", None),
                    # 原始 URL 也进缓存: 缓存命中时正文外链图才能换成上传媒体引用
                    urls=media_urls,
                ),
            )
            logger.debug(
                f"富文本媒体已写入缓存: count={len(cached_media)}, 引用{quoted_items} 回复{reply_items}"
            )
        else:
            logger.warning(
                f"媒体提取数量不符, 本次不写 file_id 缓存: 期望{expected} 提取到{len(cached_media)}"
            )
    return True


async def _post_rich(
    sender: MessageSender,
    rich: InputRichMessage,
    *,
    reporter: "MessageStatusReporter | None" = None,
) -> Message:
    """发布富文本结果: 优先编辑状态消息, 不行再新建。

    编辑失败 (状态消息被用户手动删掉、内容被判为未变更等) 时退回新建 ——
    结果不能因为"收尾方式"失败而丢失。
    """
    from pyrogram.errors import WebpageCurlFailed

    from plugins.parse.inline_rich import strip_video_cover

    async def _send(r: InputRichMessage) -> Message:
        if reporter is not None and reporter.has_message:
            try:
                if (edited := await reporter.finalize(r)) is not None:
                    return edited
            except WebpageCurlFailed:
                raise  # 交给外层"去掉封面重试", 别退化成新建一条
            except Exception as e:
                logger.warning(f"编辑状态消息失败, 改为新建结果: {type(e).__name__}: {e}")
        return await sender.rich_message(rich_message=r)

    try:
        return await _send(rich)
    except WebpageCurlFailed as e:
        # 服务端抓不到封面 URL 时会拒收整条 —— 去掉封面重发 (普通发送路径一直这么做,
        # 富文本路径当初漏了这一步)。
        logger.warning(f"服务端抓不到封面, 去掉封面重试: {str(e)[:90]}")
        return await _send(strip_video_cover(rich))


def build_input_media(
    media_refs: Sequence[AnyMediaRef],
    processed_list: list[ProcessedMedia],
    *,
    video_cover: bool,
    is_sensitive: bool = False,
) -> tuple[list[InputMediaPhoto | InputMediaVideo], list[InputMediaAnimation]]:
    """根据处理结果和媒体引用构建 Telegram InputMedia 列表。"""
    photos_videos: list[InputMediaPhoto | InputMediaVideo] = []
    animations: list[InputMediaAnimation] = []

    for media_ref, processed in zip(media_refs, processed_list, strict=False):
        file_paths = processed.output_paths or [processed.source.path]
        for file_path in file_paths:
            file_path_str = str(file_path)
            width, height, duration = resolve_media_info(processed, file_path_str)

            match processed.source:
                case ImageFile():
                    photos_videos.append(InputMediaPhoto(media=file_path_str, has_spoiler=is_sensitive))
                case AniFile():
                    animations.append(InputMediaAnimation(media=file_path_str, has_spoiler=is_sensitive))
                case VideoFile():
                    photos_videos.append(
                        InputMediaVideo(
                            media=file_path_str,
                            has_spoiler=is_sensitive,
                            video_cover=media_ref.thumb_url if video_cover else None,
                            duration=duration,
                            width=width,
                            height=height,
                            supports_streaming=True,
                        )
                    )
                case LivePhotoFile():
                    photos_videos.append(
                        InputMediaVideo(
                            media=processed.source.video_path,
                            has_spoiler=is_sensitive,
                            video_cover=file_path_str if video_cover else None,
                            duration=duration,
                            width=width,
                            height=height,
                            supports_streaming=True,
                        )
                    )

    return photos_videos, animations


async def send_single(
    sender: MessageSender,
    photos_videos: list[InputMediaPhoto | InputMediaVideo],
    animations: list[InputMediaAnimation],
    caption: str,
) -> list[CacheMedia] | None:
    """发送单个媒体，返回 CacheMedia 列表。上传失败时降级为 document。"""
    media_list: list[CacheMedia] = []
    all_media = animations + photos_videos

    try:
        sent: Message | None = None
        if animations:
            await sender.upload_photo()
            sent = await sender.animation(
                media_input(animations[0].media), caption=caption, has_spoiler=animations[0].has_spoiler
            )
        else:
            single = photos_videos[0]
            match single:
                case InputMediaPhoto():
                    await sender.upload_photo()
                    sent = await sender.photo(
                        media_input(single.media), caption=caption, has_spoiler=single.has_spoiler
                    )
                case InputMediaVideo():
                    await sender.upload_video()
                    sent = await sender.streaming_video_with_cover_fallback(
                        media_input(single.media),
                        caption=caption,
                        video_cover=single.video_cover,
                        duration=single.duration,
                        width=single.width,
                        height=single.height,
                        has_spoiler=single.has_spoiler,
                    )

        if sent and (cm := cache_media_from_message(sent)):
            media_list.append(cm)
    except Exception as e:
        logger.warning(f"上传失败 {e}, 使用兼容模式上传")
        await sender.upload_document()
        await sender.force_document(media_input(all_media[0].media), caption=caption)
        return None

    return media_list


def build_gif_button(media_refs: Sequence[AnyMediaRef]) -> Ikm:
    buttons = []
    n = 5
    for i, v in enumerate(media_refs):
        if isinstance(v, AniRef):
            buttons.append(Ikb(f"{i + 1}", url=v.url))
    ikbs = [list(i) for i in batched(buttons, n)]
    return Ikm(ikbs)


async def send_multi(
    sender: MessageSender,
    photos_videos: list[InputMediaPhoto | InputMediaVideo],
    animations: list[InputMediaAnimation],
    caption: str,
    media_refs: Sequence[AnyMediaRef],
    *,
    _t: PreLocaleSelector,
) -> list[CacheMedia] | None:
    """发送多个媒体（动图逐条、图片视频分批），返回 CacheMedia 列表。"""
    media_list: list[CacheMedia] = []
    not_cache = False
    if len([i for i in media_refs if isinstance(i, AniRef)]) > GIF_ONLY_SKIP_DOWNLOAD_COUNT_THRESHOLD:
        not_cache = True
        await sender.text(
            format_label(_t("GIF 过多跳过上传, 请自行下载")),
            reply_markup=build_gif_button(media_refs),
        )
    else:
        for ani in animations:
            await sender.upload_photo()
            caption_ = caption if ani == animations[-1] and not photos_videos else ""
            try:
                sent = await sender.animation(
                    media_input(ani.media), caption=caption_, has_spoiler=ani.has_spoiler
                )
            except Exception as e:
                logger.warning(f"上传失败 {e}, 使用兼容模式上传")
                not_cache = True
                await sender.upload_document()
                await sender.force_document(media_input(ani.media), caption=caption_)
            else:
                if sent and sent.document:
                    media_list.append(CacheMedia(type=CacheMediaType.DOCUMENT, file_id=sent.document.file_id))
                elif sent and sent.animation:
                    media_list.append(CacheMedia(type=CacheMediaType.ANIMATION, file_id=sent.animation.file_id))

    try:
        for batch in batched(photos_videos, 10):
            if batch[-1] == photos_videos[-1]:
                batch[0].caption = caption

            await sender.upload_photo()
            sent_msgs = await sender.media_group(list(batch))
            for m in sent_msgs:
                if cm := cache_media_from_message(m):
                    media_list.append(cm)
    except Exception as e:
        logger.warning(f"上传失败 {e}, 使用兼容模式上传")
        input_documents: list[InputMediaDocument] = [
            InputMediaDocument(media=media_input(item.media)) for item in photos_videos
        ]
        for document_batch in batched(input_documents, 10):
            if document_batch[-1] == input_documents[-1]:
                document_batch[-1].caption = caption

            await sender.upload_document()
            await sender.media_group(list(document_batch))
        return None

    return None if not_cache else media_list



