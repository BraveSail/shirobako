import asyncio
import shutil
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from functools import partial
from itertools import batched
from pathlib import Path
from tempfile import mkdtemp
from typing import TYPE_CHECKING, cast

from easy_ai18n import PreLocaleSelector
from parsehub.types import (
    AniRef,
    AnyMediaRef,
    AnyParseResult,
    BrokenMedia,
    LivePhotoFile,
)
from pyrogram import Client, enums
from pyrogram.errors import FloodWait, Forbidden, SlowmodeWait, WebpageCurlFailed
from pyrogram.types import (
    InlineKeyboardButton as Ikb,
)
from pyrogram.types import (
    InlineKeyboardMarkup as Ikm,
)
from pyrogram.types import (
    InputMediaDocument,
    InputRichBlock,
    InputRichBlockDocument,
    InputRichBlockParagraph,
    InputRichMessage,
    LinkPreviewOptions,
    Message,
    RichTextUrl,
)

from core import bs
from log import logger
from plugins.parse.covers import prepare_video_thumbs
from plugins.parse.inline_rich import (
    build_cached_rich_content,
    build_delivery_blocks,
    build_rich_media,
    cached_rich_message,
    extract_cache_media,
    rich_cache_entry,
)
from plugins.parse.notes import ParseNote, append_content_blocks
from repo.settings import SettingsConfig

if TYPE_CHECKING:
    # 仅为类型标注: reporters 反向依赖本模块的 MessageSender, 运行时不能互相导入
    from plugins.parse.reporters import MessageStatusReporter
from services import (
    CacheEntry,
    PipelineResult,
    persistent_cache,
)
from services.media import ProcessedMedia
from utils.helpers import pack_dir_to_tar_gz, to_list

logger = logger.bind(name="ParseSender")

_RICH_DOCUMENT_LIMIT = 50

MAX_RETRIES = 5


type PathType = str | Path


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

    async def _send_and_schedule_delete[T](
        self, send_coro_fn: Callable[[], Awaitable[T]], *, raise_cover_error: bool = False
    ) -> T:
        sent = await self._send(send_coro_fn, raise_cover_error=raise_cover_error)
        if isinstance(sent, Message) or isinstance(sent, list):
            self._delete_later(sent)
        return sent

    @staticmethod
    async def _send[T](send_coro_fn: Callable[[], Awaitable[T]], *, raise_cover_error: bool = False) -> T:
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
                if raise_cover_error and isinstance(e, WebpageCurlFailed):
                    raise  # The final rich sender retries without the cover.
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

    async def rich_message(
        self,
        rich_message: InputRichMessage,
        *,
        reply_markup: Ikm | None = None,
        raise_cover_error: bool = False,
    ) -> Message:
        return cast(
            Message,
            await self._send_and_schedule_delete(
                partial(
                    self.msg.reply_rich if self.config.reply_msg else self.msg.answer_rich,
                    rich_message=rich_message,
                    reply_markup=reply_markup,
                ),
                raise_cover_error=raise_cover_error,
            ),
        )


def _file_block(path: PathType) -> InputRichBlockDocument:
    return InputRichBlockDocument(InputMediaDocument(media=Path(path)))


def _original_file_blocks(result: PipelineResult, *, attach_files: bool) -> Iterator[InputRichBlock]:
    # Keep failed downloads as links, not as preview media slots (especially for ZIP).
    refs = to_list(result.parse_result.media)
    for i, processed in enumerate(result.processed_list):
        source = processed.source
        if isinstance(source, BrokenMedia):
            url = source.url or (refs[i].url if i < len(refs) else "")
            if url:
                yield InputRichBlockParagraph(RichTextUrl(url, url))
        elif attach_files:
            for path in processed.output_paths or [source.path]:
                yield _file_block(path)
            if isinstance(source, LivePhotoFile) and source.video_path:
                yield _file_block(source.video_path)
    for ref in refs[len(result.processed_list):]:
        if isinstance(ref, AniRef) and ref.url:
            yield InputRichBlockParagraph(RichTextUrl(ref.url, ref.url))


async def send_raw(
    sender: MessageSender,
    result: PipelineResult,
    reporter: "MessageStatusReporter",
    *,
    _t: PreLocaleSelector,
    custom_content: str | ParseNote = "",
    spoiler_tag: str = "",
) -> None:
    """Raw 模式：将文件以原始文档形式上传。"""
    logger.debug("Raw 模式, 直接上传文件")
    display_result = result.display_result or result.parse_result
    try:
        await reporter.report_result(display_result, _t("上 传 中..."))
        refs = to_list(result.parse_result.media)
        # Inline buttons are outside the rich blocks, so don't expose GIF links with a spoiler.
        markup = (
            build_gif_button(refs)
            if not spoiler_tag and len(refs) > 1 and any(isinstance(ref, AniRef) for ref in refs)
            else None
        )
        await sender.upload_document()
        await send_rich_media(
            sender, display_result, [], _t=_t, custom_content=custom_content, reporter=reporter,
            spoiler_tag=spoiler_tag, reply_markup=markup,
            file_blocks=tuple(_original_file_blocks(result, attach_files=True)),
        )
    except Exception as e:
        logger.opt(exception=e).debug("详细堆栈")
        logger.error(f"Raw 模式上传失败: {e}")
        await reporter.report_error(_t("上传"), e)
        return
    finally:
        result.cleanup()


