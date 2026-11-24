from collections.abc import Awaitable, Callable
from html import escape
from time import monotonic
from typing import Any

from easy_ai18n import PreLocaleSelector
from parsehub import ParseHub
from pyrogram import Client
from pyrogram.errors import FloodWait, Forbidden, SlowmodeWait
from pyrogram.types import (
    InlineKeyboardMarkup,
    InputRichBlock,
    InputRichBlockDivider,
    InputRichBlockFooter,
    InputRichBlockParagraph,
    InputRichBlockPreformatted,
    InputRichMessage,
    LinkPreviewOptions,
    Message,
    RichTextBold,
    RichTextUrl,
)

from core import bs
from db import get_session
from log import logger
from plugins.context import get_config_target
from plugins.helpers import build_progress_markdown, format_label
from plugins.parse.inline_rich import build_delivery_blocks, edit_inline_rich_message, has_structured_body
from plugins.parse.notes import ParseNote, append_content_blocks, note_block
from plugins.parse.notices import notice_label, rich_notice
from plugins.parse.rich_blocks import markdown_to_blocks
from plugins.parse.sender import MessageSender
from repo.settings import SettingsConfig
from services import SettingsService, StatusReporter

logger = logger.bind(name="ParseReporter")

# 收尾用编辑、不用删除（避免 Telegram 后台的删除记录）。空串或零宽字符会被拒绝。
STATUS_DONE_TEXT = "·"

# 富文本新消息本身不生成预览；编辑时显式关闭，避免保留旧预览卡片。
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# 阶段切换不受节流；只限制下载百分比等密集更新。
_MIN_PROGRESS_INTERVAL = 1.5


async def disable_progress_on_report_forbidden(msg: Message, config: SettingsConfig) -> None:
    """状态消息无权限时自动关闭解析进度。"""
    config.noprogress = True
    target = get_config_target(msg, include_member=False)
    async with get_session() as session:
        await SettingsService(session).patch_config(target=target, noprogress=True)
    logger.warning(f"已自动关闭解析进度: {target}")


def _render_progress(
    parse_result: Any,
    text: str,
    *,
    t: PreLocaleSelector,
    config: SettingsConfig,
    raw_url: str,
    spoiler_tag: str,
    custom_content: str | ParseNote,
) -> list[InputRichBlock]:
    if parse_result is None:
        blocks: list[InputRichBlock] = [notice_label(text)]
        if raw_url:
            platform = ParseHub().get_platform(raw_url)
            display = platform.display_name if platform else ""
            _t = t
            if display and t.locale:
                label = _t(f"来源（{display}）")
            else:
                label = _t("来源") if t.locale else "Source"
            blocks.extend([InputRichBlockDivider(), InputRichBlockFooter(RichTextUrl(label, raw_url))])
        return blocks

    options = {
        "config": config,
        "lang": t.locale,
        "view_label": t("查看"),
        "custom_content": custom_content,
    }
    if has_structured_body(parse_result):
        # The native footer turns this app-owned label envelope into RichTextBold.
        return build_delivery_blocks(
            parse_result, progress=format_label(text), hide_content=spoiler_tag, raw_url=raw_url, **options,
        )
    # Old cache entries can still have legacy bodies. Adapt them once, before dispatch.
    options["custom_content"] = custom_content if isinstance(custom_content, str) else ""
    rendered = markdown_to_blocks(build_progress_markdown(
        parse_result, progress=format_label(escape(text)), spoiler_tag=spoiler_tag, **options,
    ))
    if isinstance(custom_content, ParseNote) and custom_content:
        append_content_blocks(rendered, [note_block(custom_content)], spoiler_tag)
    return rendered


async def _progress_signature(rich_message: InputRichMessage) -> bytes:
    # Progress blocks contain no media. Compare every block, including the changing footer,
    # using local TL serialization, not the SDK object's printable representation.
    rich = await rich_message.write(client=None)
    return rich.write()


