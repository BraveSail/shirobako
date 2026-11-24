import ast
import asyncio
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from parsehub.types import AniRef, MultimediaParseResult, RichTextParseResult
from pyrogram.enums import ChatType
from pyrogram.types import InputRichBlockParagraph, RichTextBold, RichTextUrl

from i18n import LANG_MAP, t_
from plugins.parse import handlers
from plugins.parse.context import GIF_ONLY_SKIP_DOWNLOAD_COUNT_THRESHOLD, ParseRequest
from repo.settings import ParseMode, SettingsConfig
from services.pipeline import PipelineResult
from utils.rate_limit import ParseRateLimitExceeded

_URL = "https://x.com/a/status/1"


def test_parse_publication_has_no_text_caption_or_markup_transport():
    root = Path(handlers.__file__).parent
    ordinary = {
        "InputTextMessageContent",
        "send_media_group",
        "reply_document",
        "answer_document",
        "text_no_preview",
        "edit_text_with_entities",
        "build_delivery_caption",
    }
    for name in ("sender", "handlers", "inline", "guest", "inline_rich", "reporters"):
        module = ast.parse((root / f"{name}.py").read_text())
        for call in (node for node in ast.walk(module) if isinstance(node, ast.Call)):
            method = getattr(call.func, "id", getattr(call.func, "attr", ""))
            assert method not in ordinary, (name, call.lineno, method)
            if method == "InputRichMessage":
                assert [argument.arg for argument in call.keywords] == ["blocks"]
            if method == "edit_inline_text":  # This SDK method is overloaded for rich payloads.
                assert any(argument.arg == "rich_message" for argument in call.keywords)


class _Locale:
    locale = "zh-hans"

    def __call__(self, text):
        return text


def _message(**kwargs):
    attrs = {
        "id": 42,
        "from_user": SimpleNamespace(id=7, language_code="zh"),
        "chat": SimpleNamespace(id=7, type=ChatType.PRIVATE),
        "text": None,
        "caption": None,
        "entities": None,
        "command": None,
        "reply_to_message": None,
        "answer": AsyncMock(),
        "reply": AsyncMock(),
        "answer_rich": AsyncMock(),
        "reply_rich": AsyncMock(),
        "reply_chat_action": AsyncMock(),
    }
    return SimpleNamespace(**(attrs | kwargs))


@pytest.mark.parametrize(
    "allowed,command,text,reply_policy,notice",
    [
        (False, None, _URL, False, "仅限白名单用户使用"),
        (True, ["jx"], "/jx", True, "▎请加上链接或回复一条消息"),
        (True, None, "unsupported", False, "▎不支持的平台"),
    ],
)
def test_parse_feedback_uses_native_paragraphs(monkeypatch, allowed, command, text, reply_policy, notice):
    msg = _message(command=command, text=text)
    config = SettingsConfig(reply_msg=reply_policy)
    monkeypatch.setattr(handlers, "get_session", lambda: nullcontext(None))
    monkeypatch.setattr(handlers, "get_config_target", Mock())
    monkeypatch.setattr(
        handlers, "SettingsService", lambda _: SimpleNamespace(get_config=AsyncMock(return_value=config))
    )
    monkeypatch.setattr(
        handlers, "UserService", lambda _: SimpleNamespace(ensure_lang=AsyncMock(return_value="zh-hans"))
    )
    monkeypatch.setattr(
        handlers, "AdminUserService", lambda _: SimpleNamespace(is_whitelisted=AsyncMock(return_value=allowed))
    )
    monkeypatch.setattr(handlers, "_handle_parse_request", AsyncMock())

    asyncio.run(handlers.parse(SimpleNamespace(), msg))

    send = msg.reply_rich if reply_policy else msg.answer_rich
    send.assert_awaited_once()
    (block,) = send.await_args.kwargs["rich_message"].blocks
    assert isinstance(block, InputRichBlockParagraph)
    assert (block.text.text if isinstance(block.text, RichTextBold) else block.text) == notice
    msg.answer.assert_not_awaited()
    handlers._handle_parse_request.assert_not_awaited()


