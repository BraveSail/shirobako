"""Exercise ordinary bot delivery through the SDK's request encoder, without network I/O."""

import asyncio
import tarfile
from contextlib import nullcontext
from io import BytesIO
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from parsehub import Platform
from parsehub.types import (
    AniFile,
    AniRef,
    BrokenMedia,
    ImageFile,
    ImageRef,
    LivePhotoFile,
    MultimediaParseResult,
    VideoFile,
    VideoRef,
)
from parsehub.types.document import Block, Document, paragraph
from parsehub.types.x_status import TextRun, XPost, XStatus
from pyrogram import Client, enums, raw
from pyrogram.errors import MessageIdInvalid, WebpageCurlFailed
from pyrogram.file_id import FileId
from pyrogram.raw.core import TLObject
from pyrogram.types import Chat, Message, MessageEntity, User

from i18n import t_
from plugins import helpers
from plugins.parse import handlers, rich_blocks
from plugins.parse import sender as delivery
from plugins.parse.notes import extract_note
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


class _Wire:
    """Only peer lookup, file upload and network I/O are replaced."""

    def __init__(self, monkeypatch, *, reject_edit=None, reject_cover=False, config=None):
        self.events = []
        self.reject_edit = reject_edit
        self.reject_cover = reject_cover
        self.cli = Client("offline-delivery", in_memory=True)
        self.cli.link_preview_options = None
        monkeypatch.setattr(self.cli, "resolve_peer", AsyncMock(return_value=_PEER))
        monkeypatch.setattr(self.cli, "save_file", self.save_file)
        monkeypatch.setattr(self.cli, "invoke", self.invoke)

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
def test_file_delivery_sends_originals_with_literal_rich_body(monkeypatch, tmp_path, mode, file_count):
    cache = AsyncMock()
    monkeypatch.setattr(delivery, "persistent_cache", SimpleNamespace(set=cache))
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
        if mode == ParseMode.ZIP:
            async def save_archive(path, **kwargs):
                if path is None:
                    return None
                assert not out.exists()
                assert Path(path).parent != out.parent
                with tarfile.open(path, "r:gz") as tar:
                    (name,) = [member.name for member in tar.getmembers() if member.isfile()]
                    assert name == "output/0.jpg"
                    original = tar.extractfile(name)
                    assert original is not None and original.read() == b"offline upload"
                return await wire.save_file(path, **kwargs)

            monkeypatch.setattr(wire.cli, "save_file", save_archive)
        _set_note(wire, mode)
        incoming_spans = [(entity.offset, entity.length) for entity in wire.msg.entities]
        note = extract_note(wire.msg.text, wire.msg.entities)
        send = delivery.send_zip if mode == ParseMode.ZIP else delivery.send_raw
        await send(wire.sender, result, wire.reporter, _t=_t, custom_content=note)
        assert not wire.select(_RPCS.SendMedia) and not wire.select(_RPCS.SendMultiMedia)
        (query,) = wire.select(_RPCS.SendMessage)
        assert query.message == "" and not query.entities
        assert isinstance(query.rich_message, raw.types.InputRichMessage)
        blocks = query.rich_message.blocks
        assert _text(blocks).startswith(_LITERAL + _LINK_LABEL)
        assert _NOTE in _text(blocks) and "ignored" not in _text(blocks) and "second" not in _text(blocks)
        bold = {_text(n) for n in _walk(blocks) if isinstance(n, raw.types.TextBold)}
        assert {_LINK_LABEL, _NOTE.split(_NOTE_LABEL)[0], _NOTE_LABEL} <= bold
        links = {(n.url, _text(n)) for n in _walk(blocks) if isinstance(n, raw.types.TextUrl)}
        assert links == {(_LINK, _LINK_LABEL), (_NOTE_LINK, _NOTE_LABEL), (_SOURCE, "Source (Twitter)")}
        assert all(isinstance(b, raw.types.PageBlockDocument) for b in blocks[-2 - file_count:-2])
        assert isinstance(blocks[-1], raw.types.PageBlockFooter)
        assert len(query.rich_message.documents) == file_count and not query.rich_message.photos
        uploads = wire.select(_RPCS.UploadMedia)
        assert all(isinstance(u.media, raw.types.InputMediaUploadedDocument) and u.media.force_file for u in uploads)
        assert [Path(u.media.file.name).name for u in uploads] == (
            ["output.tar.gz"] if mode == ParseMode.ZIP else [p.name for p in paths]
        )
        assert [(entity.offset, entity.length) for entity in wire.msg.entities] == incoming_spans
        cache.assert_not_awaited()
        assert not out.exists()
        assert not out.with_suffix(".tar.gz").exists()
        if mode == ParseMode.ZIP:
            assert not Path(uploads[0].media.file.name).parent.exists()

    asyncio.run(run())


