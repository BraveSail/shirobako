"""Document delivery and both cache layers, with the real Telegram TL codec."""

import asyncio
import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from parsehub.types import BrokenMedia, ImageFile, ImageRef, MultimediaParseResult, Platform
from parsehub.types.document import Block, Document, TextRun, paragraph
from parsehub.types.x_status import XPost, XStatus
from pyrogram import utils as tg_utils
from pyrogram.file_id import FileId, FileType, ThumbnailSource
from pyrogram.raw import types as tl
from pyrogram.raw.core import TLObject
from pyrogram.types import InputRichBlockParagraph, InputRichMessage, RichMessage

from plugins import helpers
from plugins.parse import inline_rich, rich_blocks
from repo.settings.schema import DEFAULT_SETTINGS_CONFIG
from services import cache
from services.media import ProcessedMedia

URL = "https://www.zhihu.com/question/123/answer/456"
X_URL = "https://x.com/example/status/123"
CDN = tuple(f"https://cdn.example/{i}.jpg" for i in range(4))
SOURCE_TEXT = "    Raw **bold** || <details> &amp; {{media:3}}"
CONFIG = DEFAULT_SETTINGS_CONFIG.model_copy(update={"hide_source": False, "hide_desc": False})


def _result(*, duplicate=False):
    result = MultimediaParseResult(
        content="wrong legacy body",
        document=Document((
            paragraph((TextRun(SOURCE_TEXT), TextRun(" shown link", "https://example.org", ("bold",)))),
            Block("media", index=3, url=CDN[3]),
            paragraph("separator"),
            Block("media", index=0, url=CDN[0]),
            Block("media", index=1, url=CDN[1]),
            *((Block("media", index=3, url=CDN[3]),) if duplicate else ()),
            Block("quote", role="reply", children=(paragraph("reply body"), Block("media", index=2, url=CDN[2]))),
        )),
        media=[ImageRef(url=url) for url in CDN],
        reply_media_count=0,
    )
    result.raw_url, result.platform = URL, Platform.ZHIHU
    return result


def _photo_id(media_id):
    return FileId(
        file_type=FileType.PHOTO, dc_id=2, media_id=media_id, access_hash=42, volume_id=0, local_id=0,
        thumbnail_source=ThumbnailSource.THUMBNAIL, thumbnail_file_type=FileType.PHOTO, thumbnail_size="x",
    ).encode()


def _cache_photo(media_id):
    return cache.CacheMedia(type=cache.CacheMediaType.PHOTO, file_id=_photo_id(media_id))


def _media(result):
    return inline_rich.build_rich_media(result.media, [
        ProcessedMedia(BrokenMedia(path="", url=CDN[0])),
        ProcessedMedia(ImageFile(path=_photo_id(11), width=8, height=8), [_photo_id(11), _photo_id(12)]),
        ProcessedMedia(ImageFile(path=_photo_id(22), width=8, height=8)),
        ProcessedMedia(ImageFile(path=_photo_id(33), width=8, height=8)),
    ], reply_media_count=result.reply_media_count)


def _blocks(result, media, *, raw_url=URL):
    _, main, mapped, quoted, reply, urls = media
    return inline_rich.build_delivery_blocks(
        result, config=CONFIG, raw_url=raw_url, lang="en-us",
        media_placeholders=main, quote_media_placeholders=quoted,
        reply_media_placeholders=reply, media_blocks=mapped, media_urls=urls,
    )


def _decode(raw):
    encoded = BytesIO(raw.write())
    decoded = TLObject.read(encoded)
    assert encoded.read() == b""
    return decoded


def _wire(blocks):
    async def encode():
        return await InputRichMessage(blocks=blocks).write(client=None)

    return _decode(asyncio.run(encode()))


def _walk(value):
    if isinstance(value, list):
        for item in value:
            yield from _walk(item)
    elif isinstance(value, TLObject):
        yield value
        for key in value.__slots__:
            yield from _walk(getattr(value, key, None))


def _photos(wire):
    return [node.photo_id for node in _walk(wire.blocks) if isinstance(node, tl.PageBlockPhoto)]


