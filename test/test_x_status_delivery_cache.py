"""Exercise both X cache readers and the real rich-message SDK offline."""

import asyncio
import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from parsehub.types import AniFile, AniRef, BrokenMedia, ImageFile, ImageRef, MultimediaParseResult, Platform
from parsehub.types.x_status import TextRun, XPost, XStatus, XTrending
from pyrogram.file_id import FileId, FileType, ThumbnailSource
from pyrogram.raw import types as tl
from pyrogram.raw.core import TLObject
from pyrogram.types import InputRichBlockAnimation, InputRichBlockParagraph, InputRichMessage, RichMessage

from plugins import helpers
from plugins.parse import inline_rich, rich_blocks
from repo.settings.schema import DEFAULT_SETTINGS_CONFIG
from services import cache
from services.media import ProcessedMedia

URL = "https://x.com/author/status/123"
ARTICLE = "https://x.com/i/article/123"
THREADS = "https://www.threads.com/@author/post/abc"
IMAGES = [f"https://cdn.example/{name}.jpg" for name in ("failed", "main", "cover", "reply", "quote")]
CONFIG = DEFAULT_SETTINGS_CONFIG.model_copy(update={"hide_desc": False, "hide_title": False, "hide_source": False})
LITERAL = "    Keep **literal** || _ {{media:2}} &amp;"


def _status():
    result = MultimediaParseResult(
        content="legacy markdown must not render", media=[ImageRef(url=url) for url in IMAGES],
        reply_media_count=1, quoted_media_count=1, is_sensitive=True,
        x_status=XStatus(
            XPost((TextRun(LITERAL), TextRun("link", "https://example.org", ("bold",)))),
            reply=XPost((TextRun("reply"),)), quoted=XPost((TextRun("quote"),)),
            trending=XTrending("trend", "summary", "https://x.com/i/trending/123", cover_index=2),
        ),
    )
    result.raw_url = URL
    result.platform = Platform.TWITTER
    return result


def _photo(media_id):
    return FileId(
        file_type=FileType.PHOTO, dc_id=2, media_id=media_id, access_hash=42, volume_id=0, local_id=0,
        thumbnail_source=ThumbnailSource.THUMBNAIL, thumbnail_file_type=FileType.PHOTO, thumbnail_size="x",
    ).encode()


def _cache_photo(media_id):
    return cache.CacheMedia(type=cache.CacheMediaType.PHOTO, file_id=_photo(media_id))


@pytest.fixture
def redis(monkeypatch):
    store = {}

    async def get(key):
        return store.get(key)

    async def set_(key, payload, ttl):
        store[key] = payload

    async def delete(keys):
        for key in keys:
            store.pop(key, None)

    monkeypatch.setattr(cache, "get_optional", get)
    monkeypatch.setattr(cache, "set_with_ttl", set_)
    monkeypatch.setattr(cache, "delete_keys", delete)
    return store


def _walk(value):
    if isinstance(value, list):
        for item in value:
            yield from _walk(item)
    elif isinstance(value, TLObject):
        yield value
        for key in value.__slots__:
            yield from _walk(getattr(value, key, None))


def _wire(message):
    async def encode():
        return await message.write(client=None)  # No client: uploading would fail.

    raw = asyncio.run(encode())
    stream = BytesIO(raw.write())
    decoded = TLObject.read(stream)
    assert stream.read() == b""
    assert isinstance(decoded, tl.InputRichMessage)
    return decoded


def _text(value):
    return "".join(n.text for n in _walk(value) if isinstance(n, tl.TextPlain))


def _photos(value):
    return [n.photo_id for n in _walk(value) if isinstance(n, tl.PageBlockPhoto)]