def test_zip_waits_for_packer_even_when_cancelled_twice(monkeypatch, tmp_path):
    out = tmp_path / "download"
    out.mkdir()
    (out / "source.txt").write_text("original")
    release = Event()
    destinations = []
    original_pack = delivery.pack_dir_to_tar_gz

    async def run():
        wire = _Wire(monkeypatch)
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def paused_pack(source, output_path=None):
            destinations.append(output_path)
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise TimeoutError("test did not release archive thread")
            return original_pack(source, output_path)

        monkeypatch.setattr(delivery, "pack_dir_to_tar_gz", paused_pack)
        pending = asyncio.create_task(
            delivery.send_zip(wire.sender, PipelineResult(_result(), output_dir=out), wire.reporter, _t=_t),
        )
        await asyncio.wait_for(started.wait(), timeout=3)
        try:
            for _ in range(2):
                pending.cancel()
                await asyncio.sleep(0.01)
            waiting = not pending.done() and out.is_dir() and destinations[0].parent.is_dir()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=3)
        assert waiting
        assert not out.exists() and not destinations[0].parent.exists()
        assert not wire.select(_RPCS.UploadMedia)

    asyncio.run(run())


@pytest.mark.parametrize("status, spoiler", [("edit", ""), ("edit", "#剧透"), ("rejected", ""), ("none", "")])
def test_raw_finalizes_with_live_photo_gif_and_failed_link(monkeypatch, tmp_path, status, spoiler):
    paths = [tmp_path / name for name in ("live-processed.jpg", "live.mov", "local.gif")]
    for path in paths:
        path.write_bytes(b"original bytes")
    gif = AniRef(url="https://example.org/local.gif")
    broken = "https://example.org/broken.png"
    missing_gif = "https://example.org/not-downloaded.gif"
    parsed = _result(media=[
        ImageRef(url="https://example.org/live.jpg"), gif, ImageRef(url=broken), AniRef(url=missing_gif),
    ])
    result = PipelineResult(parsed, [
        ProcessedMedia(LivePhotoFile(path=tmp_path / "unused.jpg", video_path=paths[1], width=1), [paths[0]]),
        ProcessedMedia(AniFile(path=paths[2], width=1, duration=1)),
        ProcessedMedia(BrokenMedia(path="", url=broken)),
    ])

    async def run():
        wire = _Wire(monkeypatch, reject_edit=MessageIdInvalid() if status == "rejected" else None)
        if status != "none":
            wire.status_message()
        await delivery.send_raw(wire.sender, result, wire.reporter, _t=_t, spoiler_tag=spoiler)
        assert not wire.select(_RPCS.SendMedia) and not wire.select(_RPCS.SendMultiMedia)
        uploads = [Path(u.media.file.name) for u in wire.select(_RPCS.UploadMedia)]
        if status == "edit":
            (query,) = wire.select(_RPCS.EditMessage)
            assert query.id == 99 and not wire.select(_RPCS.SendMessage)
        else:
            (query,) = wire.select(_RPCS.SendMessage)
            if status == "rejected":
                (edit,) = wire.select(_RPCS.EditMessage)
                assert edit.id == 99 and edit.reply_markup.write() == query.reply_markup.write()
                assert edit.rich_message.write() == query.rich_message.write()
            else:
                assert not wire.select(_RPCS.EditMessage)
        assert query.message == ""
        assert uploads == (paths * 2 if status == "rejected" else paths)
        assert len(query.rich_message.documents) == 3
        outer = query.rich_message.blocks
        if spoiler:
            (fold,) = [b for b in outer if isinstance(b, raw.types.PageBlockDetails)]
            assert _text(fold.title) == "⚠️ #剧透" and not fold.open
            assert not any(isinstance(b, raw.types.PageBlockDocument) for b in outer)
            assert query.reply_markup is None
            blocks = fold.blocks
        else:
            buttons = [button.type.url for row in query.reply_markup.rows for button in row.buttons]
            assert buttons == [gif.url, missing_gif]
            blocks = outer
        assert sum(isinstance(b, raw.types.PageBlockDocument) for b in blocks) == 3
        assert {broken, missing_gif} <= {b.url for b in _walk(blocks) if isinstance(b, raw.types.TextUrl)}

    asyncio.run(run())


