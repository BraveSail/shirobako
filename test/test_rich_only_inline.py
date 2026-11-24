"""Inline and guest notices use the real rich-message TL constructor, offline."""

import asyncio
from contextlib import asynccontextmanager
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from parsehub.types import ImageRef, MultimediaParseResult, Platform
from pyrogram.errors import BadRequest
from pyrogram.parser import Parser
from pyrogram.raw import types as tl
from pyrogram.raw.core import TLObject

from plugins.parse import guest, inline
from repo.settings import SettingsConfig

LANG = "zh-hans"
URL = "https://x.com/a/status/123"
USER = SimpleNamespace(id=1, language_code=LANG)
CLIENT = SimpleNamespace(parser=Parser(None))


async def _wire(article):
    stream = BytesIO((await article.write(CLIENT)).write())
    decoded = TLObject.read(stream)
    assert not stream.read()
    return decoded


def _nodes(value):
    if isinstance(value, list):
        for child in value:
            yield from _nodes(child)
    elif isinstance(value, TLObject):
        yield value
        for field in value.__slots__:
            yield from _nodes(getattr(value, field, None))


def _rich(article):
    assert isinstance(article.send_message, tl.InputBotInlineMessageRichMessage)
    return article.send_message.rich_message


def _text(value):
    return "".join(node.text for node in _nodes(value) if isinstance(node, tl.TextPlain))


class Query:
    from_user = USER
    chat_type = "private"

    def __init__(self, query="", *, reject_first=False):
        self.query = query
        self.reject_first = reject_first
        self.answers = []

    async def answer(self, results, *, cache_time):
        self.answers.append(([await _wire(item) for item in results], cache_time))
        if self.reject_first and len(self.answers) == 1:
            raise BadRequest("EXTERNAL_MEDIA_NOT_SUPPORTED")


@pytest.fixture
def services(monkeypatch):
    @asynccontextmanager
    async def session():
        yield None

    whitelist = AsyncMock(return_value=True)
    config = SettingsConfig()
    for module in (inline, guest):
        monkeypatch.setattr(module, "get_session", session)
        monkeypatch.setattr(module, "UserService", lambda _: SimpleNamespace(ensure_lang=AsyncMock(return_value=LANG)))
        monkeypatch.setattr(module, "AdminUserService", lambda _: SimpleNamespace(is_whitelisted=whitelist))
        monkeypatch.setattr(
            module, "SettingsService", lambda _: SimpleNamespace(get_config_by_user=AsyncMock(return_value=config))
        )
    return whitelist, config


def test_inline_start_serializes_formatting(services):
    query = Query()
    asyncio.run(inline.inline_parse_tip(CLIENT, query))
    (([article], ttl),) = query.answers
    assert ttl == 1
    rich = _rich(article)
    nodes = list(_nodes(rich))
    assert any(isinstance(node, tl.TextBold) and "发送分享链接" in _text(node) for node in nodes)
    assert any(isinstance(node, tl.TextUrl) and _text(node) == "GitHub" for node in nodes)
    assert "<blockquote" not in _text(rich)


def test_inline_unsupported_link_is_a_rich_notice(services):
    query = Query("https://example.org/unsupported")
    asyncio.run(inline.inline_parse_tip(CLIENT, query))
    (([article], _),) = query.answers
    assert _text(_rich(article)) == "不支持的平台"


def test_inline_denied_is_a_rich_notice(services):
    whitelist, _ = services
    whitelist.return_value = False
    query = Query(URL)
    asyncio.run(inline.call_inline_parse(CLIENT, query))
    (([article], ttl),) = query.answers
    assert ttl == 0
    assert _text(_rich(article)) == "无权限"