def _error_notice(_t: PreLocaleSelector, stage: str, error: Exception) -> InputRichMessage:
    blocks: list[InputRichBlock] = [notice_label(_t(f"{stage}错误:"))]
    if detail := str(error):
        blocks.append(InputRichBlockPreformatted(detail))
    if bs.demo_mode:
        # Strip the fixed formatting of the existing translation key, not the error string.
        feedback = _t("\n\n<b>问题反馈: @MisakaSisters</b>").strip().removeprefix("<b>").removesuffix("</b>")
        label, handle, tail = feedback.partition("@MisakaSisters")
        parts = [label, RichTextUrl(handle, "https://t.me/MisakaSisters"), tail] if handle else [feedback]
        blocks.append(InputRichBlockParagraph(RichTextBold(parts)))
    return InputRichMessage(blocks=blocks)


class MessageStatusReporter(StatusReporter):
    """向聊天发送首条状态消息，随后编辑同一条消息。"""

    def __init__(
        self,
        cli: Client,
        user_msg: Message,
        *,
        t: PreLocaleSelector,
        config: SettingsConfig,
        on_forbidden: Callable[[Message, SettingsConfig], Awaitable[None]] | None = None,
        raw_url: str = "",
        spoiler_tag: str = "",
        custom_content: str | ParseNote = "",
    ):
        self._cli = cli
        self._user_msg = user_msg
        self._msg: Message | None = None
        self._t = t
        self._config = config
        self._on_forbidden = on_forbidden
        self._raw_url = raw_url
        self._spoiler_tag = spoiler_tag
        self._custom_content = custom_content
        self._last_signature: bytes | None = None
        self._last_rich_at = 0.0
        # 进度刷新复用最后一次拿到的解析结果，不能退回骨架。
        self._parse_result: Any = None

    def _render(self, parse_result: Any, text: str) -> InputRichMessage:
        return InputRichMessage(blocks=_render_progress(
            parse_result,
            text,
            t=self._t,
            config=self._config,
            raw_url=self._raw_url,
            spoiler_tag=self._spoiler_tag,
            custom_content=self._custom_content,
        ))

    async def report(self, text: str) -> None:
        """阶段切换：没有解析结果时显示骨架，不节流。"""
        if not self._config.noprogress:
            await self._send_rich(self._render(None, text))

    async def report_progress(self, text: str) -> None:
        """下载百分比等密集更新，复用最近的结果渲染。"""
        if not self._config.noprogress:
            await self._send_rich(self._render(self._parse_result, text), throttle=True)

    async def report_result(self, parse_result: Any, text: str) -> None:
        """把解析结果按最终版式展示（尚无媒体）。"""
        if not self._config.noprogress:
            self._parse_result = parse_result
            await self._send_rich(self._render(parse_result, text))

    async def _send_rich(
        self, rich_message: InputRichMessage, *, throttle: bool = False, keep_result: bool = False,
    ) -> None:
        if not rich_message.blocks or (throttle and monotonic() - self._last_rich_at < _MIN_PROGRESS_INTERVAL):
            return
        try:
            signature = await _progress_signature(rich_message)
            if signature == self._last_signature:
                return
            self._last_signature = signature
            self._last_rich_at = monotonic()
            if self._msg is None or keep_result:
                notice = await MessageSender(self._cli, self._user_msg, self._config).rich_message(
                    rich_message=rich_message,
                )
                if not keep_result:
                    self._msg = notice
            else:
                await self._msg.edit_text(rich_message=rich_message, link_preview_options=_NO_PREVIEW)
        except (FloodWait, SlowmodeWait):
            pass
        except Forbidden as e:
            logger.warning(f"状态消息发送失败, Bot 无权限: {e}")
            if self._on_forbidden:
                await self._on_forbidden(self._user_msg, self._config)
        except Exception as e:  # noqa: BLE001
            # 被用户删除或编辑遭拒不应打断解析。
            logger.debug(f"状态消息更新失败 (已忽略): {type(e).__name__}: {e}")

    async def report_error(self, stage: str, error: Exception, *, keep_result: bool = False) -> None:
        if not self._config.hide_error:
            await self._send_rich(_error_notice(self._t, stage, error), keep_result=keep_result)

    @property
    def has_message(self) -> bool:
        """是否已经发出过状态消息 (noprogress 或首条发送失败时为 False)。"""
        return self._msg is not None

    async def finalize(
        self, rich_message: InputRichMessage, *, reply_markup: InlineKeyboardMarkup | None = None,
    ) -> Message | None:
        """原地编辑最终结果；无状态消息时返回 None，编辑失败交给发送层回退。"""
        if self._msg is None:
            return None
        logger.debug(f"编辑状态消息为最终结果: msg_id={self._msg.id}")
        kwargs: dict[str, Any] = {"rich_message": rich_message, "link_preview_options": _NO_PREVIEW}
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        edited = await self._msg.edit_text(**kwargs)
        return edited or self._msg

    async def dismiss(self) -> None:
        """收尾：把现有状态消息编辑成最小标记，不删除或新建。"""
        if self._msg is not None:
            try:
                await self._send_rich(rich_notice(STATUS_DONE_TEXT))
            except Exception as e:  # noqa: BLE001 - 清理动作不该影响结果
                logger.debug(f"状态消息收尾失败 (已忽略): {type(e).__name__}: {e}")


