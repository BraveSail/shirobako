"""Exercise the installed SDK's native rich-message wire format, without Telegram I/O."""

import asyncio
from datetime import UTC, datetime
from io import BytesIO

import pytest
from parsehub.types import MultimediaParseResult, Platform
from parsehub.types.result import Poll, PollOption
from parsehub.types.x_status import TextRun, XPost, XStatus, XTrending
from pyrogram.file_id import FileId, FileType, ThumbnailSource
from pyrogram.raw import types as tl
from pyrogram.raw.core import TLObject
from pyrogram.types import InputMediaPhoto, InputMediaVideo, InputRichBlockParagraph, InputRichMessage, RichTextUrl

from plugins.parse import rich_blocks
from plugins.parse.rich_blocks import SpoilerPhotoBlock, SpoilerVideoBlock
from plugins.parse.x_status import build_x_status_blocks
from repo.settings.schema import DEFAULT_SETTINGS_CONFIG

URL = "https://x.com/author/status/123?a=1&b=2"
SPECIAL = "    indented\n# literal\n> || ** _ ` {{poll:0}} <b> &amp; []()\n---"
DEST = "https://example.org/a_(b)?x=1&y=2"


def _result(main, *, reply=None, quoted=None, trending=None, **kwargs):
    result = MultimediaParseResult(
        content="DO NOT RENDER LEGACY CONTENT", x_status=XStatus(main, reply, quoted, trending), **kwargs,
    )
    result.platform = Platform.TWITTER
    result.raw_url = URL
    return result


def _wire(result, *, settings=None, **kwargs):
    config = DEFAULT_SETTINGS_CONFIG.model_copy(update=settings or {})
    message = InputRichMessage(blocks=build_x_status_blocks(result, config=config, **kwargs))

    async def encode():
        return await message.write(client=None)

    stream = BytesIO(asyncio.run(encode()).write())
    decoded = TLObject.read(stream)
    assert stream.read() == b""
    assert isinstance(decoded, tl.InputRichMessage)
    assert not decoded.noautolink
    return decoded.blocks


def _nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from _nodes(item)
    elif isinstance(value, TLObject):
        yield value
        for field in value.__slots__:
            yield from _nodes(getattr(value, field, None))


def _text(value):
    return "".join(node.text for node in _nodes(value) if isinstance(node, tl.TextPlain))


def _media(value):
    return [
        (node.photo_id if isinstance(node, tl.PageBlockPhoto) else node.video_id, node.spoiler)
        for node in _nodes(value) if isinstance(node, (tl.PageBlockPhoto, tl.PageBlockVideo))
    ]


def _photo(media_id):
    file_id = FileId(
        file_type=FileType.PHOTO, dc_id=2, media_id=media_id, access_hash=42, volume_id=0, local_id=0,
        thumbnail_source=ThumbnailSource.THUMBNAIL, thumbnail_file_type=FileType.PHOTO, thumbnail_size="x",
    ).encode()
    return SpoilerPhotoBlock(InputMediaPhoto(file_id))


def _video(media_id):
    file_id = FileId(file_type=FileType.VIDEO, dc_id=2, media_id=media_id, access_hash=42).encode()
    return SpoilerVideoBlock(InputMediaVideo(file_id))


