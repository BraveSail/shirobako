"""Ordinary X captions use literal text and Telegram's real MTProto entities."""

import asyncio
from datetime import UTC, datetime
from io import BytesIO

import pytest
from parsehub import Platform
from parsehub.types import MultimediaParseResult, Poll, PollOption
from parsehub.types.x_status import TextRun, XPost, XStatus, XTrending
from pyrogram import Client, enums
from pyrogram.raw.core import TLObject
from pyrogram.utils import parse_text_entities

from plugins.parse.x_caption import build_x_status_caption
from repo.settings import SettingsConfig

_SOURCE = "https://x.com/__author__/status/2095537240726450480?x=1&y=2"
_AUTHOR = "https://x.com/__author__"
_LABEL = "▎Source (Twitter)"


def _result(main: XPost, *, reply=None, quoted=None, trending=None, **kwargs) -> MultimediaParseResult:
    result = MultimediaParseResult(
        content="WRONG legacy HTML <b>body</b>",
        author_name="WRONG legacy author",
        x_status=XStatus(main=main, reply=reply, quoted=quoted, trending=trending),
        **kwargs,
    )
    result.platform = Platform.TWITTER
    return result


def _wire(text, entities):
    async def serialize():
        client = Client("x-caption-test", in_memory=True)
        payload = await parse_text_entities(client, text, enums.ParseMode.DISABLED, entities)
        assert payload["message"] == text
        wire = [TLObject.read(BytesIO(entity.write())) for entity in (payload["entities"] or ())]
        encoded = text.encode("utf-16-le")
        for entity in wire:
            # Strict decode checks BOTH ends of every actual TL entity for a split surrogate.
            assert entity.length > 0
            assert 0 <= entity.offset < entity.offset + entity.length <= len(encoded) // 2
            _label(encoded, entity)
        return wire

    return asyncio.run(serialize())


def _label(encoded, entity):
    return encoded[2 * entity.offset : 2 * (entity.offset + entity.length)].decode("utf-16-le")


def _spans(text, wire):
    encoded = text.encode("utf-16-le")
    return [(type(e).__name__, _label(encoded, e), getattr(e, "url", "")) for e in wire]


def test_literal_body_title_and_author_round_trip_with_utf16_links():
    raw = "    😀 <b>&amp;</b> **raw** ||no spoiler|| \\ #fake\n> not a quote\n{{poll:0}}\n"
    url = "https://example.org/a_(b)/__x__?q=1&z=2"
    result = _result(
        XPost(
            (TextRun(raw), TextRun("🔗链接", url, ("bold",)), TextRun("😄后半", url, ("italic", "strike"))),
            "🧑‍🎨 <Alice&>",
            "__author__",
            _AUTHOR,
        ),
        title="🏷️ <title> &amp;",
    )
    result.raw_url = _SOURCE

    text, entities = build_x_status_caption(result, config=SettingsConfig(), lang="en-us")
    assert text == f"🧑‍🎨 <Alice&> @__author__:\n\n🏷️ <title> &amp;\n\n{raw}🔗链接😄后半\n\n{_LABEL}"
    wire = _wire(text, entities)
    spans = _spans(text, wire)
    assert [(label, target) for kind, label, target in spans if kind == "MessageEntityTextUrl"] == [
        ("🧑‍🎨 <Alice&> @__author__", _AUTHOR),
        ("🔗链接😄后半", url),
        ("Source (Twitter)", _SOURCE),
    ]
    assert ("MessageEntityBold", "🧑‍🎨 <Alice&> @__author__:", "") in spans
    assert ("MessageEntityBold", "🏷️ <title> &amp;", "") in spans
    assert ("MessageEntityBold", "🔗链接", "") in spans
    assert ("MessageEntityItalic", "😄后半", "") in spans
    assert ("MessageEntityStrike", "😄后半", "") in spans
    assert all(kind not in {"MessageEntitySpoiler", "MessageEntityBlockquote"} for kind, _, _ in spans)
    source_entity = next(e for e in wire if getattr(e, "url", "") == _SOURCE)
    assert source_entity.offset == len(text[: text.rindex("Source")].encode("utf-16-le")) // 2


def test_related_cards_and_typed_polls_keep_role_and_valid_entity_nesting():
    reply_url = "https://x.com/__reply__"
    video_url = "https://www.youtube.com/watch?v=example"
    topic_url = "https://x.com/i/trending/123"
    option = "<b>🏳️‍🌈 | A</b> &amp; {{poll:2}}"
    result = _result(
        XPost((TextRun("MAIN {{poll:0}}"),), poll_index=1),
        reply=XPost(
            (TextRun("😀 replied", styles=("bold", "italic", "italic")),),
            "<reply>",
            "__reply__",
            reply_url,
            0,
            TextRun("🎞️ <video> **title**", video_url, ("italic",)),
        ),
        quoted=XPost((), "引用🦋", "quoted", "https://x.com/quoted"),
        trending=XTrending("<趋势> 🚀", "**subtitle**", topic_url, 0),
        polls=[Poll([PollOption(option, 3), PollOption("B", 1)], total_votes=4), Poll([PollOption("MAIN choice", 0)])],
    )
    text, entities = build_x_status_caption(result, config=SettingsConfig(hide_source=True), lang="en-us")
    wire = _wire(text, entities)
    spans = _spans(text, wire)
    quotes = [label for kind, label, _ in spans if kind == "MessageEntityBlockquote"]
    assert len(quotes) == 3
    assert "<reply> @__reply__\n😀 replied" in quotes[0]
    assert option in quotes[0] and "3 · 75%" in quotes[0] and "Votes" in quotes[0]
    assert "🎞️ <video> **title**" in quotes[0]
    assert "MAIN {{poll:0}}" in text and "MAIN choice" in text and "0 · 0%" in text
    assert text.index("😀 replied") < text.index("MAIN {{poll:0}}") < text.index("<趋势>") < text.index("引用🦋")
    assert "**subtitle**" in quotes[1] and quotes[2] == "引用🦋 @quoted"
    assert "{{media:0}}" not in text
    assert ("MessageEntityTextUrl", "<reply> @__reply__", reply_url) in spans
    assert ("MessageEntityTextUrl", "🎞️ <video> **title**", video_url) in spans
    assert any(kind == "MessageEntityTextUrl" and url == topic_url for kind, _, url in spans)
    for index, a in enumerate(wire):
        for b in wire[index + 1 :]:
            end_a, end_b = a.offset + a.length, b.offset + b.length
            if max(a.offset, b.offset) < min(end_a, end_b):
                assert (a.offset <= b.offset and end_b <= end_a) or (b.offset <= a.offset and end_a <= end_b)
                assert type(a) is not type(b)


