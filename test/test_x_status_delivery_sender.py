"""Exercise ordinary bot delivery through the SDK's request encoder, without network I/O."""

import asyncio
from contextlib import nullcontext
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from parsehub import Platform
from parsehub.types import AniRef, ImageFile, ImageRef, MultimediaParseResult, VideoFile, VideoRef
from parsehub.types.document import Block, Document, paragraph
from parsehub.types.x_status import TextRun, XPost, XStatus
from pyrogram import Client, enums, raw, utils
from pyrogram.errors import MessageIdInvalid, WebpageCurlFailed
from pyrogram.file_id import FileId
from pyrogram.raw.core import TLObject
from pyrogram.types import Chat, Message, MessageEntity, User

from i18n import t_
from plugins import helpers
from plugins.parse import handlers, rich_blocks
from plugins.parse import sender as delivery
from plugins.parse.reporters import MessageStatusReporter
from repo.settings import ParseMode, SettingsConfig
from services import PipelineResult
from services.media import ProcessedMedia

_t = t_["en-us"]
_SOURCE = "https://x.com/user/status/2095537240726450480"
_DOCUMENT_SOURCE = "https://www.pixiv.net/artworks/12345"
_LINK = "https://example.org/a_(b)?x=1&y=2"
_LITERAL = "    😀 <b>&amp;</b> **literal** ||not a spoiler||\n#not_a_heading {{poll:0}}\n"
_LINK_LABEL = "🔗 read"
_NOTE_LINK = "https://example.org/note?a=1&b=2"
_NOTE = '🤖 <b>&amp;</b> <a href="x">literal</a>\n👀 check'
_NOTE_LABEL = "👀 check"
_PEER = raw.types.InputPeerUser(user_id=91, access_hash=1)
_RPCS = raw.functions.messages
_PHOTO = raw.types.Photo(
    id=234, access_hash=56, file_reference=b"photo", date=1, dc_id=2,
    sizes=[raw.types.PhotoSize(type="x", w=1, h=1, size=10)],
)


def _result(text=_LITERAL, *, media=(), entities=True, legacy="<b>LEGACY BODY</b>"):
    runs = (TextRun(text), TextRun(_LINK_LABEL, _LINK, ("bold",))) if entities else (TextRun(text),)
    result = MultimediaParseResult(content=legacy, x_status=XStatus(XPost(runs)), media=media)
    result.platform = Platform.TWITTER
    result.raw_url = _SOURCE
    return result


def _walk(node):
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, TLObject):
        yield node
        for name in node.__slots__:
            yield from _walk(getattr(node, name, None))


def _text(node):
    return "".join(n.text for n in _walk(node) if isinstance(n, raw.types.TextPlain))


def _caption_spans(rpc):
    return {
        (
            type(e),
            rpc.message.encode("utf-16-le")[e.offset * 2 : (e.offset + e.length) * 2].decode("utf-16-le"),
            getattr(e, "url", None),
        )
        for e in rpc.entities or ()
    }


def _assert_caption(rpc):
    assert rpc.message.startswith(_LITERAL + _LINK_LABEL)
    spans = _caption_spans(rpc)
    assert (raw.types.MessageEntityTextUrl, _LINK_LABEL, _LINK) in spans
    assert (raw.types.MessageEntityBold, _LINK_LABEL, None) in spans
    assert (raw.types.MessageEntityTextUrl, "Source (Twitter)", _SOURCE) in spans
    assert not any(kind is raw.types.MessageEntitySpoiler for kind, _, _ in spans)