def test_status_uses_typed_text_entities_and_literal_metadata_on_the_wire(monkeypatch):
    def forbid_conversion(*args, **kwargs):
        pytest.fail("Typed X data must never be parsed as markup")

    monkeypatch.setattr(rich_blocks, "parse_inline", forbid_conversion)
    monkeypatch.setattr(rich_blocks, "markdown_to_blocks", forbid_conversion)
    result = _result(
        XPost((TextRun(SPECIAL), TextRun("🌕 (#本物)", DEST, ("bold", "italic", "strike"))),
              "**A<b>**", "__a__", "https://x.com/__a__"),
        title="<em>__T__</em>", tags=["tag_|&"], published_at=datetime(2026, 11, 24, 4, 5, tzinfo=UTC),
        view_count=1550, like_count=0, reply_count=7,
    )
    blocks = _wire(result, lang="zh-hans", view_label="[_view_]", settings={"hide_desc": False, "hide_title": False})
    title, author, divider, body, tags, end_divider, footer = blocks
    assert _text(title) == "<em>__T__</em>"
    assert _text(author) == "**A<b>** @__a__"
    assert [node.url for node in _nodes(author) if isinstance(node, tl.TextUrl)] == ["https://x.com/__a__"]
    assert _text(next(node for node in _nodes(author) if isinstance(node, tl.TextFixed))) == "@__a__"
    assert isinstance(divider, tl.PageBlockDivider) and isinstance(end_divider, tl.PageBlockDivider)
    assert isinstance(body, tl.PageBlockParagraph)
    assert _text(body) == SPECIAL + "🌕 (#本物)"
    link, = (node for node in _nodes(body) if isinstance(node, tl.TextUrl))
    assert (link.url, _text(link)) == (DEST, "🌕 (#本物)")
    assert {tl.TextBold, tl.TextItalic, tl.TextStrike} <= {type(node) for node in _nodes(body)}
    assert not any(isinstance(node, tl.TextSpoiler) for node in _nodes(body))
    assert _text(tags) == "#tag_|&"
    assert [node.url for node in _nodes(tags) if isinstance(node, tl.TextUrl)] == ["https://x.com/hashtag/tag_%7C%26"]
    assert isinstance(footer, tl.PageBlockFooter)
    sent_time, = (node for node in _nodes(footer) if isinstance(node, tl.TextDate))
    assert sent_time.date == int(result.published_at.timestamp())
    assert sent_time.long_date and sent_time.short_time
    assert "1,550 [_view_] · 0 点赞 · 7 回复" in _text(footer)
    assert [node.url for node in _nodes(footer) if isinstance(node, tl.TextUrl)] == [URL]


@pytest.mark.parametrize("count", [4, 5])
def test_media_collages_preserve_role_order_and_four_item_threshold(count):
    result = _result(
        XPost((TextRun("main"),)), reply=XPost((TextRun("reply"),)),
        quoted=XPost((TextRun("quoted"),), youtube=TextRun("<b>YT</b>", DEST)),
        trending=XTrending("<b>Trend</b>", "summary\n  line", "https://x.com/i/trending/1", 0),
    )
    blocks = _wire(
        result, settings={"hide_source": True},
        main_media=[_photo(101), _video(102), *(_photo(100 + i) for i in range(3, count + 1))],
        reply_media=[_photo(201), _video(202)], quoted_media=[_photo(301), _video(302)],
        trending_media=[_photo(401)],
    )
    reply, main, trending, gap, preview, *tail = blocks
    assert isinstance(gap, tl.PageBlockDivider)
    assert _text(reply) == "reply" and isinstance(reply.blocks[-1], tl.PageBlockCollage)
    assert _media(reply) == [(201, True), (202, True)]
    assert _text(main) == "main"
    assert _text(trending.blocks[:1]) == "<b>Trend</b>" and _media(trending.blocks[1]) == [(401, True)]
    assert _text(trending.blocks[2]) == "summary\n  line"
    assert isinstance(preview, tl.PageBlockCollage)
    assert _media(preview) == [(i, True) for i in range(101, 105)]
    if count == 5:
        detail = tail.pop(0)
        assert isinstance(detail, tl.PageBlockDetails) and _media(detail) == [(105, True)]
    between, quoted = tail
    assert isinstance(between, tl.PageBlockDivider)
    assert _text(quoted) == "quoted<b>YT</b>" and _media(quoted) == [(301, True), (302, True)]
    assert [node.url for node in _nodes(quoted) if isinstance(node, tl.TextUrl)] == [DEST]
    assert isinstance(quoted.blocks[-1], tl.PageBlockCollage)


def test_long_body_and_quotes_expand_independently_without_losing_link_boundaries():
    lead = "x" * 290
    link_text = "✨ first link here"
    tail = "   " + "y" * 510
    reply = XPost((TextRun("r" * 550),), "Author")
    quote = XPost((TextRun("q" * 550),), "Long author")
    result = _result(XPost((TextRun(lead), TextRun(link_text, DEST), TextRun(tail))), reply=reply, quoted=quote)
    blocks = _wire(result, reply_media=[_photo(201)], quoted_media=[_photo(301)], settings={"hide_source": True})
    reply_block, preview, body_detail, quote_block = blocks
    assert all(isinstance(card, tl.PageBlockBlockquoteBlocks) for card in (reply_block, quote_block))
    assert _text(preview) == lead
    assert isinstance(body_detail, tl.PageBlockDetails)
    assert _text(body_detail.blocks) == link_text + tail
    link, = (node for node in _nodes(body_detail) if isinstance(node, tl.TextUrl))
    assert (link.url, _text(link)) == (DEST, link_text)
    for card, media_id in [(reply_block, 201), (quote_block, 301)]:
        assert isinstance(card.blocks[1], tl.PageBlockDivider)
        assert isinstance(card.blocks[3], tl.PageBlockDetails)
        assert not _media(card.blocks[3])
        assert _media(card.blocks[4]) == [(media_id, True)]