@pytest.mark.parametrize("cache_source", ["upload", "rendered"])
def test_x_cold_and_warm_delivery_keeps_native_media_roles_and_reuses_file_ids(redis, monkeypatch, cache_source):
    result = _status()
    raw_override = URL + "?source=override"

    def reject_markup(*_args, **_kwargs):
        pytest.fail("X delivery must not invoke the legacy Markdown renderers")

    monkeypatch.setattr(helpers, "build_rich_markdown", reject_markup)
    monkeypatch.setattr(helpers, "build_rich_markdown_by_str", reject_markup)
    monkeypatch.setattr(rich_blocks, "markdown_to_blocks", reject_markup)

    def processed(i, url):
        source = BrokenMedia(path="", url=url) if i == 0 else ImageFile(path=_photo(i), width=8, height=8)
        return ProcessedMedia(source=source)

    _, main, typed, quote, reply, urls = inline_rich.build_rich_media(
        result.media, [processed(i, url) for i, url in enumerate(IMAGES)],
        reply_media_count=1, quoted_media_count=1, is_sensitive=True,
    )
    cold = _wire(InputRichMessage(blocks=inline_rich.build_delivery_blocks(
        result, config=CONFIG, raw_url=raw_override, lang="en-us",
        media_placeholders=main, quote_media_placeholders=quote, reply_media_placeholders=reply,
        media_blocks=typed, media_urls=urls,
    )))
    assert LITERAL in _text(cold)
    assert IMAGES[0] in _text(cold)  # Failed image is a native link, not a shifted media ID.
    assert raw_override in [n.url for n in _walk(cold) if isinstance(n, tl.TextUrl)]
    assert [(role, _photos(b)) for b in cold.blocks if isinstance(b, tl.PageBlockBlockquoteBlocks)
            for role in ("reply", "trend", "quote") if role in _text(b)] == [
                ("reply", [3]), ("trend", [2]), ("quote", [4]),
            ]
    assert _photos(cold) == [3, 2, 1, 4]

    async def roundtrip():
        result_cache, persistent = cache.ResultCache(), cache.PersistentCache()
        await result_cache.set(URL, result)
        rebuilt = await result_cache.get(URL)
        assert rebuilt is not None
        assert rebuilt.x_status == result.x_status
        if cache_source == "rendered":
            # At the server-return boundary, the SDK photos come back in block traversal order.
            from pyrogram.types import RichBlockPhoto

            cached = inline_rich.extract_cache_media(SimpleNamespace(blocks=[
                RichBlockPhoto(photo=SimpleNamespace(file_id=_photo(i))) for i in _photos(cold)
            ]))
        else:
            cached = [_cache_photo(i) for i in range(1, 5)]
        original = inline_rich.rich_cache_entry(
            rebuilt, cached,
            quoted_media_count=1, reply_media_count=1, urls=urls,
        )
        await persistent.set(URL, original)
        return await persistent.get(URL)

    entry = asyncio.run(roundtrip())
    assert entry is not None
    assert entry.parse_result.x_status == result.x_status
    assert isinstance(entry.parse_result.x_status.main.segments, tuple)
    assert entry.parse_result.x_status.main.segments[-1].styles == ("bold",)
    blocks, media, media_blocks = inline_rich.build_cached_rich_content(
        entry, raw_override, config=CONFIG, lang="en-us"
    )
    assert isinstance(blocks, list)
    warm = _wire(inline_rich.cached_rich_message(blocks, media, media_blocks))
    assert warm.write() == cold.write()