def _set_note(wire, mode, *, formatted=True, source=_SOURCE):
    command = {ParseMode.RAW: "raw", ParseMode.ZIP: "zip", ParseMode.PREVIEW: "jxjx"}[mode]
    prefix = f"/{command} {source} 🧪\n"
    wire.msg.text = f"{prefix}==  {_NOTE}  ==\nignored\n==second=="
    wire.msg.command = wire.msg.text[1:].split()
    if formatted:
        emoji_at = len(prefix[:prefix.index("🧪")].encode("utf-16-le")) // 2
        wire.msg.entities = [
            MessageEntity(type=enums.MessageEntityType.ITALIC, offset=emoji_at, length=2),
            # A source style crossing both note delimiters must be clipped, not lost.
            MessageEntity(
                type=enums.MessageEntityType.BOLD,
                offset=len(prefix.encode("utf-16-le")) // 2,
                length=len(f"==  {_NOTE}  ==".encode("utf-16-le")) // 2,
            ),
            MessageEntity(
                type=enums.MessageEntityType.TEXT_LINK,
                offset=len(wire.msg.text[:wire.msg.text.index(_NOTE_LABEL)].encode("utf-16-le")) // 2,
                length=len(_NOTE_LABEL.encode("utf-16-le")) // 2,
                url=_NOTE_LINK,
            ),
        ]


async def _parse_from_message(monkeypatch, wire, result, *, source=_SOURCE):
    monkeypatch.setattr(handlers, "get_session", nullcontext)
    wire.msg.from_user = User(id=_PEER.user_id, language_code="en-us")
    monkeypatch.setattr(handlers, "UserService", lambda _: SimpleNamespace(ensure_lang=AsyncMock(return_value="en-us")))
    monkeypatch.setattr(
        handlers, "SettingsService", lambda _: SimpleNamespace(get_config=AsyncMock(return_value=wire.config)),
    )
    monkeypatch.setattr(
        handlers, "AdminUserService", lambda _: SimpleNamespace(is_whitelisted=AsyncMock(return_value=True)),
    )
    monkeypatch.setattr(handlers, "ParseService", lambda: SimpleNamespace(
        get_raw_url=AsyncMock(return_value=source),
        parser=SimpleNamespace(get_platform=lambda url: result.parse_result.platform if url == source else None),
    ))
    monkeypatch.setattr(handlers, "parse_cache", SimpleNamespace(get=AsyncMock(return_value=None), set=AsyncMock()))

    class Pipeline:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        async def run(self):
            return result

    monkeypatch.setattr(handlers, "ParsePipeline", Pipeline)
    monkeypatch.setattr(handlers, "MessageStatusReporter", lambda *args, **kwargs: wire.reporter)
    if result.parse_result.x_status is not None:
        monkeypatch.setattr(wire.cli.parser, "unparse", _reject_legacy)
    await handlers.parse(wire.cli, wire.msg)


def _assert_note(query, *, formatted=True):
    assert f"\n\n{_NOTE}" in query.message
    assert "ignored" not in query.message and "second" not in query.message
    spans = _caption_spans(query)
    assert not any(label == "🧪" for _, label, _ in spans)
    expected = {(raw.types.MessageEntityBold, _NOTE, None), (raw.types.MessageEntityTextUrl, _NOTE_LABEL, _NOTE_LINK)}
    assert (expected & spans) == (expected if formatted else set())


