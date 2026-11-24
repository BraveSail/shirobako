"""Serialize ordinary source documents through Kurigram's explicit-entities path."""

import asyncio
from datetime import UTC, datetime
from inspect import signature
from io import BytesIO

import pytest
from parsehub import Platform
from parsehub.types import MultimediaParseResult, Poll, PollOption, VideoRef
from parsehub.types.document import Block, Document, TextRun, paragraph
from pyrogram import Client, enums
from pyrogram.raw.core import TLObject
from pyrogram.types import MessageEntity
from pyrogram.utils import parse_text_entities

from plugins.parse.document_caption import build_document_caption
from plugins.parse.x_caption import build_x_status_caption, extract_caption_note
from repo.settings import SettingsConfig
from services.cache import CacheParseResult

SOURCE = "https://example.org/post?a=1&b=2"
LINK = "https://example.org/a_(b)/__x__?q=1&z=2"


def _result(*, platform=None, **kwargs):
    result = MultimediaParseResult(**kwargs)
    result.platform = platform
    return result


def _build(result, **kwargs):
    kwargs.setdefault("config", SettingsConfig())
    kwargs.setdefault("lang", "en-us")
    return build_document_caption(result, **kwargs)


def _wire(text, entities):
    async def serialize():
        client = Client("document-caption-test", in_memory=True)
        payload = await parse_text_entities(client, text, enums.ParseMode.DISABLED, entities)
        assert payload["message"] == text
        return [TLObject.read(BytesIO(entity.write())) for entity in (payload["entities"] or ())]

    encoded = text.encode("utf-16-le")
    wire = asyncio.run(serialize())
    spans = []
    for index, entity in enumerate(wire):
        lo, hi = entity.offset, entity.offset + entity.length
        assert 0 <= lo < hi <= len(encoded) // 2
        label = encoded[2 * lo : 2 * hi].decode("utf-16-le")
        kind = type(entity).__name__
        spans.append((kind, label, getattr(entity, "url", "")))
        for other in wire[index + 1 :]:
            end = other.offset + other.length
            if max(lo, other.offset) < min(hi, end):
                assert (lo <= other.offset and end <= hi) or (other.offset <= lo and hi <= end)
                assert type(entity) is not type(other)
                assert kind not in {"MessageEntityCode", "MessageEntityPre"}
                assert type(other).__name__ not in {"MessageEntityCode", "MessageEntityPre"}
    return spans


def test_matches_x_caption_signature():
    assert signature(build_document_caption) == signature(build_x_status_caption)


def test_literal_markup_and_real_links_roundtrip_in_utf16():
    literal = "    😀 <b>&amp;</b> **raw** ||no spoiler|| \\ #fake\n> not a quote\n{{poll:0}}"
    result = _result(
        title="🧑‍🎨 <Title>",
        content="WRONG legacy <b>body</b>",
        platform=Platform.BILIBILI,
        author_name="Author 🦋",
        author_url="https://example.org/author",
        position_label="#4",
        document=Document(
            (
                paragraph(
                    (
                        TextRun(literal),
                        TextRun("🔗甲", LINK, ("bold", "bold")),
                        TextRun("🚀乙", LINK, ("italic", "code", "strike")),
                    )
                ),
            ),
            origin=(TextRun("Origin", "https://example.org/origin"),),
            topic=(TextRun("Topic", "https://example.org/topic"),),
        ),
    )
    result.raw_url = SOURCE
    text, entities = _build(result, raw_url="https://example.org/ignored")
    spans = _wire(text, entities)
    assert literal + "🔗甲🚀乙" in text
    assert "WRONG legacy" not in text and "#4" in text
    assert ("MessageEntityBold", "🧑‍🎨 <Title>", "") in spans
    assert ("MessageEntityBold", "🔗甲", "") in spans
    assert ("MessageEntityItalic", "🚀乙", "") in spans
    assert ("MessageEntityStrike", "🚀乙", "") in spans
    assert {label: url for kind, label, url in spans if kind == "MessageEntityTextUrl"} == {
        "Author 🦋": "https://example.org/author",
        "Origin": "https://example.org/origin",
        "Topic": "https://example.org/topic",
        "🔗甲🚀乙": LINK,
        "Source (Bilibili)": SOURCE,
    }
    assert not any(kind in {"MessageEntitySpoiler", "MessageEntityBlockquote"} for kind, _, _ in spans)


def test_truncates_heading_quote_and_list_without_losing_source_or_in_budget_link():
    nested = Block("quote", children=(paragraph("inner 🐈"),), role="quote")
    items = (
        (paragraph((TextRun("🔗ok", LINK, ("bold",)),)), nested),
        (paragraph("🚀" * 100),),
        (paragraph("not reached"),),
    )
    result = _result(
        platform=Platform.LINUXDO,
        quote_roles=["reply", "quoted"],  # The document alone owns placement and content.
        document=Document(
            (
                Block("heading", runs=(TextRun("Heading 🚲", styles=("bold", "bold")),)),
                Block(
                    "quote",
                    runs=(TextRun("Signer 🦄"),),
                    children=(Block("list", items=items, ordered=True, start=7),),
                    role="reply",
                ),
            )
        ),
    )
    text, entities = _build(result, raw_url=SOURCE, max_length=91)
    spans = _wire(text, entities)
    assert text.startswith("Heading 🚲\n\nSigner 🦄\n\n7. 🔗ok")
    assert "inner 🐈" in text and "\n8. 🚀" in text
    assert "not reached" not in text and text.count("Signer 🦄") == 1
    assert text.endswith("…\n\n▎Source (linux.do)")
    assert len(text.encode("utf-16-le")) // 2 <= 91
    assert spans.count(("MessageEntityBold", "Heading 🚲", "")) == 1
    quotes = [label for kind, label, _ in spans if kind == "MessageEntityBlockquote"]
    assert quotes == [text.split("\n\n", 1)[1].rsplit("\n\n", 1)[0]]
    assert ("MessageEntityTextUrl", "🔗ok", LINK) in spans
    assert ("MessageEntityTextUrl", "Source (linux.do)", SOURCE) in spans


