import re

from easy_ai18n import PreLocaleSelector
from parsehub import AnyParseResult
from parsehub.types import (
    AniRef,
    ImageRef,
    VideoRef,
)
from parsehub.utils.helpers import strip_spoiler_flag
from pyrogram import Client, raw, utils
from pyrogram.errors import BadRequest
from pyrogram.types import (
    ChosenInlineResult,
    InlineQuery,
    InlineQueryResult,
    InlineQueryResultArticle,
    InputRichMessage,
    InputRichMessageContent,
    InputTextMessageContent,
    LinkPreviewOptions,
)
from pyrogram.types import (
    InlineKeyboardButton as Ikb,
)
from pyrogram.types import (
    InlineKeyboardMarkup as Ikm,
)

from db import get_session
from i18n import t_
from log import logger
from plugins.filters import platform_filter
from plugins.helpers import (
    build_rich_markdown,
    build_start_text,
    markdown_needs_blocks,
)
from plugins.parse.covers import prepare_video_thumbs
from plugins.parse.inline_rich import (
    RICH_RESULT_ID,
    build_cached_rich_content,
    build_rich_media,
    cache_media_blocks,
    cached_rich_message,
    edit_inline_rich_message,
    rich_cache_entry,
    upload_media_for_cache,
)
from plugins.parse.reporters import InlineStatusReporter
from plugins.parse.rich_blocks import markdown_to_blocks
from repo.settings import SettingsConfig
from services import AdminUserService, ParseService, SettingsService, UserService
from services.cache import CacheEntry, parse_cache, persistent_cache
from services.pipeline import ParsePipeline
from utils.helpers import to_list, with_request_id

logger = logger.bind(name="InlineParse")

INLINE_TITLE_LIMIT = 90
"""inline 结果项标题上限 (列表里只显示一行, 超长会把结果项撑爆)"""

INLINE_DESC_LIMIT = 200
"""inline 结果项描述上限"""

def clip_inline_text(text: str | None, limit: int) -> str:
    """压平换行并截断, 用于 inline 结果项的 title/description。

    有些平台的标题承载正文 (抖音把文案塞进 title, yt-dlp 对 Facebook 返回
    "27K views · … | 正文 | 频道名"), 不截断会让 inline 结果列表变得极长。
    """
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def count_inline_media(media: object) -> int:
    """inline 结果对应的媒体数量 (无媒体/None 都算 0)。"""
    return 0 if media is None else len(to_list(media))  # type: ignore[arg-type]


SEARCH_ICON = "https://i.imgloc.com/2023/06/15/Vbfazk.png"
DEFAULT_PARSE_RESULT_THUMB_URL = "https://telegra.ph/file/cdfdb65b83a4b7b2b6078.png"
LINK_ICON_URL = "https://i.iij.li/i/20260627/6a3fb12066abb.png"
LINK_ICON_WIDTH = 72
LINK_ICON_HEIGHT = 72


@Client.on_inline_query(~platform_filter(False))
async def inline_parse_tip(_: Client, inline_query: InlineQuery) -> None:
    async with get_session() as session:
        lang = await UserService(session).ensure_lang(
            inline_query.from_user.id, inline_query.from_user.language_code
        )
    _t = t_[lang]
    results: list[InlineQueryResult] = [
        InlineQueryResultArticle(
            title=_t("聚合解析"),
            description=_t("请在聊天框输入链接"),
            input_message_content=InputTextMessageContent(
                build_start_text()[lang], link_preview_options=LinkPreviewOptions(is_disabled=True)
            ),
            thumb_url=SEARCH_ICON,
        )
    ]
    await inline_query.answer(results=results, cache_time=1)