class _Wire:
    """Only peer lookup, file upload and network I/O are replaced."""

    def __init__(self, monkeypatch, *, reject_edit=None, reject_cover=False, config=None):
        self.events = []
        self.modes = {}
        self.reject_edit = reject_edit
        self.reject_cover = reject_cover
        self.cli = Client("offline-delivery", in_memory=True)
        self.cli.link_preview_options = None
        monkeypatch.setattr(self.cli, "resolve_peer", AsyncMock(return_value=_PEER))
        monkeypatch.setattr(self.cli, "save_file", self.save_file)
        monkeypatch.setattr(self.cli, "invoke", self.invoke)

        encode_entities = utils.parse_text_entities

        async def capture_mode(client, text, parse_mode, entities):
            if client is self.cli:
                self.modes[text] = parse_mode
            return await encode_entities(client, text, parse_mode, entities)

        monkeypatch.setattr(utils, "parse_text_entities", capture_mode)
        self.chat = Chat(id=_PEER.user_id, type=enums.ChatType.PRIVATE, client=self.cli)
        self.msg = Message(id=41, chat=self.chat, client=self.cli)
        self.config = config or SettingsConfig(noprogress=True, video_cover=False)
        self.sender = delivery.MessageSender(self.cli, self.msg, self.config)
        self.reporter = MessageStatusReporter(self.cli, self.msg, t=_t, config=self.config)

    async def save_file(self, path, **kwargs):
        if path is None:
            return None
        # Read the actual local file before returning a Telegram upload handle.
        with open(path, "rb") as source:
            assert source.read()
        return raw.types.InputFile(id=777, parts=1, name=str(path), md5_checksum="")

    async def invoke(self, request, **kwargs):
        stream = BytesIO(request.write())
        query = TLObject.read(stream)
        assert stream.read() == b""
        self.events.append(query)
        if isinstance(query, _RPCS.EditMessage) and self.reject_edit:
            fail, self.reject_edit = self.reject_edit, None
            raise fail
        if isinstance(query, _RPCS.UploadMedia):
            if isinstance(query.media, raw.types.InputMediaPhotoExternal):
                if self.reject_cover:
                    raise WebpageCurlFailed()
                return raw.types.MessageMediaPhoto(photo=_PHOTO)
            if isinstance(query.media, raw.types.InputMediaUploadedPhoto):
                return raw.types.MessageMediaPhoto(photo=_PHOTO)
            return SimpleNamespace(document=raw.types.Document(
                id=123, access_hash=45, file_reference=b"file", date=1, mime_type="video/mp4",
                size=10, dc_id=2, attributes=[],
            ))
        if isinstance(query, _RPCS.SendMessage):
            if query.rich_message and query.rich_message.photos:
                return raw.types.Updates(
                    date=1, seq=1, users=[raw.types.User(id=_PEER.user_id, first_name="Offline")], chats=[],
                    updates=[raw.types.UpdateNewMessage(pts=1, pts_count=1, message=raw.types.Message(
                        id=42, date=1, peer_id=raw.types.PeerUser(user_id=_PEER.user_id), message="",
                        restriction_reason=[],
                        rich_message=raw.types.RichMessage(
                            blocks=query.rich_message.blocks, photos=[_PHOTO], documents=[],
                        ),
                    ))],
                )
            return raw.types.UpdateShortSentMessage(id=42, pts=1, pts_count=1, date=1)
        return raw.types.Updates(updates=[], users=[], chats=[], date=1, seq=1)

    def select(self, rpc_type):
        return [event for event in self.events if isinstance(event, rpc_type)]

    def status_message(self):
        self.reporter._msg = Message(id=99, chat=self.chat, client=self.cli)


def _reject_legacy(*args, **kwargs):
    pytest.fail("A structured result must not use the legacy renderer")


@pytest.mark.parametrize("overwrite", [False, True])
def test_final_status_sends_literal_blocks(monkeypatch, overwrite):
    monkeypatch.setattr(helpers, "build_rich_markdown", _reject_legacy)
    monkeypatch.setattr(rich_blocks, "markdown_to_blocks", _reject_legacy)

    async def run():
        wire = _Wire(monkeypatch)
        if overwrite:
            wire.status_message()
        assert await delivery.send_rich_media(wire.sender, _result(), [], _t=_t, reporter=wire.reporter)
        if overwrite:
            (query,) = wire.select(_RPCS.EditMessage)
            assert query.id == 99
            assert not wire.select(_RPCS.SendMessage)
        else:
            (query,) = wire.select(_RPCS.SendMessage)
        assert query.message == ""
        blocks = query.rich_message.blocks
        assert _text(blocks).startswith(_LITERAL + _LINK_LABEL)
        links = {(n.url, _text(n)) for n in _walk(blocks) if isinstance(n, raw.types.TextUrl)}
        assert links == {(_LINK, _LINK_LABEL), (_SOURCE, "Source (Twitter)")}
        assert not any(isinstance(n, raw.types.TextSpoiler) for n in _walk(blocks))

    asyncio.run(run())