def _urls(wire):
    return [node.url for node in _walk(wire.blocks) if isinstance(node, tl.TextUrl)]


def _text(wire):
    return "".join(n.text for n in _walk(wire.blocks) if isinstance(n, tl.TextPlain))


def _echo(wire):
    photos = [tl.Photo(
        id=index, access_hash=42, file_reference=b"", date=0, dc_id=2,
        sizes=[tl.PhotoSize(type="x", w=8, h=8, size=100)],
    ) for index in dict.fromkeys(_photos(wire))]
    value = _decode(tl.RichMessage(blocks=wire.blocks, photos=photos, documents=[]))
    async def parse():
        return await RichMessage._parse(None, value)

    return asyncio.run(parse())


@pytest.fixture
def redis(monkeypatch):
    store = {}

    async def get(key):
        return store.get(key)

    async def set_(key, payload, ttl):
        store[key] = payload

    monkeypatch.setattr(cache, "get_optional", get)
    monkeypatch.setattr(cache, "set_with_ttl", set_)
    return store


def test_document_summary_is_literal_main_body_not_legacy_text():
    result = _result()
    assert inline_rich.result_summary_text(result) == SOURCE_TEXT + " shown link\n\nseparator"
    result.document = Document()
    assert inline_rich.result_summary_text(result) == ""
    assert not inline_rich.has_structured_body(MultimediaParseResult(content=SOURCE_TEXT))
    assert inline_rich.has_structured_body(result)


def test_source_and_persistent_cache_use_document_write_version(redis):
    result = _result()

    async def check():
        source_cache = cache.ResultCache()
        file_cache = cache.PersistentCache()
        await source_cache.set(URL, result)
        short = json.loads(redis[source_cache._key(URL)])
        assert short["content_version"] == cache.CONTENT_VERSION
        rebuilt = await source_cache.get(URL)
        assert rebuilt is not None and rebuilt.document == result.document
        await file_cache.set(URL, inline_rich.rich_cache_entry(rebuilt, []))
        long = json.loads(redis[file_cache._key(URL)])["parse_result"]
        assert long["content_version"] == cache.CONTENT_VERSION
        assert long["document"] == short["document"]
        hit = await file_cache.get(URL)
        assert hit is not None and hit.parse_result.document == rebuilt.document

    asyncio.run(check())


@pytest.mark.parametrize("version", [12, 14])
def test_legacy_cache_hits_do_not_infer_a_document(redis, version):
    result = MultimediaParseResult(content="legacy body")
    url = URL
    if version == 14:
        url = X_URL
        result.x_status = XStatus(XPost((TextRun("v14 native post"),)))
        result.platform = Platform.TWITTER
    result.raw_url = url

    async def check():
        source_cache, file_cache = cache.ResultCache(), cache.PersistentCache()
        await source_cache.set(url, result)
        short = json.loads(redis[source_cache._key(url)])
        short["content_version"] = version
        redis[source_cache._key(url)] = json.dumps(short)
        entry = inline_rich.rich_cache_entry(result, [_cache_photo(33)])
        assert entry is not None
        entry.parse_result.content_version = version
        await file_cache.set(url, entry)
        return await source_cache.get(url), await file_cache.get(url)

    rebuilt, cached = asyncio.run(check())
    assert rebuilt is not None and rebuilt.document is None
    assert cached is not None and cached.parse_result.document is None and cached.media
    assert cached.media[0].file_id == _photo_id(33)
    body, _, _ = inline_rich.build_cached_rich_content(cached, url, config=CONFIG, lang="en-us")
    if version == 14:
        assert inline_rich.has_structured_body(rebuilt)
        assert inline_rich.result_summary_text(rebuilt) == "v14 native post"
        wire = _wire(body)
        assert "v14 native post" in _text(wire)
        assert _photos(wire) == [33]
    else:
        assert isinstance(body, str) and "legacy body" in body