class InlineStatusReporter(StatusReporter):
    """只编辑调用方提供的 inline_message_id（inline 回调或 guest 首帧句柄）。"""

    def __init__(
        self,
        cli: Client,
        inline_message_id: str,
        *,
        t: PreLocaleSelector,
        user_config: SettingsConfig,
        raw_url: str = "",
        spoiler_tag: str = "",
        custom_content: str | ParseNote = "",
    ):
        self._cli = cli
        self._mid = inline_message_id
        self._t = t
        self._user_config = user_config
        self._raw_url = raw_url
        self._spoiler_tag = spoiler_tag
        self._custom_content = custom_content
        self._last_signature: bytes | None = None
        self._last_rich_at = 0.0
        self._parse_result: Any = None

    def _render(self, parse_result: Any, text: str) -> list[InputRichBlock]:
        return _render_progress(
            parse_result,
            text,
            t=self._t,
            config=self._user_config,
            raw_url=self._raw_url,
            spoiler_tag=self._spoiler_tag,
            custom_content=self._custom_content,
        )

    async def report(self, text: str) -> None:
        await self._edit_rich(self._render(None, text))

    async def report_progress(self, text: str) -> None:
        await self._edit_rich(self._render(self._parse_result, text), throttle=True)

    async def report_result(self, parse_result: Any, text: str) -> None:
        self._parse_result = parse_result
        await self._edit_rich(self._render(parse_result, text))

    async def _edit_rich(self, blocks: list[InputRichBlock], *, throttle: bool = False) -> None:
        if not blocks or (throttle and monotonic() - self._last_rich_at < _MIN_PROGRESS_INTERVAL):
            return
        try:
            signature = await _progress_signature(InputRichMessage(blocks=blocks))
            if signature == self._last_signature:
                return
            self._last_signature = signature
            self._last_rich_at = monotonic()
            # 第一次进度更新就摘掉结果项上的键盘，不等最终结果。
            await edit_inline_rich_message(self._cli, self._mid, blocks=blocks)
        except (FloodWait, SlowmodeWait):
            pass
        except Forbidden as e:
            logger.warning(f"消息发送失败, Bot 无权限: {e}")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"inline 处理过程更新失败 (已忽略): {type(e).__name__}: {e}")

    async def report_error(self, stage: str, error: Exception) -> None:
        if self._user_config.hide_error:
            return
        # 不恢复报错前的富文本；失败原因应一直可见。
        try:
            await self._cli.edit_inline_text(
                inline_message_id=self._mid,
                rich_message=_error_notice(self._t, stage, error),
                link_preview_options=_NO_PREVIEW,
            )
        except (FloodWait, SlowmodeWait):
            pass
        except Forbidden as e:
            logger.warning(f"消息发送失败, Bot 无权限: {e}")

    async def dismiss(self) -> None:
        pass