@pytest.mark.parametrize("repeat_media", [False, True])
def test_final_status_caches_the_original_when_sending_a_display_override(monkeypatch, tmp_path, repeat_media):
    from parsehub.types.document import Block, Document, paragraph

    local = tmp_path / "photo.jpg"
    local.write_bytes(b"offline upload")
    media = [ImageRef(url="https://example.org/photo.jpg")]
    original = _result("ORIGINAL", media=media)
    display = _result("TRANSLATION", media=media)
    if repeat_media:
        image = Block("media", index=0)
        original.document = Document((paragraph("ORIGINAL"), image, paragraph("between"), image))
        display.document = Document((paragraph("TRANSLATION"), image, paragraph("between"), image))
    write_cache = AsyncMock()
    monkeypatch.setattr(delivery, "persistent_cache", SimpleNamespace(set=write_cache))

    async def run():
        wire = _Wire(monkeypatch)
        await delivery.send_rich_media(
            wire.sender, display, [ProcessedMedia(ImageFile(path=local, width=1, height=1))],
            _t=_t, raw_url=_SOURCE, cache_parse_result=original,
        )
        (query,) = wire.select(_RPCS.SendMessage)
        assert _text(query.rich_message.blocks).startswith("TRANSLATION")
        write_cache.assert_awaited_once()
        assert write_cache.await_args is not None
        key, entry = write_cache.await_args.args
        assert key == _SOURCE
        assert entry.parse_result.x_status == original.x_status
        assert entry.parse_result.document == original.document
        (item,) = entry.media
        assert FileId.decode(item.file_id).media_id == _PHOTO.id

    asyncio.run(run())


@pytest.mark.parametrize("overwrite", [False, True])
def test_final_cover_failure_retries_without_cover(monkeypatch, tmp_path, overwrite):
    local = tmp_path / "video.mp4"
    local.write_bytes(b"offline upload")
    cover = "https://example.org/cover.jpg"
    media = [VideoRef(url="https://example.org/video.mp4", thumb_url=cover)]
    processed = [ProcessedMedia(VideoFile(path=local, width=1, height=1, duration=1))]
    monkeypatch.setattr(delivery, "prepare_video_thumbs", AsyncMock(return_value={}))

    async def run():
        wire = _Wire(monkeypatch, reject_cover=True, config=SettingsConfig(video_cover=True))
        if overwrite:
            wire.status_message()
        assert await delivery.send_rich_media(
            wire.sender, _result(media=media), processed, _t=_t, reporter=wire.reporter,
        )
        if overwrite:
            (query,) = wire.select(_RPCS.EditMessage)
            assert query.id == 99
            assert not wire.select(_RPCS.SendMessage)
        else:
            (query,) = wire.select(_RPCS.SendMessage)
        assert _text(query.rich_message.blocks).startswith(_LITERAL)
        assert any(isinstance(n, raw.types.PageBlockVideo) for n in _walk(query.rich_message.blocks))
        cover_upload, second_upload = wire.select(_RPCS.UploadMedia)
        assert cover_upload.media.url == cover
        assert second_upload.media.video_cover is None

    asyncio.run(run())


