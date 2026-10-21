"""状态消息的收尾: 编辑成最小标记, 而不是删除。

删除会在 Telegram 后台留下"消息已删除"的记录 (用户明确要求避免), 编辑不会。
`dismiss` 还必须吞掉异常 —— 它在 handlers 的 try 块里, 抛错会把已经成功的解析
误判成"上传失败"。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from plugins.parse.reporters import STATUS_DONE_TEXT, MessageStatusReporter


class _Msg:
    """够用的假 Message: 记录 edit_text / delete 的调用。"""

    def __init__(self, text="上 传 中...", msg_id=1):
        self.text = text
        self.id = msg_id
        self.edit_text = AsyncMock()
        self.delete = AsyncMock()
        self.answer = AsyncMock()
        # 处理过程的富文本首次是**新建** (MessageSender.rich_message), 之后才编辑。
        # 返回值必须是"这条消息"本身: 真实调用返回 Message, reporter 会拿它做后续编辑
        self.answer_rich = AsyncMock(return_value=self)
        self.reply_rich = AsyncMock(return_value=self)


def _config(**kw):
    """够用的假 SettingsConfig: 含渲染与发送路径会用到的字段。"""
    base = {
        "noprogress": False,
        "hide_error": False,
        "keep_error_log": False,
        "reply_msg": False,
        "hide_title": False,
        "hide_desc": False,
        "hide_source": False,
        "video_cover": False,
    }
    base.update(kw)
    return SimpleNamespace(**base)


class _T:
    """假的 t_ 选择器: 真实类型 (PreLocaleSelector) 同时可调用且有 .locale。"""

    locale = "zh-hans"

    def __call__(self, key, **kw):
        return key


def _reporter(msg=None, *, config=None, user_msg=None) -> MessageStatusReporter:
    reporter = MessageStatusReporter(
        cli=SimpleNamespace(),
        # 首次发富文本走它的 answer_rich; 测试可以注入同一个替身来断言
        user_msg=user_msg if user_msg is not None else _Msg(),
        t=_T(),
        config=config or _config(keep_error_log=True),
    )
    reporter._msg = msg
    return reporter


def test_done_text_is_a_real_visible_character():
    """不能用空串/零宽字符: Telegram 会回 400 MESSAGE_EMPTY (实测过)"""
    assert STATUS_DONE_TEXT
    assert STATUS_DONE_TEXT.strip() == STATUS_DONE_TEXT != ""
    assert not any(ch in STATUS_DONE_TEXT for ch in ("\u200b", "\u200d"))


def _no_preview(kwargs) -> bool:
    """这些调用是否带了「关掉链接预览」的参数"""
    opts = kwargs.get("link_preview_options")
    return opts is not None and getattr(opts, "is_disabled", False) is True


def test_dismiss_edits_instead_of_deleting():
    """核心契约: 收尾是编辑, 不是删除"""
    msg = _Msg()
    asyncio.run(_reporter(msg).dismiss())
    msg.edit_text.assert_awaited_once()
    assert msg.edit_text.await_args.args[0] == STATUS_DONE_TEXT
    msg.delete.assert_not_awaited()


def test_dismiss_without_a_status_message_does_nothing():
    """noprogress / 首条发送失败时没有状态消息, 不该炸"""
    asyncio.run(_reporter(None).dismiss())


def test_dismiss_swallows_errors():
    """编辑失败也要静默 —— 抛出去会被上层当成上传失败"""
    msg = _Msg()
    msg.edit_text.side_effect = RuntimeError("从服务器得到未知错误")
    asyncio.run(_reporter(msg).dismiss())  # 不抛即通过


def test_report_edits_the_same_message():
    """进度更新是编辑同一条消息 (收尾也走同一条, 所以整个流程只留一条)"""
    msg = _Msg(text="解 析 中...")
    reporter = _reporter(msg)
    asyncio.run(reporter.report("下 载 中..."))
    msg.edit_text.assert_awaited_once()
    assert "下 载 中..." in msg.edit_text.await_args.kwargs["rich_message"].markdown
    msg.delete.assert_not_awaited()


# ── 处理过程与最终结果是同一种排版 ─────────────────────────────────────
#
# 用户要求: 处理过程别再用老格式的小字 ("群里自动触发的只有处理中这三个字"),
# 要和最终结果一致 (原话「处理过程的消息能不能和最终消息保持一致」)。
# 做法是处理过程也发**富文本**, 主体 (标题/作者/正文/标签) 与结果一致,
# 只有页尾那段从"进度"变成"时间 · 统计"。


def test_progress_is_sent_as_a_rich_message():
    """处理过程必须是富文本, 不再是纯文本的 "▎解 析 中..." """
    msg = _Msg()
    asyncio.run(_reporter(None, user_msg=msg)._send_rich("**▎解 析 中...**"))
    assert msg.answer_rich.await_args.kwargs["rich_message"].markdown == "**▎解 析 中...**"


def test_the_stage_label_stays_html_because_the_footer_needs_it():
    """阶段标签在**页脚**里, 而 footer 块只解析 HTML 标签 —— **必须**是 ``<b>``。

    实测 (真机, 四种组合):

    | 位置 | 写法 | 结果 |
    | --- | --- | --- |
    | **footer** | ``**▎…**`` (markdown) | ✗ 字面显示, 用户看到多余的 ``**`` |
    | **footer** | ``<b>▎…</b>`` | ✓ RichTextBold |
    | 正文 | ``**▎…**`` | ✓ RichTextBold |
    | 正文 | ``<b>▎…</b>`` | ✓ RichTextBold |

    (与"footer 里 markdown 链接不解析"同一个坑。) 曾经为了迁就 blocks 路径
    把它改成 ``**`` —— 那是**改错了方向**: 正确做法是让 blocks 的 ``parse_inline``
    认 ``<b>``/``<i>``, 而不是改坏 markdown 路径。
    """
    from plugins.helpers import format_label

    assert format_label("解 析 中...") == "<b>▎解 析 中...</b>"

    msg = _Msg()
    reporter = _reporter(None, user_msg=msg)
    reporter._raw_url = "https://x.com/a/status/1"
    asyncio.run(reporter.report("解 析 中..."))
    markdown = msg.answer_rich.await_args.kwargs["rich_message"].markdown
    assert "<b>▎解 析 中...</b>" in markdown
    assert "**▎" not in markdown


def test_a_stage_without_a_result_renders_a_skeleton():
    """还没有结果时给骨架 —— 结构与有结果时一致 (正文 + 页脚来源), 不跳版"""
    msg = _Msg()
    reporter = _reporter(None, user_msg=msg)
    reporter._raw_url = "https://x.com/a/status/1"
    asyncio.run(reporter.report("解 析 中..."))
    markdown = msg.answer_rich.await_args.kwargs["rich_message"].markdown
    assert "解 析 中..." in markdown
    assert "<footer>" in markdown  # 页脚的来源链接在, 与最终结果同结构


def test_a_stage_with_a_result_uses_the_full_layout():
    """有结果后: 标题/作者/正文/标签全部就位, 只差媒体 —— 与最终结果同版式"""
    import types

    msg = _Msg()
    reporter = _reporter(None, user_msg=msg)
    result = types.SimpleNamespace(
        title="标题",
        content="正文内容",
        raw_url="https://x.com/a/status/1",
        author_name="作者",
        author_handle="",
        author_url="",
        published_at=None,
        view_count=None,
        like_count=None,
        tags=None,
        platform=None,
        media=None,
        markdown_content="正文内容",
    )
    asyncio.run(reporter.report_result(result, "下 载 中..."))
    markdown = msg.answer_rich.await_args.kwargs["rich_message"].markdown
    assert "# 标题" in markdown
    assert "正文内容" in markdown
    # 进度放在页尾第一段: 与最终结果的"时间 · 统计"同一个位置
    footer = markdown.split("<footer>", 1)[1]
    assert "下 载 中..." in footer


def test_progress_refresh_reuses_the_last_result():
    """进度刷新不能退回骨架 —— 否则每刷一次进度就跳一次版"""
    import types

    msg = _Msg()
    reporter = _reporter(None, user_msg=msg)
    result = types.SimpleNamespace(
        title="标题", content="正文内容", raw_url="https://x.com/a/status/1", author_name="",
        author_handle="", author_url="", published_at=None, view_count=None, like_count=None,
        tags=None, platform=None, media=None, markdown_content="正文内容",
    )
    asyncio.run(reporter.report_result(result, "下 载 中..."))
    reporter._last_rich_at = 0.0  # 绕过节流, 只看渲染
    asyncio.run(reporter.report_progress("下载中 3/10"))
    markdown = msg.edit_text.await_args.kwargs["rich_message"].markdown
    assert "# 标题" in markdown, "进度刷新应保持完整排版"
    assert "下载中 3/10" in markdown


def test_progress_is_throttled_but_stages_are_not():
    """进度刷新走节流 (下载回调很密集), 阶段切换必须能发出去"""
    msg = _Msg(text="x")
    reporter = _reporter(msg)
    reporter._raw_url = "https://x.com/a/status/1"
    asyncio.run(reporter.report("解 析 中..."))
    # 紧接着的进度刷新被节流吞掉
    before = msg.edit_text.await_count
    reporter._raw_url = "https://x.com/a/status/1"
    asyncio.run(reporter.report_progress("下载中 1/10"))
    assert msg.edit_text.await_count == before, "间隔内的进度刷新应被丢弃"
    # 但阶段切换不受节流影响
    reporter._raw_url = "https://x.com/a/status/1"
    asyncio.run(reporter.report("处 理 中..."))
    assert msg.edit_text.await_count == before + 1, "阶段切换不该被节流吞掉"


def test_report_is_skipped_when_noprogress():
    msg = _Msg()
    reporter = _reporter(msg, config=_config(noprogress=True))
    asyncio.run(reporter.report("下 载 中..."))
    msg.edit_text.assert_not_awaited()


def test_finalize_edits_the_status_message():
    """收尾的另一种形态: 直接编辑成最终富文本 (整个流程一条消息)"""
    from pyrogram.types import InputRichMessage

    msg = _Msg()
    rich = InputRichMessage(markdown="结果")
    edited = asyncio.run(_reporter(msg).finalize(rich))
    msg.edit_text.assert_awaited_once()
    assert msg.edit_text.await_args.kwargs["rich_message"] is rich
    assert _no_preview(msg.edit_text.await_args.kwargs)
    msg.delete.assert_not_awaited()
    assert edited is not None


def test_finalize_without_a_status_message_returns_none():
    """没有状态消息 (noprogress) 时返回 None, 让调用方新建"""
    assert asyncio.run(_reporter(None).finalize(object())) is None


def test_finalize_does_not_swallow_errors():
    """编辑失败必须上抛: 调用方要据此回退新建, 不能静默丢结果"""
    msg = _Msg()
    msg.edit_text.side_effect = RuntimeError("MESSAGE_ID_INVALID")
    try:
        asyncio.run(_reporter(msg).finalize(object()))
    except RuntimeError:
        pass
    else:
        raise AssertionError("finalize 应上抛异常")


def test_finalize_text_edits_plain_caption():
    """不走富文本的结果 (GIF 过多只发文字) 也编辑进同一条消息"""
    msg = _Msg()
    asyncio.run(_reporter(msg).finalize_text("<b>文字结果</b>", reply_markup=None))
    msg.edit_text.assert_awaited_once()


# ── 链接预览一律关掉 ───────────────────────────────────────────────────
#
# 回归: 文本结果里带来源链接时, Telegram 会在消息下挂一张 link preview 卡片。
# 卡片在正文之外, **折叠块盖不住它** —— 内容遮住了, 封面图还露着
# (用户报「处理过程占位消息把preview关了, 不然还是能看到图」)。


def test_progress_updates_disable_the_link_preview():
    msg = _Msg(text="解 析 中...")
    asyncio.run(_reporter(msg).report("下 载 中..."))
    assert _no_preview(msg.edit_text.await_args.kwargs)


def test_dismiss_disables_the_link_preview():
    msg = _Msg()
    asyncio.run(_reporter(msg).dismiss())
    assert _no_preview(msg.edit_text.await_args.kwargs)


def test_finalize_text_disables_the_link_preview():
    """这条最容易漏: 文本结果里的来源链接就是预览卡片的来源"""
    msg = _Msg()
    asyncio.run(
        _reporter(msg).finalize_text('<b>正文</b> · <a href="https://x.com/a/status/1">来源</a>')
    )
    assert _no_preview(msg.edit_text.await_args.kwargs)


def test_sending_a_text_message_disables_the_preview_by_default():
    """首次状态消息是**发送**而不是编辑 —— ``MessageSender.text`` 的默认值也要安全

    这个默认值就是接口: 忘了传参数的调用点 (提示语、GIF 提示) 不该泄露预览。
    """
    from plugins.parse.sender import MessageSender

    msg = _Msg()
    sender = MessageSender(
        cli=SimpleNamespace(), msg=msg, config=SimpleNamespace(reply_msg=False)
    )
    asyncio.run(sender.text("解 析 中..."))
    assert _no_preview(msg.answer.await_args.kwargs)


def test_an_explicit_preview_option_is_not_overridden():
    """调用方显式传了的就用调用方的 —— 默认值只补缺, 不覆盖"""
    from pyrogram.types import LinkPreviewOptions

    msg = _Msg()
    allow = LinkPreviewOptions(is_disabled=False)
    asyncio.run(_reporter(msg).finalize_text("正文", link_preview_options=allow))
    assert msg.edit_text.await_args.kwargs["link_preview_options"] is allow


def test_report_survives_a_manually_deleted_status_message():
    """用户手动删掉进度消息后, 后续更新不能打断解析"""
    msg = _Msg()
    msg.edit_text.side_effect = RuntimeError("MESSAGE_ID_INVALID")
    reporter = _reporter(msg)
    asyncio.run(reporter.report("处 理 中..."))  # 不抛即通过


def test_report_error_keeps_the_error_text():
    """出错后**不能**再把消息编辑成一个点 —— 错误信息必须留着。

    用户报：「出错最后编辑成一个点这个删了，保留错误信息」——
    以前 report_error 起一个 15 秒的任务调 dismiss()，错误文本被 ``·`` 覆盖。
    """
    msg = _Msg()
    reporter = _reporter(msg, config=_config(keep_error_log=False))
    asyncio.run(reporter.report_error("解析", RuntimeError("boom")))

    # 只编辑一次（错误文本本身）；没有任何"收尾成点"的第二次编辑
    assert msg.edit_text.await_count == 1, "错误后不该再有 dismiss"
    assert "boom" in msg.edit_text.await_args.args[0]
    assert STATUS_DONE_TEXT not in msg.edit_text.await_args.args[0]