def test_inline_no_content_is_visible_and_not_cached(monkeypatch, services):
    service = SimpleNamespace(get_raw_url=AsyncMock(return_value=URL), parse=AsyncMock(return_value=None))
    cache = SimpleNamespace(get=AsyncMock(return_value=None), set=AsyncMock())
    monkeypatch.setattr(inline, "ParseService", lambda: service)
    monkeypatch.setattr(inline, "parse_cache", cache)
    monkeypatch.setattr(inline, "persistent_cache", SimpleNamespace(get=AsyncMock(return_value=None)))
    query = Query(URL)
    asyncio.run(inline.call_inline_parse(CLIENT, query))
    (([article], _),) = query.answers
    assert "没有解析结果" in _text(_rich(article))
    cache.set.assert_not_awaited()


def test_inline_error_transmits_reason_as_literal_text():
    reason = "**literal** <b> || [x](https://example.org)"
    article = asyncio.run(_wire(inline.build_error_result(inline.t_[LANG], reason)))
    (paragraph,) = _rich(article).blocks
    assert isinstance(paragraph, tl.PageBlockParagraph)
    assert isinstance(paragraph.text, tl.TextPlain)
    assert paragraph.text.text == f"解析失败\n\n{reason}"


def test_inline_bad_request_falls_back_to_clickable_rich_error():
    query = Query(URL, reject_first=True)
    article = inline.build_error_result(inline.t_[LANG], "first attempt")
    asyncio.run(inline.answer_inline(query, [article], lang=LANG, cache_time=60))
    ((_, original_ttl), ([fallback], fallback_ttl)) = query.answers
    assert (original_ttl, fallback_ttl) == (60, 1)
    assert "解析失败" in _text(_rich(fallback))
    assert "请稍后重试" in fallback.description


def test_raw_link_choice_and_first_frame_are_both_rich():
    result = MultimediaParseResult(title="Title", content="Body", media=[ImageRef(url="https://a.test/x.jpg")])
    result.platform = Platform.TWITTER
    result.raw_url = URL

    async def serialize():
        config = SettingsConfig(enable_inline_raw_url=True)
        results = await inline.build_inline_results(result, CLIENT, LANG, config)
        return [await _wire(article) for article in results]

    link, preview = asyncio.run(serialize())
    assert _text(_rich(link)) == URL
    assert any(isinstance(node, tl.TextUrl) and node.url == URL for node in _nodes(_rich(link)))
    assert "Body" in _text(_rich(preview))
    assert isinstance(preview.send_message.reply_markup, tl.ReplyInlineMarkup)
    assert not any(isinstance(node, (tl.PageBlockPhoto, tl.PageBlockVideo)) for node in _nodes(_rich(preview)))


def test_guest_denied_sends_a_rich_notice(services):
    whitelist, _ = services
    whitelist.return_value = False
    answers = []

    async def answer_guest_query(query_id, article):
        assert query_id == "q1"
        answers.append(await _wire(article))

    cli = SimpleNamespace(answer_guest_query=answer_guest_query)
    msg = SimpleNamespace(guest_query_id="q1", from_user=USER, text=URL, caption=None, chat=None)
    asyncio.run(guest.guest_parse(cli, msg))
    (article,) = answers
    assert _text(_rich(article)) == "无权限"


@pytest.mark.parametrize("hide_error", [False, True])
def test_guest_parse_error_remains_controlled_by_hide_error(monkeypatch, services, hide_error):
    _, config = services
    config.hide_error = hide_error
    reason = "**literal** <b> ||"
    service = SimpleNamespace(get_raw_url=AsyncMock(return_value=URL), parse=AsyncMock(side_effect=ValueError(reason)))
    monkeypatch.setattr(guest, "ParseService", lambda: service)
    monkeypatch.setattr(guest, "persistent_cache", SimpleNamespace(get=AsyncMock(return_value=None)))
    answers = []

    async def answer_guest_query(query_id, article):
        assert query_id == "q1"
        answers.append(await _wire(article))

    cli = SimpleNamespace(answer_guest_query=answer_guest_query)
    asyncio.run(guest._answer(cli, "q1", URL, USER.id, LANG))
    if hide_error:
        assert not answers
    else:
        (article,) = answers
        assert _text(_rich(article)) == f"解析错误:\n\n{reason}"