def test_document_preview_does_not_resolve_unbuilt_media_or_parse_old_markup(monkeypatch):
    from plugins.parse import x_status

    result = _result()
    result.x_status = XStatus(XPost((TextRun("old X DTO must not render"),)))

    def reject(*_args, **_kwargs):
        pytest.fail("typed document must not use the legacy markup path")

    monkeypatch.setattr(helpers, "build_rich_markdown", reject)
    monkeypatch.setattr(rich_blocks, "markdown_to_blocks", reject)
    monkeypatch.setattr(x_status, "build_x_status_blocks", reject)
    media, main, _, quoted, reply, urls = _media(result)
    wire = _wire(_blocks(result, (media, main, {}, quoted, reply, urls)))
    assert SOURCE_TEXT in _text(wire)
    assert "wrong legacy body" not in _text(wire)
    assert not _photos(wire)
    assert CDN[0] in _urls(wire)  # An explicit broken marker remains a link, not an upload.
    preview = _wire(inline_rich.build_delivery_blocks(result, config=CONFIG))
    assert CDN[0] not in _urls(preview) and not _photos(preview)


@pytest.mark.parametrize("source", ["inline-upload", "returned", "returned-repeated"])
def test_file_ids_bind_to_source_refs_not_render_order(redis, source):
    result = _result(duplicate=source == "returned-repeated")
    media = _media(result)
    raw_override = URL + "?source=override"
    blocks = _blocks(result, media, raw_url=raw_override)
    cold = _wire(blocks)
    assert _photos(cold).index(33) < _photos(cold).index(11)
    assert _photos(cold).count(33) == (2 if source == "returned-repeated" else 1)
    assert _photos(cold).count(12) == 1
    assert CDN[0] in _urls(cold) and raw_override in _urls(cold)
    ids = ([_cache_photo(i) for i in (11, 12, 22, 33)] if source == "inline-upload"
           else inline_rich.extract_cache_media(_echo(cold)))

    async def check():
        persistent = cache.PersistentCache()
        entry = inline_rich.rich_cache_entry(result, ids, rendered_blocks=blocks, urls=media[-1])
        assert entry is not None
        await persistent.set(URL, entry)
        return await persistent.get(URL)

    hit = asyncio.run(check())
    assert hit is not None and hit.media
    assert [(item.ref_index, FileId.decode(item.file_id).media_id) for item in hit.media] == [
        (1, 11), (1, 12), (2, 22), (3, 33),
    ]
    assert hit.parse_result.failed_media == {0: CDN[0]}
    replay, _, _ = inline_rich.build_cached_rich_content(hit, raw_override, config=CONFIG, lang="en-us")
    warm = _wire(replay)
    assert warm.write() == cold.write()
    assert _photos(warm) == _photos(cold)


def test_ambiguous_return_order_does_not_produce_a_cache_entry():
    result = _result()
    media = _media(result)
    # A return missing input identities is ambiguous even if the number of IDs matches.
    ids = inline_rich.extract_cache_media(_echo(_wire(_blocks(result, media))))
    assert ids
    assert inline_rich.rich_cache_entry(
        result, ids, urls=media[-1], rendered_blocks=[InputRichBlockParagraph("not the actual rendered media")],
    ) is None


def test_partial_source_upload_cannot_write_a_document_cache():
    result = _result()
    # Equal upload/ID counts do not prove every source slot was processed.
    *_, urls = inline_rich.build_rich_media(result.media, [
        ProcessedMedia(ImageFile(path=_photo_id(10), width=8, height=8)),
    ])
    assert inline_rich.rich_cache_entry(result, [_cache_photo(10)], urls=urls) is None