@pytest.mark.parametrize("mode, file_count", [(ParseMode.RAW, 1), (ParseMode.RAW, 2), (ParseMode.ZIP, 1)])
def test_file_delivery_sends_native_caption_or_text(monkeypatch, tmp_path, mode, file_count):
    monkeypatch.setattr(delivery, "build_caption", _reject_legacy)
    out = tmp_path / "output"
    out.mkdir()
    paths = [out / f"{i}.jpg" for i in range(file_count)]
    for path in paths:
        path.write_bytes(b"offline upload")
    result = PipelineResult(
        _result(media=[ImageRef(url=f"https://example.org/{p.name}") for p in paths]),
        [ProcessedMedia(ImageFile(path=p, width=1, height=1)) for p in paths],
        out,
    )

    async def run():
        wire = _Wire(monkeypatch, config=SettingsConfig(noprogress=True, custom_content=True))
        _set_note(wire, mode)
        incoming_spans = [(entity.offset, entity.length) for entity in wire.msg.entities]
        await _parse_from_message(monkeypatch, wire, result)
        if file_count == 1:
            (query,) = wire.select(_RPCS.SendMedia)
            assert isinstance(query.media, raw.types.InputMediaUploadedDocument)
            assert not wire.select(_RPCS.SendMessage)
        else:
            (group,) = wire.select(_RPCS.SendMultiMedia)
            assert len(group.multi_media) == file_count
            assert all(not item.message and not item.entities for item in group.multi_media)
            (query,) = wire.select(_RPCS.SendMessage)
            assert query.no_webpage
        _assert_caption(query)
        _assert_note(query)
        assert [(entity.offset, entity.length) for entity in wire.msg.entities] == incoming_spans
        assert wire.modes[query.message] is enums.ParseMode.DISABLED
        assert not out.exists()
        assert not out.with_suffix(".tar.gz").exists()

    asyncio.run(run())


@pytest.mark.parametrize("status", ["edit", "rejected", "no-status", "entityless"])
def test_gif_only_uses_ordinary_entities_with_buttons_on_edit_and_fallback(monkeypatch, status):
    monkeypatch.setattr(delivery, "build_caption", _reject_legacy)
    media = [AniRef(url=f"https://example.org/{i}.gif") for i in range(6)]
    entityless = status == "entityless"
    parsed = _result(media=media, entities=not entityless)

    async def run():
        wire = _Wire(
            monkeypatch,
            reject_edit=MessageIdInvalid() if status == "rejected" else None,
            config=SettingsConfig(noprogress=True, hide_source=entityless, custom_content=True),
        )
        if status in {"edit", "rejected"}:
            wire.status_message()
        _set_note(wire, ParseMode.PREVIEW, formatted=not entityless)
        await _parse_from_message(monkeypatch, wire, PipelineResult(parsed))
        if status == "edit":
            (query,) = wire.select(_RPCS.EditMessage)
            assert query.id == 99
            assert not wire.select(_RPCS.SendMessage)
        else:
            (query,) = wire.select(_RPCS.SendMessage)
            if status == "rejected":
                (attempt,) = wire.select(_RPCS.EditMessage)
                assert attempt.message == query.message
                assert _caption_spans(attempt) == _caption_spans(query)
        assert not query.rich_message
        assert query.no_webpage
        assert [b.type.url for row in query.reply_markup.rows for b in row.buttons] == [m.url for m in media]
        if entityless:
            assert query.message == f"{_LITERAL}\n\n{_NOTE}" and not query.entities
        else:
            _assert_caption(query)
        _assert_note(query, formatted=not entityless)
        assert wire.modes[query.message] is enums.ParseMode.DISABLED

    asyncio.run(run())


def test_legacy_gif_note_still_uses_the_markup_adapter(monkeypatch):
    result = MultimediaParseResult(
        content="legacy body", media=[AniRef(url=f"https://example.org/{i}.gif") for i in range(6)],
    )
    result.platform = Platform.TWITTER

    async def run():
        wire = _Wire(monkeypatch, config=SettingsConfig(noprogress=True, hide_source=True, custom_content=True))
        prefix = f"/jxjx {_SOURCE}\n=="
        wire.msg.text = prefix + "legacy note=="
        wire.msg.command = wire.msg.text[1:].split()
        wire.msg.entities = [MessageEntity(type=enums.MessageEntityType.BOLD, offset=len(prefix), length=11)]
        await _parse_from_message(monkeypatch, wire, PipelineResult(result))
        (query,) = wire.select(_RPCS.SendMessage)
        assert (raw.types.MessageEntityBold, "legacy note", None) in _caption_spans(query)

    asyncio.run(run())


