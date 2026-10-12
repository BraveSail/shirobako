import asyncio
import re
from dataclasses import replace
from typing import Any

from parsehub.types import AniRef, RichTextParseResult
from parsehub.utils.helpers import strip_spoiler_flag
from pyrogram import Client, filters
from pyrogram.enums import ChatType
from pyrogram.types import Message

from core import bs
from db import get_session
from i18n import t_
from log import logger
from plugins.context import get_config_target
from plugins.filters import (
    allow_channel_auto_forward_parse_filter,
    forwarded_from_bot_filter,
    platform_filter,
    url_only_filter,
    via_me_filter,
)
from plugins.helpers import build_caption, format_label
from plugins.parse.context import GIF_ONLY_SKIP_DOWNLOAD_COUNT_THRESHOLD, ParseOptions, ParseRequest
from plugins.parse.reporters import MessageStatusReporter, disable_progress_on_report_forbidden
from plugins.parse.sender import (
    MessageSender,
    build_gif_button,
    send_cached,
    send_raw,
    send_rich_media,
    send_zip,
)
from repo.settings import ParseMode
from services import AdminUserService, ParsePipeline, ParseService, SettingsService, UserService
from services.cache import parse_cache, persistent_cache
from utils.helpers import to_list, with_request_id
from utils.rate_limit import ParseRateLimitExceeded, parse_rate_limit

logger = logger.bind(name="Parse")


@Client.on_message(
    filters.command(["jx", "jxjx", "raw", "zip"])
    | (
        (filters.text | filters.caption)
        & ~via_me_filter
        & url_only_filter
        & platform_filter(True)
        & ~forwarded_from_bot_filter
        & allow_channel_auto_forward_parse_filter
    )
)
async def parse(cli: Client, msg: Message) -> None:
    bypass_cache = False
    lang = None
    mode = ParseMode.PREVIEW

    async with get_session() as session:
        if msg.from_user:
            lang = await UserService(session).ensure_lang(msg.from_user.id, msg.from_user.language_code)
        config = await SettingsService(session).get_config(get_config_target(msg))
        mode = config.default_mode

        # **非同群调用要鉴权**（2026-10-12 权限模型）: 群内发链接是 bot 的群内自动匹配
        # 触发机制, 不鉴权; 私聊（含 /jx /jxjx /raw /zip 命令）属于非同群调用,
        # 只认**本地库白名单**（``users.role`` 非空 ∪ 配置超管）—— 不再检查同群。
        if msg.chat and msg.chat.type == ChatType.PRIVATE:
            allowed = await AdminUserService(session).is_whitelisted(msg.from_user.id if msg.from_user else None)
            if not allowed:
                user_id = msg.from_user.id if msg.from_user else None
                logger.info(f"私聊解析被拦截(不在白名单): user_id={user_id}, msg_id={msg.id}")
                await MessageSender(cli, msg, config).text(t_[lang]("仅限白名单用户使用"))
                return

    _t = t_[lang]

    if msg.command:
        match msg.command[0]:
            case "raw":
                mode = ParseMode.RAW
            case "jx":
                mode = ParseMode.PREVIEW
            case "jxjx":
                mode = ParseMode.PREVIEW
                bypass_cache = True
            case "zip":
                mode = ParseMode.ZIP

        text = " ".join(msg.command[1:]) if msg.command[1:] else ""
        if not text and msg.reply_to_message:
            text = msg.reply_to_message.text or msg.reply_to_message.caption or ""
        if not text:
            await MessageSender(cli, msg, config).text(format_label(_t("请加上链接或回复一条消息")))
            return
    else:
        text = msg.text or msg.caption or ""

    # 手动打码开关: 链接后跟独立的 #nsfw / #spoiler —— 先剥掉, 免得它被当成不支持的平台
    text, spoiler_tag = strip_spoiler_flag(text)
    lines = text.strip().split()
    urls = list({i for i in lines if ParseService().parser.get_platform(i)})[:10]

    if not urls:
        await MessageSender(cli, msg, config).text(format_label(_t("不支持的平台")))
        return

    custom_content = ""
    if len(urls) == 1 and config.custom_content and msg.text:
        raw_text = msg.text
        if msg.entities:
            raw_text = cli.parser.unparse(msg.text, msg.entities, is_html=True)  # type: ignore[assignment]
        items: list[str] = [m.group(2) for m in re.finditer(r"^(={2})(.*?)\1", raw_text, flags=re.S | re.M)]
        custom_content = items[0].strip() if items else ""

    tasks = [
        _handle_parse_request(
            ParseRequest(
                cli=cli,
                msg=msg,
                url=url,
                mode=mode,
                config=config,
                t_=_t,
                bypass_cache=bypass_cache,
                delete_share_url_msg=config.auto_delete_url,
                custom_content=custom_content,
                spoiler_tag=spoiler_tag,
            )
        )
        for url in urls
    ]
    await asyncio.gather(*tasks)