async def answer_inline(
    inline_query: InlineQuery,
    results: list[InlineQueryResult],
    *,
    lang: str,
    cache_time: int = 0,
    fallback_results: list[InlineQueryResult] | None = None,
) -> None:
    """回答内联查询, 失败时退化成一条可读的错误结果。

    Telegram 对内联回答很挑 (例如富文本里带外部媒体会直接
    ``400 EXTERNAL_MEDIA_NOT_SUPPORTED``); 一旦答案送不出去, 客户端只会一直转圈,
    用户完全看不到原因。所以这里兜一层: 先按完整结果发, 失败就把富文本项剔掉再试,
    仍失败则回一条纯文字提示。
    """
    _t = t_[lang]
    try:
        await inline_query.answer(results[:50], cache_time=cache_time)
        return
    except BadRequest as e:
        logger.warning(f"内联回答被拒: {e}")

    # 富文本项最可能是元凶 (它依赖的设置最多), 去掉后再试一次
    plain = [r for r in results if not _is_rich_result(r)]
    if not plain and fallback_results:
        plain = fallback_results  # 结果全是富文本时换用调用方给的备选 (通常是媒体项)
    if plain and plain != results:
        try:
            await inline_query.answer(plain[:50], cache_time=cache_time)
            return
        except BadRequest as e:
            logger.warning(f"去掉富文本项后仍被拒: {e}")

    await inline_query.answer(
        results=[
            InlineQueryResultArticle(
                title=_t("解析失败"),
                description=_t("请稍后重试, 或直接把链接发到聊天里"),
                input_message_content=InputTextMessageContent(
                    _t("解析失败"), link_preview_options=LinkPreviewOptions(is_disabled=True)
                ),
            )
        ],
        cache_time=1,
    )


def inline_cover_url(parse_result: AnyParseResult) -> str:
    """取一个可用于 inline 占位的封面 URL (Telegram 服务端去抓)。

    视频/动图用平台的缩略图, 图片用它自己的地址 —— 这正是老模式的做法:
    消息里先出现封面, 选中后处理完再换成真正的媒体。
    """
    for ref in to_list(parse_result.media):
        thumb = getattr(ref, "thumb_url", None)
        if isinstance(ref, VideoRef | AniRef) and thumb:
            return str(thumb)
        if isinstance(ref, ImageRef):
            return str(thumb or ref.url)
    return ""


def inline_reply_markup(parse_result: AnyParseResult) -> Ikm | None:
    """富文本结果项的键盘。

    只有**有媒体**时才挂: 它的唯一作用是让 Telegram 回传 inline_message_id,
    以便选中后把消息编辑成带媒体的版本。纯文字结果不需要二次编辑, 也就不要键盘。
    """
    # 用 count_inline_media 判空: to_list(None) 会给出 [None], 不能直接当"有媒体"
    if not count_inline_media(parse_result.media):
        return None
    return Ikm([[Ikb("原链接", url=parse_result.raw_url)]])


def _is_rich_result(result: InlineQueryResult) -> bool:
    """结果项是不是那条需要 Telegram 特殊支持的富文本结果 (被拒时优先剔掉它)。"""
    if getattr(result, "id", None) == RICH_RESULT_ID:
        return True
    return isinstance(getattr(result, "input_message_content", None), InputRichMessageContent)


@Client.on_inline_query(platform_filter(False))
@with_request_id
async def call_inline_parse(cli: Client, inline_query: InlineQuery) -> None:
    logger.info(f"收到内联解析请求: query={inline_query.query}, from_user={inline_query.from_user.id}")
    try:
        await _call_inline_parse(cli, inline_query)
    except Exception as e:
        # 解析异常同样不能让客户端转圈: 给一条可读的结果
        logger.opt(exception=e).warning(f"内联解析失败: {e}")
        try:
            async with get_session() as session:
                lang = await UserService(session).ensure_lang(
                    inline_query.from_user.id, inline_query.from_user.language_code
                )
        except Exception:  # noqa: BLE001 - 兜底路径不再抛错
            lang = ""
        await answer_inline(inline_query, [], lang=lang)


def build_denied_result(_t: PreLocaleSelector) -> InlineQueryResult:
    """鉴权不通过时返回的结果项 (不解析, 只说明原因)。"""
    return InlineQueryResultArticle(
        title=_t("无权限"),
        description=_t("仅限白名单用户使用"),
        input_message_content=InputTextMessageContent(
            _t("无权限"), link_preview_options=LinkPreviewOptions(is_disabled=True)
        ),
    )


