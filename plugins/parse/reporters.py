from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any

from easy_ai18n import PreLocaleSelector
from pyrogram import Client
from pyrogram.errors import FloodWait, Forbidden, SlowmodeWait
from pyrogram.types import InputRichMessage, LinkPreviewOptions, Message

from core import bs
from db import get_session
from log import logger
from plugins.context import get_config_target
from plugins.helpers import build_progress_markdown, format_label
from plugins.parse.inline_rich import edit_inline_rich_message
from plugins.parse.sender import MessageSender
from repo.settings import SettingsConfig
from services import SettingsService, StatusReporter

logger = logger.bind(name="ParseReporter")

#: 状态消息收尾时编辑成的内容。
#:
#: **收尾用编辑、不用删除**: 删除会在 Telegram 的后台留下删除记录 (用户明确要求避免)。
#: 也不能编辑成空串/零宽字符/空格 —— 实测 Telegram 一律回 ``400 MESSAGE_EMPTY``
#: (U+200B、U+200D、空格、空串都试过), 所以只能用最小的可见字符。
STATUS_DONE_TEXT = "·"

#: 状态/占位消息一律关掉链接预览。
#:
#: 不关的话, 只要消息文本里带链接 (文本结果、GIF 提示、错误信息里的 URL), Telegram
#: 就会在消息下面挂一张 **link preview 卡片** —— 卡片在正文之外, 折叠块盖不住它,
#: 于是"内容已经遮住了但封面图还露在外面" (用户报「不然还是能看到图」)。
#:
#: 实测: 含链接的文本消息不传该参数 -> ``web_page`` 有值; 传 ``is_disabled=True`` -> 无。
#: 富文本消息本身不生成预览 (实测), 但这里一并传, 免得编辑时把旧预览留下来。
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

#: 处理过程的富文本更新最小间隔 (秒)。
#:
#: 下载进度回调会**很密集**地调 ``report()``, 而富文本编辑比纯文本重得多
#: (重渲染整篇 markdown + 更大 payload), 太密会被 Telegram 限流。
#: 阶段切换之间的间隔通常远大于此值, 所以用户该看到的阶段提示不会丢;
#: 丢掉的是阶段内部的百分比刷新 (末尾仍会停在最后一个能发出的值)。
_MIN_PROGRESS_INTERVAL = 1.5


async def disable_progress_on_report_forbidden(msg: Message, config: SettingsConfig) -> None:
    """状态消息无权限时自动关闭解析进度。"""
    config.noprogress = True
    target = get_config_target(msg, include_member=False)
    async with get_session() as session:
        await SettingsService(session).patch_config(target=target, noprogress=True)
    logger.warning(f"已自动关闭解析进度: {target}")