@pytest.mark.parametrize("fail_overflow", [False, True])
def test_raw_files_over_limit_keep_edit_and_protect_every_batch(monkeypatch, tmp_path, fail_overflow):
    out = tmp_path / "files"
    out.mkdir()
    paths = [out / f"{i}.jpg" for i in range(51)]
    for path in paths:
        path.write_bytes(path.name.encode())
    media = [ImageRef(url=f"https://example.org/{path.name}") for path in paths]
    processed = [ProcessedMedia(ImageFile(path=path, width=1, height=1)) for path in paths]

    async def run():
        wire = _Wire(monkeypatch)
        wire.status_message()
        if fail_overflow:
            invoke = wire.invoke

            async def reject_last_file(request, **kwargs):
                if isinstance(request, _RPCS.UploadMedia) and Path(request.media.file.name) == paths[-1]:
                    raise RuntimeError("overflow upload failed")
                return await invoke(request, **kwargs)

            monkeypatch.setattr(wire.cli, "invoke", reject_last_file)
        await delivery.send_raw(
            wire.sender.delete_after(0.001), PipelineResult(_result(media=media), processed, out), wire.reporter,
            _t=_t, spoiler_tag="#剧透", custom_content="**app note**",
        )
        (first,) = wire.select(_RPCS.EditMessage)
        (second,) = wire.select(_RPCS.SendMessage)
        assert first.id == 99
        if fail_overflow:
            assert len(first.rich_message.documents) == 50
            assert "Upload error:" in _text(second.rich_message.blocks)
            assert not second.message and not second.rich_message.documents
            assert not out.exists()
            await asyncio.sleep(0.02)
            return
        assert [len(q.rich_message.documents) for q in (first, second)] == [50, 1]
        for query in (first, second):
            assert not query.message and not query.reply_markup
            (fold,) = [b for b in query.rich_message.blocks if isinstance(b, raw.types.PageBlockDetails)]
            assert _text(fold.title) == "⚠️ #剧透" and not fold.open
            assert sum(isinstance(b, raw.types.PageBlockDocument) for b in fold.blocks) == len(
                query.rich_message.documents
            )
            assert not any(isinstance(b, raw.types.PageBlockDocument) for b in query.rich_message.blocks)
        assert "app note" in _text(first.rich_message.blocks)
        assert [Path(u.media.file.name) for u in wire.select(_RPCS.UploadMedia)] == paths
        assert not out.exists()
        await asyncio.sleep(0.02)
        assert sorted(q.id for q in wire.select(_RPCS.DeleteMessages)) == [[42], [99]]

    asyncio.run(run())