@pytest.mark.parametrize("mode", [ParseMode.RAW, ParseMode.ZIP, ParseMode.PREVIEW])
def test_non_x_document_caption_and_note_on_sdk_wire(monkeypatch, tmp_path, mode):
    monkeypatch.setattr(delivery, "build_caption", _reject_legacy)
    output = tmp_path / "output"
    output.mkdir()
    if mode == ParseMode.PREVIEW:
        media = [AniRef(url=f"https://example.org/{i}.gif") for i in range(6)]
        processed = []
    else:
        photo = output / "photo.jpg"
        photo.write_bytes(b"offline upload")
        media = [ImageRef(url="https://example.org/photo.jpg")]
        processed = [ProcessedMedia(ImageFile(path=photo, width=1, height=1))]
    parsed = MultimediaParseResult(
        content="LEGACY", media=media,
        document=Document((paragraph((TextRun(_LITERAL), TextRun(_LINK_LABEL, _LINK, ("bold",)))),)),
    )
    parsed.platform = Platform.PIXIV
    parsed.raw_url = _DOCUMENT_SOURCE

    async def run():
        wire = _Wire(monkeypatch, config=SettingsConfig(noprogress=True, custom_content=True, hide_source=True))
        monkeypatch.setattr(wire.cli.parser, "unparse", _reject_legacy)
        _set_note(wire, mode, source=_DOCUMENT_SOURCE)
        await _parse_from_message(monkeypatch, wire, PipelineResult(parsed, processed, output), source=_DOCUMENT_SOURCE)
        rpc = _RPCS.SendMessage if mode == ParseMode.PREVIEW else _RPCS.SendMedia
        (query,) = wire.select(rpc)
        assert query.message.startswith(_LITERAL + _LINK_LABEL)
        assert (raw.types.MessageEntityTextUrl, _LINK_LABEL, _LINK) in _caption_spans(query)
        _assert_note(query)
        assert wire.modes[query.message] is enums.ParseMode.DISABLED

    asyncio.run(run())


def test_non_x_document_final_send_writes_original_document_to_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(helpers, "build_rich_markdown", _reject_legacy)
    monkeypatch.setattr(rich_blocks, "markdown_to_blocks", _reject_legacy)
    local = tmp_path / "photo.jpg"
    local.write_bytes(b"offline upload")
    media = [ImageRef(url="https://example.org/photo.jpg")]
    original, display = (
        MultimediaParseResult(
            content="LEGACY", media=media, document=Document((paragraph(text), Block("media", index=0))),
        )
        for text in ("ORIGINAL", _LITERAL)
    )
    original.platform = display.platform = Platform.PIXIV
    original.raw_url = display.raw_url = _DOCUMENT_SOURCE
    write_cache = AsyncMock()
    monkeypatch.setattr(delivery, "persistent_cache", SimpleNamespace(set=write_cache))

    async def run():
        wire = _Wire(monkeypatch)
        await delivery.send_rich_media(
            wire.sender, display, [ProcessedMedia(ImageFile(path=local, width=1, height=1))],
            _t=_t, raw_url=_DOCUMENT_SOURCE, cache_parse_result=original,
        )
        (query,) = wire.select(_RPCS.SendMessage)
        assert _LITERAL in _text(query.rich_message.blocks)
        write_cache.assert_awaited_once()
        assert write_cache.await_args is not None
        key, entry = write_cache.await_args.args
        assert key == _DOCUMENT_SOURCE
        assert entry.parse_result.document == original.document
        (item,) = entry.media
        assert FileId.decode(item.file_id).media_id == _PHOTO.id

        await delivery.send_cached(wire.sender, entry, key, _t=_t)
        warm = wire.select(_RPCS.SendMessage)[-1]
        assert "ORIGINAL" in _text(warm.rich_message.blocks)
        assert _LITERAL not in _text(warm.rich_message.blocks)

    asyncio.run(run())
