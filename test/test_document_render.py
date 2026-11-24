"""Shared source documents survive real Kurigram serialization without markup parsing."""
import asyncio
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace

import pytest
from parsehub.types import Platform
from parsehub.types.document import Block, Document, TextRun, html_blocks, paragraph
from parsehub.types.result import Poll, PollOption
from pyrogram.file_id import FileId, FileType, ThumbnailSource
from pyrogram.raw import types as tl
from pyrogram.raw.core import TLObject
from pyrogram.types import InputMediaPhoto, InputRichBlockParagraph, InputRichBlockPhoto, InputRichMessage, RichTextUrl

from plugins.parse import document_render, rich_blocks
from repo.settings.schema import DEFAULT_SETTINGS_CONFIG

URL = "https://weibo.com/u/1?x=1&y=2"
IMAGE = "https://example.org/"


def _render(*body, document=None, settings=None, platform=Platform.WEIBO, fields=None, **options):
    result = SimpleNamespace(
        document=document if document is not None else Document(body), raw_url=URL, platform=platform,
        content="DO NOT PARSE THE LEGACY BODY", **(fields or {}),
    )
    config = DEFAULT_SETTINGS_CONFIG.model_copy(update={"hide_source": True, **(settings or {})})
    message = InputRichMessage(blocks=document_render.build_document_blocks(result, config=config, **options))

    async def encode():
        return await message.write(client=None)

    stream = BytesIO(asyncio.run(encode()).write())
    raw = TLObject.read(stream)
    assert isinstance(raw, tl.InputRichMessage) and stream.read() == b""
    return raw.blocks


def _walk(value):
    if isinstance(value, list):
        for child in value:
            yield from _walk(child)
    elif isinstance(value, TLObject):
        yield value
        for field in value.__slots__:
            yield from _walk(getattr(value, field, None))


def _of(value, kind):
    return [node for node in _walk(value) if isinstance(node, kind)]


def _text(value):
    return "".join(node.text for node in _of(value, tl.TextPlain))


def _photos(value):
    return [node.photo_id for node in _of(value, tl.PageBlockPhoto)]


def _slot(index, photo_id, url=None):
    file_id = FileId(
        file_type=FileType.PHOTO, dc_id=2, media_id=photo_id, access_hash=42, volume_id=0, local_id=0,
        thumbnail_source=ThumbnailSource.THUMBNAIL, thumbnail_file_type=FileType.PHOTO, thumbnail_size="x",
    ).encode()
    return index, url or f"{IMAGE}{index}", InputRichBlockPhoto(InputMediaPhoto(file_id))


def _media(index, url=None, **kwargs):
    return Block("media", index=index, url=url or f"{IMAGE}{index}", **kwargs)