@pytest.mark.parametrize("mode", [ParseMode.RAW, ParseMode.ZIP])
def test_file_delivery_reports_upload_failure_and_cleans_up(monkeypatch, tmp_path, mode):
    output = tmp_path / "output"
    output.mkdir()
    photo = output / "photo.jpg"
    photo.write_bytes(b"original bytes")
    media = [ImageRef(url="https://example.org/photo.jpg")]
    result = PipelineResult(_result(media=media), [ProcessedMedia(ImageFile(path=photo, width=1, height=1))], output)

    async def run():
        wire = _Wire(monkeypatch)
        report_error = AsyncMock()
        monkeypatch.setattr(wire.reporter, "report_error", report_error)

        async def invoke(request, **kwargs):
            if isinstance(request, _RPCS.UploadMedia):
                raise RuntimeError("offline upload failed")
            return await wire.invoke(request, **kwargs)

        monkeypatch.setattr(wire.cli, "invoke", invoke)
        send = delivery.send_zip if mode == ParseMode.ZIP else delivery.send_raw
        await send(wire.sender, result, wire.reporter, _t=_t)
        report_error.assert_awaited_once()
        assert report_error.await_args is not None and report_error.await_args.args[0] == _t("上传")
        assert not wire.select(_RPCS.SendMessage)
        assert not output.exists() and not output.with_suffix(".tar.gz").exists()

    asyncio.run(run())


@pytest.mark.parametrize("status", ["edit", "rejected", "no-status", "entityless", "spoiler"])
def test_gif_only_uses_rich_blocks_with_buttons_on_edit_and_fallback(monkeypatch, status):
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
        if status == "spoiler":
            wire.msg.text += "\n#剧透"
            wire.msg.command = wire.msg.text[1:].split()
        await _parse_from_message(monkeypatch, wire, PipelineResult(parsed))
        if status == "edit":
            (query,) = wire.select(_RPCS.EditMessage)
            assert query.id == 99
            assert not wire.select(_RPCS.SendMessage)
        else:
            (query,) = wire.select(_RPCS.SendMessage)
            if status == "rejected":
                (attempt,) = wire.select(_RPCS.EditMessage)
                assert attempt.rich_message.write() == query.rich_message.write()
        assert query.rich_message and not query.message and not query.entities
        if isinstance(query, _RPCS.EditMessage):
            assert query.no_webpage
        blocks = query.rich_message.blocks
        gif_links = {(m.url, f"GIF {i}") for i, m in enumerate(media, 1)}
        if status == "spoiler":
            assert query.reply_markup is None
            (fold,) = [block for block in blocks if isinstance(block, raw.types.PageBlockDetails)]
            assert [n.url for n in _walk(fold.blocks) if isinstance(n, raw.types.TextUrl) and n.url.endswith(".gif")]
        else:
            assert [b.type.url for row in query.reply_markup.rows for b in row.buttons] == [m.url for m in media]
        nodes = list(_walk(blocks))
        assert _text(query.rich_message.blocks).startswith(_LITERAL)
        assert _NOTE in _text(query.rich_message.blocks)
        assert "ignored" not in _text(query.rich_message.blocks)
        links = {(n.url, _text(n)) for n in nodes if isinstance(n, raw.types.TextUrl)}
        assert links == (set() if entityless else {
            (_LINK, _LINK_LABEL), (_NOTE_LINK, _NOTE_LABEL), (_SOURCE, "Source (Twitter)"),
        } | (gif_links if status == "spoiler" else set()))
        assert not any(isinstance(n, raw.types.TextSpoiler) for n in nodes)

    asyncio.run(run())


def test_legacy_body_uses_a_native_gif_note(monkeypatch):
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
        monkeypatch.setattr(wire.cli.parser, "unparse", _reject_legacy)
        await _parse_from_message(monkeypatch, wire, PipelineResult(result))
        (query,) = wire.select(_RPCS.SendMessage)
        assert any(
            _text(node) == "legacy note" for node in _walk(query.rich_message.blocks)
            if isinstance(node, raw.types.TextBold)
        )

    asyncio.run(run())