async def _call_inline_parse(cli: Client, inline_query: InlineQuery) -> None:
    async with get_session() as session:
        lang = await UserService(session).ensure_lang(
            inline_query.from_user.id, inline_query.from_user.language_code
        )
        config = await SettingsService(session).get_config_by_user(inline_query.from_user.id)
        # **inline 是"非同群调用", 一律鉴权**（2026-10-12 权限模型）: 只认本地库白名单
        # （``users.role`` 非空 ∪ 配置超管）—— 不再检查"与 bot 同群"; 私聊唤起的 inline
        # 也一视同仁（以前私聊豁免, 现在同样要白名单）。
        allowed = await AdminUserService(session).is_whitelisted(inline_query.from_user.id)

    if not allowed:
        _t = t_[lang]
        logger.info(
            f"inline 被拦截(不在白名单): user_id={inline_query.from_user.id}, chat_type={inline_query.chat_type}"
        )
        await answer_inline(inline_query, [build_denied_result(_t)], lang=lang, cache_time=0)
        return

    # 手动打码开关: query 里那个独立的 /s 先剥掉, 否则会被当成 URL 的一部分
    query, spoiler_tag = strip_spoiler_flag(inline_query.query)
    raw_url = await ParseService().get_raw_url(query)
    if cached := await persistent_cache.get(raw_url):
        logger.debug("inline: 缓存命中, 构建富文本结果")
        # 缓存里已有 file_id: 富文本项可以直接带上媒体, 无需二次编辑 (file_id 复用不上传)。
        # 没有 file_id 的旧缓存则给纯文字占位, 选中后由 inline_result_download 补齐媒体。
        results = [build_cached_rich_result(cached, raw_url, lang, config, spoiler_tag=spoiler_tag)]
        await answer_inline(inline_query, results, lang=lang, cache_time=60)
        return

    parse_result = await parse_cache.get(raw_url)
    if parse_result is None:
        parse_result = await ParseService().parse(query)
        await parse_cache.set(raw_url, parse_result)

    results = await build_inline_results(parse_result, cli, lang, config, spoiler_tag=spoiler_tag)
    logger.debug(f"inline 查询完成, 返回 {len(results)} 个结果")
    await answer_inline(inline_query, results, lang=lang, cache_time=0)


async def _drop_inline_keyboard(cli: Client, inline_message_id: str) -> None:
    """摘掉 inline 结果自带的键盘。

    带键盘是 Telegram 回传 inline_message_id 的前提, 但用户并不需要这个按钮,
    所以一拿到句柄就立刻清掉。

    不能用空的 InlineKeyboardMarkup: 它序列化成 ReplyInlineMarkup(rows=[]),
    会被 Telegram 以 REPLY_MARKUP_INVALID 拒掉。移除键盘要用 ReplyKeyboardHide
    (Bot API 传空 inline_keyboard 时映射的也是这个类型)。
    """
    try:
        unpacked = utils.unpack_inline_message_id(inline_message_id)
        session = await cli.get_session(unpacked.dc_id, is_media=True)
        await session.invoke(
            raw.functions.messages.EditInlineBotMessage(
                id=unpacked, reply_markup=raw.types.ReplyKeyboardHide()
            )
        )
    except Exception as e:
        # 用 warning 而不是 debug: 摘不掉 = 用户会一直看到那个按钮, 不算无关紧要的噪音
        # (曾经在 debug 级吞掉过, 结果"按钮怎么还在"完全查不出原因)
        logger.warning(f"摘除 inline 键盘失败: {type(e).__name__}: {e}")


