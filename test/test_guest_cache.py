"""guest 的 file_id 缓存: 命中就直接发, 不再下载/上传。

guest 以前完全没接缓存 —— 同一个链接被反复 @, 每次都重新下载 + 上传。
现在与私聊/群/inline 一致: 首次发送后把 file_id 存进 persistent_cache,
之后再有人请求同一链接直接复用 (跳过解析/下载/转码/上传)。
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from parsehub.types import ImageParseResult, Platform

from plugins.parse import guest as guest_mod
from plugins.parse.reporters import InlineStatusReporter
from services.cache import CacheEntry, CacheParseResult


def _rich_text(rich_message) -> str:
    """把 blocks 富文本拼成可搜索的文本（测试断言用）。

    富文本一律走 ``InputRichMessage(blocks=[...])``（平台原文当字面量，服务端不解析），
    所以 ``rich_message.markdown`` 恒为 None —— 断言要看 blocks 里的文字。
    """
    parts: list[str] = []

    def _walk(obj) -> None:
        if obj is None:
            return
        if isinstance(obj, str):
            parts.append(obj)
            return
        if isinstance(obj, (list, tuple)):
            for item in obj:
                _walk(item)
            return
        text = getattr(obj, "text", None)
        if isinstance(text, str):
            parts.append(text)
        elif isinstance(text, list):
            _walk(text)
        for attr in ("items", "children", "blocks", "content", "title"):
            child = getattr(obj, attr, None)
            if child is not None and child is not obj:
                _walk(child)

    _walk(getattr(rich_message, "blocks", None))
    return "\n".join(parts)



def _cached_entry() -> CacheEntry:
    return CacheEntry(
        parse_result=CacheParseResult(title="标题", content="正文", author_name="作者"),
        media=[],
        rich=True,
    )


MID = "BQAAAH8EAACb7f-02gbl1xUz2F4"


def _cli(inline_message_id: str | None = MID):
    """假 client: 占位调用返回带 inline_message_id 的对象 (None 模拟占位发不出去)。"""
    answered = SimpleNamespace(inline_message_id=inline_message_id)
    return SimpleNamespace(answer_guest_query=AsyncMock(return_value=answered))


def _run_answer(*, cached, pipeline_should_run: bool):
    """跑一次 _answer, 返回 (ParsePipeline mock, _deliver mock)。"""

    @asynccontextmanager
    async def fake_session():
        yield None

    pipeline_cls = MagicMock()

    def _pipeline(*a, **kw):  # 不该被调用 (命中缓存时)
        if not pipeline_should_run:
            raise AssertionError("命中缓存时不该启动 ParsePipeline")
        ctx = MagicMock()
        ctx.run = AsyncMock(return_value=None)
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    pipeline_cls.side_effect = _pipeline
    deliver = AsyncMock(return_value=None)

    with (
        patch.object(guest_mod, "get_session", fake_session),
        patch.object(
            guest_mod,
            "SettingsService",
            return_value=SimpleNamespace(
                get_config_by_user=AsyncMock(
                    return_value=SimpleNamespace(
                        video_cover=False, hide_title=False, hide_desc=False, hide_source=False, hide_error=False
                    )
                )
            ),
        ),
        patch.object(
            guest_mod,
            "ParseService",
            return_value=SimpleNamespace(
                get_raw_url=AsyncMock(return_value="https://x.com/a/status/1"),
                parse=AsyncMock(return_value=_parsed_result()),
            ),
        ),
        patch.object(guest_mod.persistent_cache, "get", AsyncMock(return_value=cached)),
        patch.object(
            guest_mod,
            "build_cached_rich_content",
            MagicMock(return_value=("**缓存正文**", [], {})),
        ),
        patch.object(guest_mod, "ParsePipeline", pipeline_cls),
        patch.object(guest_mod, "_deliver", deliver),
    ):
        ok = asyncio.run(
            guest_mod._answer(_cli(), "q1", "https://x.com/a/status/1", 1879026273, "zh-hans")
        )
    return pipeline_cls, deliver, ok


def test_cache_hit_sends_without_running_the_pipeline():
    """命中缓存: 直接用 file_id 发, 绝不启动解析流水线"""
    pipeline_cls, deliver, ok = _run_answer(cached=_cached_entry(), pipeline_should_run=False)

    assert ok is True
    pipeline_cls.assert_not_called()
    deliver.assert_awaited_once()
    kwargs = deliver.await_args.kwargs
    assert kwargs["markdown"] == "**缓存正文**"     # 用的是缓存渲染结果
    assert kwargs["title"] == "标题"
    # 缓存命中不经过进度: 首帧就是结果本身, 载体还不存在 (走 answer 直发)
    assert deliver.await_args.args[2] is None


def test_cache_miss_falls_through_to_the_pipeline():
    """没有缓存才走解析"""
    pipeline_cls, _deliver, _ok = _run_answer(cached=None, pipeline_should_run=True)
    pipeline_cls.assert_called_once()


# ── guest 的处理过程 ───────────────────────────────────────────────────
#
# guest 消息就是一条 inline 消息 (SentGuestMessage: "inline message sent by a guest
# bot"), 所以**首帧那条消息本身就是句柄的来源**: answer 出去 -> 拿
# `SentGuestMessage.inline_message_id` -> 用它与 inline 同构地编辑到结果。
#
# 以前是在**召唤消息上 reply** 一条状态消息 —— 但 guest 场景 bot 通常不在召唤群里
# (这正是 guest 模式的意义), 那条 reply 发不出去 (实测 400 CHANNEL_PRIVATE),
# 于是 guest 从来没有处理过程、只有结果。用户报的就是这个。
#
# 2026-10-09: 首帧内容从「解 析 中」占位改成**解析到的文字排版** —— 与私聊/群一致。


def test_progress_uses_the_guest_message_itself_as_its_carrier():
    """进度句柄来自 guest 消息本身 —— 缓存命中时首帧就是结果, 不需要句柄 (None)

    （现场解析路径的句柄来自 answer 首帧，见
    ``test_the_first_frame_is_sent_before_the_pipeline_starts``。）
    """
    _pipeline, deliver, _ok = _run_answer(cached=_cached_entry(), pipeline_should_run=False)
    assert deliver.await_args.args[2] is None


def test_the_parse_step_hands_the_handle_to_the_reporter():
    """解析出的句柄交给 reporter —— 它只负责编辑, 与 inline 完全同构"""
    seen: dict = {}

    @asynccontextmanager
    async def fake_session():
        yield None

    ctx = MagicMock()
    ctx.run = AsyncMock(return_value=None)
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)

    def pipeline(*a, **kw):
        seen["reporter"] = a[2]
        return ctx

    with (
        patch.object(guest_mod, "get_session", fake_session),
        patch.object(
            guest_mod,
            "SettingsService",
            return_value=SimpleNamespace(
                get_config_by_user=AsyncMock(
                    return_value=SimpleNamespace(
                        video_cover=False, hide_title=False, hide_desc=False, hide_source=False, hide_error=False
                    )
                )
            ),
        ),
        patch.object(guest_mod, "ParseService", return_value=_service()),
        patch.object(guest_mod.persistent_cache, "get", AsyncMock(return_value=None)),
        patch.object(guest_mod, "ParsePipeline", pipeline),
        patch.object(guest_mod, "_deliver", AsyncMock()),
    ):
        asyncio.run(guest_mod._answer(_cli(), "q1", "https://x.com/a/status/1", 1, "zh-hans"))

    reporter = seen["reporter"]
    assert isinstance(reporter, InlineStatusReporter)
    # 句柄就是 answer 首帧拿到的那一个, reporter 只是它的使用者 (与 inline 同构)
    assert reporter._mid == MID


def test_deliver_edits_the_guest_message():
    """主路径: 编辑 guest 消息本身, 一条消息走完整个流程"""
    edit = AsyncMock()
    cli = SimpleNamespace()
    with patch.object(guest_mod, "edit_inline_rich_message", edit):
        asyncio.run(
            guest_mod._deliver(cli, "q1", MID, title="t", description="d", markdown="正文", media=None)
        )
    edit.assert_awaited_once()
    assert edit.await_args.args[1] == MID
    assert edit.await_args.kwargs["markdown"] == "正文"


def test_deliver_falls_back_when_editing_fails():
    """编辑失败必须退回 answer_guest_query —— 结果不能因为编辑不被接受就丢掉"""
    cli = SimpleNamespace(answer_guest_query=AsyncMock(return_value=SimpleNamespace(inline_message_id="X")))
    with patch.object(guest_mod, "edit_inline_rich_message", AsyncMock(side_effect=RuntimeError("不接受编辑"))):
        asyncio.run(
            guest_mod._deliver(cli, "q1", MID, title="t", description="d", markdown="正文", media=None)
        )
    cli.answer_guest_query.assert_awaited_once()


def test_deliver_without_a_placeholder_goes_straight_to_guest_query():
    """没有占位 id 时直接走 guest 通道 (与改动前的行为一致)"""
    cli = SimpleNamespace(answer_guest_query=AsyncMock(return_value=SimpleNamespace(inline_message_id="X")))
    with patch.object(guest_mod, "edit_inline_rich_message", AsyncMock()) as edit:
        asyncio.run(
            guest_mod._deliver(cli, "q1", None, title="t", description="d", markdown="正文", media=None)
        )
    edit.assert_not_awaited()
    cli.answer_guest_query.assert_awaited_once()


# ── 首帧: 不再有「解 析 中」占位 ─────────────────────────────────────
#
# 用户要求「消息首帧取消掉解析中, 直接发解析到的文字结果」(2026-10-09)。
# guest 的消息就是一条 inline 消息, 所以"发出首帧"既是回复也是载体 —— 载体因此
# **不必**在解析前建立: ``InlineStatusReporter._ensure_carrier`` 在第一次真要发
# 内容时才把它发出去, 那次发的内容就是首帧。


def _parsed_result() -> ImageParseResult:
    result = ImageParseResult(content="解析到的正文", photo=[])
    result.title = "解析到的标题"
    result.platform = Platform.TWITTER
    result.raw_url = "https://x.com/a/status/1"
    return result


def _service():
    """假的 ParseService: 取 raw_url 与解析都给结果 (解析现在由 _answer 自己调)。"""
    return SimpleNamespace(
        get_raw_url=AsyncMock(return_value="https://x.com/a/status/1"),
        parse=AsyncMock(return_value=_parsed_result()),
    )


def _first_frame_markdown(answer_calls: list) -> str:
    """第一次 answer_guest_query 发出的 markdown。"""
    assert answer_calls, "guest 一条消息都没发出去"
    return _rich_text(answer_calls[0].input_message_content.rich_message)


def _run_guest(answer_calls: list, events: list, *, mid: str | None = MID):
    """跑一次真实 `_answer`（解析用替身、流水线用会真调 reporter 的替身）。"""

    async def _answer_guest_query(_qid, result, **kw):  # noqa: ANN001, ANN202
        events.append("answer")
        answer_calls.append(result)
        return SimpleNamespace(inline_message_id=mid)

    @asynccontextmanager
    async def fake_session():
        yield None

    class _Pipeline:
        """流水线替身: 记录收到的 parse_result, 并让 reporter 走一次"下载中"就结束。"""

        def __init__(self, *a, **kw):
            events.append("pipeline")
            self._reporter = a[2]
            seen["parse_result"] = kw.get("parse_result")

        async def run(self):
            await self._reporter.report_result(_parsed_result(), "下 载 中...")
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen: dict = {}
    cli = SimpleNamespace(answer_guest_query=AsyncMock(side_effect=_answer_guest_query))
    with (
        patch.object(guest_mod, "get_session", fake_session),
        patch.object(
            guest_mod,
            "SettingsService",
            return_value=SimpleNamespace(
                get_config_by_user=AsyncMock(
                    return_value=SimpleNamespace(
                        video_cover=False, hide_title=False, hide_desc=False, hide_source=False, hide_error=False
                    )
                )
            ),
        ),
        patch.object(guest_mod, "ParseService", return_value=_service()),
        patch.object(guest_mod.persistent_cache, "get", AsyncMock(return_value=None)),
        patch.object(guest_mod, "ParsePipeline", _Pipeline),
        patch.object(guest_mod, "_deliver", AsyncMock()),
    ):
        asyncio.run(guest_mod._answer(cli, "q1", "https://x.com/a/status/1", 1, "zh-hans"))
    return seen


def test_the_first_frame_is_the_parsed_layout():
    """**核心**: 第一次 answer 出去的就是解析到的文字排版 (标题/正文 + 页脚「下载中」)"""
    answer_calls: list = []
    _run_guest(answer_calls, [])

    markdown = _first_frame_markdown(answer_calls)
    assert "解析到的正文" in markdown, markdown
    assert "解析到的标题" in markdown, markdown
    assert "下 载 中" in markdown, markdown           # 页脚的处理状态保留
    assert "解 析 中" not in markdown, markdown       # 首帧不再是「解析中」


def test_the_first_frame_exactly_matches_the_first_progress_frame():
    """首帧与流水线随后要发的「下载中」逐字相同 —— 所以那次编辑被判重跳过, 不闪版"""
    answer_calls: list = []
    _run_guest(answer_calls, [])
    assert _first_frame_markdown(answer_calls).count("下 载 中") == 1


def test_the_first_frame_is_sent_before_the_pipeline_starts():
    """**顺序**: 先 answer 首帧拿句柄, 再用这个句柄构造 reporter 跑下载 —— 与 inline 同构"""
    events: list = []
    _run_guest([], events)
    assert events == ["answer", "pipeline"], events


def test_the_pipeline_gets_the_already_parsed_result():
    """解析只做一次: 结果传给流水线, 不让它再解析一遍"""
    seen = _run_guest([], [])
    assert seen["parse_result"] is not None
    assert seen["parse_result"].content == "解析到的正文"


def test_without_a_handle_the_pipeline_gets_a_null_reporter():
    """首帧发不出去 (拿不到句柄) 时退回静默, 解析照跑 —— 结果由 _deliver 兜底发出"""
    seen: dict = {}
    answer_calls: list = []

    async def _answer_guest_query(_qid, result, **kw):  # noqa: ANN001, ANN202
        answer_calls.append(result)
        return SimpleNamespace(inline_message_id=None)  # 拿不到句柄

    @asynccontextmanager
    async def fake_session():
        yield None

    def _pipeline(*a, **kw):
        seen["reporter"] = a[2]
        ctx = MagicMock()
        ctx.run = AsyncMock(return_value=None)
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    with (
        patch.object(guest_mod, "get_session", fake_session),
        patch.object(
            guest_mod,
            "SettingsService",
            return_value=SimpleNamespace(
                get_config_by_user=AsyncMock(
                    return_value=SimpleNamespace(
                        video_cover=False, hide_title=False, hide_desc=False, hide_source=False, hide_error=False
                    )
                )
            ),
        ),
        patch.object(guest_mod, "ParseService", return_value=_service()),
        patch.object(guest_mod.persistent_cache, "get", AsyncMock(return_value=None)),
        patch.object(guest_mod, "ParsePipeline", _pipeline),
        patch.object(guest_mod, "_deliver", AsyncMock()),
        patch.object(
            guest_mod, "InlineStatusReporter", side_effect=AssertionError("没有句柄时不该构造 reporter")
        ),
    ):
        cli = SimpleNamespace(answer_guest_query=AsyncMock(side_effect=_answer_guest_query))
        asyncio.run(guest_mod._answer(cli, "q1", "https://x.com/a/status/1", 1, "zh-hans"))

    assert isinstance(seen["reporter"], guest_mod._NullReporter)