def test_user_mention_in_note_is_native_and_clickable(monkeypatch):
    name = "Mention 👀"
    source = f"==😀 {name}=="
    span = MessageEntity(
        type=enums.MessageEntityType.TEXT_MENTION, user=User(id=932), offset=5,
        length=len(name.encode("utf-16-le")) // 2,
    )

    async def run():
        wire = _Wire(monkeypatch)
        await delivery.send_rich_media(wire.sender, _result(), [], _t=_t, custom_content=extract_note(source, [span]))
        (rpc,) = wire.select(_RPCS.SendMessage)
        (mention,) = [node for node in _walk(rpc.rich_message.blocks) if isinstance(node, raw.types.TextMentionName)]
        assert (mention.user_id, _text(mention)) == (932, name)

    asyncio.run(run())


@pytest.mark.parametrize("mode", [ParseMode.RAW, ParseMode.ZIP, ParseMode.PREVIEW])
def test_non_x_document_and_note_use_rich_blocks_on_sdk_wire(monkeypatch, tmp_path, mode):
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
        document=Document((
            paragraph((TextRun(_LITERAL), TextRun(_LINK_LABEL, _LINK, ("bold",)))),
            *((Block("media", index=0), paragraph("After the original media slot")) if processed else ()),
        )),
    )
    parsed.platform = Platform.PIXIV
    parsed.raw_url = _DOCUMENT_SOURCE

    async def run():
        wire = _Wire(monkeypatch, config=SettingsConfig(noprogress=True, custom_content=True, hide_source=True))
        monkeypatch.setattr(wire.cli.parser, "unparse", _reject_legacy)
        _set_note(wire, mode, source=_DOCUMENT_SOURCE)
        if mode == ParseMode.PREVIEW:
            await _parse_from_message(
                monkeypatch, wire, PipelineResult(parsed, processed, output), source=_DOCUMENT_SOURCE,
            )
            (query,) = wire.select(_RPCS.SendMessage)
            blocks = query.rich_message.blocks
            assert not query.message and _text(blocks).startswith(_LITERAL + _LINK_LABEL)
            assert _NOTE in _text(blocks)
            assert {(n.url, _text(n)) for n in _walk(blocks) if isinstance(n, raw.types.TextUrl)} == {
                (_LINK, _LINK_LABEL), (_NOTE_LINK, _NOTE_LABEL),
            }
        else:
            note = extract_note(wire.msg.text, wire.msg.entities)
            send = delivery.send_zip if mode == ParseMode.ZIP else delivery.send_raw
            if mode == ParseMode.ZIP:
                wire.status_message()
            await send(
                wire.sender, PipelineResult(parsed, processed, output), wire.reporter, _t=_t,
                custom_content=note, spoiler_tag="#剧透" if mode == ParseMode.ZIP else "",
            )
            if mode == ParseMode.ZIP:
                (query,) = wire.select(_RPCS.EditMessage)
                assert query.id == 99 and not wire.select(_RPCS.SendMessage)
                (fold,) = query.rich_message.blocks
                assert _text(fold.title) == "⚠️ #剧透"
                blocks = fold.blocks
            else:
                (query,) = wire.select(_RPCS.SendMessage)
                blocks = query.rich_message.blocks
            assert query.message == "" and not wire.select(_RPCS.SendMedia)
            assert _text(blocks).startswith(_LITERAL + _LINK_LABEL) and _NOTE in _text(blocks)
            assert {(n.url, _text(n)) for n in _walk(blocks) if isinstance(n, raw.types.TextUrl)} == {
                (_LINK, _LINK_LABEL), (_NOTE_LINK, _NOTE_LABEL),
            }
            assert "After the original media slot" in _text(blocks[:-1])
            assert not any(isinstance(b, raw.types.PageBlockPhoto) for b in _walk(blocks))
            assert isinstance(blocks[-1], raw.types.PageBlockDocument)

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