@pytest.mark.parametrize("mode", [ParseMode.PREVIEW, ParseMode.RAW, ParseMode.ZIP])
@pytest.mark.parametrize("rich_source", [False, True])
def test_gif_buttons_and_file_modes_share_rich_context(monkeypatch, mode, rich_source):
    media = [AniRef(url=f"https://example.org/{i}.gif") for i in range(GIF_ONLY_SKIP_DOWNLOAD_COUNT_THRESHOLD + 1)]
    parsed = (
        RichTextParseResult(markdown_content="literal [body]", media=media)
        if rich_source
        else MultimediaParseResult(content="literal [body]", media=media)
    )
    result = PipelineResult(parsed, processed_list=[] if mode == ParseMode.PREVIEW else [object()])
    req = ParseRequest(
        SimpleNamespace(),
        _message(text=f"{_URL}\n==a [note]=="),
        _URL,
        mode,
        SettingsConfig(noprogress=True),
        _Locale(),
        include_custom_content=True,
        spoiler_tag="#spoiler",
    )
    monkeypatch.setattr(handlers, "ParseService", lambda: SimpleNamespace(get_raw_url=AsyncMock(return_value=_URL)))
    monkeypatch.setattr(handlers, "parse_cache", SimpleNamespace(get=AsyncMock(return_value=None), set=AsyncMock()))
    monkeypatch.setattr(handlers, "persistent_cache", SimpleNamespace(get=AsyncMock(return_value=None)))
    monkeypatch.setattr(
        handlers, "ParsePipeline", Mock(return_value=nullcontext(SimpleNamespace(run=AsyncMock(return_value=result))))
    )
    for name in ("send_rich_media", "send_raw", "send_zip"):
        monkeypatch.setattr(handlers, name, AsyncMock())

    assert asyncio.run(handlers.handle_parse.__wrapped__(req))

    if mode == ParseMode.PREVIEW:
        sender = handlers.send_rich_media
        assert sender.await_args.args[1:] == (parsed, [])
        assert handlers.ParsePipeline.call_args.kwargs["gif_only_skip_download_count_threshold"] < len(media)
        kwargs = sender.await_args.kwargs
        assert kwargs["raw_url"] == _URL
        assert kwargs["reporter"] is handlers.ParsePipeline.call_args.args[2]
        assert kwargs["reply_markup"] is None
        (links,) = kwargs["file_blocks"]
        assert [item.url for item in links.text if isinstance(item, RichTextUrl)] == [m.url for m in media]
    else:
        sender = handlers.send_raw if mode == ParseMode.RAW else handlers.send_zip
        assert sender.await_args.args[1] is result
        assert sender.await_args.args[2] is handlers.ParsePipeline.call_args.args[2]
        assert handlers.ParsePipeline.call_args.kwargs["gif_only_skip_download_count_threshold"] == 0
    sender.assert_awaited_once()
    assert sender.await_args.kwargs["spoiler_tag"] == "#spoiler"
    assert sender.await_args.kwargs["custom_content"].text == "a [note]"
    req.msg.answer.assert_not_awaited()


@pytest.mark.parametrize("notify", [True, False])
def test_rate_limit_preserves_demo_link_and_timed_deletion(monkeypatch, notify):
    monkeypatch.setattr(handlers, "bs", SimpleNamespace(demo_mode=True))
    monkeypatch.setattr(
        handlers, "handle_parse", AsyncMock(side_effect=ParseRateLimitExceeded(3.5, should_notify=notify))
    )
    sender = Mock()
    sender.delete_after.return_value = sender
    sender.rich_message = AsyncMock()
    sender.text_no_preview = AsyncMock()
    monkeypatch.setattr(handlers, "MessageSender", lambda *_: sender)
    req = ParseRequest(SimpleNamespace(), _message(), _URL, ParseMode.PREVIEW, SettingsConfig(), _Locale())

    asyncio.run(handlers._handle_parse_request(req))

    sender.text_no_preview.assert_not_awaited()
    if not notify:
        sender.delete_after.assert_not_called()
        sender.rich_message.assert_not_awaited()
        return
    sender.delete_after.assert_called_once_with(3.5)
    rich = sender.rich_message.await_args.kwargs["rich_message"]
    assert rich.blocks[0].text.text == "▎解析过于频繁, 请在 3.5s 后重试"
    link = next(part for part in rich.blocks[-1].text.text if isinstance(part, RichTextUrl))
    assert link.text == "GitHub" and link.url == "https://github.com/BraveSail/shirobako"


def test_rich_notice_serializes_punctuation_literally():
    from plugins.parse.notices import rich_notice

    literal = "  <b>[link](https://example.org)</b> #tag | *x*"
    rich = rich_notice(literal)
    assert rich.markdown is None

    async def write():
        return await rich.write(client=None)

    raw = asyncio.run(write())
    assert raw.blocks[0].text.text == literal


@pytest.mark.parametrize("locale", LANG_MAP)
def test_localized_demo_rate_limit_keeps_the_github_link(monkeypatch, locale):
    monkeypatch.setattr(handlers, "bs", SimpleNamespace(demo_mode=True))
    limited = AsyncMock(side_effect=ParseRateLimitExceeded(3.5, should_notify=True))
    monkeypatch.setattr(handlers, "handle_parse", limited)
    msg = _message()
    req = ParseRequest(SimpleNamespace(), msg, _URL, ParseMode.PREVIEW, SettingsConfig(reply_msg=False), t_[locale])

    asyncio.run(handlers._handle_parse_request(req))

    rich = msg.answer_rich.await_args.kwargs["rich_message"]
    link = next(part for part in rich.blocks[-1].text.text if isinstance(part, RichTextUrl))
    assert link.url == "https://github.com/BraveSail/shirobako"
    if locale == "en-us":
        assert rich.blocks[1].blocks[0].text.text.startswith("To ensure a good experience")