async def _upload_and_cache_inline_media(
    cli: Client,
    raw_url: str,
    parse_result,
    media: list,
    quoted_placeholders: list[str],
    reply_placeholders: list[str],
    media_urls: list[str] | None = None,
):
    """先自己上传拿 file_id, 并把这条 inline 消息的媒体写进缓存。

    为什么要自己上传: inline 消息只能用 ``EditInlineMessage`` 更新, 它**只回 Bool**,
    拿不到服务端生成的 file_id —— 所以 inline 路径以前**从不写**缓存, 每次选中都要
    重新下载 + 上传。这里先上传一次, file_id 就能记下来 (调用方随后用 file_id 引用编辑,
    不会二次上传)。

    记账是 **all-or-nothing**: 只有**全部**媒体都拿到 file_id 才写 —— 部分成功时引用/回复
    媒体的计数会对不上, 下次命中会把图放错块 (宁可这次不写)。返回写成功的缓存条目, 否则 None。

    ``media_urls`` 是原始 URL 表 (与 media 平铺对齐) —— 存进缓存, 缓存命中时正文外链图
    才能换成上传媒体引用 (见 ``inline_rich.rich_cache_entry``)。
    """
    uploaded = await upload_media_for_cache(cli, media)
    if not uploaded:
        return None
    if len(uploaded) != len(media):
        logger.warning(
            f"inline 媒体只记上 {len(uploaded)}/{len(media)} 条, 跳过写缓存 (避免引用计数错位)"
        )
        return None
    if not raw_url:
        return None
    quoted_items = min(len(quoted_placeholders), len(uploaded))
    reply_items = min(len(reply_placeholders), len(uploaded) - quoted_items)
    entry = rich_cache_entry(
        parse_result,
        uploaded,
        quoted_media_count=quoted_items,
        reply_media_count=reply_items,
        # 投票同进缓存 (inline 命中缓存时同样要渲染表格)
        polls=getattr(parse_result, "polls", None),
        # 原始 URL 同进缓存: 正文外链图在缓存命中时也换成上传媒体引用
        urls=media_urls,
    )
    await persistent_cache.set(raw_url, entry)
    logger.info(
        f"inline 媒体已写 file_id 缓存: count={len(uploaded)} "
        f"引用{quoted_items} 回复{reply_items} (下次选中零下载/零上传)"
    )
    return entry