def test_unsupported_rich_blocks_keep_visible_content_and_links():
    result = MultimediaParseResult(
        polls=[Poll([PollOption("A <a>", 3), PollOption("B", 1)], title="Poll 🗳", total_votes=4)],
        document=Document(
            (
                Block("details", runs=(TextRun("Summary"),), children=(paragraph("Details"),)),
                Block("code", runs=(TextRun("x = '\U0001f680'\n  # **raw**"),), language="python"),
                Block("table", rows=(((TextRun("row"),), (TextRun("🧪 cell", LINK),)),)),
                Block(
                    "collage",
                    children=(
                        Block("media", index=0, url="https://example.org/attachment"),
                        Block("media", url="https://example.org/img"),
                    ),
                ),
                Block("poll", index=0),
            )
        ),
    )
    text, entities = _build(result, config=SettingsConfig(hide_source=True))
    spans = _wire(text, entities)
    assert all(line in text for line in ("Summary", "Details", "row · 🧪 cell", "Poll 🗳", "A <a> · 3 · 75%"))
    assert ("MessageEntityPre", "x = '🚀'\n  # **raw**", "") in spans
    assert ("MessageEntityTextUrl", "🧪 cell", LINK) in spans
    assert any(url == "https://example.org/img" for _, _, url in spans)
    assert any(url == "https://example.org/attachment" for _, _, url in spans)
    assert "{{media:" not in text and "{{poll:" not in text


def test_localized_source_labels_and_cached_poll_keep_their_literals():
    result = CacheParseResult(
        document=Document(
            (Block("localize", runs=(TextRun("查看原話題", LINK),)), Block("poll", index=0)),
            origin=(TextRun("讨论", styles=("localize",)), TextRun(" ***raw***")),
        ),
        polls=[{"options": [{"text": "||one||"}], "title": "🎲 [Poll]"}],
    )
    text, entities = _build(result, config=SettingsConfig(hide_source=True))
    spans = _wire(text, entities)
    assert "Discussion ***raw***" in text
    assert "🎲 [Poll]" in text and "||one||" in text
    assert ("MessageEntityTextUrl", "查看原話題", LINK) in spans  # No en-us translation is in the catalog yet.


def test_cached_platform_hidden_config_and_native_note():
    result = CacheParseResult(
        platform=Platform.WEIBO.id,
        title="SECRET TITLE",
        content="LEGACY BODY",
        document=Document((paragraph("SECRET BODY"),)),
        published_at=datetime(2026, 10, 4, 1, 37, tzinfo=UTC),
        view_count=1234,
        like_count=0,
        reply_count=2,
    )
    message = "😀 original\n== note 🏳️‍🌈 **raw** =="
    label = "note 🏳️‍🌈"
    offset = len(message[: message.index(label)].encode("utf-16-le")) // 2
    span = MessageEntity(
        type=enums.MessageEntityType.TEXT_LINK, offset=offset, length=len(label.encode("utf-16-le")) // 2, url=LINK
    )
    note = extract_caption_note(message, [span])
    text, entities = _build(
        result,
        config=SettingsConfig(hide_title=True, hide_desc=True),
        raw_url=SOURCE,
        view_label="reads 👀",
        custom_content=note,
    )
    assert "SECRET" not in text and "LEGACY" not in text
    assert text.startswith("note 🏳️‍🌈 **raw**\n\n2026-10-04 09:37 · 1,234 reads 👀 · 0 likes")
    spans = _wire(text, entities)
    assert ("MessageEntityFormattedDate", "2026-10-04 09:37", "") in spans
    assert ("MessageEntityTextUrl", "Source (微博)", SOURCE) in spans
    assert ("MessageEntityTextUrl", label, LINK) in spans
    assert note.entities[0].offset == 0 and span.offset == offset
    hidden, entities = _build(
        result,
        config=SettingsConfig(hide_title=True, hide_desc=True, hide_source=True),
        raw_url=SOURCE,
        custom_content="**literal note**",
    )
    assert hidden.startswith("**literal note**\n\n")
    assert not any(url for _, _, url in _wire(hidden, entities))


def test_long_body_leaves_room_for_user_note_duration_and_source():
    result = _result(
        platform=Platform.YOUTUBE,
        media=[VideoRef(url="https://example.org/video", duration=3723)],
        document=Document((paragraph("🦋" * 600),)),
        view_count=8,
    )
    text, entities = _build(result, raw_url=SOURCE, max_length=99, custom_content="<b>note</b>")
    assert "…\n\n<b>note</b>\n\n" in text
    assert "1:02:03" in text and "8 views" in text
    assert text.endswith("▎Source (Youtube)")
    assert len(text.encode("utf-16-le")) // 2 <= 99
    _wire(text, entities)


def test_source_shortens_in_small_budget_and_untyped_content_is_not_parsed():
    text, entities = _build(
        _result(document=Document((paragraph("body"),)), platform=Platform.DOUYIN), raw_url=SOURCE, max_length=6
    )
    assert text == "Source"
    assert set(_wire(text, entities)) == {
        ("MessageEntityBold", "Source", ""),
        ("MessageEntityTextUrl", "Source", SOURCE),
    }
    with pytest.raises(ValueError, match="document"):
        _build(MultimediaParseResult(content="<b>legacy</b>"))