async def _post_text(
    sender: MessageSender,
    reporter: MessageStatusReporter,
    caption: str,
    markup: Any = None,
) -> bool:
    """把纯文本结果编辑进状态消息; 成功返回 True, 需要调用方新建时返回 False。"""
    try:
        if await reporter.finalize_text(format_label(caption), reply_markup=markup) is not None:
            return True
    except Exception as e:  # noqa: BLE001 - 收尾失败就回退新建, 不能让结果丢失
        logger.warning(f"编辑状态消息为文本结果失败, 改为新建: {type(e).__name__}: {e}")
    return False


async def parse_url(cli: Client, msg: Message, url: str) -> None:
    """按用户默认设置解析单个链接并发送。

    给"非文本入口"用: 走与普通消息完全相同的解析/发送流程 (含限流与状态消息)。
    """
    async with get_session() as session:
        if msg.from_user:
            lang = await UserService(session).ensure_lang(msg.from_user.id, msg.from_user.language_code)
        else:
            lang = bs.language
        config = await SettingsService(session).get_config(get_config_target(msg))
    _t = t_[lang]
    await _handle_parse_request(
        ParseRequest(
            cli=cli,
            msg=msg,
            url=url,
            mode=config.default_mode,
            config=config,
            t_=_t,
        )
    )


@with_request_id
async def _handle_parse_request(req: ParseRequest) -> None:
    try:
        r = await handle_parse(req)
    except ParseRateLimitExceeded as e:
        if e.should_notify:
            logger.warning(f"速率限制 {e.retry_after:.1f}s, chat_id={req.chat_id}, msg_id={req.msg.id}")
            text = format_label(req.t_(f"解析过于频繁, 请在 {e.retry_after:.1f}s 后重试"))
            if bs.demo_mode:
                text += req.t_(
                    "\n\n>**为保障所有用户的使用体验, 当前已启用速率限制**\n\n"
                    ">本项目为开源项目, 如有高频或批量解析需求, 建议自行部署实例, "
                    "以免触发 Telegram API 全局速率限制\n\n"
                    "**开源地址: [GitHub](https://github.com/BraveSail/shirobako)**"
                )
            await MessageSender(req.cli, req.msg, req.config).delete_after(e.retry_after).text_no_preview(text)
    else:
        if not r:
            return
        if req.delete_share_url_msg:
            logger.debug(f"自动删除分享链接消息: chat_id={req.chat_id}, msg_id: {req.msg.id}")
            try:
                await req.msg.delete()
            except Exception as e:
                logger.warning(f"删除分享链接消息失败: chat_id={req.chat_id}, msg_id: {req.msg.id}, error: {e}")


async def _try_send_cached(
    sender: MessageSender,
    cached: Any,
    raw_url: str,
    req: ParseRequest,
) -> bool:
    """试着用缓存的 file_id 直接发; 返回 True 表示已发出。

    **失败要清掉这条缓存再继续走解析**, 不能直接把失败抛给用户: Telegram 官方
    明确说 file_id 可能失效、不建议长期存储 —— 真失效时旧代码只 `return False`,
    用户**什么都收不到**(只留一条日志)。清掉记录后正常流程会重新下载并重建缓存。
    """
    try:
        await send_cached(
            sender,
            cached,
            raw_url,
            custom_content=req.custom_content,
            _t=req.t_,
            spoiler_tag=req.spoiler_tag,
        )
    except Exception as e:
        logger.exception(e)
        logger.warning(f"从缓存发送失败 (file_id 可能已失效), 清除缓存并重新解析: url={raw_url}")
        try:
            await persistent_cache.remove(raw_url)
        except Exception as ce:  # noqa: BLE001 - 清缓存失败不该挡住重新解析
            logger.warning(f"清除失效缓存失败: {type(ce).__name__}: {ce}")
        return False
    logger.debug("file_id 缓存发送成功")
    return True


def _get_parse_user_id(req: ParseRequest) -> int | None:
    return req.chat_id