@Client.on_chosen_inline_result()
@with_request_id
async def inline_result_download(cli: Client, chosen_result: ChosenInlineResult) -> None:
    """选中富文本结果后, 下载 → 上传 → 把消息编辑成带媒体的富文本。"""
    logger.info(
        f"收到 inline 选中回调: result_id={chosen_result.result_id!r}, "
        f"inline_message_id={chosen_result.inline_message_id!r}, query={chosen_result.query!r}"
    )
    if chosen_result.result_id != RICH_RESULT_ID:
        logger.info(f"跳过: result_id={chosen_result.result_id!r} 非富文本结果")
        return

    # 选中回调带的是原始 query, 手动打码标记同样要剥掉 (否则 /s 会被拼进 URL)
    _, spoiler_tag = strip_spoiler_flag(chosen_result.query)

    inline_message_id = chosen_result.inline_message_id
    if inline_message_id is None:
        # 没有键盘就不会有句柄: 说明这条结果本来没媒体, 无需二次编辑
        logger.info("inline 选中回调缺少 inline_message_id, 无需编辑")
        return

    # 键盘只为换取 inline_message_id 而存在 (Telegram 只在消息带 inline keyboard 时
    # 才回传句柄), 到手就立刻摘掉 —— 用户不该看到那个"原链接"按钮。
    await _drop_inline_keyboard(cli, inline_message_id)

    async with get_session() as session:
        lang = await UserService(session).ensure_lang(
            chosen_result.from_user.id, chosen_result.from_user.language_code
        )
        config = await SettingsService(session).get_config_by_user(chosen_result.from_user.id)
        _t = t_[lang]

    # 用剥掉标记后的 query, 别把 /s 拼进 URL
    query, _ = strip_spoiler_flag(chosen_result.query)
    raw_url = await ParseService().get_raw_url(query)
    cached_result = await parse_cache.get(raw_url)
    # 处理过程要与最终结果同一种排版: 不再拼老 caption, 由 reporter 自己渲染富文本。
    # 打码标记一并传下去 —— 处理过程也要遮, 否则"下载中"那几秒就露出来了
    reporter = InlineStatusReporter(
        cli,
        inline_message_id,
        t=_t,
        user_config=config,
        raw_url=raw_url,
        spoiler_tag=spoiler_tag,
    )

    with ParsePipeline(query, raw_url, reporter, parse_result=cached_result, singleflight=False, t=_t) as pipeline:
        if (result := await pipeline.run()) is None:
            return

        parse_result = result.parse_result
        await reporter.report_result(parse_result, _t("上 传 中..."))
        try:
            media_refs = to_list(parse_result.media)
            video_thumbs = (
                await prepare_video_thumbs(media_refs, platform=parse_result.platform)
                if config.video_cover
                else {}
            )
            (
                media,
                placeholders,
                media_blocks,
                quoted_placeholders,
                reply_placeholders,
                media_urls,
            ) = build_rich_media(
                media_refs,
                result.processed_list,
                video_cover=config.video_cover,
                is_sensitive=parse_result.is_sensitive,
                video_thumbs=video_thumbs,
                quoted_media_count=getattr(parse_result, "quoted_media_count", 0),
                reply_media_count=getattr(parse_result, "reply_media_count", 0),
            )
            markdown = build_rich_markdown(
                parse_result,
                config=config,
                lang=lang,
                view_label=_t("查看"),
                media_placeholders=placeholders,
                quote_media_placeholders=quoted_placeholders,
                reply_media_placeholders=reply_placeholders,
                media_urls=media_urls,
                hide_content=spoiler_tag,
            )
            logger.debug(
                f"inline 编辑为富文本: media={len(media)}, blocks={len(media_blocks)}, "
                f"markdown={len(markdown)}, sensitive={parse_result.is_sensitive}"
            )
            # 两条路都先自己上传拿 file_id: 编辑时就用 file_id 引用 (不再上传), 同时这条
            # inline 消息的媒体也进了缓存 —— 以前 inline 从不写, 每次选中都得重新下载。
            entry = await _upload_and_cache_inline_media(
                cli, raw_url, parse_result, media, quoted_placeholders, reply_placeholders, media_urls
            )
            if (parse_result.is_sensitive and media_blocks) or markdown_needs_blocks(markdown):
                # 敏感内容: markdown+media 路径的 raw 类型没有 spoiler, 只能走 blocks。
                # 记账成功时用**缓存里那套 file_id 版 blocks** 编辑 —— 与缓存命中时同一套
                # 渲染, 形态一致, 也不会把本地文件再上传一遍。
                cached_blocks = cache_media_blocks(entry)[4] if entry is not None else None
                blocks = markdown_to_blocks(markdown, media_blocks=cached_blocks or media_blocks)
                await edit_inline_rich_message(cli, inline_message_id, markdown=markdown, blocks=blocks)
            else:
                await edit_inline_rich_message(cli, inline_message_id, markdown=markdown, media=media)
        except Exception as e:
            logger.opt(exception=e).debug("详细堆栈")
            logger.error(f"inline 富文本编辑失败: {e}")
            await reporter.report_error(_t("上传"), e)
        finally:
            logger.debug("inline 富文本任务完成")


def build_cached_rich_result(
    entry: CacheEntry,
    raw_url: str,
    lang: str,
    config: SettingsConfig,
    *,
    spoiler_tag: str = "",
) -> InlineQueryResult:
    """缓存路径的富文本结果项 (带 file_id 媒体, 一项同时给图和页脚)。"""
    _t = t_[lang]
    markdown, media, media_blocks = build_cached_rich_content(
        entry,
        raw_url,
        lang=lang,
        config=config,
        view_label=_t("查看"),
        spoiler_tag=spoiler_tag,
    )
    # 没有 file_id 时挂键盘: 提示客户端保留句柄, 选中后可补上媒体
    reply_markup = None if media else Ikm([[Ikb("原链接", url=raw_url)]])
    return InlineQueryResultArticle(
        id=RICH_RESULT_ID,
        title=clip_inline_text(entry.parse_result.title, INLINE_TITLE_LIMIT) or "-",
        description=clip_inline_text(entry.parse_result.content, INLINE_DESC_LIMIT),
        input_message_content=InputRichMessageContent(
            cached_rich_message(
                markdown, media, media_blocks, sensitive=bool(entry.parse_result.is_sensitive)
            )
        ),
        reply_markup=reply_markup,
    )


