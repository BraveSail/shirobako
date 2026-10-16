"""富文本 inline 的二次替换: 选中后把占位消息编辑成带媒体的完整富文本。

inline **回答查询时**不能带外部媒体 (Telegram 直接回 `EXTERNAL_MEDIA_NOT_SUPPORTED`),
所以占位结果只发文字; 用户选中后再走「下载 → 上传 → 编辑」: 用
`messages.EditInlineBotMessage(rich_message=...)` 把同一条消息换成带媒体的富文本。
这条编辑路径的媒体字段要的是**已上传的** photo/document (pyrogram 的
`InputRichMessageMedia.write` 内部用 `messages.UploadMedia`, peer=self)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from parsehub.types import AniFile, AnyMediaRef, ImageFile, LivePhotoFile, VideoFile
from pyrogram import raw, utils
from pyrogram.types import (
    InputMediaAnimation,
    InputMediaPhoto,
    InputMediaVideo,
    InputRichBlockAnimation,
    InputRichMessage,
    InputRichMessageMedia,
)

from log import logger

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
    placeholders: list[str] = []
    media_blocks: dict[str, Any] = {}
    index = 0
    # 每个 ref 产出的占位符 (一个 ref 可能展开成多个文件), 供末尾切分引用帖媒体
    per_ref: list[list[str]] = []
    # 与 placeholders **平铺对齐**的原始 URL (同一个 ref 展开出的多份共用它的 url)。
    # 渲染层靠它把正文外链图换成上传媒体引用 (见 helpers.rewrite_external_images)。
    urls_flat: list[str] = []

    for media_ref, processed in zip(media_refs, processed_list, strict=False):
        group: list[str] = []
        per_ref.append(group)
        ref_url = str(getattr(media_ref, "url", "") or "")
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
            media.append(InputRichMessageMedia(media_id, item))
            placeholder = f"![](tg://{kind}?id={media_id})"
            placeholders.append(placeholder)
            group.append(placeholder)
            # URL 与占位符**同步追加** —— 一个 ref 展开成多份时共用它的 url
            urls_flat.append(ref_url)
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

    def flat(groups: list[list[str]]) -> list[str]:
        return [p for ref_group in groups for p in ref_group]

    # urls 按 placeholder 的顺序切同一刀: urls_flat 与 placeholders 是逐步同步追加的,
    # 按 per_ref 的分组边界切出正文段 (前面切给引用块的段不带回 — 渲染层只用正文段)。
    def urls_slice(end: int) -> list[str]:
        count = sum(len(g) for g in per_ref[:end])
        return urls_flat[:count]

    return (
        media,
        flat(per_ref[:head_split]),
        media_blocks,
        flat(per_ref[tail_split:]),
        flat(per_ref[head_split:tail_split]),
        urls_slice(head_split),
    )


async def edit_inline_rich_message(
    cli: Client,
    inline_message_id: str,
    *,
    markdown: str,
    media: list[InputRichMessageMedia] | None = None,
    blocks: list | None = None,
) -> bool:
    """把一条 inline 消息编辑成富文本 (可带媒体)。

    媒体会在这一步真正上传 (peer=self), 所以调用方要保证文件还在本地。
    返回是否成功 —— **返回值不能当成功判据**, 用户看到的消息才是。

    ``blocks`` 非空时走 blocks 路径: 官方 API 的富文本媒体块没有 spoiler 字段,
    敏感内容只有 raw 的 ``PageBlockPhoto/Video(spoiler=True)`` 才能打码。
    """
    unpacked = utils.unpack_inline_message_id(inline_message_id)
    session = await cli.get_session(unpacked.dc_id, is_media=True)
    if blocks:
        rich = InputRichMessage(blocks=blocks)
    else:
        rich = InputRichMessage(markdown=markdown, media=media or None)
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
    (``[正文..., 被回复..., 被引用...]``), 按两个计数从末尾往前划给对应的引用块。

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

    from services.cache import CacheMediaType

    sensitive = bool(getattr(entry.parse_result, "is_sensitive", False))
    media: list[InputRichMessageMedia] = []
    placeholders: list[str] = []
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
        placeholders.append(f"![](tg://{kind}?id={media_id})")
        # 映射**总是**产出 (spoiler 由 sensitive 决定): 需要它的不只有打码 ——
        # "折叠的引用卡片带媒体"也必须走 blocks 才能把图留在引用块内
        # (见 helpers.markdown_needs_blocks)。
        from plugins.parse.rich_blocks import SpoilerPhotoBlock, SpoilerVideoBlock

        match m.type:
            case CacheMediaType.PHOTO:
                media_blocks[media_id] = SpoilerPhotoBlock(item, spoiler=sensitive)
            case CacheMediaType.VIDEO | CacheMediaType.ANIMATION:
                media_blocks[media_id] = SpoilerVideoBlock(item, spoiler=sensitive)

    pr = entry.parse_result
    quoted_count = max(0, min(int(getattr(pr, "quoted_media_count", 0) or 0), len(placeholders)))
    tail_split = len(placeholders) - quoted_count
    reply_count = max(0, min(int(getattr(pr, "reply_media_count", 0) or 0), tail_split))
    head_split = tail_split - reply_count
    # 原始 URL 表（与占位符平铺对齐）: 老缓存没有 url 字段 ⇒ 全是空串，正文外链图
    # 在渲染时会被剥掉（图仍在末尾出现，只是不在原位）。
    urls_flat = [str(getattr(m, "url", "") or "") for m in (entry.media or [])]
    return (
        media,
        placeholders[:head_split],
        placeholders[tail_split:],
        placeholders[head_split:tail_split],
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


def rich_cache_entry(
    parse_result,
    media: list,
    *,
    quoted_media_count: int = 0,
    reply_media_count: int = 0,
    polls: list | None = None,
    urls: list[str] | None = None,
):
    """把一次富文本发送的字段与媒体 file_id 收成缓存条目。

    ``quoted_media_count`` 记的是**媒体项数** (缓存里平铺的条数), 不是 ref 数
    —— 一个 ref 可能展开成多个文件, 缓存只能按项切分。

    ``polls`` 是**投票数据** (``result.polls`` 的序列化) —— 正文里的 ``{{poll:N}}``
    占位符要靠它才能渲染; 不传 (老调用点/没有投票) 时按空处理, 渲染层不替换。

    ``urls`` 是**与媒体项平铺对齐**的原始 URL 表 (``build_rich_media`` 第六个返回值)
    —— 缓存命中时正文外链图靠它换成上传媒体引用; 不传则那些图只能剥掉。

    放在这里而不是发送层: 私聊/群 (``sender``) 与 guest 都要写同一种条目。
    """
    # 两个都是 bot 侧模块, 函数内延迟导入避免循环依赖 (与上面的 helper 同一手法)
    from plugins.helpers import get_parse_author_name
    from services.cache import CONTENT_VERSION, FOOTER_META_FIELDS_VERSION, CacheEntry, CacheParseResult

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
            position_label=getattr(parse_result, "position_label", "") or "",
            quote_roles=list(getattr(parse_result, "quote_roles", None) or []),
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
    custom_content: str = "",
    spoiler_tag: str = "",
) -> tuple[str, list[InputRichMessageMedia]]:
    """从缓存条目重建富文本正文与媒体块 (file_id 复用, 零上传)。

    **直发与 inline 共用这一条**, 免得两边各写一份排版:
    缓存里存的只有解析字段, 排版一律交给 ``build_rich_markdown_by_str``。

    :param spoiler_tag: 用户这次要求遮住内容 (``/s``)。缓存存的是**解析字段**
        而非渲染结果, 所以遮与不遮在排版时决定 —— 命中缓存也必须照遮。
    """
    from plugins.helpers import build_rich_markdown_by_str

    # 平台: 优先用缓存里存的短名; 老缓存没这个字段 ⇒ 按 raw_url 推断。
    # **必须给一个真实平台** —— 渲染里标签等依赖它, 传 None 会静默降级成纯文本。
    platform = _platform_from_id(entry.parse_result.platform)
    if platform is None:
        from parsehub import ParseHub

        platform = ParseHub().get_platform(raw_url) if raw_url else None

    media, placeholders, quoted_placeholders, reply_placeholders, media_blocks, media_urls = cache_media_blocks(entry)
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
        quote_roles=entry.parse_result.quote_roles,
        origin_line=entry.parse_result.origin_line,
        reply_count=entry.parse_result.reply_count,
        hashtags=entry.parse_result.hashtags,
        # 投票数据（``{{poll:N}}`` 的渲染源）—— 老缓存没有这个字段时为 [],
        # 渲染层不替换占位符（老缓存里本来也没有占位符）
        polls=entry.parse_result.polls,
        author_name=entry.parse_result.author_name,
        author_handle=entry.parse_result.author_handle,
        author_url=entry.parse_result.author_url,
        published_at=entry.parse_result.published_at,
        view_count=entry.parse_result.view_count,
        like_count=entry.parse_result.like_count,
        tags=entry.parse_result.tags,
        custom_content=custom_content,
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
    return markdown, media, media_blocks


def cached_rich_message(
    markdown: str,
    media: list[InputRichMessageMedia],
    media_blocks: dict | None = None,
    *,
    sensitive: bool = False,
) -> InputRichMessage:
    """按"是否敏感"选渲染路径 —— **敏感内容必须走 blocks 才能打码**。

    富文本的 markdown 路径 (``InputRichMessage(markdown=…, media=…)``) 打不了码:
    官方 API 的 ``InputRichBlockPhoto`` 没有 spoiler 字段, 只有 raw 的
    ``PageBlockPhoto/Video(spoiler=)`` 才有。直发路径一直在这么做
    (``send_rich_media`` 里 ``is_sensitive and media_blocks`` 时切 blocks),
    **缓存路径以前漏了这一步, 于是"第二次发同一个链接"就是没打码的**。

    抽成一个函数给三条缓存路径 (私聊/群、inline、guest) 共用, 免得三处再各写一遍。

    另外: "折叠的引用卡片带媒体"同样只能走 blocks (``markdown_needs_blocks``) ——
    那条形态 markdown 表达不了 (块内嵌不了 details 与图)。
    """
    from plugins.helpers import markdown_needs_blocks

    if media_blocks and (sensitive or markdown_needs_blocks(markdown)):
        from plugins.parse.rich_blocks import markdown_to_blocks

        return InputRichMessage(blocks=markdown_to_blocks(markdown, media_blocks=media_blocks))
    return InputRichMessage(markdown=markdown, media=media or None)


async def upload_media_for_cache(cli: Client, media: list[InputRichMessageMedia]) -> list:
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
                uploaded = await cli.invoke(
                    raw.functions.messages.UploadMedia(
                        peer=raw.types.InputPeerSelf(),
                        media=raw.types.InputMediaUploadedDocument(
                            file=await cli.save_file(source),
                            mime_type=getattr(inner, "mime_type", None) or "video/mp4",
                            thumb=thumb,
                            attributes=[
                                raw.types.DocumentAttributeVideo(
                                    duration=int(getattr(inner, "duration", 0) or 0),
                                    w=int(getattr(inner, "width", 0) or 0),
                                    h=int(getattr(inner, "height", 0) or 0),
                                    supports_streaming=bool(getattr(inner, "supports_streaming", True)),
                                )
                            ],
                        ),
                    )
                )
                document = uploaded.document
                file_id = FileId(
                    file_type=FileType.VIDEO,
                    dc_id=document.dc_id,
                    media_id=document.id,
                    access_hash=document.access_hash,
                    file_reference=document.file_reference,
                ).encode()
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
    与直发路径 ``cache_media_from_message`` 的取法一致。
    """
    from services.cache import CacheMedia, CacheMediaType

    found: list = []

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

    def walk(node) -> None:
        if node is None or isinstance(node, (str, int)):
            return
        name = type(node).__name__
        if name == "RichBlockPhoto":
            file_id = _file_id(getattr(node, "photo", None))
            if file_id:
                found.append(CacheMedia(type=CacheMediaType.PHOTO, file_id=file_id))
                return
        if name == "RichBlockVideo":
            video = getattr(node, "video", None)
            file_id = _file_id(video)
            if file_id:
                found.append(
                    CacheMedia(type=CacheMediaType.VIDEO, file_id=file_id, cover_file_id=_cover_file_id(video))
                )
                return
        if name == "RichBlockAnimation":
            file_id = _file_id(getattr(node, "animation", None))
            if file_id:
                found.append(CacheMedia(type=CacheMediaType.ANIMATION, file_id=file_id))
                return
        if name == "RichBlockDocument":
            file_id = _file_id(getattr(node, "document", None))
            if file_id:
                found.append(CacheMedia(type=CacheMediaType.DOCUMENT, file_id=file_id))
                return
        for attr in ("blocks", "items"):
            value = getattr(node, attr, None)
            if value is None or isinstance(value, (str, int)):
                continue
            for child in value if isinstance(value, list) else [value]:
                walk(child)

    for block in getattr(rich_message, "blocks", None) or []:
        walk(block)
    return found


__all__ = [
    "RICH_MEDIA_KIND",
    "RICH_RESULT_ID",
    "build_cached_rich_content",
    "build_rich_media",
    "cache_media_blocks",
    "edit_inline_rich_message",
    "extract_cache_media",
]