@pytest.mark.parametrize("sensitive", [False, True])
def test_x_animation_sdk_readback_preserves_cold_and_cached_wire_shape(sensitive):
    document = tl.Document(
        id=42, access_hash=43, file_reference=b"ref", date=0, mime_type="video/mp4", size=100, dc_id=2,
        attributes=[tl.DocumentAttributeVideo(duration=1, w=8, h=8), tl.DocumentAttributeAnimated()],
    )
    file_id = FileId(
        file_type=FileType.ANIMATION, dc_id=2, media_id=42, access_hash=43, file_reference=b"ref",
    ).encode()
    result = MultimediaParseResult(
        media=[AniRef(url="https://cdn.example/loop.mp4", ext="mp4")], is_sensitive=sensitive,
        x_status=XStatus(XPost((TextRun(LITERAL),))),
    )
    result.platform, result.raw_url = Platform.TWITTER, URL
    processed = ProcessedMedia(AniFile(path=file_id, width=8, height=8, duration=1))
    _, main, typed, _, _, urls = inline_rich.build_rich_media(
        result.media, [processed], is_sensitive=sensitive,
    )
    cold = _wire(InputRichMessage(blocks=inline_rich.build_delivery_blocks(
        result, config=CONFIG, media_placeholders=main, media_blocks=typed,
    )))
    echo = tl.RichMessage(blocks=cold.blocks, photos=[], documents=[document])
    sdk_message = asyncio.run(RichMessage._parse(None, TLObject.read(BytesIO(echo.write()))))
    found = inline_rich.extract_cache_media(sdk_message)
    assert [(item.type, item.file_id) for item in found] == [(cache.CacheMediaType.ANIMATION, file_id)]

    written = inline_rich.rich_cache_entry(result, found, urls=urls)
    entry = cache.CacheEntry.model_validate_json(written.model_dump_json())
    blocks, media, media_blocks = inline_rich.build_cached_rich_content(entry, URL, config=CONFIG, lang="")
    block_type = rich_blocks.SpoilerVideoBlock if sensitive else InputRichBlockAnimation
    assert any(isinstance(block, block_type) for block in blocks)
    warm = _wire(inline_rich.cached_rich_message(blocks, media, media_blocks))
    (video,) = (node for node in _walk(warm) if isinstance(node, tl.PageBlockVideo))
    assert video.video_id == document.id and bool(video.spoiler) is sensitive
    assert warm.write() == cold.write()


@pytest.mark.parametrize(("url", "platform", "markdown", "hits"), [
    (URL, "twitter", "", False),
    (URL, "", "", False),
    (URL, "twitter", "## Article attached to status", True),
    (ARTICLE, "twitter", "", True),
    (THREADS, "threads", "", True),
])
def test_persistent_cache_refreshes_only_confirmed_old_statuses(redis, url, platform, markdown, hits):
    persistent = cache.PersistentCache()
    entry = inline_rich.rich_cache_entry(_status(), [])
    data = json.loads(entry.model_dump_json())
    data["parse_result"].pop("x_status", None)
    data["parse_result"].update(platform=platform, markdown_content=markdown, content_version=13)
    redis[persistent._key(url)] = json.dumps(data)
    assert (asyncio.run(persistent.get(url)) is not None) is hits


def test_result_layer_refreshes_legacy_x_multimedia(redis):
    async def check():
        result_cache = cache.ResultCache()
        post = _status()
        await result_cache.set(URL, post)
        data = json.loads(redis[result_cache._key(URL)])
        data.pop("x_status")
        data["content_version"] = 13
        redis[result_cache._key(URL)] = json.dumps(data)
        assert await result_cache.get(URL) is None

    asyncio.run(check())


def test_inline_edit_prefers_explicit_blocks(monkeypatch):
    from pyrogram import utils

    invoke = AsyncMock()
    get_session = AsyncMock(return_value=SimpleNamespace(invoke=invoke))
    monkeypatch.setattr(utils, "unpack_inline_message_id", lambda _: SimpleNamespace(dc_id=2))
    cli = SimpleNamespace(get_session=get_session)
    assert asyncio.run(inline_rich.edit_inline_rich_message(
        cli, "offline", blocks=[InputRichBlockParagraph("**literal**")],
    ))
    assert _text(invoke.call_args.args[0].rich_message) == "**literal**"
    # Empty blocks are invalid in the SDK, but must not silently select the old channel.
    with pytest.raises(ValueError, match="provide either blocks, markdown or html"):
        asyncio.run(inline_rich.edit_inline_rich_message(cli, "offline", blocks=[], markdown="legacy"))