#: markdown 里的图片/视频语法 (![](url) / ![alt](url)); inline 不支持外部媒体, 必须剥掉
_MEDIA_MARKDOWN_RE = re.compile(r"!\[[^\]]*\]\([^)\s]+\)")


def strip_media_markdown(markdown: str) -> str:
    """剥掉正文里的媒体语法。

    inline 结果**不支持外部媒体** —— 带 URL 的图片会让 Telegram 直接回
    ``400 EXTERNAL_MEDIA_NOT_SUPPORTED``, 整条 inline 回答失败 (表现是客户端一直转圈)。
    所以 inline 的富文本只保留文字排版, 图由单独的 InlineQueryResultPhoto/Video 提供。
    """
    return _MEDIA_MARKDOWN_RE.sub("", markdown or "")


def build_inline_rich_content(
    parse_result: AnyParseResult, *, lang: str, config: SettingsConfig, spoiler_tag: str = ""
) -> InputRichMessageContent:
    """inline 结果的富文本内容 (纯文字排版 + 页尾, 不带任何媒体)。

    :param spoiler_tag: 手动打码标记 (``#nsfw`` 等)。**必须传** —— 这条是"现场解析"
        路径的结果项, 漏传的话用户在 inline 列表里选中的就是没遮的版本
        (缓存命中的那条路径 ``build_cached_rich_content`` 一直是收这个参数的,
        两条路径行为必须一致)。
    """
    _t = t_[lang]
    markdown = build_rich_markdown(
        parse_result, config=config, lang=lang, view_label=_t("查看"), hide_content=spoiler_tag
    )
    return InputRichMessageContent(InputRichMessage(markdown=strip_media_markdown(markdown)))


async def build_inline_results(
    parse_result: AnyParseResult,
    cli: Client,
    lang: str,
    config: SettingsConfig,
    *,
    spoiler_tag: str = "",
) -> list[InlineQueryResult]:
    """根据解析结果构建内联查询结果列表"""
    logger.debug(f"构建 inline 结果: type={parse_result.type}, title={parse_result.title}")
    _t = t_[lang]

    title = clip_inline_text(parse_result.title, INLINE_TITLE_LIMIT) or "-"

    results: list[InlineQueryResult] = []
    if config.enable_inline_raw_url:
        results.append(
            InlineQueryResultArticle(
                title=_t("原始链接"),
                description=parse_result.raw_url,
                input_message_content=InputTextMessageContent(
                    parse_result.raw_url, link_preview_options=LinkPreviewOptions(is_disabled=True)
                ),
                thumb_url=LINK_ICON_URL,
                thumb_width=LINK_ICON_WIDTH,
                thumb_height=LINK_ICON_HEIGHT,
            )
        )

    # ── 富文本 (rich message): 还原原文排版, 统计与来源进页尾 ──
    # 所有类型都用这一项。回答查询时带不了媒体 (Telegram 会拒 EXTERNAL_MEDIA_NOT_SUPPORTED),
    # 正文里先放占位符, 用户选中后由 inline_result_download 下载上传、再编辑成带媒体的富文本;
    # 键盘是为了换取 inline_message_id (编辑的前提), 选中后立刻摘掉。
    # 不要用 InlineQueryResultPhoto 做"封面占位": 那样发出来就是一张图 + caption,
    # 富文本的排版、图集、标签、页脚全没了 (实测就是这个现象)。
    cover = inline_cover_url(parse_result)
    results.append(
        InlineQueryResultArticle(
            id=RICH_RESULT_ID,
            title=title,
            description=clip_inline_text(parse_result.content, INLINE_DESC_LIMIT),
            thumb_url=cover or None,
            thumb_width=320 if cover else None,
            thumb_height=320 if cover else None,
            input_message_content=build_inline_rich_content(
                parse_result, lang=lang, config=config, spoiler_tag=spoiler_tag
            ),
            reply_markup=inline_reply_markup(parse_result),
        )
    )
    logger.debug(f"inline 结果构建完成: count={len(results)}")
    return results