async def send_zip(
    sender: MessageSender,
    result: PipelineResult,
    reporter: "MessageStatusReporter",
    *,
    _t: PreLocaleSelector,
    custom_content: str | ParseNote = "",
    spoiler_tag: str = "",
) -> None:
    logger.debug("Zip 模式, 开始打包")
    display_result = result.display_result or result.parse_result
    archive_dir: Path | None = None
    try:
        try:
            await reporter.report_result(display_result, _t("打 包 中..."))
            if (source := result.output_dir) is None:
                raise ValueError("缺少打包目录")
            # The source directory can be reused as soon as cleanup finishes;
            # the upload must own a separate archive directory until it ends.
            archive_dir = Path(mkdtemp(prefix="parsehub-archive-", dir=source.parent))
            destination = archive_dir / source.with_suffix(".tar.gz").name
            packer = asyncio.create_task(asyncio.to_thread(pack_dir_to_tar_gz, source, destination))
            try:
                pack_path = await asyncio.shield(packer)
            except asyncio.CancelledError:
                # Cancelling to_thread does not stop the archive writer. Keep
                # both directories until it exits, even on repeated cancel.
                while not packer.done():
                    try:
                        await asyncio.shield(packer)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not packer.cancelled():
                    packer.exception()
                raise
        except Exception as e:
            logger.opt(exception=e).debug("详细堆栈")
            logger.error(f"打包失败: {e}")
            await reporter.report_error(_t("打包"), Exception("..."))
            return
        finally:
            result.cleanup()

        await reporter.report_result(display_result, _t("上 传 中..."))
        await sender.upload_document()
        await send_rich_media(
            sender, display_result, [], _t=_t, custom_content=custom_content, reporter=reporter,
            spoiler_tag=spoiler_tag,
            file_blocks=(_file_block(pack_path), *_original_file_blocks(result, attach_files=False)),
        )
    except Exception as e:
        logger.opt(exception=e).debug("详细堆栈")
        logger.error(f"上传失败: {e}")
        await reporter.report_error(_t("上传"), e)
    finally:
        if archive_dir is not None and not bs.debug_skip_cleanup:
            shutil.rmtree(archive_dir, ignore_errors=True)