def test_failed_reply_slot_does_not_shift_split_main_outputs_into_reply():
    result = _result()
    result.reply_media_count = 2
    result.document = Document((
        paragraph("main body"),
        Block("media", index=0, url=CDN[0]),
        Block("media", index=1, url=CDN[1]),
        Block("quote", role="reply", children=(
            paragraph("reply body"), Block("media", index=3, url=CDN[3]), Block("media", index=2, url=CDN[2]),
        )),
    ))
    media = inline_rich.build_rich_media(result.media, [
        ProcessedMedia(ImageFile(path=_photo_id(10), width=8, height=8)),
        ProcessedMedia(ImageFile(path=_photo_id(11), width=8, height=8), [_photo_id(11), _photo_id(12)]),
        ProcessedMedia(BrokenMedia(path="", url=CDN[2])),
        ProcessedMedia(ImageFile(path=_photo_id(33), width=8, height=8)),
    ], reply_media_count=2)
    blocks = _blocks(result, media)
    cold = _wire(blocks)
    (reply,) = [block for block in cold.blocks if isinstance(block, tl.PageBlockBlockquoteBlocks)]
    assert _photos(reply) == [33] and _urls(reply) == [CDN[2]]
    assert sorted(_photos(cold)) == [10, 11, 12, 33]

    entry = inline_rich.rich_cache_entry(
        result, inline_rich.extract_cache_media(_echo(cold)), rendered_blocks=blocks, urls=media[-1],
    )
    assert entry is not None
    entry = cache.CacheEntry.model_validate_json(entry.model_dump_json())
    assert entry.parse_result.reply_media_count == 2
    assert entry.parse_result.failed_media == {2: CDN[2]}
    replay, _, _ = inline_rich.build_cached_rich_content(entry, URL, config=CONFIG, lang="en-us")
    assert _wire(replay).blocks == cold.blocks


@pytest.mark.parametrize("own_photo", [False, True])
def test_linuxdo_reply_card_keeps_its_photo_before_the_floor_text_and_topic_tag(own_photo):
    url = "https://linux.do/t/topic/3248768/2"
    result = MultimediaParseResult(
        tags=["纯水"],
        # The CDN URL can repeat across floors; only the absolute index identifies each source ref.
        media=[ImageRef(url=CDN[0]) for _ in range(1 + own_photo)],
        document=Document((
            Block("quote", role="reply", runs=(TextRun("First floor · #1"),), children=(
                Block("media", index=int(own_photo), url=CDN[0]), paragraph("First-floor text"),
            )),
            *((Block("media", index=0, url=CDN[0]),) if own_photo else ()),
            paragraph("Second-floor text"),
        )),
    )
    result.raw_url, result.platform = url, Platform.LINUXDO
    photo_ids = [11, 22] if own_photo else [22]
    media = inline_rich.build_rich_media(result.media, [
        ProcessedMedia(ImageFile(path=_photo_id(i), width=8, height=8)) for i in photo_ids
    ])
    blocks = _blocks(result, media, raw_url=url)

    async def edit():
        invoke = AsyncMock(return_value=True)
        cli = SimpleNamespace(get_session=AsyncMock(return_value=SimpleNamespace(invoke=invoke)))
        inline_id = tg_utils.pack_inline_message_id(tl.InputBotInlineMessageID(dc_id=2, id=17, access_hash=33))
        assert await inline_rich.edit_inline_rich_message(cli, inline_id, blocks=blocks)
        invoke.assert_awaited_once()
        call = invoke.await_args
        assert call is not None
        return _decode(call.args[0]).rich_message

    cold = asyncio.run(edit())
    (card,) = (b for b in cold.blocks if isinstance(b, tl.PageBlockBlockquoteBlocks))
    assert _photos(card) == [22]
    assert isinstance(card.blocks[-2], tl.PageBlockPhoto) and isinstance(card.blocks[-1], tl.PageBlockParagraph)
    assert _photos(cold) == ([22, 11] if own_photo else [22])
    assert _text(cold).index("First-floor text") < _text(cold).index("Second-floor text") < _text(cold).index("#纯水")
    assert isinstance(cold.blocks[-1], tl.PageBlockFooter)
    assert "https://linux.do/tag/%E7%BA%AF%E6%B0%B4" in _urls(cold)

    entry = inline_rich.rich_cache_entry(result, [_cache_photo(i) for i in photo_ids], urls=media[-1])
    assert entry is not None and entry.parse_result.reply_media_count == 0
    hit = cache.CacheEntry.model_validate_json(entry.model_dump_json())
    warm, _, _ = inline_rich.build_cached_rich_content(hit, url, config=CONFIG, lang="en-us")
    assert _wire(warm).write() == cold.write()

    hidden, _, _ = inline_rich.build_cached_rich_content(
        hit, url, config=CONFIG.model_copy(update={"hide_desc": True}), lang="en-us",
    )
    assert 22 not in _photos(_wire(hidden))  # Hiding the card cannot leave its image in the gallery.
