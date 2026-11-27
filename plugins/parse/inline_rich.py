"""富文本 inline 的二次替换: 选中后把占位消息编辑成带媒体的完整富文本。

inline **回答查询时**不能带外部媒体 (Telegram 直接回 `EXTERNAL_MEDIA_NOT_SUPPORTED`),
所以占位结果只发文字; 用户选中后再走「下载 → 上传 → 编辑」: 用
`messages.EditInlineBotMessage(rich_message=...)` 把同一条消息换成带媒体的富文本。
这条编辑路径的媒体字段要的是**已上传的** photo/document (pyrogram 的
`InputRichMessageMedia.write` 内部用 `messages.UploadMedia`, peer=self)。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from parsehub.types import AniFile, AnyMediaRef, BrokenMedia, ImageFile, LivePhotoFile, VideoFile
from parsehub.types.document import Block, visible_text
from pyrogram import raw, utils
from pyrogram.types import (
    InputMediaAnimation,
    InputMediaPhoto,
    InputMediaVideo,
    InputRichBlockAnimation,
    InputRichBlockParagraph,
    InputRichMessage,
    InputRichMessageMedia,
    RichTextUrl,
)

from log import logger
from plugins.parse.notes import ParseNote, append_content_blocks, note_block

if TYPE_CHECKING:
    from pyrogram import Client

    from services.media import ProcessedMedia

logger = logger.bind(name="InlineRichEdit")

#: 媒体类型 -> tg:// 占位符的 kind
RICH_MEDIA_KIND: dict[type, str] = {
    ImageFile: "photo",
    VideoFile: "video",
    LivePhotoFile: "video",
    AniFile: "video",
}

#: 富文本结果项的 result_id (选中回调靠它区分)
RICH_RESULT_ID = "rich"


class _MediaPlaceholder(str):
    """Legacy placeholder plus native media identity; never parse it back as Markdown."""

    url: str
    media_id: str
    ref_index: int | None

    def __new__(cls, value: str, url: str, media_id: str = "", *, ref_index: int | None = None):
        item = super().__new__(cls, value)
        item.url = url
        item.media_id = media_id
        item.ref_index = ref_index
        return item


class _MediaURLs(list[str]):
    """Legacy body URL list plus the complete source layout for structured cache writes."""

    def __init__(
        self, main: list[_MediaPlaceholder], reply: list[_MediaPlaceholder], quoted: list[_MediaPlaceholder],
        *, ref_count: int | None = None,
    ):
        super().__init__(item.url for item in main)
        self.main = main
        self.reply = reply
        self.quoted = quoted
        self.ref_count = ref_count


class _RenderedCacheMedia(list):
    """File IDs extracted in rendered order rather than upload/source order."""


def _has_native_body(parse_result: object) -> bool:
    """An empty Document is intentional; XStatus alone is the v14 compatibility path."""
    return getattr(parse_result, "document", None) is not None or getattr(parse_result, "x_status", None) is not None


def has_structured_body(parse_result: object) -> bool:
    """Route bodies and legacy structured polls through the native delivery builder."""
    return _has_native_body(parse_result) or bool(getattr(parse_result, "polls", None))


def _summary_blocks(blocks: Sequence[Block]) -> Iterator[Block]:
    for block in blocks:
        if block.kind in {"media", "poll", "localize"} or (block.kind == "quote" and block.role):
            continue
        if block.children or block.items:
            block = replace(
                block,
                children=tuple(_summary_blocks(block.children)),
                items=tuple(tuple(_summary_blocks(item)) for item in block.items),
            )
        yield block


def result_summary_text(parse_result: object) -> str | None:
    """Literal main-body text, excluding attachments and contextual cards."""
    status = getattr(parse_result, "x_status", None)
    if (document := getattr(parse_result, "document", None)) is not None:
        if x_body := tuple(block for block in document.blocks if block.role == "x_body"):
            return visible_text(x_body)
        youtube = status.main.youtube if status is not None else None
        return visible_text(_summary_blocks(tuple(
            block for block in document.blocks
            if not (youtube and block.kind == "paragraph" and block.runs == (youtube,))
        )))
    if status is not None:
        return "".join(run.text for run in status.main.segments)
    return getattr(parse_result, "content", None)


def _trending_cover_url(parse_result) -> str:
    if (url := getattr(parse_result, "x_trending_cover_url", "")):
        return url
    status = getattr(parse_result, "x_status", None)
    index = status.trending.cover_index if status and status.trending else None
    refs = getattr(parse_result, "media", None)
    if isinstance(refs, Sequence) and index is not None and 0 <= index < len(refs):
        return str(getattr(refs[index], "url", "") or "")
    return ""


def _split_trending_media(main, cover_url: str, cover_index: int | None):
    # The cover is one image ref appended to main before the reply/quote tails.
    # On cache hits the original index is no longer meaningful; select its actual URL.
    for i in range(len(main) - 1, -1, -1):
        item = main[i]
        if isinstance(item, _MediaPlaceholder) and cover_url and item.url == cover_url and (
            item.ref_index is None or item.ref_index == cover_index
        ):
            return [*main[:i], *main[i + 1:]], [item]
    return main, []


def _resolve_media_placeholders(
    placeholders: Sequence[str], blocks: Mapping[str, Any], *, sensitive: bool,
) -> list[tuple[int | None, str, Any]]:
    from plugins.parse.rich_blocks import SpoilerVideoBlock

    resolved = []
    for item in placeholders:
        if not isinstance(item, _MediaPlaceholder):
            continue
        if item.media_id:
            if (block := blocks.get(item.media_id)) is None:
                continue
            # The SDK animation writer emits PageBlockVideo without its spoiler flag.
            if sensitive and isinstance(block, InputRichBlockAnimation):
                block = SpoilerVideoBlock(block.animation, caption=block.caption)
        elif item.url:
            block = InputRichBlockParagraph(RichTextUrl(item.url, item.url))
        else:
            continue
        resolved.append((item.ref_index, item.url, block))
    return resolved


def build_delivery_blocks(
    parse_result,
    *,
    config,
    raw_url: str = "",
    lang: str = "",
    view_label: str = "",
    custom_content: str | ParseNote = "",
    hide_content: str = "",
    progress: str = "",
    media_placeholders: Sequence[str] = (),
    quote_media_placeholders: Sequence[str] = (),
    reply_media_placeholders: Sequence[str] = (),
    media_blocks: dict | None = None,
    media_urls: Sequence[str] = (),
    omit_legacy_media: bool = False,
) -> list:
    """Render a Document first; keep the legacy paths for existing cached results."""
    document = getattr(parse_result, "document", None)
    status = getattr(parse_result, "x_status", None)
    if document is not None or status is not None:
        blocks = media_blocks or {}
        sensitive = bool(getattr(parse_result, "is_sensitive", False))

        def resolve(placeholders):
            return _resolve_media_placeholders(placeholders, blocks, sensitive=sensitive)

        render_options = {
            "raw_url": raw_url, "lang": lang, "view_label": view_label,
            "custom_content": custom_content, "hide_content": hide_content, "progress": progress,
        }
        if document is not None:
            from plugins.parse.document_render import build_document_blocks

            return build_document_blocks(
                parse_result, config=config, **render_options, main_media=resolve(media_placeholders),
                reply_media=resolve(reply_media_placeholders), quoted_media=resolve(quote_media_placeholders),
            )

        from plugins.parse.x_status import build_x_status_blocks

        def legacy_blocks(placeholders):
            return [block for _, _, block in resolve(placeholders)]

        assert status is not None
        main, trending = _split_trending_media(
            media_placeholders, _trending_cover_url(parse_result),
            status.trending.cover_index if status.trending else None,
        )
        return build_x_status_blocks(
            parse_result, config=config, **render_options, main_media=legacy_blocks(main),
            reply_media=legacy_blocks(reply_media_placeholders), quoted_media=legacy_blocks(quote_media_placeholders),
            trending_media=legacy_blocks(trending),
        )

    from plugins.helpers import build_rich_markdown
    from plugins.parse.rich_blocks import LegacyPollSlots, markdown_to_blocks

    polls = getattr(parse_result, "polls", None)
    poll_slots = LegacyPollSlots(polls, lang) if polls else None
    markdown = build_rich_markdown(
        parse_result, config=config, lang=lang, view_label=view_label,
        custom_content=custom_content if isinstance(custom_content, str) else "",
        hide_content=hide_content, progress=progress, media_placeholders=media_placeholders,
        quote_media_placeholders=quote_media_placeholders, reply_media_placeholders=reply_media_placeholders,
        media_urls=media_urls, defer_polls=poll_slots.reserve if poll_slots is not None else None,
    )
    if omit_legacy_media:
        # inline imports this module; only import its legacy helper when rendering a preview.
        from plugins.parse.inline import strip_media_markdown

        markdown = strip_media_markdown(markdown)
    rendered = markdown_to_blocks(markdown, media_blocks=media_blocks, poll_slots=poll_slots)
    if isinstance(custom_content, ParseNote) and custom_content:
        append_content_blocks(rendered, [note_block(custom_content)], hide_content)
    return rendered


def build_rich_media(
    media_refs: Sequence[AnyMediaRef],
    processed_list: list[ProcessedMedia],
    *,
    video_cover: bool = True,
    is_sensitive: bool = False,
    video_thumbs: Mapping[str, Path] | None = None,
    quoted_media_count: int = 0,
    reply_media_count: int = 0,
) -> tuple[list[InputRichMessageMedia], list[str], dict[str, Any], list[str], list[str], list[str]]:
    """把已下载的媒体构造成富文本 media 块 + 正文占位符 + (打码用的) blocks 映射。

    媒体用下载好的本地文件上传 (``send_rich_message`` 内部走 ``messages.UploadMedia``),
    不把原始 URL 交给 Telegram 抓取 —— 那条路有 20 MB 上限。

    直发与 inline 编辑**共用这一份**, 免得两边各写一遍。

    :param video_thumbs: 视频封面 URL -> 已下载好的本地图片 (见 ``covers.prepare_video_thumbs``)。
        必须是本地文件: 富文本路径只上传 document 本身, 远端 cover 参数会被丢弃。
    :param quoted_media_count: ``media_refs`` 末尾有多少个属于**被引用内容**。
    :param reply_media_count: 紧接在被引用媒体之前的多少个属于**被回复内容**。
        两者的占位符分别作为第四、五个返回值给出, 由调用方放进对应的引用块内部。
    :return: ``(media, placeholders, media_blocks, quoted_placeholders, reply_placeholders, media_urls)``
        —— ``media_urls`` 与 ``placeholders`` **平铺对齐** (同一个 ref 展开成多份时
        共用它的 URL), 渲染层用它把正文外链图换成上传媒体引用
        (见 ``helpers.rewrite_external_images``)。
    """
    from plugins.parse.rich_blocks import SpoilerPhotoBlock, SpoilerVideoBlock
    from services.media import resolve_media_info

    thumbs = video_thumbs or {}
    media: list[InputRichMessageMedia] = []
    media_blocks: dict[str, Any] = {}
    index = 0
    # 每个 ref 产出的占位符 (一个 ref 可能展开成多个文件), 供末尾切分引用帖媒体
    per_ref: list[list[_MediaPlaceholder]] = []

    for ref_index, (media_ref, processed) in enumerate(zip(media_refs, processed_list, strict=False)):
        group: list[_MediaPlaceholder] = []
        per_ref.append(group)
        ref_url = str(getattr(media_ref, "url", "") or "")

        # **下载失败的媒体降级成链接**（`_do_download` 里内容不是图片时用
        # `BrokenMedia` 占位、带原 URL）。必须**保留一个占位符**，
        # 否则后面的项整体错位、把 A 图显示到 B 的位置。
        # 用户口径：下载失败保留 URL 就行。
        fallback_url = str(getattr(processed.source, "url", "") or "")
        if isinstance(processed.source, BrokenMedia) or not str(getattr(processed.source, "path", "") or ""):
            fallback = fallback_url or ref_url
            text = f"[{fallback}]({fallback})" if fallback else ""
            group.append(_MediaPlaceholder(text, fallback, ref_index=ref_index))
            continue

        file_paths = processed.output_paths or [processed.source.path]
        for file_path in file_paths:
            file_path_str = str(file_path)
            width, height, duration = resolve_media_info(processed, file_path_str)
            kind = RICH_MEDIA_KIND.get(type(processed.source))
            if kind is None:
                continue

            match processed.source:
                case ImageFile():
                    item = InputMediaPhoto(media=file_path_str, has_spoiler=is_sensitive)
                case AniFile():
                    item = InputMediaAnimation(media=file_path_str, has_spoiler=is_sensitive)
                case VideoFile():
                    thumb_url = str(getattr(media_ref, "thumb_url", "") or "")
                    item = InputMediaVideo(
                        media=file_path_str,
                        has_spoiler=is_sensitive,
                        thumb=thumbs.get(thumb_url),
                        video_cover=thumb_url if video_cover else None,
                        duration=duration,
                        width=width,
                        height=height,
                        supports_streaming=True,
                    )
                case LivePhotoFile():
                    # 实况照片本身就是一张图 + 一段视频, 用图片那半当封面最贴切
                    item = InputMediaVideo(
                        media=processed.source.video_path,
                        has_spoiler=is_sensitive,
                        thumb=thumbs.get(str(getattr(media_ref, "thumb_url", "") or "")),
                        video_cover=file_path_str if video_cover else None,
                        duration=duration,
                        width=width,
                        height=height,
                        supports_streaming=True,
                    )
                case _:
                    continue

            media_id = f"m{index}"
            index += 1
            # Wrapper blocks may be regrouped or recreated, retaining the same InputMedia.
            # The SDK writer ignores this local-only identity.
            item._parsehub_media_id = media_id
            media.append(InputRichMessageMedia(media_id, item))
            placeholder = _MediaPlaceholder(f"![](tg://{kind}?id={media_id})", ref_url, media_id, ref_index=ref_index)
            group.append(placeholder)
            # blocks 路径: 官方 API 的 InputRichBlockPhoto/Video 没有 spoiler, 自定义块才有
            match processed.source:
                case ImageFile():
                    media_blocks[media_id] = SpoilerPhotoBlock(item, spoiler=is_sensitive)
                case VideoFile() | LivePhotoFile():
                    media_blocks[media_id] = SpoilerVideoBlock(item, spoiler=is_sensitive)
                case AniFile():
                    media_blocks[media_id] = InputRichBlockAnimation(item)

    # refs 顺序是 [正文..., 被回复..., 被引用...]: 从末尾往前切出两个引用块
    quoted_count = max(0, min(quoted_media_count, len(per_ref)))
    tail_split = len(per_ref) - quoted_count
    reply_count = max(0, min(reply_media_count, tail_split))
    head_split = tail_split - reply_count

    def flat(groups: list[list[_MediaPlaceholder]]) -> list[_MediaPlaceholder]:
        return [p for ref_group in groups for p in ref_group]

    main = flat(per_ref[:head_split])
    quoted = flat(per_ref[tail_split:])
    reply = flat(per_ref[head_split:tail_split])
    return media, list(main), media_blocks, list(quoted), list(reply), _MediaURLs(
        main, reply, quoted, ref_count=len(media_refs),
    )


async def edit_inline_rich_message(
    cli: Client,
    inline_message_id: str,
    *,
    blocks: list,
) -> bool:
    """Edit the selected inline message with native blocks in its home DC."""
    unpacked = utils.unpack_inline_message_id(inline_message_id)
    session = await cli.get_session(unpacked.dc_id, is_media=True)
    rich = InputRichMessage(blocks=blocks)
    # 媒体上传发生在 write 里 (blocks 路径由各 block 自己上传); 封面 URL 抓不到时
    # 服务端会拒收整条 —— 去掉封面重发一次 (普通路径一直有这个兜底)。
    raw_rich = await with_cover_fallback(rich, lambda r: r.write(client=cli, chat_id=None))
    await session.invoke(
        raw.functions.messages.EditInlineBotMessage(
            id=unpacked,
            rich_message=raw_rich,
            reply_markup=raw.types.ReplyKeyboardHide(),
        )
    )
    return True


def cache_media_blocks(entry) -> tuple[list, list[str], list[str], list[str], dict, list[str]]:
    """把缓存里的 file_id 变成富文本的 media 块 + 正文/被引用/被回复三级占位符。

    用已存在的 file_id 走 ``InputMediaPhoto/Document``: pyrogram 的 ``_get_input_photo``
    遇到 ``InputMediaPhoto`` 直接返回它的 id, **不触发任何上传**, 所以是即时可用的。

    缓存的媒体是平铺的媒体项列表, 顺序与 ``media_refs`` 一致
    (``[正文..., 被回复..., 被引用...]``)。Document 先按源下标恢复坏链槽位，
    再按两个计数从末尾划分角色。

    :return: ``(media, placeholders, quoted_placeholders, reply_placeholders, media_blocks, urls)``
        —— ``urls`` 与**正文段**占位符平铺对齐（老缓存无 url 字段时为空列表）。

    **敏感内容 (``is_sensitive``) 额外产出一份 blocks 映射** —— 官方 API 的富文本媒体块
    没有 spoiler 字段, 只有 raw 的 ``PageBlockPhoto/Video(spoiler=)`` 才有, 所以敏感内容
    必须走 blocks 路径 (与直发路径 ``build_rich_media`` 同一套做法)。
    不产出的话**缓存命中的敏感内容就是没打码的** —— 而"第二次发同一个链接"恰恰总是命中缓存。
    """
    from pyrogram.types import (
        InputMediaAnimation,
        InputMediaDocument,
        InputMediaPhoto,
        InputMediaVideo,
    )

    from plugins.parse.rich_blocks import SpoilerPhotoBlock, SpoilerVideoBlock
    from services.cache import CacheMediaType

    pr = entry.parse_result
    native_document = getattr(pr, "document", None) is not None
    native_x = not native_document and getattr(pr, "x_status", None) is not None
    sensitive = bool(getattr(pr, "is_sensitive", False))
    media: list[InputRichMessageMedia] = []
    placeholders: list[_MediaPlaceholder] = []
    media_blocks: dict[str, Any] = {}
    for index, m in enumerate(entry.media or []):
        match m.type:
            case CacheMediaType.PHOTO:
                kind, item = "photo", InputMediaPhoto(m.file_id)
            case CacheMediaType.VIDEO:
                # 缓存里存了封面的 file_id 就用上 —— 否则"第二次发同一个链接"视频没封面
                # (第一次直发是本地 thumb, 缓存这条路以前把它丢了)。
                kind, item = "video", InputMediaVideo(m.file_id, thumb=m.cover_file_id or None)
            case CacheMediaType.ANIMATION:
                kind, item = "video", InputMediaAnimation(m.file_id)
            case CacheMediaType.DOCUMENT:
                kind, item = "document", InputMediaDocument(m.file_id)
            case _:
                continue
        media_id = f"m{index}"
        media.append(InputRichMessageMedia(media_id, item))
        placeholders.append(_MediaPlaceholder(
            f"![](tg://{kind}?id={media_id})", getattr(m, "url", "") or "", media_id,
            ref_index=getattr(m, "ref_index", None) if native_document else None,
        ))
        # 映射**总是**产出 (spoiler 由 sensitive 决定): 需要它的不只有打码 ——
        # "折叠的引用卡片带媒体"也必须走 blocks 才能把图留在引用块内
        # (见 helpers.markdown_needs_blocks)。
        match m.type:
            case CacheMediaType.PHOTO:
                media_blocks[media_id] = SpoilerPhotoBlock(item, spoiler=sensitive)
            case CacheMediaType.ANIMATION if (native_document or native_x) and not sensitive:
                media_blocks[media_id] = InputRichBlockAnimation(item)
            case CacheMediaType.VIDEO | CacheMediaType.ANIMATION:
                media_blocks[media_id] = SpoilerVideoBlock(item, spoiler=sensitive)

    if native_document:
        placeholders.extend(
            _MediaPlaceholder(f"[{url}]({url})", url, ref_index=index)
            for index, url in pr.failed_media.items()
        )
        # Stable sort preserves split outputs of one ref while restoring the failed slots.
        placeholders.sort(key=lambda item: (item.ref_index is None, item.ref_index or 0))
    elif native_x:
        for position, url in sorted((getattr(pr, "x_failed_media", None) or {}).items()):
            placeholders.insert(position, _MediaPlaceholder(f"[{url}]({url})", url))
    quoted_count = max(0, min(int(getattr(pr, "quoted_media_count", 0) or 0), len(placeholders)))
    tail_split = len(placeholders) - quoted_count
    reply_count = max(0, min(int(getattr(pr, "reply_media_count", 0) or 0), tail_split))
    head_split = tail_split - reply_count
    # 原始 URL 表（与占位符平铺对齐）: 老缓存没有 url 字段 ⇒ 全是空串，正文外链图
    # 在渲染时会被剥掉（图仍在末尾出现，只是不在原位）。
    urls_flat = [str(getattr(m, "url", "") or "") for m in (entry.media or [])]
    return (
        media,
        list(placeholders[:head_split]),
        list(placeholders[tail_split:]),
        list(placeholders[head_split:tail_split]),
        media_blocks,
        urls_flat[: head_split] if len(urls_flat) == len(placeholders) else [],
    )


def _poll_to_cache_dicts(polls) -> list[dict]:
    """把 ``result.polls`` 序列化成缓存用的 dict 列表。

    字段名与 lib 的 ``asdict(Poll)`` 对齐（协议已冻结, 见计划文件）：
    ``options[{text, count}]`` / ``total_votes`` / ``voters`` / ``title`` / ``is_finished``。

    **显式逐个取字段**（而不是 ``dataclasses.asdict``）：lib 的 ``Poll`` 可能是
    dataclass、dict 或 duck-type, 结果里也允许直接塞 dict（渲染协议同构）。
    拿不到就填默认值 —— 少一个字段不该让整条缓存写失败。
    """
    from plugins.helpers import _poll_field  # 与渲染层共用同一套字段读取

    out: list[dict] = []
    for poll in polls or []:
        options = [
            {
                "text": str(_poll_field(option, "text") or ""),
                "count": _poll_field(option, "count"),
            }
            for option in (_poll_field(poll, "options") or [])
        ]
        out.append(
            {
                "options": options,
                "total_votes": _poll_field(poll, "total_votes"),
                "voters": _poll_field(poll, "voters"),
                "title": str(_poll_field(poll, "title") or ""),
                "is_finished": bool(_poll_field(poll, "is_finished", False)),
            }
        )
    return out


def _cache_x_media(media, urls: _MediaURLs, cover_url: str, cover_index: int | None):
    """Write file IDs in source role order, preserving links for failed downloads."""
    main, trending = _split_trending_media(urls.main, cover_url, cover_index)
    source = [*urls.main, *urls.reply, *urls.quoted]
    # Native delivery emits reply and trending before the main gallery; inline pre-upload
    # already gives source order. Pair IDs before assigning URLs or replaying the roles.
    items = [*urls.reply, *trending, *main, *urls.quoted] if isinstance(media, _RenderedCacheMedia) else source
    uploaded = [item for item in items if item.media_id]
    if len(media) != len(uploaded):
        return None
    by_id = {
        item.media_id: cached.model_copy(update={"url": item.url})
        for item, cached in zip(uploaded, media, strict=True)
    }
    return (
        [by_id[item.media_id] for item in source if item.media_id],
        {i: item.url for i, item in enumerate(source) if not item.media_id},
        len(urls.quoted),
        len(urls.reply),
    )


def _walk_rich_blocks(blocks: Sequence[Any] | None) -> Iterator[Any]:
    """Depth-first wire order shared by sent blocks and the SDK response."""
    for block in blocks or ():
        yield block
        for attr in ("blocks", "items"):
            children = getattr(block, attr, None)
            if children is not None and not isinstance(children, (str, int)):
                yield from _walk_rich_blocks(children if isinstance(children, (list, tuple)) else (children,))


def _rendered_document_media_ids(blocks):
    from services.cache import CacheMediaType

    media_types = (
        ("photo", CacheMediaType.PHOTO), ("video", CacheMediaType.VIDEO),
        ("animation", CacheMediaType.ANIMATION), ("document", CacheMediaType.DOCUMENT),
    )
    for block in _walk_rich_blocks(blocks):
        for attr, kind in media_types:
            if (media := getattr(block, attr, None)) is not None:
                # Sensitive animations are rendered via the spoiler-preserving video block.
                if attr == "video" and isinstance(media, InputMediaAnimation):
                    kind = CacheMediaType.ANIMATION
                yield getattr(media, "_parsehub_media_id", None), kind
                break


def _same_cached_media(first, second) -> bool:
    if first.file_id == second.file_id:
        return True
    # A duplicate can have a refreshed file_reference but must still be the same object.
    from pyrogram.file_id import FileId

    def identity(encoded):
        fid = FileId.decode(encoded)
        return fid.file_type, fid.dc_id, fid.media_id, fid.access_hash

    try:
        return identity(first.file_id) == identity(second.file_id)
    except Exception:  # An unrecognizable ID cannot prove that two placements are equivalent.
        return False


def _cache_document_media(media, urls: _MediaURLs, rendered_blocks):
    """Pair IDs with the sent leaves, then restore source order for cache replay."""
    source = [*urls.main, *urls.reply, *urls.quoted]
    uploaded = [item for item in source if item.media_id]
    source_ids = {item.media_id for item in uploaded}
    source_indices = {item.ref_index for item in source}
    if len(source_ids) != len(uploaded) or None in source_indices:
        return None
    if urls.ref_count is not None and source_indices != set(range(urls.ref_count)):
        return None
    if isinstance(media, _RenderedCacheMedia):
        if rendered_blocks is None:
            return None
        order = list(_rendered_document_media_ids(rendered_blocks))
        if len(order) != len(media) or {media_id for media_id, _ in order} != source_ids:
            return None
    else:
        if len(uploaded) != len(media):
            return None
        order = [(item.media_id, row.type) for item, row in zip(uploaded, media, strict=True)]

    by_id = {}
    for (media_id, kind), row in zip(order, media, strict=True):
        if row.type != kind or ((prior := by_id.get(media_id)) is not None and not _same_cached_media(prior, row)):
            return None
        by_id[media_id] = row
    return (
        [by_id[item.media_id].model_copy(update={"url": item.url, "ref_index": item.ref_index}) for item in uploaded],
        {item.ref_index: item.url for item in source if not item.media_id and item.ref_index is not None},
        len(urls.quoted),
        len(urls.reply),
    )


def rich_cache_entry(
    parse_result,
    media: list,
    *,
    quoted_media_count: int = 0,
    reply_media_count: int = 0,
    polls: list | None = None,
    urls: list[str] | None = None,
    rendered_blocks: Sequence | None = None,
):
    """把一次富文本发送的字段与媒体 file_id 收成缓存条目。

    ``quoted_media_count`` 记的是**媒体项数** (缓存里平铺的条数), 不是 ref 数
    —— 一个 ref 可能展开成多个文件, 缓存只能按项切分。

    ``polls`` 是**投票数据** (``result.polls`` 的序列化) —— 正文里的 ``{{poll:N}}``
    占位符要靠它才能渲染; 不传 (老调用点/没有投票) 时按空处理, 渲染层不替换。

    ``urls`` 是**与媒体项平铺对齐**的原始 URL 表 (``build_rich_media`` 第六个返回值)
    —— 缓存命中时正文外链图靠它换成上传媒体引用; 不传则那些图只能剥掉。

    ``rendered_blocks`` is required for a Document's post-send IDs; pre-uploaded
    IDs are already in source order. Return None if that association is ambiguous.

    放在这里而不是发送层: 私聊/群 (``sender``) 与 guest 都要写同一种条目。
    """
    # 两个都是 bot 侧模块, 函数内延迟导入避免循环依赖 (与上面的 helper 同一手法)
    from plugins.helpers import get_parse_author_name
    from services.cache import CONTENT_VERSION, FOOTER_META_FIELDS_VERSION, CacheEntry, CacheParseResult

    document = getattr(parse_result, "document", None)
    status = getattr(parse_result, "x_status", None)
    cover_url = _trending_cover_url(parse_result) if status is not None else ""
    failed_media: dict[int, str] = {}
    x_failed_media: dict[int, str] = {}
    if document is not None:
        if isinstance(urls, _MediaURLs):
            mapped = _cache_document_media(media, urls, rendered_blocks)
            if mapped is None:
                logger.warning("Document media cache skipped: source and returned media cannot be matched")
                return None
            media, failed_media, quoted_media_count, reply_media_count = mapped
            urls = None
        elif media:
            logger.warning("Document media cache skipped: source media identities are missing")
            return None
    elif status is not None and isinstance(urls, _MediaURLs):
        mapped = _cache_x_media(
            media, urls, cover_url, status.trending.cover_index if status.trending else None,
        )
        if mapped is not None:
            media, x_failed_media, quoted_media_count, reply_media_count = mapped
            urls = None  # Already aligned and written using native identities.

    # 把 URL 灌进媒体项 (顺序一致; 数量不符时不硬灌, 让那几项留空 —— 退化为剥图)
    url_list = list(urls or [])
    if url_list and len(url_list) == len(media or []):
        media = [
            m.model_copy(update={"url": url}) if hasattr(m, "model_copy") else m
            for m, url in zip(media, url_list, strict=False)
        ]
    elif url_list:
        from log import logger as _logger

        _logger.warning(f"缓存媒体的 URL 数不符, 本次不写 URL: urls={len(url_list)}, media={len(media or [])}")

    return CacheEntry(
        parse_result=CacheParseResult(
            title=parse_result.title,
            content=parse_result.content,
            # **富文本正文的源**：RichText 平台的 ``content`` 是 markdown 转出来的纯文本,
            # 只存它会让缓存命中时引用块/链接全塌（见 CacheParseResult.markdown_content）。
            markdown_content=getattr(parse_result, "markdown_content", "") or "",
            document=document,
            x_status=status,
            x_trending_cover_url=cover_url,
            x_failed_media=x_failed_media,
            failed_media=failed_media,
            position_label=getattr(parse_result, "position_label", "") or "",
            topic_label=getattr(parse_result, "topic_label", "") or "",
            quote_roles=list(getattr(parse_result, "quote_roles", None) or []),
            quote_ids=dict(getattr(parse_result, "quote_ids", None) or {}),
            origin_line=getattr(parse_result, "origin_line", "") or "",
            reply_count=getattr(parse_result, "reply_count", None),
            hashtags=list(getattr(parse_result, "hashtags", None) or []),
            # 投票: 不存的话"第二次发同一条链接"占位符就会被静默删掉 (缓存命中路径
            # 没有数据可渲染)。默认参数只是兜底, 调用点应显式传 result.polls。
            polls=_poll_to_cache_dicts(polls if polls is not None else getattr(parse_result, "polls", None)),
            author_name=get_parse_author_name(parse_result),
            author_handle=getattr(parse_result, "author_handle", ""),
            author_url=getattr(parse_result, "author_url", ""),
            is_sensitive=parse_result.is_sensitive,
            published_at=getattr(parse_result, "published_at", None),
            view_count=getattr(parse_result, "view_count", None),
            like_count=getattr(parse_result, "like_count", None),
            # 页脚元数据写入版本（读取端低于当前版本即重新解析，见 CacheParseResult.meta_fields）
            meta_fields=FOOTER_META_FIELDS_VERSION,
            tags=list(getattr(parse_result, "tags", None) or []),
            # 平台短名也要存: 缓存路径渲染时用它拼标签页链接（漏了就不只是少个字段）
            platform=getattr(getattr(parse_result, "platform", None), "id", "") or "",
            quoted_media_count=quoted_media_count,
            reply_media_count=reply_media_count,
            # 板头图数量: 不存的话缓存命中时 banner 掉回正文中间（与现场解析不一致）
            banner_media_count=getattr(parse_result, "banner_media_count", 0) or 0,
            # 正文格式版本（twitter 的 richtext 粗体 / 区间换算修正让正文形态或取值变了,
            # 见 CacheParseResult 与 services.cache.CONTENT_VERSION）：
            # 写**当前版本**让这次之后写入的条目不再被判为旧格式。
            # ⚠️ 必须引用常量 —— 手抄字面量会在 bump 时漏掉这里, 导致新条目立刻又被判旧、
            # 无限重解析（测试 test_new_entries_carry_the_version 就是钉这个）。
            content_version=CONTENT_VERSION,
        ),
        media=media or None,
        rich=True,
    )


def _platform_from_id(platform_id: str):
    """按**短名**找平台枚举 (与库侧 serialize 同一手法)。

    ⚠️ 不能用 ``Platform(platform_id)``: 枚举成员的 value 是
    ``(短名, 显示名)`` 的 tuple, 按 value 构造**永远失败且静默**。
    """
    from parsehub.types import Platform

    return next((platform for platform in Platform if platform.id == platform_id), None)


def build_cached_rich_content(
    entry,
    raw_url: str,
    *,
    lang: str,
    config,
    view_label: str = "",
    custom_content: str | ParseNote = "",
    spoiler_tag: str = "",
    omit_media: bool = False,
) -> tuple[str | list, list[InputRichMessageMedia], dict]:
    """从缓存条目重建富文本正文与媒体块 (file_id 复用, 零上传)。

    **直发与 inline 共用这一条**, 免得两边各写一份排版:
    缓存里只存解析字段: Document、旧 XStatus、含结构化投票的旧正文输出 native blocks。

    :param spoiler_tag: 用户这次要求遮住内容 (``/s``)。缓存存的是**解析字段**
        而非渲染结果, 所以遮与不遮在排版时决定 —— 命中缓存也必须照遮。
    """
    media, placeholders, quoted_placeholders, reply_placeholders, media_blocks, media_urls = cache_media_blocks(entry)
    if omit_media:
        # Keep source slots for broken-link fallbacks but drop the Telegram media.
        media, media_blocks = [], {}
    if _has_native_body(entry.parse_result):
        blocks = build_delivery_blocks(
            entry.parse_result, config=config, raw_url=raw_url, lang=lang,
            view_label=view_label, custom_content=custom_content, hide_content=spoiler_tag,
            media_placeholders=placeholders, quote_media_placeholders=quoted_placeholders,
            reply_media_placeholders=reply_placeholders, media_blocks=media_blocks, media_urls=media_urls,
        )
        return blocks, media, media_blocks

    from plugins.helpers import build_rich_markdown_by_str
    from plugins.parse.rich_blocks import LegacyPollSlots, markdown_to_blocks

    # 平台: 优先用缓存里存的短名; 老缓存没这个字段 ⇒ 按 raw_url 推断。
    # **必须给一个真实平台** —— 渲染里标签等依赖它, 传 None 会静默降级成纯文本。
    platform = _platform_from_id(entry.parse_result.platform)
    if platform is None:
        from parsehub import ParseHub

        platform = ParseHub().get_platform(raw_url) if raw_url else None

    polls = entry.parse_result.polls
    poll_slots = LegacyPollSlots(polls, lang) if polls else None
    markdown = build_rich_markdown_by_str(
        entry.parse_result.title,
        entry.parse_result.content,
        raw_url,
        config=config,
        lang=lang,
        view_label=view_label,
        # 三个都是"渲染要用的解析字段": 少传一个就是缓存命中时那段格式消失
        markdown_content=entry.parse_result.markdown_content,
        position_label=entry.parse_result.position_label,
        topic_label=entry.parse_result.topic_label,
        quote_roles=entry.parse_result.quote_roles,
        origin_line=entry.parse_result.origin_line,
        reply_count=entry.parse_result.reply_count,
        hashtags=entry.parse_result.hashtags,
        defer_polls=poll_slots.reserve if poll_slots is not None else None,
        # 板头图数量: 漏传就是缓存命中时 banner 掉回正文中间
        banner_media_count=entry.parse_result.banner_media_count,
        author_name=entry.parse_result.author_name,
        author_handle=entry.parse_result.author_handle,
        author_url=entry.parse_result.author_url,
        published_at=entry.parse_result.published_at,
        view_count=entry.parse_result.view_count,
        like_count=entry.parse_result.like_count,
        tags=entry.parse_result.tags,
        custom_content=custom_content if isinstance(custom_content, str) else "",
        # 原位标记 (``{{media:N}}``) 的 N 是**原始占位符列表**的下标, 末尾追加
        # 才用 collage 合并 —— 所以两条通道分开传: 这里给原始列表, `build_rich_markdown`
        # 内部互换 (原位取原始, 追加走 collage)。
        media_placeholders=placeholders,
        quote_media_placeholders=quoted_placeholders,
        reply_media_placeholders=reply_placeholders,
        # 原始 URL (缓存里存的): 正文外链图换成上传媒体引用 —— 不传就全被剥掉
        media_urls=media_urls,
        hide_content=spoiler_tag,
        platform=platform,
    )
    note = custom_content if isinstance(custom_content, ParseNote) else None
    if poll_slots is None and not note:
        return markdown, media, media_blocks
    blocks = markdown_to_blocks(markdown, media_blocks=media_blocks, poll_slots=poll_slots)
    if note:
        append_content_blocks(blocks, [note_block(note)], spoiler_tag)
    return blocks, media, media_blocks


def cached_rich_message(
    markdown: str | list,
    media: list[InputRichMessageMedia],
    media_blocks: dict | None = None,
    *,
    sensitive: bool = False,
) -> InputRichMessage:
    """缓存命中路径的富文本构造 —— **一律走 blocks**（与直发/进度/inline 同一通道）。

    历史：这里按"是否敏感"二选一，敏感走 blocks（才能打码）、其余走 markdown 字符串。
    现在统一走 blocks，两个理由：

    - 平台原文在 markdown 字符串通道里会被解析（行首空格→代码块、``||``→spoiler），
      与最终消息不一致；
    - blocks 的媒体块本来就带 spoiler 参数，敏感与非敏感同一套代码。

    ``sensitive`` 参数保留但不再决定通道 —— 打码信息已经在 ``media_blocks`` 里了。
    """
    if isinstance(markdown, list):
        return InputRichMessage(blocks=markdown)

    from plugins.parse.rich_blocks import markdown_to_blocks

    return InputRichMessage(blocks=markdown_to_blocks(markdown, media_blocks=media_blocks))


async def upload_media_for_cache(
    cli: Client, media: list[InputRichMessageMedia], *, preserve_animations: bool = False
) -> list:
    """把 ``media`` 里的**本地文件**上传成 file_id, 就地换成 file_id 引用, 并返回可缓存的条目。

    为什么需要这一步:

    - inline 消息只能用 ``EditInlineBotMessage`` 更新, 而它**只回 Bool** —— 拿不到服务端
      生成的 file_id。所以 inline 路径**从来不写** file_id 缓存, 每次选中都要重新
      下载 + 上传 (直发路径能从返回的 Message 里提取, inline 不行)。
    - 常规做法是让 ``InputRichMessageMedia.write`` 内部上传, 那样 file_id 就留在库里拿不出来。
      这里**先自己上传**, 从 ``messages.UploadMedia`` 的 raw 结果构造出 file_id
      (照抄 pyrogram ``Photo._parse`` / ``Document._parse`` 的构造参数), 再把媒体换成
      ``InputMediaPhoto(file_id)`` 形式 —— 于是 write 时直接引用, **不会二次上传**。

    返回的条目与直发路径写进缓存的格式一致 (``CacheMedia``), 下一次选中即可零上传。

    ``media`` 是 ``build_rich_media`` 的产物, 两条路 (markdown / 敏感内容的 blocks) 都产出
    同一份 ``InputRichMessageMedia`` 列表 —— 敏感路径只是**发送时**改用 blocks 渲染, 所以
    这个函数对两者一样有效 (原先只给 markdown 路径记账, 敏感内容因此每次都重新下载)。
    """
    from pyrogram.file_id import FileId, FileType, ThumbnailSource

    from services.cache import CacheMedia, CacheMediaType

    entries: list = []
    for item in media:
        inner = getattr(item, "media", None)
        source = getattr(inner, "media", None)
        if not isinstance(source, str) or not Path(source).exists():
            # 已经是 file_id (缓存命中) 或没有本地文件: 保留原样, 不产生条目
            continue

        is_photo = isinstance(inner, InputMediaPhoto)
        try:
            if is_photo:
                uploaded = await cli.invoke(
                    raw.functions.messages.UploadMedia(
                        peer=raw.types.InputPeerSelf(),
                        media=raw.types.InputMediaUploadedPhoto(file=await cli.save_file(source)),
                    )
                )
                photo = uploaded.photo
                sizes = list(getattr(photo, "sizes", None) or [])
                biggest = max(sizes, key=lambda s: getattr(s, "w", 0) * getattr(s, "h", 0)) if sizes else None
                file_id = FileId(
                    file_type=FileType.PHOTO,
                    dc_id=photo.dc_id,
                    media_id=photo.id,
                    access_hash=photo.access_hash,
                    file_reference=photo.file_reference,
                    thumbnail_source=ThumbnailSource.THUMBNAIL,
                    thumbnail_file_type=FileType.PHOTO,
                    thumbnail_size=getattr(biggest, "type", "") or "",
                    volume_id=0,
                    local_id=0,
                ).encode()
                item.media = InputMediaPhoto(file_id)
                entries.append(CacheMedia(type=CacheMediaType.PHOTO, file_id=file_id))
            else:
                # 封面: 本地文件必须**一起上传**才存得进 document 的缩略图里。
                # 以前这里没传 thumb, 于是 inline 记账过的视频**永远没有封面**
                # (编辑时用的是这份 file_id, 原来的 thumb 已经不在参数里了)。
                thumb_path = getattr(inner, "thumb", None)
                thumb = await cli.save_file(thumb_path) if thumb_path else None
                attributes: list[raw.base.DocumentAttribute] = [
                    raw.types.DocumentAttributeVideo(
                        duration=int(getattr(inner, "duration", 0) or 0),
                        w=int(getattr(inner, "width", 0) or 0),
                        h=int(getattr(inner, "height", 0) or 0),
                        supports_streaming=bool(getattr(inner, "supports_streaming", True)),
                    )
                ]
                is_animation = preserve_animations and isinstance(inner, InputMediaAnimation)
                if is_animation:
                    # RichBlock._parse distinguishes animations from videos by the document attributes.
                    attributes.append(raw.types.DocumentAttributeAnimated())
                uploaded = await cli.invoke(
                    raw.functions.messages.UploadMedia(
                        peer=raw.types.InputPeerSelf(),
                        media=raw.types.InputMediaUploadedDocument(
                            file=await cli.save_file(source),
                            mime_type=getattr(inner, "mime_type", None) or "video/mp4",
                            thumb=thumb,
                            attributes=attributes,
                        ),
                    )
                )
                document = uploaded.document
                file_id = FileId(
                    file_type=FileType.ANIMATION if is_animation else FileType.VIDEO,
                    dc_id=document.dc_id,
                    media_id=document.id,
                    access_hash=document.access_hash,
                    file_reference=document.file_reference,
                ).encode()
                if is_animation:
                    item.media = InputMediaAnimation(file_id)
                    entries.append(CacheMedia(type=CacheMediaType.ANIMATION, file_id=file_id))
                else:
                    # 换成 file_id 引用: **带着封面**（从刚上传的 document 里把缩略图
                    # 的 file_id 取出来）—— 否则编辑出去的就是没封面的视频。
                    cover = _thumb_file_id(document, cli)
                    item.media = InputMediaVideo(file_id, thumb=cover, supports_streaming=True)
                    entries.append(CacheMedia(type=CacheMediaType.VIDEO, file_id=file_id, cover_file_id=cover))
            logger.debug(f"inline 媒体已上传并记账: type={type(entries[-1]).__name__}")
        except Exception as e:  # noqa: BLE001 - 记账失败不能影响发送本身
            logger.warning(f"inline 媒体上传记账失败 (改由后续步骤上传, 只是不写缓存): {type(e).__name__}: {e}")
    return entries


def strip_video_cover(rich: InputRichMessage) -> InputRichMessage:
    """把富文本媒体里的**远端封面地址**去掉（``video_cover=`` 是给服务端去抓的 URL）。

    服务端抓不到那个 URL 时整条发送会以 ``WEBPAGE_CURL_FAILED`` 失败 —— 封面只是装饰，
    不该让它挡住消息。就地清空并返回同一个对象。
    """
    def clear(node, depth: int = 0) -> None:
        if node is None or isinstance(node, (str, int)) or depth > 8:
            return
        if hasattr(node, "video_cover"):
            node.video_cover = None
        media = getattr(node, "media", None)
        if media is not None:
            clear(media, depth + 1)
        for attr in ("video", "photo", "blocks", "items"):
            child = getattr(node, attr, None)
            if child is None or isinstance(child, (str, int, bytes)):
                continue
            for one in child if isinstance(child, list) else [child]:
                clear(one, depth + 1)

    for item in getattr(rich, "media", None) or []:
        clear(item)
    for block in getattr(rich, "blocks", None) or []:
        clear(block)
    return rich


async def with_cover_fallback(rich: InputRichMessage, send):
    """发送富文本；若服务端因抓不到封面而拒绝，去掉封面重发一次。

    普通发送路径一直有这个兜底（``WebpageCurlFailed`` → 移除封面继续），
    富文本路径当初漏了 —— 于是"封面 URL 抓不到"会直接变成发送失败。
    """
    from pyrogram.errors import WebpageCurlFailed

    try:
        return await send(rich)
    except WebpageCurlFailed as e:
        logger.warning(f"服务端抓不到封面, 去掉封面重试: {str(e)[:90]}")
        return await send(strip_video_cover(rich))


def _thumb_file_id(document, client) -> str | None:
    """从上传返回的 document 里取出**缩略图**的 file_id（缓存用, 下次可当 thumb）。

    document 的缩略图在 ``thumbs`` 里（``PhotoSize`` 列表）; 构造方式与上面照片分支
    取 file_id 的手法一致（Telegram 的 file_id 是"引用"的编码, 不是新文件）。
    拿不到就返回 None —— 封面只是锦上添花, 不能因此让上传失败。
    """
    from pyrogram.file_id import FileId, FileType, ThumbnailSource

    sizes = list(getattr(document, "thumbs", None) or [])
    if not sizes:
        return None
    biggest = max(sizes, key=lambda s: getattr(s, "w", 0) * getattr(s, "h", 0))
    return FileId(
        file_type=FileType.PHOTO,
        dc_id=document.dc_id,
        media_id=document.id,
        access_hash=document.access_hash,
        file_reference=document.file_reference,
        thumbnail_source=ThumbnailSource.THUMBNAIL,
        thumbnail_file_type=FileType.PHOTO,
        thumbnail_size=getattr(biggest, "type", "") or "",
        volume_id=0,
        local_id=0,
    ).encode()


def extract_cache_media(rich_message) -> list:
    """从发送后返回的富文本消息里取出媒体 file_id (写回缓存用, 下次零上传)。

    blocks 路径与 markdown 路径返回的结构不同, 这里统一递归找 Photo/Video/Document/Animation。

    ⚠️ **封面的字段在 ``video`` 上, 不在块上**（2026-10-08 实测踩坑）:
    ``RichBlockVideo`` 只有 ``video`` / ``has_spoiler`` / ``caption`` —— 写
    ``getattr(node, "cover", …)`` 永远拿到 ``None``, 于是缓存命中时视频**没有封面**
    (用户报「缓存…没图了」)。正确来源是 ``node.video`` 的 ``video_cover``(兜底 ``thumb``),
    Rich and inline delivery share this extraction.
    """
    from services.cache import CacheMedia, CacheMediaType

    found: list = _RenderedCacheMedia()

    def _file_id(obj) -> str | None:
        """从一个 pyrogram 媒体对象上取 file_id(未绑 client 时可能为 None)。"""
        if obj is None:
            return None
        for attr in ("file_id", "document"):
            value = getattr(obj, attr, None)
            if isinstance(value, str) and value:
                return value
            nested = getattr(value, "file_id", None) if value is not None else None
            if isinstance(nested, str) and nested:
                return nested
        return None

    def _cover_file_id(video) -> str | None:
        """视频封面: ``video.video_cover``(兜底 ``video.thumb``) —— 块上**没有** cover 字段。"""
        for attr in ("video_cover", "thumb"):
            return_value = getattr(video, attr, None)
            if return_value is not None:
                cover = _file_id(return_value)
                if cover:
                    return cover
        return None

    for node in _walk_rich_blocks(getattr(rich_message, "blocks", None)):
        name = type(node).__name__
        if name == "RichBlockPhoto":
            file_id = _file_id(getattr(node, "photo", None))
            if file_id:
                found.append(CacheMedia(type=CacheMediaType.PHOTO, file_id=file_id))
        elif name == "RichBlockVideo":
            video = getattr(node, "video", None)
            file_id = _file_id(video)
            if file_id:
                found.append(
                    CacheMedia(type=CacheMediaType.VIDEO, file_id=file_id, cover_file_id=_cover_file_id(video))
                )
        elif name == "RichBlockAnimation":
            file_id = _file_id(getattr(node, "animation", None))
            if file_id:
                found.append(CacheMedia(type=CacheMediaType.ANIMATION, file_id=file_id))
        elif name == "RichBlockDocument":
            file_id = _file_id(getattr(node, "document", None))
            if file_id:
                found.append(CacheMedia(type=CacheMediaType.DOCUMENT, file_id=file_id))
    return found


__all__ = [
    "RICH_MEDIA_KIND",
    "RICH_RESULT_ID",
    "build_cached_rich_content",
    "build_delivery_blocks",
    "build_rich_media",
    "cache_media_blocks",
    "edit_inline_rich_message",
    "extract_cache_media",
    "has_structured_body",
]