async def send_cached(
    sender: MessageSender,
    entry: CacheEntry,
    url: str,
    *,
    custom_content: str | ParseNote = "",
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


def _attachment_batches(blocks: Sequence[InputRichBlock]) -> Iterator[list[InputRichBlock]]:
    chunk: list[InputRichBlock] = []
    documents = 0
    for block in blocks:
        if isinstance(block, InputRichBlockDocument):
            if documents == _RICH_DOCUMENT_LIMIT:
                yield chunk
                chunk, documents = [], 0
            documents += 1
        chunk.append(block)
    yield chunk


async def send_rich_media(
    sender: MessageSender,
    parse_result: AnyParseResult,
    processed_list: list[ProcessedMedia],
    *,
    _t: PreLocaleSelector,
    custom_content: str | ParseNote = "",
    raw_url: str = "",
    reporter: "MessageStatusReporter | None" = None,
    spoiler_tag: str = "",
    cache_parse_result: AnyParseResult | None = None,
    reply_markup: Ikm | None = None,
    file_blocks: Sequence[InputRichBlock] = (),
) -> bool:
    """以富文本 (rich message) 发送解析结果: 正文保留原文格式, 统计与来源进页尾。

    :param spoiler_tag: 用户手动要求遮住内容 (链接后跟 ``/s``) —— 正文折成
        ``<details><summary>⚠️</summary>``, 不留预览。
    :param file_blocks: 原文件及下载失败链接，不充当正文媒体槽。附件分批，不写媒体缓存。

    给了 ``reporter`` 且它已经发过状态消息时, 结果会**编辑进那条状态消息**
    (整个流程只留一条消息, 没有删除记录、没有多余临时消息); 否则新建一条。

    发送后把服务端返回的媒体 file_id 写回缓存 —— 下次同一链接零上传直发,
    inline 也能直接带媒体 (不用二次编辑)。
    """
    media_refs = to_list(parse_result.media)
    # 视频封面要本地文件: 富文本的 document 只带 id, 远端 cover 会被丢掉
    video_thumbs = (
        await prepare_video_thumbs(media_refs, platform=parse_result.platform)
        if sender.config.video_cover and processed_list
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
    blocks = build_delivery_blocks(
        parse_result,
        config=sender.config,
        raw_url=parse_result.raw_url or raw_url,
        lang=_t.locale,
        view_label=_t("查看"),
        custom_content=custom_content,
        media_placeholders=placeholders,
        quote_media_placeholders=quoted_placeholders,
        reply_media_placeholders=reply_placeholders,
        media_blocks=media_blocks,
        media_urls=media_urls,
        hide_content=spoiler_tag,
    )
    batches = _attachment_batches(file_blocks)
    attachments = next(batches)
    append_content_blocks(blocks, attachments, spoiler_tag)
    logger.debug(f"富文本(blocks): media={len(media_blocks)}, blocks={len(blocks)}")
    rich = InputRichMessage(blocks=blocks)

    message = await _post_rich(sender, rich, reporter=reporter, reply_markup=reply_markup)
    # Only the first batch replaces the status message. Overflow attachments
    # inherit the spoiler fold but do not repeat the body or reply keyboard.
    for attachments in batches:
        extra: list[InputRichBlock] = []
        append_content_blocks(extra, attachments, spoiler_tag)
        try:
            await _post_rich(sender, InputRichMessage(blocks=extra))
        except Exception as exc:
            if reporter is None:
                raise
            logger.warning("后续文件批次上传失败，保留已发结果: {}", type(exc).__name__)
            await reporter.report_error(_t("上传"), exc, keep_result=True)
            return False

    if raw_url and media and not file_blocks:
        cached_media = extract_cache_media(getattr(message, "rich_message", None))
        is_document = getattr(parse_result, "document", None) is not None
        # A document may display one downloaded image several times. The mapper
        # checks the order, identities and coverage; the old positional path
        # still requires a strict one-to-one count.
        expected = len(media)
        if is_document or len(cached_media) == expected:
            # 缓存里媒体是平铺的: 末尾 N 项属于被引用块、其前 M 项属于被回复块
            # (用占位符数量推, 两者一一对应)
            quoted_items = min(len(quoted_placeholders), len(cached_media))
            reply_items = min(len(reply_placeholders), len(cached_media) - quoted_items)
            render_options = {"rendered_blocks": rich.blocks} if is_document else {}
            entry = rich_cache_entry(
                cache_parse_result or parse_result, cached_media,
                quoted_media_count=quoted_items, reply_media_count=reply_items,
                polls=getattr(parse_result, "polls", None), urls=media_urls, **render_options,
            )
            if entry is not None:
                await persistent_cache.set(raw_url, entry)
                logger.debug(f"富文本媒体已写入缓存: count={len(entry.media or [])}")
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
    reply_markup: Ikm | None = None,
) -> Message:
    """发布富文本结果: 优先编辑状态消息, 不行再新建。

    编辑失败 (状态消息被用户手动删掉、内容被判为未变更等) 时退回新建 ——
    结果不能因为"收尾方式"失败而丢失。
    """
    from plugins.parse.inline_rich import strip_video_cover

    markup_options = {} if reply_markup is None else {"reply_markup": reply_markup}

    async def _send(r: InputRichMessage) -> Message:
        if reporter is not None and reporter.has_message:
            try:
                if (edited := await reporter.finalize(r, **markup_options)) is not None:
                    if getattr(sender, "delete_after_seconds", None) is not None:
                        sender._delete_later(edited)
                    return edited
            except WebpageCurlFailed:
                raise  # 交给外层"去掉封面重试", 别退化成新建一条
            except Exception as e:
                logger.warning(f"编辑状态消息失败, 改为新建结果: {type(e).__name__}: {e}")
        return await sender.rich_message(rich_message=r, raise_cover_error=True, **markup_options)

    try:
        return await _send(rich)
    except WebpageCurlFailed as e:
        # 服务端抓不到封面 URL 时会拒收整条 —— 去掉封面重发 (普通发送路径一直这么做,
        # 富文本路径当初漏了这一步)。
        logger.warning(f"服务端抓不到封面, 去掉封面重试: {str(e)[:90]}")
        return await _send(strip_video_cover(rich))


def build_gif_button(media_refs: Sequence[AnyMediaRef]) -> Ikm:
    buttons = []
    n = 5
    for i, v in enumerate(media_refs):
        if isinstance(v, AniRef):
            buttons.append(Ikb(f"{i + 1}", url=v.url))
    ikbs = [list(i) for i in batched(buttons, n)]
    return Ikm(ikbs)