def test_footer_source_fallback_and_hide_flags():
    result = _result(
        XPost((TextRun("BODY"),), "Alice", "alice"),
        reply=XPost((TextRun("REPLY"),)),
        title="TITLE",
        published_at=datetime(2026, 10, 4, 1, 37, tzinfo=UTC),
        view_count=1234,
        like_count=0,
    )
    text, entities = build_x_status_caption(
        result,
        config=SettingsConfig(hide_title=True, hide_desc=True),
        raw_url=_SOURCE,
        lang="en-us",
        view_label="reads 👀",
        custom_content="<b>user text</b> &amp; **literal**",
    )
    assert text == (
        f"@alice:\n\n<b>user text</b> &amp; **literal**\n\n2026-10-04 09:37 · 1,234 reads 👀 · 0 likes\n\n{_LABEL}"
    )
    wire = _wire(text, entities)
    spans = _spans(text, wire)
    assert [(label, url) for kind, label, url in spans if kind == "MessageEntityTextUrl"] == [
        ("Source (Twitter)", _SOURCE),
    ]
    timestamp = next(e for e in wire if type(e).__name__ == "MessageEntityFormattedDate")
    assert ("MessageEntityFormattedDate", "2026-10-04 09:37", "") in spans
    assert timestamp.date == int(result.published_at.timestamp()) and timestamp.long_date and timestamp.short_time
    assert all("user text" not in label for _, label, _ in spans)


def test_truncation_reserves_source_and_footer_with_emoji_and_links():
    url = "https://example.org/link"
    result = _result(
        XPost((TextRun("😀" * 53, styles=("bold",)), TextRun("🔗 long linked label", url), TextRun("b" * 1400))),
        like_count=10,
    )
    text, entities = build_x_status_caption(
        result,
        config=SettingsConfig(),
        raw_url=_SOURCE,
        lang="en-us",
        max_length=140,
    )
    assert text.startswith("😀" * 53)
    assert text.endswith(f"…\n\n10 likes\n\n{_LABEL}")
    assert "🔗" not in text  # Cut at the link boundary, never show a partial clickable label.
    assert len(text.encode("utf-16-le")) // 2 <= 140
    spans = _spans(text, _wire(text, entities))
    assert ("MessageEntityBold", "😀" * 53, "") in spans
    assert ("MessageEntityTextUrl", "Source (Twitter)", _SOURCE) in spans
    assert not any(target == url for _, _, target in spans)


def test_default_limit_clips_emoji_styles_inside_a_quote_on_utf16_boundaries():
    result = _result(XPost(()), reply=XPost((TextRun("!" + "🚀" * 600, styles=("bold", "italic")),)))
    text, entities = build_x_status_caption(result, config=SettingsConfig(), raw_url=_SOURCE, lang="en-us")
    assert len(text.encode("utf-16-le")) // 2 <= 1024
    assert text.endswith(f"…\n\n{_LABEL}")
    rocket_prefix = text.partition("…")[0]
    # The ASCII prefix leaves an odd number of code units for the emoji: use one fewer.
    assert rocket_prefix == "!" + "🚀" * ((1024 - len(_LABEL) - 4) // 2)
    assert len(text.encode("utf-16-le")) // 2 == 1023
    spans = _spans(text, _wire(text, entities))
    assert ("MessageEntityBlockquote", rocket_prefix + "…", "") in spans
    assert ("MessageEntityBold", rocket_prefix, "") in spans
    assert spans.count(("MessageEntityItalic", rocket_prefix, "")) == 1


def test_entityless_plain_caption_is_not_parsed_as_markup():
    raw = "**text** <i>plain</i> &amp; ||no spoiler||"
    text, entities = build_x_status_caption(_result(XPost((TextRun(raw),))), config=SettingsConfig(hide_source=True))
    assert text == raw
    assert entities == []
    assert _wire(text, entities) == []


def test_canonical_source_precedes_raw_url_and_short_source_fits_small_budget():
    result = _result(XPost((TextRun("BODY"),), poll_index=999), like_count=123)
    result.raw_url = _SOURCE
    text, entities = build_x_status_caption(
        result,
        config=SettingsConfig(),
        raw_url="https://twitter.com/original/status/1",
        lang="en-us",
        max_length=6,
    )
    assert text == "Source"
    assert ("MessageEntityTextUrl", "Source", _SOURCE) in _spans(text, _wire(text, entities))
    assert build_x_status_caption(result, config=SettingsConfig(), max_length=0) == ("", [])


def test_legacy_result_is_not_read_as_x_status():
    with pytest.raises(ValueError, match="x_status"):
        build_x_status_caption(MultimediaParseResult(content="<b>legacy</b>"), config=SettingsConfig())