def test_manual_spoiler_wraps_content_without_nested_automatic_folds():
    long = "body" * 140
    result = _result(
        XPost((TextRun(long),), "Author"), reply=XPost((TextRun(long),)),
        quoted=XPost((TextRun(long),)), tags=["useful"],
    )
    author, divider, detail, end_divider, footer = _wire(
        result, hide_content="#剧透", main_media=[_photo(i) for i in range(1, 6)],
        reply_media=[_photo(21)], quoted_media=[_video(31)],
    )
    assert _text(author) == "Author"
    assert isinstance(divider, tl.PageBlockDivider) and isinstance(end_divider, tl.PageBlockDivider)
    assert _text(detail.title) == "⚠️ #剧透"
    assert isinstance(footer, tl.PageBlockFooter)
    assert _text(detail.blocks) == long * 3 + "#useful"
    assert len([node for node in _nodes(detail) if isinstance(node, tl.PageBlockDetails)]) == 1
    assert _media(detail) == [(i, True) for i in (21, 1, 2, 3, 4, 5, 31)]


def test_localized_poll_cells_keep_option_text_and_actual_counts():
    label = "  A|<b>x</b> **literal**\n🧪"
    poll = Poll([PollOption(label, 12), PollOption("B", 8), PollOption("C", 0), PollOption("D")], total_votes=30)
    result = _result(XPost((TextRun("poll"),), poll_index=0), polls=[poll])
    _, table = _wire(result, lang="en-us", settings={"hide_source": True})
    assert isinstance(table, tl.PageBlockTable)
    header, *rows = table.rows
    assert [_text(cell) for cell in header.cells] == ["Option", "Votes", "Share"]
    assert all(cell.header for cell in header.cells)
    assert [[_text(cell) for cell in row.cells] for row in rows] == [
        [label, "12", "40%"], ["B", "8", "27%"], ["C", "0", "0%"], ["D", "", ""],
    ]


def test_display_switches_preserve_main_media_and_tags_and_support_own_progress():
    result = _result(
        XPost((TextRun("hidden body"),), "Author"), reply=XPost((TextRun("hidden reply"),)),
        title="hidden title", tags=["tag"],
    )
    blocks = _wire(
        result, settings={"hide_title": True, "hide_desc": True, "hide_source": True},
        progress="<b>▎Downloading</b>", custom_content="<b>Note</b>",
        main_media=[_photo(1)], reply_media=[_photo(2)],
    )
    assert _text(blocks) == "AuthorNote#tag▎Downloading"
    assert _media(blocks) == [(1, True)]
    assert isinstance(blocks[-1], tl.PageBlockFooter)
    for name in ("Note", "▎Downloading"):
        assert any(_text(node) == name for node in _nodes(blocks) if isinstance(node, tl.TextBold))


def test_youtube_card_in_main_stays_between_post_text_and_gallery():
    result = _result(XPost((TextRun("main"),), youtube=TextRun("[video]", DEST)))
    body, card, photo = _wire(result, settings={"hide_source": True}, main_media=[_photo(101)])
    assert _text(body) == "main" and _text(card) == "[video]"
    assert [node.url for node in _nodes(card) if isinstance(node, tl.TextUrl)] == [DEST]
    assert _media(photo) == [(101, True)]


def test_media_only_reply_and_failed_media_link_stay_with_their_role():
    result = _result(XPost(()), reply=XPost((), author_name="Reply"), quoted=XPost((), author_name="Quote"))
    fallback = InputRichBlockParagraph(RichTextUrl("failed photo", DEST))
    blocks = _wire(
        result, settings={"hide_source": True}, main_media=[_photo(101)],
        reply_media=[_photo(201), fallback, _photo(202), _video(203)], quoted_media=[_photo(301)],
    )
    reply, first_gap, main, second_gap, quote = blocks
    assert _text(reply) == "Replyfailed photo" and _text(quote) == "Quote"
    assert isinstance(first_gap, tl.PageBlockDivider) and isinstance(second_gap, tl.PageBlockDivider)
    assert isinstance(reply.blocks[2], tl.PageBlockParagraph)
    assert isinstance(reply.blocks[3], tl.PageBlockCollage)
    assert _media(reply) == [(201, True), (202, True), (203, True)]
    assert _media(main) == [(101, True)] and _media(quote) == [(301, True)]