@parse_rate_limit(_get_parse_user_id)
async def handle_parse(req: ParseRequest) -> bool:
    options = ParseOptions.from_mode(req.mode, bypass_cache=req.bypass_cache)
    logger.info(
        f"收到解析请求: url={req.url}, chat_id={req.chat_id}, msg_id={req.msg.id}, "
        f"mode={req.mode}, spoiler_tag={req.spoiler_tag!r}"
    )
    if req.bypass_cache:
        logger.debug("bypass_cache=True 绕过缓存")

    reporter = MessageStatusReporter(
        req.cli,
        req.msg,
        t=req.t_,
        config=req.config,
        on_forbidden=disable_progress_on_report_forbidden,
        # 处理过程要和最终结果同一种排版: 骨架阶段要显示来源, 打码也要从第一秒就生效
        raw_url=req.url,
        spoiler_tag=req.spoiler_tag,
        custom_content=req.custom_content,
    )
    sender = MessageSender(req.cli, req.msg, req.config)
    try:
        raw_url = await ParseService().get_raw_url(req.url)
    except Exception as e:
        await reporter.report_error(req.t_("获取原始链接"), e)
        return False

    if options.use_caching and not req.bypass_cache and (cached := await persistent_cache.get(raw_url)):
        logger.debug("file_id 缓存命中, 直接发送")
        if await _try_send_cached(sender, cached, raw_url, req):
            return True
        # 缓存失效: 已清掉那条记录, 继续往下走正常解析 (重新下载并重建缓存)

    cached_parse_result = None if req.bypass_cache else await parse_cache.get(raw_url)
    with ParsePipeline(
        req.url,
        raw_url,
        reporter,
        parse_result=cached_parse_result,
        singleflight=options.singleflight,
        skip_media_processing=options.skip_media_processing,
        gif_only_skip_download_count_threshold=options.gif_only_skip_download_count_threshold,
        save_metadata=options.save_metadata,
        t=req.t_,
    ) as pipeline:
        if (result := await pipeline.run()) is None:
            if pipeline.waited:
                logger.debug("Singleflight 等待完成, 重新检查缓存")
                if not req.bypass_cache and (cached := await persistent_cache.get(raw_url)):
                    if await _try_send_cached(sender, cached, raw_url, req):
                        return True
                # 缓存失效或没有缓存: 重新走一遍解析
                return await handle_parse(replace(req, delete_share_url_msg=False))

            else:
                logger.debug("Pipeline 返回 None, 跳过后续处理")
            return False

        parse_result = result.parse_result
        await parse_cache.set(raw_url, parse_result)

        if isinstance(parse_result, RichTextParseResult):
            # 长文与普通结果同一条路径: 正文取 markdown_content, 排版交给富文本
            await sender.typing()
            # 结果直接编辑进状态消息: 聊天里只留一条消息, 不新建也不删除
            await send_rich_media(
                sender,
                parse_result,
                result.processed_list,
                _t=req.t_,
                custom_content=req.custom_content,
                raw_url=raw_url,
                reporter=reporter,
                spoiler_tag=req.spoiler_tag,
            )
            return True

        caption = build_caption(
            parse_result,
            config=req.config,
            custom_content=req.custom_content,
            lang=req.t_.locale,
            view_label=req.t_("查看"),
        )
        media_refs = to_list(parse_result.media)
        # 显式要求非空: all() 对空列表返回 True, 不能让它把"没有媒体"当成"全是 GIF"
        gif_only = bool(media_refs) and all(isinstance(i, AniRef) for i in media_refs)
        if (
            req.mode == ParseMode.PREVIEW
            and gif_only
            and len(media_refs) > GIF_ONLY_SKIP_DOWNLOAD_COUNT_THRESHOLD
        ):
            await sender.typing()
            markup = build_gif_button(to_list(parse_result.media))
            if not await _post_text(sender, reporter, caption, markup):
                await sender.text_no_preview(caption, reply_markup=markup)
            return True

        if not result.processed_list:
            logger.debug("无媒体文件, 仅发送文本")
            await sender.typing()
            # 富文本的正文是 Telegram 服务端解析的 markdown, 与缓存里的 caption 格式不同,
            # 所以这条路径不写缓存 (下次重新解析)
            await send_rich_media(
                sender,
                parse_result,
                [],
                _t=req.t_,
                custom_content=req.custom_content,
                raw_url=raw_url,
                reporter=reporter,
                spoiler_tag=req.spoiler_tag,
            )
            return True

        if req.mode == ParseMode.RAW:
            await send_raw(sender, result, reporter, _t=req.t_, custom_content=req.custom_content)
            return True
        if req.mode == ParseMode.ZIP:
            await send_zip(sender, result, reporter, _t=req.t_, custom_content=req.custom_content)
            return True

        logger.debug(f"开始上传媒体: media_count={len(result.processed_list)}")
        await reporter.report_result(parse_result, req.t_("上 传 中..."))
        try:
            # 敏感内容的媒体打码在 send_rich_media 内部切到 blocks 路径 (官方 API 的
            # 富文本媒体块没有 spoiler, 只有 raw 的 PageBlockPhoto/Video 有)
            # 结果编辑进状态消息, 不再新建 + 删除
            await send_rich_media(
                sender,
                parse_result,
                result.processed_list,
                _t=req.t_,
                custom_content=req.custom_content,
                raw_url=raw_url,
                reporter=reporter,
                spoiler_tag=req.spoiler_tag,
            )
            return True
        except Exception as e:
            logger.exception(e)
            logger.error("上传失败, 以上为错误信息")
            await reporter.report_error(req.t_("上传"), e)
            return False
