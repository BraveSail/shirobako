"""Offline entry-point tests: encode real SDK rich messages and intercept network I/O."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from parsehub.types import (
    AniFile,
    AniRef,
    BrokenMedia,
    ImageFile,
    ImageRef,
    MultimediaParseResult,
    Platform,
    VideoFile,
    VideoRef,
)
from parsehub.types.document import Block, Document, paragraph
from parsehub.types.x_status import TextRun, XPost, XStatus
from PIL import Image
from pyrogram.errors import BadRequest, WebpageCurlFailed
from pyrogram.parser import Parser
from pyrogram.raw import functions as rpc
from pyrogram.raw import types as tl
from pyrogram.raw.core import TLObject

from i18n import t_
from plugins import translate
from plugins.parse import guest, inline, reporters
from plugins.parse.inline_rich import RICH_RESULT_ID, rich_cache_entry
from repo.settings.schema import DEFAULT_SETTINGS_CONFIG
from services import pipeline
from services.cache import CacheEntry, CacheMediaType
from services.media import ProcessedMedia

URL = "https://x.com/a/status/123"
DOCUMENT_URL = "https://www.pixiv.net/artworks/12345"
MID = "BQAAAH8EAACb7f-02gbl1xUz2F4"
DEST = "https://example.org/(literal)?x=1&y=2"
COVER = "https://example.org/cover.jpg"
LITERAL = "    # literal\n> || ** [x] <b> &amp; {{media:1}}"
ORIGINAL = " link __raw__"
TRANSLATED = " link __translated__ 🌕"
CONFIG = DEFAULT_SETTINGS_CONFIG.model_copy(
    update={"hide_title": False, "hide_desc": False, "hide_source": False, "video_cover": False, "noprogress": False}
)
USER = SimpleNamespace(id=1, language_code="zh-hans")


def _status(*, gallery=True, typed=True):
    result = MultimediaParseResult(
        title="Title",
        content="legacy summary",
        media=[ImageRef(url=f"https://example.org/{role}.jpg") for role in ("main", "reply", "quote")]
        if gallery
        else [],
        reply_media_count=int(gallery),
        quoted_media_count=int(gallery),
        is_sensitive=True,
        x_status=XStatus(
            XPost((TextRun(LITERAL), TextRun(ORIGINAL, DEST, ("bold",))), "Alice"),
            XPost((TextRun("Reply"),)),
            XPost((TextRun("Quote"),)),
        )
        if typed
        else None,
    )
    result.platform = Platform.TWITTER
    result.raw_url = URL
    return result


def _wire(value):
    stream = BytesIO(value.write())
    decoded = TLObject.read(stream)
    assert not stream.read()
    return decoded


def _nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from _nodes(item)
    elif isinstance(value, TLObject):
        yield value
        for name in value.__slots__:
            yield from _nodes(getattr(value, name, None))


def _text(value):
    return "".join(node.text for node in _nodes(value) if isinstance(node, tl.TextPlain))


def _photos(value):
    return [node.photo_id for node in _nodes(value) if isinstance(node, tl.PageBlockPhoto)]


def _videos(value):
    return [node for node in _nodes(value) if isinstance(node, tl.PageBlockVideo)]


def _assert_status(value, *, translated=False):
    assert isinstance(value, tl.InputRichMessage)
    assert LITERAL in _text(value)
    (link,) = (node for node in _nodes(value) if isinstance(node, tl.TextUrl) and node.url == DEST)
    assert _text(link) == (TRANSLATED if translated else ORIGINAL)
    assert any(isinstance(node, tl.TextBold) and link in list(_nodes(node)) for node in _nodes(value))
    assert "legacy summary" not in _text(value)


def _assert_gallery(value):
    reply, quote = (node for node in _nodes(value) if isinstance(node, tl.PageBlockBlockquoteBlocks))
    assert (_text(reply), _photos(reply)) == ("Reply", [102])
    assert (_text(quote), _photos(quote)) == ("Quote", [103])
    assert _photos(value) == [102, 101, 103]
    assert all(node.spoiler for node in _nodes(value) if isinstance(node, tl.PageBlockPhoto))


class FakeTelegram:
    me = SimpleNamespace(username="testbot")
    parser = Parser(None)

    def __init__(
        self,
        *,
        reject_edits=False,
        reject_guest_media=False,
        fail_preupload=False,
        guest_handle: str | None = MID,
        fail_cover=False,
    ):
        self.guest, self.edits, self.uploads = [], [], []
        self.guest_results, self.documents, self.cover_attempts = [], [], []
        self.reject_edits = reject_edits
        self.reject_guest_media = reject_guest_media
        self.fail_preupload = fail_preupload
        self.guest_handle = guest_handle
        self.fail_cover = fail_cover

    async def answer_guest_query(self, query_id, article):
        assert query_id == "q1"
        result = _wire(await article.write(self))
        self.guest_results.append(result)
        sent = result.send_message
        self.guest.append(sent)
        if self.reject_guest_media and (_photos(sent) or _videos(sent)):
            raise RuntimeError("guest media rejected")
        return SimpleNamespace(inline_message_id=self.guest_handle)

    async def get_session(self, dc_id, is_media):
        assert dc_id == 5 and is_media
        return self

    @staticmethod
    def guess_mime_type(_):
        return "video/mp4"

    async def save_file(self, path, **_):
        if path is not None:
            return tl.InputFile(id=int(path[-5]) + 100, parts=1, name=path.rsplit("/", 1)[-1], md5_checksum="")

    async def invoke(self, request):
        if isinstance(request, rpc.messages.UploadMedia):
            _wire(request)
            if isinstance(request.media, tl.InputMediaPhotoExternal):
                self.cover_attempts.append(request.media.url)
                if self.fail_cover:
                    raise WebpageCurlFailed()
            self.uploads.append(request.media.file.id)
            if self.fail_preupload and len(self.uploads) <= 3:
                raise RuntimeError("preupload failed")
            if isinstance(request.media, tl.InputMediaUploadedDocument):
                self.documents.append(request.media)
                return SimpleNamespace(
                    document=SimpleNamespace(dc_id=5, id=request.media.file.id, access_hash=42, file_reference=b"ref")
                )
            return SimpleNamespace(
                photo=SimpleNamespace(
                    dc_id=5,
                    id=request.media.file.id,
                    access_hash=42,
                    file_reference=b"ref",
                    sizes=[SimpleNamespace(type="x", w=8, h=8)],
                )
            )
        assert isinstance(request, rpc.messages.EditInlineBotMessage)
        self.edits.append(_wire(request))
        assert isinstance(request.reply_markup, tl.ReplyKeyboardHide)
        if self.reject_edits and _photos(request.rich_message):
            raise RuntimeError("edit rejected")
        return True


@pytest.fixture
def setup(monkeypatch, tmp_path):
    def prepare(result, *, missing=(), allowed=True, cached=None, config=CONFIG, url=URL):
        @asynccontextmanager
        async def session():
            yield None

        folders = tmp_path / "download"
        folders.mkdir(exist_ok=True)
        processed = []
        for index, ref in enumerate(result.media or [], 1):
            if index not in missing:
                path = folders / f"{index}.{ref.ext}"
                if isinstance(ref, (AniRef, VideoRef)):
                    path.touch()
                    kind = AniFile if isinstance(ref, AniRef) else VideoFile
                    source = kind(path=str(path), width=8, height=8, duration=1)
                else:
                    Image.new("RGB", (8, 8)).save(path)
                    source = ImageFile(path=str(path))
                processed.append(ProcessedMedia(source))
            else:
                processed.append(ProcessedMedia(BrokenMedia(path="", url=ref.url)))
        result.download = AsyncMock(return_value=SimpleNamespace(output_dir=folders, media=[]))
        monkeypatch.setattr(pipeline, "process_media_files", AsyncMock(return_value=processed))
        cache_write = AsyncMock()
        persistent_write = AsyncMock()
        service = SimpleNamespace(get_raw_url=AsyncMock(return_value=url), parse=AsyncMock(return_value=result))
        for module in (guest, inline):
            monkeypatch.setattr(module, "get_session", session)
            monkeypatch.setattr(module, "ParseService", lambda: service)
            monkeypatch.setattr(
                module, "UserService", lambda _: SimpleNamespace(ensure_lang=AsyncMock(return_value="zh-hans"))
            )
            monkeypatch.setattr(
                module, "AdminUserService", lambda _: SimpleNamespace(is_whitelisted=AsyncMock(return_value=allowed))
            )
            monkeypatch.setattr(
                module, "SettingsService", lambda _: SimpleNamespace(get_config_by_user=AsyncMock(return_value=config))
            )
        monkeypatch.setattr(inline.parse_cache, "get", AsyncMock(return_value=result))
        monkeypatch.setattr(inline.parse_cache, "set", cache_write)
        monkeypatch.setattr(inline.persistent_cache, "get", AsyncMock(return_value=cached))
        monkeypatch.setattr(inline.persistent_cache, "set", persistent_write)

        # Keep the real translation display-copy functions; only replace the translation provider/cache work.
        async def translate_status(status, *_):
            return replace(
                status, main=replace(status.main, segments=(TextRun(LITERAL), TextRun(TRANSLATED, DEST, ("bold",))))
            )

        monkeypatch.setattr(translate, "_translate_x_status", translate_status)
        return service, persistent_write

    return prepare


def _inline_preview(cli, query, *, article=False):
    results = []

    async def answer(items, **_):
        results.extend([_wire(await item.write(cli)) for item in items])

    asyncio.run(inline.call_inline_parse(cli, SimpleNamespace(query=query, from_user=USER, answer=answer)))
    (result,) = (item for item in results if item.id == RICH_RESULT_ID)
    return result if article else result.send_message


def _guest(cli, text=URL):
    asyncio.run(
        guest.guest_parse(
            cli,
            SimpleNamespace(
                text=f"@testbot {text}",
                caption=None,
                from_user=USER,
                chat=SimpleNamespace(id=-100),
                guest_query_id="q1",
            ),
        )
    )


def _choose(cli, query=URL, *, mid: str | None = MID):
    asyncio.run(
        inline.inline_result_download(
            cli,
            SimpleNamespace(
                result_id=RICH_RESULT_ID,
                query=query,
                inline_message_id=mid,
                from_user=USER,
            ),
        )
    )


@pytest.mark.parametrize("surface", ["preview", "chosen", "guest", "warm-preview", "warm-guest"])
def test_non_x_document_skips_legacy_on_inline_and_guest_sdk_wire(setup, monkeypatch, surface):
    from plugins import helpers
    from plugins.parse import rich_blocks

    def reject_legacy(*args, **kwargs):
        pytest.fail("A non-X document must not use a legacy renderer")

    result = MultimediaParseResult(
        content="legacy body", media=[ImageRef(url="https://example.org/photo.jpg")],
        document=Document((paragraph(LITERAL), Block("media", index=0))),
    )
    result.platform = Platform.PIXIV
    result.raw_url = DOCUMENT_URL
    _, persistent_write = setup(result, url=DOCUMENT_URL)
    for module, name in (
        (helpers, "build_rich_markdown"), (rich_blocks, "markdown_to_blocks"),
        (inline, "build_rich_markdown"), (inline, "markdown_to_blocks"),
        (guest, "build_progress_markdown"), (guest, "build_rich_markdown"),
        (guest, "markdown_to_blocks"), (reporters, "build_progress_markdown"),
        (reporters, "markdown_to_blocks"),
    ):
        monkeypatch.setattr(module, name, reject_legacy)
    cli = FakeTelegram(reject_edits=surface == "guest", reject_guest_media=True)
    if surface == "preview":
        messages = [_inline_preview(cli, DOCUMENT_URL).rich_message]
        assert not _photos(messages[0])
    elif surface == "chosen" or surface.startswith("warm-"):
        _choose(cli, DOCUMENT_URL)
        messages = [edit.rich_message for edit in cli.edits if edit.rich_message is not None]
        assert _photos(messages[-1]) == [101]
        if surface.startswith("warm-"):
            persistent_write.assert_awaited_once()
            assert persistent_write.await_args is not None
            _, entry = persistent_write.await_args.args
            setup(result, url=DOCUMENT_URL, cached=entry)
            cli = FakeTelegram()
            if surface == "warm-preview":
                messages = [_inline_preview(cli, DOCUMENT_URL).rich_message]
            else:
                _guest(cli, DOCUMENT_URL)
                messages = [answer.rich_message for answer in cli.guest]
            assert len(messages) == 1 and _photos(messages[0]) == [101]
    else:
        _guest(cli, DOCUMENT_URL)
        messages = [entry.rich_message for entry in cli.guest]
        assert len(messages) == 3
        assert not _photos(messages[0]) and _photos(messages[1]) == [101] and not _photos(messages[-1])
    for message in messages:
        assert LITERAL in _text(message)
        assert "legacy body" not in _text(message)


def test_inline_preview_is_native_and_translation_leaves_parse_cache_original(setup):
    original = _status()
    setup(original)
    cli = FakeTelegram()
    preview = _inline_preview(cli, f"{URL} #t #nsfw")
    _assert_status(preview.rich_message, translated=True)
    assert isinstance(preview.reply_markup, tl.ReplyInlineMarkup)
    assert not _photos(preview)
    (details,) = [node for node in preview.rich_message.blocks if isinstance(node, tl.PageBlockDetails)]
    assert LITERAL in _text(details)
    assert original.x_status and original.x_status.main.segments[-1].text == ORIGINAL
    _assert_status(_inline_preview(cli, URL).rich_message)


@pytest.mark.parametrize("warm", [False, True])
def test_inline_article_summary_uses_only_current_x_main_runs(setup, warm):
    original = _status(gallery=False)
    setup(original, cached=rich_cache_entry(original, []) if warm else None)
    cli = FakeTelegram()
    for flag, tail in [(" #t #nsfw", TRANSLATED), ("", ORIGINAL)]:
        item = _inline_preview(cli, URL + flag, article=True)
        assert item.title == "Title"
        assert item.description == " ".join((LITERAL + tail).split())


@pytest.mark.parametrize("warm", [False, True])
def test_guest_article_fallback_title_and_description_use_display_runs(setup, warm):
    original = _status(gallery=False)
    original.title = ""
    setup(original, cached=rich_cache_entry(original, []) if warm else None)
    cli = FakeTelegram(guest_handle=None)
    _guest(cli, f"{URL} #t")
    assert len(cli.guest_results) == (1 if warm else 2)
    expected = " ".join((LITERAL + TRANSLATED).split())
    assert [(item.title, item.description) for item in cli.guest_results] == [(expected, expected)] * len(cli.guest)


@pytest.mark.parametrize("sensitive", [False, True])
def test_inline_preupload_keeps_animation_file_ids_and_spoiler(setup, sensitive):
    result = _status(gallery=False)
    result.media = [AniRef(url="https://example.org/loop.mp4", ext="mp4")]
    result.is_sensitive = sensitive
    _, write = setup(result)
    cli = FakeTelegram()
    _choose(cli)
    write.assert_awaited_once()
    entry = CacheEntry.model_validate_json(write.await_args.args[1].model_dump_json())
    assert [item.type for item in entry.media or []] == [CacheMediaType.ANIMATION]
    (upload,) = cli.documents
    assert any(isinstance(attr, tl.DocumentAttributeAnimated) for attr in upload.attributes)
    cold = cli.edits[-1].rich_message
    (video,) = _videos(cold)
    assert bool(video.spoiler) is sensitive

    setup(result, cached=entry)
    warm = _inline_preview(cli, URL).rich_message
    assert warm.write() == cold.write()
    assert len(cli.documents) == 1


@pytest.mark.parametrize("fail_preupload", [False, True])
def test_inline_selected_preserves_media_roles_with_or_without_file_id(setup, fail_preupload):
    original = _status()
    _, write = setup(original)
    cli = FakeTelegram(fail_preupload=fail_preupload)
    _choose(cli, f"{URL} #t")
    assert cli.edits[0].rich_message is None  # drop the handle-only keyboard before doing work
    for request in cli.edits[1:]:
        _assert_status(request.rich_message, translated=True)
    _assert_gallery(cli.edits[-1].rich_message)
    if fail_preupload:
        write.assert_not_awaited()
        assert len(cli.uploads) == 6  # failed cache upload, then local media used for the real edit
    else:
        write.assert_awaited_once()
        assert cli.uploads == [101, 102, 103]
        entry = CacheEntry.model_validate_json(write.await_args.args[1].model_dump_json())
        setup(original, cached=entry)
        cached = _inline_preview(cli, URL)
        _assert_status(cached.rich_message)
        _assert_gallery(cached.rich_message)
        assert cached.reply_markup is None
        assert cli.uploads == [101, 102, 103]


def test_inline_without_a_handle_is_not_downloaded(setup):
    original = _status(gallery=False)
    setup(original)
    cli = FakeTelegram()
    preview = _inline_preview(cli, URL)
    _assert_status(preview.rich_message)
    assert preview.reply_markup is None
    _choose(cli, mid=None)
    assert not cli.edits
    original.download.assert_not_awaited()


def test_inline_cache_without_file_ids_keeps_a_handle_for_the_final_edit(setup):
    original = _status()
    setup(original, cached=rich_cache_entry(original, []))
    cli = FakeTelegram()
    preview = _inline_preview(cli, URL)
    _assert_status(preview.rich_message)
    assert isinstance(preview.reply_markup, tl.ReplyInlineMarkup)
    assert not _photos(preview)
    original.download.assert_not_awaited()

    _choose(cli)
    original.download.assert_awaited_once()
    _assert_gallery(cli.edits[-1].rich_message)


def test_inline_rejected_cached_media_returns_rich_error_without_cache_mutation(setup):
    original = _status()
    _, write = setup(original)
    _choose(FakeTelegram())
    cached = CacheEntry.model_validate_json(write.await_args.args[1].model_dump_json())
    service, write = setup(original, cached=cached)
    cli = FakeTelegram()
    attempts = []

    async def answer(results, *, cache_time):
        (item,) = [await article.write(cli) for article in results]
        attempts.append((_wire(item), cache_time))
        if _photos(item):
            raise BadRequest("FILE_REFERENCE_EXPIRED")

    asyncio.run(inline.call_inline_parse(cli, SimpleNamespace(query=URL, from_user=USER, answer=answer)))
    (first, first_ttl), (fallback, fallback_ttl) = attempts
    assert (first_ttl, fallback_ttl) == (60, 1)
    _assert_gallery(first.send_message.rich_message)
    assert isinstance(fallback.send_message, tl.InputBotInlineMessageRichMessage)
    assert fallback.id == "parse-error" and not _photos(fallback)
    assert "请稍后重试" in _text(fallback)
    write.assert_not_awaited()
    service.parse.assert_not_awaited()
    original.download.assert_not_awaited()
    assert not cli.uploads and not cli.edits


@pytest.mark.parametrize("reject_edits,reject_media", [(False, False), (True, False), (True, True)])
def test_guest_edits_or_falls_back_without_losing_native_text(setup, reject_edits, reject_media):
    setup(_status())
    cli = FakeTelegram(reject_edits=reject_edits, reject_guest_media=reject_media)
    _guest(cli, f"{URL} #t")
    first_frame = cli.guest[0].rich_message
    _assert_status(first_frame, translated=True)
    assert "下 载 中" in _text(first_frame) and not _photos(first_frame)
    _assert_status(cli.edits[-1].rich_message, translated=True)
    _assert_gallery(cli.edits[-1].rich_message)
    assert len(cli.guest) == 1 + reject_edits + reject_media
    if reject_edits:
        _assert_gallery(cli.guest[1].rich_message)
    last = cli.guest[-1].rich_message
    _assert_status(last, translated=True)
    if reject_media:
        assert not _photos(last)
        assert "下 载 中" not in _text(last)


def test_guest_broken_media_stays_a_link_in_its_role(setup):
    setup(_status(), missing=(1, 2, 3))
    cli = FakeTelegram()
    _guest(cli)
    result = cli.edits[-1].rich_message
    _assert_status(result)
    assert not _photos(result)
    reply, quote = (node for node in _nodes(result) if isinstance(node, tl.PageBlockBlockquoteBlocks))
    assert "https://example.org/reply.jpg" in _text(reply)
    assert "https://example.org/quote.jpg" in _text(quote)


def test_guest_sends_full_result_directly_if_first_frame_has_no_handle(setup):
    setup(_status())
    cli = FakeTelegram(guest_handle=None)
    _guest(cli)
    first, final = (article.rich_message for article in cli.guest)
    assert not _photos(first) and not cli.edits
    _assert_status(final)
    _assert_gallery(final)


@pytest.mark.parametrize("reject_video", [False, True])
def test_guest_direct_answer_retries_without_cover_before_dropping_video(setup, monkeypatch, reject_video):
    result = _status(gallery=False)
    result.media = [VideoRef(url="https://example.org/video.mp4", thumb_url=COVER)]
    setup(result, config=CONFIG.model_copy(update={"video_cover": True}))
    monkeypatch.setattr(guest, "prepare_video_thumbs", AsyncMock(return_value={}))
    cli = FakeTelegram(guest_handle=None, fail_cover=True, reject_guest_media=reject_video)
    _guest(cli, f"{URL} #t")
    assert not cli.edits
    assert cli.cover_attempts == [COVER]
    assert not _videos(cli.guest[0])
    (video,) = _videos(cli.guest[1])
    assert video.video_id == 101 and video.spoiler
    assert len(cli.guest) == 2 + reject_video
    assert bool(_videos(cli.guest[-1])) is not reject_video
    _assert_status(cli.guest[-1].rich_message, translated=True)


@pytest.mark.parametrize("reject_media", [False, True])
def test_guest_cache_replays_file_ids_or_keeps_native_text_and_broken_links(setup, reject_media):
    original = _status()
    _, write = setup(original, missing=(2,))
    _choose(FakeTelegram())
    write.assert_awaited_once()
    entry = CacheEntry.model_validate_json(write.await_args.args[1].model_dump_json())

    service, _ = setup(original, cached=entry)
    cli = FakeTelegram(reject_guest_media=reject_media)
    _guest(cli, f"{URL} #t")
    service.parse.assert_not_awaited()
    original.download.assert_not_awaited()
    assert not cli.uploads and not cli.edits
    assert len(cli.guest) == 1 + reject_media
    assert _photos(cli.guest[0]) == [101, 103]
    final = cli.guest[-1].rich_message
    _assert_status(final, translated=True)
    reply, quote = (node for node in _nodes(final) if isinstance(node, tl.PageBlockBlockquoteBlocks))
    assert "https://example.org/reply.jpg" in _text(reply) and not _photos(reply)
    assert _photos(quote) == ([] if reject_media else [103])
    status = entry.parse_result.x_status
    assert status and status.main.segments[-1].text == ORIGINAL


def test_guest_legacy_edit_contains_text_and_media_not_mapping_keys(setup):
    original = _status(typed=False)
    setup(original)
    cli = FakeTelegram()
    _guest(cli)
    final = cli.edits[-1].rich_message
    assert isinstance(final, tl.InputRichMessage)
    assert "legacy summary" in _text(final)
    assert 101 in _photos(final)


def test_guest_denial_emits_rich_notice_without_parse(setup):
    service, _ = setup(_status(), allowed=False)
    cli = FakeTelegram()
    _guest(cli)
    (denied,) = cli.guest
    assert isinstance(denied, tl.InputBotInlineMessageRichMessage)
    assert _text(denied.rich_message) == "无权限"
    service.parse.assert_not_awaited()


@pytest.mark.parametrize("inline_status", [False, True])
def test_reporters_keep_native_content_and_dedup_the_changing_footer(monkeypatch, inline_status):
    cli = FakeTelegram()
    regular = SimpleNamespace(answer_rich=AsyncMock(), reply_rich=AsyncMock(), edit_text=AsyncMock())
    regular.answer_rich.return_value = regular.reply_rich.return_value = regular
    reporter = (
        reporters.InlineStatusReporter(cli, MID, t=t_["zh-hans"], user_config=CONFIG, raw_url=URL)
        if inline_status
        else reporters.MessageStatusReporter(cli, regular, t=t_["zh-hans"], config=CONFIG, raw_url=URL)
    )
    clock = [10.0]
    monkeypatch.setattr(reporters, "monotonic", lambda: clock[0])

    async def run():
        await reporter.report("Waiting for parse")
        await reporter.report_result(_status(), "Downloading")
        await reporter.report_progress("10%")  # throttled
        clock[0] += 2
        await reporter.report_progress("25%")
        clock[0] += 2
        await reporter.report_progress("25%")  # duplicate
        await reporter.report_result(_status(), "Processing")  # stage bypasses throttle
        if inline_status:
            return [req.rich_message for req in cli.edits]
        initial = (regular.answer_rich.await_args or regular.reply_rich.await_args).kwargs["rich_message"]
        messages = [initial, *(call.kwargs["rich_message"] for call in regular.edit_text.await_args_list)]
        return [_wire(await msg.write(client=None)) for msg in messages]

    skeleton, downloading, refreshed, processing = asyncio.run(run())
    assert "Waiting for parse" in _text(skeleton) and LITERAL not in _text(skeleton)
    for stage, frame in [("Downloading", downloading), ("25%", refreshed), ("Processing", processing)]:
        _assert_status(frame)
        assert not _photos(frame)
        assert f"▎{stage}" in _text(frame.blocks[-1])
        assert "<b>▎" not in _text(frame)
