"""A cached native send recovers stale IDs without discarding valid shared media."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from parsehub.types.document import Block, Document, paragraph
from pyrogram.errors import ChatWriteForbidden, FileIdInvalid, FileReferenceExpired, FileReferenceInvalid
from pyrogram.raw import types as tl

from i18n import t_
from plugins.parse import handlers
from plugins.parse.context import ParseRequest
from plugins.parse.sender import MessageSender
from repo.settings import ParseMode
from repo.settings.schema import DEFAULT_SETTINGS_CONFIG
from services.cache import CacheEntry, CacheParseResult
from test.test_document_delivery_cache import _cache_photo, _photos, _walk

URL = "https://x.com/a/status/1"


def _run(*, send_error=None, remove_error=None, spoiler_tag="", translate_error=None, sensitive=False):
    msg = SimpleNamespace(reply_rich=AsyncMock(side_effect=send_error))
    config = DEFAULT_SETTINGS_CONFIG.model_copy(update={"reply_msg": True})
    req = ParseRequest(None, msg, URL, ParseMode.PREVIEW, config, t_["zh-hans"],
                       spoiler_tag=spoiler_tag, translate=translate_error is not None)
    sender = MessageSender(req.cli, req.msg, req.config)
    entry = CacheEntry(parse_result=CacheParseResult(
        platform="twitter", document=Document((paragraph("正文"), Block("media", index=0))), is_sensitive=sensitive,
    ), media=[_cache_photo(101)])
    remove = AsyncMock(side_effect=remove_error)
    with (
        patch.object(handlers.persistent_cache, "remove", remove),
        patch.object(handlers, "translate_cache_entry", AsyncMock(side_effect=translate_error)),
    ):
        ok = asyncio.run(handlers._try_send_cached(sender, entry, URL, req))
    if translate_error is None:
        msg.reply_rich.assert_awaited_once()
        rendered = msg.reply_rich.call_args.kwargs["rich_message"]
    else:
        msg.reply_rich.assert_not_awaited()
        rendered = None
    return ok, remove, rendered


def test_successful_cache_send_reports_sent():
    ok, remove, rendered = _run()
    assert ok is True
    remove.assert_not_awaited()
    assert _photos(asyncio.run(_encode(rendered))) == [101]


@pytest.mark.parametrize("error", [FileIdInvalid, FileReferenceExpired, FileReferenceInvalid])
def test_failed_cache_send_drops_the_entry_and_reports_not_sent(error):
    ok, remove, _ = _run(send_error=error())
    assert ok is False
    remove.assert_awaited_once_with(URL)


async def _encode(value):
    return await value.write(client=None)


@pytest.mark.parametrize("sensitive", [False, True])
def test_try_send_cached_passes_the_spoiler_flag(sensitive):
    ok, remove, rendered = _run(spoiler_tag="#nsfw", sensitive=sensitive)
    assert ok is True
    remove.assert_not_awaited()
    wire = asyncio.run(_encode(rendered))
    (details,) = [block for block in wire.blocks if isinstance(block, tl.PageBlockDetails)]
    assert not details.open and "#nsfw" in details.title.text
    assert _photos(details) == _photos(wire) == [101]
    (photo,) = [block for block in _walk(details) if isinstance(block, tl.PageBlockPhoto)]
    assert photo.spoiler is sensitive


def test_cache_removal_failure_does_not_raise():
    ok, remove, _ = _run(send_error=FileReferenceExpired(), remove_error=RuntimeError("db down"))
    assert ok is False
    remove.assert_awaited_once_with(URL)


@pytest.mark.parametrize("error", [ChatWriteForbidden, TimeoutError, RuntimeError])
def test_unrelated_send_failure_keeps_cached_file_ids(error):
    ok, remove, _ = _run(send_error=error())
    assert ok is False
    remove.assert_not_awaited()


def test_translation_error_keeps_cached_file_ids():
    ok, remove, _ = _run(translate_error=RuntimeError("translation unavailable"))
    assert ok is False
    remove.assert_not_awaited()