def test_source_markup_is_literal_but_source_entities_are_native(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("platform content entered a markup parser")

    monkeypatch.setattr(rich_blocks, "parse_inline", forbidden)
    monkeypatch.setattr(rich_blocks, "markdown_to_blocks", forbidden)
    literal = "    # | ** {{media:0}} {{poll:0}} <b>&amp;</b>\n> [x](y)"
    doc = Document(
        (paragraph(literal), *html_blocks("""
          <h2><strong>Section</strong></h2><ol start="3"><li>item<ul><li>nested</li></ul></li></ol>
          <table><tr><th><u>name</u></th></tr><tr><td><sub>value</sub></td></tr></table>
          <pre><code class="language-python"> print('&lt;')\n</code></pre>
        """), paragraph((TextRun("link", IMAGE, ("bold", "italic", "strike", "spoiler")),))),
        origin=(TextRun("Area", IMAGE, ("bold",)),),
        topic=(TextRun("Topic", IMAGE + "topic"),),
    )
    blocks = _render(document=doc, fields={
        "title": "<i>Title</i>", "author_name": "Name", "author_handle": "handle",
        "author_url": IMAGE + "profile", "position_label": "#7",
    })
    title, origin, author, separator, raw, heading, numbered, table, code, linked = blocks
    assert [_text(x) for x in (title, origin, author, raw)] == [
        "<i>Title</i>", "Area", "Name @handle · #7 > Topic", literal,
    ]
    assert [n.url for n in _of(author, tl.TextUrl)] == [IMAGE + "profile", IMAGE + "topic"]
    assert _text(_of(author, tl.TextFixed)) == "@handle"
    assert isinstance(separator, tl.PageBlockDivider)
    assert isinstance(heading, tl.PageBlockHeading2) and _text(_of(heading, tl.TextBold)) == "Section"
    assert isinstance(numbered, tl.PageBlockOrderedList) and numbered.items[0].value == 3
    assert isinstance(numbered.items[0].blocks[1], tl.PageBlockList)
    assert _text(numbered.items[0].blocks[1]) == "nested"
    assert isinstance(table, tl.PageBlockTable) and table.rows[0].cells[0].header
    assert _text(_of(table, tl.TextUnderline)) == "name"
    assert _text(_of(table, tl.TextSubscript)) == "value"
    assert code.language == "python" and _text(code) == " print('<')\n"
    assert _of(linked, tl.TextUrl)[0].url == IMAGE
    assert _text(_of(linked, tl.TextSpoiler)) == "link"


def test_media_index_requires_consistent_url_and_repeats_remove_only_one_tail_slot():
    broken = InputRichBlockParagraph(RichTextUrl("broken link", IMAGE + "broken"))
    slots = [_slot(0, 10), _slot(2, 12), (None, IMAGE + "broken", broken), _slot(3, 13)]
    body = (paragraph("first"), _media(0, IMAGE + "2"), _media(2), paragraph("middle"),
            _media(90, IMAGE + "broken"), _media(91), paragraph("last"))
    blocks = _render(*body, main_media=slots)
    assert [_text(x) for x in blocks[:6]] == ["first", "", "", "middle", "broken link", "last"]
    assert _photos(blocks) == [12, 12, 10, 13]
    assert [_text(x) for x in _render(*body)] == ["first", "middle", "last"]


def test_multi_output_ref_stays_at_its_source_node_and_exact_index_wins_over_shared_url():
    shared_url = IMAGE + "shared"
    slots = [_slot(0, 10, shared_url), _slot(1, 11, shared_url), _slot(1, 12, shared_url)]
    blocks = _render(paragraph("first"), _media(1, shared_url), paragraph("last"), main_media=slots)
    assert [_text(blocks[0]), _text(blocks[-2])] == ["first", "last"]
    assert _photos(blocks[1:-2]) == [11, 12] and _photos(blocks[-1]) == [10]


def test_explicit_attachment_nodes_fold_after_four_but_ptt_inline_images_stay_in_place():
    body = (paragraph("body"), *(_media(i) for i in range(6)))
    slots = [_slot(i, i + 10) for i in range(6)]
    _, preview, folded = _render(*body, main_media=slots)
    assert isinstance(preview, tl.PageBlockCollage) and _photos(preview) == [10, 11, 12, 13]
    assert isinstance(folded, tl.PageBlockDetails) and _photos(folded) == [14, 15]
    ptt = _render(*body, platform=Platform.PTT, main_media=slots)
    assert all(isinstance(b, tl.PageBlockPhoto) for b in ptt[1:]) and _photos(ptt) == list(range(10, 16))


def test_youtube_gallery_remains_when_description_and_linked_video_card_are_hidden():
    body = (paragraph("body"), *(_media(i) for i in range(5)), paragraph("watch"), _media(5, role="attached_video"))
    slots = [_slot(i, i + 10) for i in range(6)]
    preview, folded = _render(*body, platform=Platform.YOUTUBE, main_media=slots, settings={"hide_desc": True})
    assert _photos(preview) == [10, 11, 12, 13] and _photos(folded) == [14]


def test_card_roles_and_media_survive_independent_text_folding():
    reply = Block("quote", runs=(TextRun("sign"),), children=(paragraph("R" * 510), _media(7)), role="reply")
    quoted = Block("quote", children=(paragraph("quoted"),), role="quoted")
    trending = Block("quote", children=(paragraph("trend"), _media(1), paragraph("subtitle")), role="trending")
    source_quote = Block("quote", children=(paragraph("source quote"),))
    blocks = _render(
        quoted, paragraph("body"), source_quote, reply, trending,
        main_media=[_slot(1, 11), _slot(2, 12)], reply_media=[_slot(7, 17)], quoted_media=[_slot(8, 18)],
    )
    front, body, original, _, trend, gap_before_media, media, gap_before_quote, back = blocks
    assert _text(body) == "body" and _text(original) == "source quote"
    assert isinstance(front.blocks[1], tl.PageBlockDivider)
    detail = front.blocks[3]
    assert isinstance(detail, tl.PageBlockDetails) and not _photos(detail)
    assert _text(front.blocks[2]) + _text(detail.blocks) == "R" * 510
    assert _text(_of(front.blocks[2], tl.TextItalic)) == "R" * 300
    assert _photos(front.blocks[-1]) == [17]
    assert [_text(b) for b in trend.blocks] == ["trend", "", "subtitle"] and _photos(trend) == [11]
    assert isinstance(gap_before_media, tl.PageBlockDivider) and isinstance(gap_before_quote, tl.PageBlockDivider)
    assert _photos(media) == [12] and _photos(back) == [18] and _text(back) == "quoted"


def test_explicit_nested_reply_media_may_be_in_bilibili_main_bucket():
    video = Block("quote", role="reply", children=(paragraph("video"), _media(3)))
    parent = Block("quote", role="reply", children=(paragraph("parent"), _media(0)))
    slots = {"main_media": [_slot(0, 10), _slot(1, 11)], "reply_media": [_slot(3, 13)]}
    blocks = _render(video, parent, paragraph("main"), _media(1), platform=Platform.BILIBILI, **slots)
    assert [(_text(card), _photos(card)) for card in _of(blocks, tl.PageBlockBlockquoteBlocks)] == [
        ("video", [13]), ("parent", [10]),
    ]
    assert _photos(blocks) == [13, 10, 11]
    hidden = _render(video, parent, paragraph("main"), _media(1), platform=Platform.BILIBILI,
                     settings={"hide_desc": True}, **slots)
    assert _photos(hidden) == [11]


def test_banner_goes_above_metadata_and_is_not_repeated_in_main_gallery():
    blocks = _render(_media(0, role="banner"), paragraph("body"), _media(1), platform=Platform.BILIBILI,
                     main_media=[_slot(0, 10), _slot(1, 11)], fields={"title": "heading"})
    assert isinstance(blocks[1], tl.PageBlockDivider) and isinstance(blocks[2], tl.PageBlockHeading1)
    assert _photos(blocks) == [10, 11] and _photos(blocks[0]) == [10]


def test_native_poll_in_a_card_keeps_role_and_literal_option_text():
    poll = Poll(options=[PollOption(text="**yes**", count=2), PollOption(text="<b>no</b>", count=1)], total_votes=3)
    reply = Block("quote", role="reply", children=(Block("poll", index=0),))
    blocks = _render(reply, paragraph("body"), fields={"polls": [poll]}, lang="zh-hans")
    (table,) = _of(blocks[0], tl.PageBlockTable)
    assert _text(table.rows[0]) == "选项票数占比"
    assert [_text(row) for row in table.rows[1:]] == ["**yes**267%", "<b>no</b>133%"]


def test_body_fold_keeps_later_details_sibling_and_structures_intact():
    later = Block("details", runs=(TextRun("comments"),), children=(paragraph("c" * 7000),), independent=True)
    body_list = Block("list", items=((paragraph("one"),), (paragraph("two"),)))
    linked = TextRun("linked" * 30, IMAGE + "long")
    blocks = _render(paragraph((TextRun("A" * 250), linked, TextRun("B" * 210))), body_list, later)
    preview, folded, comments = blocks
    assert _text(preview) == "A" * 250
    assert isinstance(folded, tl.PageBlockDetails) and isinstance(comments, tl.PageBlockDetails)
    assert _text(_of(folded, tl.TextUrl)) == linked.text
    assert isinstance(folded.blocks[-1], tl.PageBlockList)
    assert _text(comments.title) == "comments" and len(_text(comments.blocks)) == 7000


def test_6800_visible_budget_does_not_count_closed_details_or_hide_footer():
    giant_title = "T" * 6801
    independent = Block("details", runs=(TextRun("later"),), children=(paragraph("X" * 7000),), independent=True)
    folded, later, divider, footer = _render(
        paragraph("body"), independent, fields={"title": giant_title}, settings={"hide_source": False},
    )
    assert isinstance(folded, tl.PageBlockDetails) and giant_title in _text(folded.blocks)
    assert _text(later.title) == "later" and len(_text(later.blocks)) == 7000
    assert isinstance(divider, tl.PageBlockDivider) and isinstance(footer, tl.PageBlockFooter)
    assert len(_render(independent)) == 1


def test_footer_uses_native_time_and_platform_labels_and_source_override():
    published = datetime(2026, 2, 3, 4, 5, tzinfo=UTC)
    (footer,) = _render(
        settings={"hide_source": False}, lang="zh-hans", raw_url=URL + "&z=3", progress="<b>▎Progress</b>",
        fields={"published_at": published, "view_count": 1000, "reply_count": 0},
    )
    assert isinstance(footer, tl.PageBlockFooter)
    assert _text(_of(footer, tl.TextBold)) == "▎Progress"
    (sent_time,) = _of(footer, tl.TextDate)
    assert sent_time.date == int(published.timestamp()) and sent_time.long_date and sent_time.short_time
    assert "1,000 查看 · 0 评论" in _text(footer)
    (source,) = _of(footer, tl.TextUrl)
    assert source.url == URL + "&z=3" and _text(source).startswith("来源（")
    (bgm_footer,) = _render(platform=Platform.BANGUMI, lang="zh-hans", fields={"like_count": 9})
    assert _text(bgm_footer) == "9 状态"


def test_manual_hide_disables_nested_auto_folds_but_keeps_full_content():
    reply = Block("quote", children=(paragraph("reply " * 115),), role="reply")
    blocks = _render(
        _media(0, role="banner"), reply, paragraph("body " * 150), *(_media(i) for i in range(1, 6)),
        fields={"title": "title", "tags": ["tag"]}, main_media=[_slot(i, i + 10) for i in range(6)],
        hide_content="#nsfw", custom_content="**app note**",
    )
    (details,) = _of(blocks, tl.PageBlockDetails)
    assert _text(details.title) == "⚠️ #nsfw" and _text(blocks[0]) == "title"
    assert _photos(blocks) == _photos(details) == list(range(10, 16))
    assert _text(_of(details, tl.TextBold)) == "app note"
    assert _text(details.blocks[-1]) == "#tag"


def test_hide_desc_drops_role_content_not_main_attachments_or_custom_note():
    reply = Block("quote", children=(paragraph("reply"), _media(7)), role="reply")
    blocks = _render(
        reply, paragraph("hide this"), _media(1), paragraph("hide this too"), _media(2),
        settings={"hide_title": True, "hide_desc": True}, fields={"title": "hide title", "tags": ["tag"]},
        main_media=[_slot(1, 11), _slot(2, 12)], reply_media=[_slot(7, 17)], custom_content="**app note**",
    )
    assert _text(blocks) == "app note#tag" and _photos(blocks) == [12]