class MessageStatusReporter(StatusReporter):
    """基于 Telegram Message 的状态报告器"""

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
        custom_content: str = "",
    ):
        self._cli = cli
        self._user_msg = user_msg
        self._msg: Message | None = None
        self._t = t
        self._config = config
        self._on_forbidden = on_forbidden
        # 渲染处理过程富文本要用的上下文 (骨架阶段的来源链接、打码标记、自定义内容)
        self._raw_url = raw_url
        self._spoiler_tag = spoiler_tag
        self._custom_content = custom_content
        self._last_markdown: str | None = None
        self._last_rich_at = 0.0
        # 最后一次拿到的解析结果 —— 进度刷新要用它渲染完整排版, 不能退回骨架
        self._parse_result: Any = None

    def _render(self, parse_result: Any, text: str) -> str:
        """把处理过程渲染成与最终结果**同一种排版**的富文本。"""
        return build_progress_markdown(
            parse_result,
            progress=format_label(text),
            config=self._config,
            lang=self._t.locale,
            view_label=self._t("查看"),
            spoiler_tag=self._spoiler_tag,
            custom_content=self._custom_content,
            raw_url=self._raw_url,
        )

    async def report(self, text: str) -> None:
        """**阶段切换**: 还没有解析结果 -> 同版式的骨架。必发 (不被节流)。"""
        if self._config.noprogress:
            return
        await self._send_rich(self._render(None, text))

    async def report_progress(self, text: str) -> None:
        """进度刷新 (下载百分比等): 内容密集变化, 走节流。

        复用**最近一次**的结果渲染: 下载阶段的进度刷新必须保持"完整排版",
        退回骨架的话每刷一次进度就跳一次版。
        """
        if self._config.noprogress:
            return
        await self._send_rich(self._render(self._parse_result, text), throttle=True)

    async def report_result(self, parse_result: Any, text: str) -> None:
        """**已有解析结果** -> 完整排版 (标题/作者/正文/标签/页脚), 只差媒体。必发。"""
        if self._config.noprogress:
            return
        self._parse_result = parse_result
        await self._send_rich(self._render(parse_result, text))

    async def _send_rich(self, markdown: str, *, throttle: bool = False) -> None:
        """把处理过程作为**富文本**发送/编辑 (与最终结果同一种排版)。

        判重靠自己记录的 markdown 而不是 ``msg.text`` —— 富文本消息的 ``text`` 是空的,
        用它会每次都不相等、每次都编辑。

        :param throttle: 进度刷新用 —— 距上次编辑不足 ``_MIN_PROGRESS_INTERVAL`` 就丢弃。
            **阶段切换与结果不走节流**, 否则"解析中 → 下载中"这种快切换会被吞掉。
        """
        if not markdown or markdown == self._last_markdown:
            return
        if throttle and monotonic() - self._last_rich_at < _MIN_PROGRESS_INTERVAL:
            return
        self._last_markdown = markdown
        self._last_rich_at = monotonic()
        try:
            if self._msg is None:
                self._msg = await MessageSender(self._cli, self._user_msg, self._config).rich_message(
                    rich_message=InputRichMessage(markdown=markdown)
                )
            else:
                await self._msg.edit_text(
                    rich_message=InputRichMessage(markdown=markdown), link_preview_options=_NO_PREVIEW
                )
        except (FloodWait, SlowmodeWait):
            pass
        except Forbidden as e:
            logger.warning(f"状态消息发送失败, Bot 无权限: {e}")
            if self._on_forbidden:
                await self._on_forbidden(self._user_msg, self._config)
        except Exception as e:  # noqa: BLE001
            # 状态消息是尽力而为: 被用户删掉、或编辑被拒, 都不该打断解析本身
            logger.debug(f"状态消息更新失败 (已忽略): {type(e).__name__}: {e}")

    async def report_error(self, stage: str, error: Exception) -> None:
        if self._config.hide_error:
            return

        t = format_label(self._t(f"{stage}错误:"))
        text = self._t(f"{t} \n```\n{error}```")
        if bs.demo_mode:
            text += self._t("\n\n<b>问题反馈: @MisakaSisters</b>")
        await self._edit_text(
            text,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        # 错误信息**永久保留** —— 以前这里起一个 15 秒的任务调 `dismiss()`, 把错误
        # 编辑成 ``·``（STATUS_DONE_TEXT）: 用户刚看到"出错了"就变成一个点, 原因被
        # 吞掉（用户明确要求删掉这个行为: "出错最后编辑成一个点这个删了，保留错误信息"）。

    @property
    def has_message(self) -> bool:
        """是否已经发出过状态消息 (noprogress 或首条发送失败时为 False)。"""
        return self._msg is not None

    async def finalize(self, rich_message: InputRichMessage) -> Message | None:
        """把状态消息**编辑成最终结果** (同一条消息走完整个流程)。

        这样聊天里只留一条消息 —— 从"解析中"一路编辑到最终富文本, 既没有删除记录,
        也没有多余的临时消息。

        没有状态消息时返回 None (调用方改为新建); 编辑失败**不吞异常** ——
        由调用方决定回退(新建)还是报错, 免得结果静默丢失。
        """
        if self._msg is None:
            return None
        logger.debug(f"编辑状态消息为最终结果: msg_id={self._msg.id}")
        # 富文本自身不生成预览 (实测), 但这里显式关掉 —— 免得把状态消息阶段可能留下的
        # 预览保留下来 (编辑时不传该参数 = 保持原值)
        edited = await self._msg.edit_text(rich_message=rich_message, link_preview_options=_NO_PREVIEW)
        return edited or self._msg

    async def finalize_text(self, text: str, **kwargs: Any) -> Message | None:
        """把状态消息编辑成**纯文本**结果 (不新建消息)。

        给不走富文本的结果用 (例如 GIF 过多只发文字提示那种)。失败不吞异常 ——
        调用方据此回退新建。
        """
        if self._msg is None:
            return None
        # 文本结果里常带来源链接 —— 不关预览就会挂出一张封面卡片 (折叠盖不到)
        kwargs.setdefault("link_preview_options", _NO_PREVIEW)
        logger.debug(f"编辑状态消息为文本结果: msg_id={self._msg.id}")
        edited = await self._msg.edit_text(text, **kwargs)
        return edited or self._msg

    async def dismiss(self) -> None:
        """收尾: 把状态消息编辑成最小标记, **而不是删除**。

        删除会在聊天后台留下"消息已删除"的记录; 编辑不会。用户明确要求避免前者。

        这里吞掉所有异常: dismiss 是流程末尾的清理动作, 它失败不该让调用方把
        已经成功的解析误判成失败 (handlers 里 dismiss 在 try 块内, 抛错会被当成"上传失败")。
        """
        if self._msg is None:
            return
        try:
            await self._edit_text(STATUS_DONE_TEXT)
        except Exception as e:  # noqa: BLE001 - 清理动作不该影响结果
            logger.debug(f"状态消息收尾失败 (已忽略): {type(e).__name__}: {e}")

    async def _edit_text(self, text: str, **kwargs: Any) -> None:
        # 状态/占位消息默认关预览: 文本里万一带链接, 预览卡片会把内容露在折叠之外
        kwargs.setdefault("link_preview_options", _NO_PREVIEW)
        try:
            if self._msg is None:
                self._msg = await MessageSender(self._cli, self._user_msg, self._config).text(text, **kwargs)
            else:
                if self._msg.text != text:
                    await self._msg.edit_text(text, **kwargs)
        except (FloodWait, SlowmodeWait):
            pass
        except Forbidden as e:
            logger.warning(f"状态消息发送失败, Bot 无权限: {e}")
            if self._on_forbidden:
                await self._on_forbidden(self._user_msg, self._config)
        except Exception as e:  # noqa: BLE001
            # 状态消息是尽力而为: 它被用户手动删掉、或编辑被拒, 都不该打断解析本身
            logger.debug(f"状态消息更新失败 (已忽略): {type(e).__name__}: {e}")


class InlineStatusReporter(StatusReporter):
    """基于 inline_message_id 的状态报告器

    **只负责编辑**：句柄由调用方拿到后传进来 —— inline 是回调给的
    （``chosen_result.inline_message_id``），guest 是自己 answer 首帧拿的
    （``answer_guest_query`` 返回的 ``SentGuestMessage.inline_message_id``）。
    两条链路因此完全同构。
    """

    def __init__(
        self,
        cli: Client,
        inline_message_id: str,
        *,
        t: PreLocaleSelector,
        user_config: SettingsConfig,
        raw_url: str = "",
        spoiler_tag: str = "",
        custom_content: str = "",
    ):
        self._cli = cli
        self._mid = inline_message_id
        self._t = t
        self._user_config = user_config
        self._raw_url = raw_url
        self._spoiler_tag = spoiler_tag
        self._custom_content = custom_content
        self._last_markdown: str | None = None
        self._last_rich_at = 0.0
        # 最后一次拿到的解析结果 —— 进度刷新要用它渲染完整排版, 不能退回骨架
        self._parse_result: Any = None
        # 报错后要恢复的内容 (按下"回退"时的占位)
        self._last_text: str | None = None

    def _render(self, parse_result: Any, text: str) -> str:
        """与最终结果**同一种排版**的处理过程 markdown。"""
        return build_progress_markdown(
            parse_result,
            progress=format_label(text),
            config=self._user_config,
            lang=self._t.locale,
            view_label=self._t("查看"),
            spoiler_tag=self._spoiler_tag,
            custom_content=self._custom_content,
            raw_url=self._raw_url,
        )

    async def report(self, text: str) -> None:
        """**阶段切换**: 还没有解析结果 -> 同版式的骨架。必发。"""
        self._last_text = format_label(text)
        await self._edit_rich(self._render(None, text))

    async def report_progress(self, text: str) -> None:
        """进度刷新 (下载百分比等): 内容密集变化, 走节流 (复用最近的结果渲染)。"""
        self._last_text = format_label(text)
        await self._edit_rich(self._render(self._parse_result, text), throttle=True)

    async def report_result(self, parse_result: Any, text: str) -> None:
        """**已有解析结果** -> 完整排版 (标题/作者/正文/标签/页脚), 只差媒体。必发。"""
        self._parse_result = parse_result
        await self._edit_rich(self._render(parse_result, text))

    async def _edit_rich(self, markdown: str, *, throttle: bool = False) -> None:
        """把处理过程编辑成**富文本** (与最终结果同一种排版)。

        顺带把结果项上的键盘摘掉 —— ``edit_inline_rich_message`` 内部就是
        ``EditInlineBotMessage(..., reply_markup=ReplyKeyboardHide())``,
        所以第一次进度更新时按钮就消失了 (比等结果出来再摘更早)。
        """
        if not markdown or markdown == self._last_markdown:
            return
        if throttle and monotonic() - self._last_rich_at < _MIN_PROGRESS_INTERVAL:
            return
        self._last_markdown = markdown
        self._last_rich_at = monotonic()
        try:
            await edit_inline_rich_message(self._cli, self._mid, markdown=markdown)
        except (FloodWait, SlowmodeWait):
            pass
        except Forbidden as e:
            logger.warning(f"消息发送失败, Bot 无权限: {e}")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"inline 处理过程更新失败 (已忽略): {type(e).__name__}: {e}")

    async def report_error(self, stage: str, error: Exception) -> None:
        if self._user_config.hide_error:
            return

        text = self._t(f"{format_label(f'{stage}错误:')} \n```\n{error}```")
        if bs.demo_mode:
            text += self._t("\n\n<b>问题反馈: @MisakaSisters</b>")
        await self._edit_inline_text(text=text)
        # 错误信息**永久保留** —— 以前这里 15 秒后把消息编辑回"报错前那条处理过程富文本"，
        # 等于把失败原因覆盖掉（用户要求："保留错误信息"）。失败就该一直看得见原因。

    async def _edit_inline_text(self, **kwargs: Any) -> None:
        # 与 MessageStatusReporter._edit_text 同一个理由: 文本里带链接时 Telegram 会挂
        # link preview 卡片, 卡片在正文之外、折叠盖不住 -> 内容遮了图还露着。
        # 进度/结果/收尾都走这里, 默认关掉; 调用方显式传了就用调用方的。
        kwargs.setdefault("link_preview_options", _NO_PREVIEW)
        try:
            await self._cli.edit_inline_text(inline_message_id=self._mid, **kwargs)
        except (FloodWait, SlowmodeWait):
            pass
        except Forbidden as e:
            logger.warning(f"消息发送失败, Bot 无权限: {e}")

    async def dismiss(self) -> None:
        pass
