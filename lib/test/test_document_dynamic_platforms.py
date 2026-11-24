"""Source-structured bodies alongside the public CLI projections."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_bilibili_comment import COMMENT_URL, NESTED_RPID, PARENT_RPID, _reply
from test_bilibili_opus_page import DYN_ID, _page_detail, _para
from test_bilibili_video_url import BILIVIDEO, INFO
from test_douyin_article import _article_payload
from test_youtube_post import MULTI_IMAGE_ID, POLL_ID, TRUNCATED_POST_ID, VIDEO_ID, _parse_fixture

from parsehub.parsers.parser.bilibili import BiliParse, BiliVideoParseResult
from parsehub.parsers.parser.douyin import DouyinApiResult, DouyinParser
from parsehub.parsers.parser.youtube import YtbVideoParseResult
from parsehub.provider_api.bilibili import BiliAPI, BiliDynamic, BiliImage, BiliReply
from parsehub.provider_api.youtube_video import parse_player_response, select_streams
from parsehub.types import VideoRef
from parsehub.types.document import TextRun, visible_text

FIXTURES = Path(__file__).with_name("fixtures")


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


def _bili_dynamic(monkeypatch, dynamic):
    parser = BiliParse()
    monkeypatch.setattr(parser, "get_dynamic_info", AsyncMock(return_value=dynamic))
    return asyncio.run(parser._do_parse(f"https://t.bilibili.com/{DYN_ID}"))


def test_bilibili_opus_banner_and_inline_media(monkeypatch):
    banner = "https://i0.hdslb.com/banner.jpg"
    body = "https://i0.hdslb.com/body.jpg"
    item = BiliDynamic.item_from_opus_page(_page_detail(banner=banner, paragraphs=[
        {"heading": {"nodes": [{"word": {"words": "原样 **标题**"}}]}},
        _para("字面 {{media:9}}"), _para(pics=[{"url": body}]), _para("后文"),
    ]))
    out = _bili_dynamic(monkeypatch, BiliDynamic.parse({"item": item}))
    top, heading, text, image, tail = out.document.blocks
    assert (top.kind, top.role, top.index, top.url) == ("media", "banner", 0, banner)
    assert (heading.kind, heading.runs) == ("heading", (TextRun("原样 **标题**"),))
    assert text.runs == (TextRun("字面 {{media:9}}"),)
    assert (image.kind, image.index, image.url) == ("media", 1, body)
    assert out.media[image.index].url == body
    assert tail.runs == (TextRun("后文"),)
    assert out.banner_media_count == 1 and "{{media:1}}" in out.content


def test_bilibili_forward_owns_its_media(monkeypatch):
    dynamic = BiliDynamic.parse(_fixture("bilibili_dynamic_forward.json")["data"])
    dynamic.images = [BiliImage(url="https://i0.hdslb.com/outer.jpg")]
    out = _bili_dynamic(monkeypatch, dynamic)
    quote = out.document.blocks[-1]
    assert (quote.kind, quote.role) == ("quote", "quoted")
    assert dynamic.forward.title in visible_text(quote.children)
    media = [block for block in quote.children if block.kind == "media"]
    assert media and all(b.index > 0 and out.media[b.index].url == b.url for b in media)
    assert any(run.url == dynamic.forward.video_url for b in quote.children for run in b.runs)


def test_bilibili_source_topic_without_parsing_generated_anchor(monkeypatch):
    dynamic = BiliDynamic(
        content="C# 与 #tag# <b>literal</b>", topics=[{"name": "tag", "url": "//www.bilibili.com/tag/1"}],
    )
    out = _bili_dynamic(monkeypatch, dynamic)
    assert out.document.blocks[0].runs == (
        TextRun("C# 与 "), TextRun("#tag#", "https://www.bilibili.com/tag/1"), TextRun(" <b>literal</b>"),
    )


def test_bilibili_comment_context_and_original_media_indices(monkeypatch):
    parent = BiliReply.parse(_reply(PARENT_RPID, "0", content={
        "message": "父评论", "pictures": [{"img_src": "https://i0.hdslb.com/parent.jpg"}],
    }))
    target = BiliReply.parse(_reply(NESTED_RPID, PARENT_RPID, content={
        "message": "目标评论 {{media:9}}", "pictures": [{"img_src": "https://i0.hdslb.com/target.jpg"}],
    }))
    parser = BiliParse()
    video = BiliVideoParseResult(title="视频", author_name="UP主", video=VideoRef(url="https://example.com/v.mp4"))
    monkeypatch.setattr(parser, "bili_api_parse", AsyncMock(return_value=video))
    monkeypatch.setattr(BiliAPI, "get_video_info", AsyncMock(return_value={"data": {"View": {"aid": 1}}}))
    monkeypatch.setattr(BiliAPI, "get_reply_chain", AsyncMock(return_value=[parent, target]))
    out = asyncio.run(parser._parse_with_comment(COMMENT_URL, NESTED_RPID))
    card, reply, subject, subject_image = out.document.blocks
    assert (card.kind, card.role, reply.kind, reply.role) == ("quote", "reply", "quote", "reply")
    assert any(r.url == COMMENT_URL.split("?")[0] for b in card.children for r in b.runs)
    assert card.children[-1].index == 2 and card.children[-1].url == out.media[2].url
    assert reply.children[-1].index == 0 and reply.children[-1].url == out.media[0].url
    assert subject.runs == (TextRun("目标评论 {{media:9}}"),)
    assert subject_image.index == 1 and subject_image.url == out.media[1].url
    assert out.reply_media_count == 1 and out.quote_roles == ["reply"]


def test_bilibili_video_description_is_literal(monkeypatch):
    description = "   字面 <b>**</b> {{media:9}}"
    view = {**INFO["data"]["View"], "desc": description}
    monkeypatch.setattr(BiliAPI, "get_video_info", AsyncMock(return_value={"data": {"View": view}}))
    monkeypatch.setattr(BiliAPI, "get_buvid", AsyncMock(return_value=("b3", "b4")))
    monkeypatch.setattr(BiliAPI, "get_video_playurl", AsyncMock(return_value={"data": {"durl": [{"url": BILIVIDEO}]}}))
    out = asyncio.run(BiliParse()._do_parse(COMMENT_URL.split("?")[0]))
    assert out.document.blocks[0].runs == (TextRun(description),)
    assert out.content == description.strip()


@pytest.mark.parametrize("image", [False, True])
def test_douyin_regular_source_captions_and_entities(image):
    payload = _fixture("douyin_text_extra.json")
    if image:
        payload["aweme_detail"]["images"] = [{"url_list": ["https://example.com/image.jpg"]}]
    parsed = DouyinApiResult.parse(payload)
    builder = DouyinParser._build_image_result if image else DouyinParser._build_video_result
    out = builder(parsed)
    runs = out.document.blocks[0].runs
    assert "".join(run.text for run in runs) == out.title == payload["aweme_detail"]["desc"]
    assert [run.text for run in runs if run.url] == ["#热点", "#日本", "#驻日美军", "#国际"]


def test_douyin_markdown_source_keeps_repeated_image_slots_and_origin():
    body = "https://example.com/tos-cn-i-test/a123~thumb.heic"
    origin = "https://example.org/tos-cn-i-test/a123~origin.heic"
    payload = _article_payload(markdown=f"## **标题**\n\n{{{{media:9}}}}\n\n![图]({body})\n\n> 一段\n\n![图]({body})")
    payload["aweme_detail"]["article_info"]["fe_data"] = json.dumps({"image_list": [{"origin_image_url": origin}]})
    out = DouyinParser._build_article_result(DouyinApiResult.parse(payload))
    heading, literal, first, quote, second = out.document.blocks
    assert (heading.kind, heading.level, heading.runs) == ("heading", 2, (TextRun("标题", styles=("bold",)),))
    assert literal.runs == (TextRun("{{media:9}}"),)
    assert first == second and (first.index, first.url) == (0, origin)
    assert quote.kind == "quote" and visible_text(quote.children) == "一段"
    assert out.media[0].url == origin and len(out.media) == 1 and out.requires_media_download
    assert out.markdown_content.count("{{media:0}}") == 2


def test_douyin_article_abstract_is_literal():
    payload = _article_payload(markdown="", abstract="**摘要** {{poll:1}}")
    result = DouyinParser._build_article_result(DouyinApiResult.parse(payload))
    assert result.document.blocks[0].runs == (TextRun("**摘要** {{poll:1}}"),)


def test_douyin_reference_picture_has_origin_url_without_inventing_legacy_slot():
    body = "https://example.com/tos-cn-i-test/a123~thumb.heic"
    origin = "https://example.org/tos-cn-i-test/a123~origin.heic"
    payload = _article_payload(markdown=f"![图][pic]\n\n[pic]: {body}")
    payload["aweme_detail"]["article_info"]["fe_data"] = json.dumps({"image_list": [{"origin_image_url": origin}]})
    parsed = DouyinApiResult.parse(payload)
    out = DouyinParser._build_article_result(parsed)
    image, = out.document.blocks
    assert (image.kind, image.index, image.url) == ("media", -1, origin)
    assert out.markdown_content == parsed.article_markdown and not out.media


def test_youtube_native_link_runs():
    out = _parse_fixture("youtube_post_truncated_links.html", TRUNCATED_POST_ID)
    link = TextRun("https://www.youtube.com/playlist?list...", "https://www.youtube.com/playlist?list=PLcsS6p8iu5r4")
    assert link in out.document.blocks[0].runs
    assert '<a href="https://www.youtube.com/playlist?list=PLcsS6p8iu5r4">' in out.content


def test_youtube_poll_and_attachments_keep_source_slots():
    poll_post = _parse_fixture("youtube_post_poll.html", POLL_ID)
    assert (poll_post.document.blocks[-1].kind, poll_post.document.blocks[-1].index) == ("poll", 0)
    assert poll_post.polls[0].total_votes == 1_800_000
    video_post = _parse_fixture("youtube_post_video.html", VIDEO_ID)
    *_, title, cover = video_post.document.blocks
    assert title.runs[0].url == "https://www.youtube.com/watch?v=iAwRpKe61PA"
    assert (cover.kind, cover.role, cover.index, cover.url) == ("media", "attached_video", 0, video_post.media[0].url)
    image_post = _parse_fixture("youtube_post_multi_image.html", MULTI_IMAGE_ID)
    assert [(b.index, b.url) for b in image_post.document.blocks if b.kind == "media"] == [
        (i, ref.url) for i, ref in enumerate(image_post.media)
    ]


def test_youtube_video_description_is_literal():
    payload = _fixture("youtube_player_visionos.json")
    description = "    code?\n[a](https://example.com) <b>x</b> {{poll:0}}"
    payload["videoDetails"]["shortDescription"] = description
    video = parse_player_response(payload)
    out = YtbVideoParseResult(info=video, selected=select_streams(video))
    assert out.document.blocks[0].runs == (TextRun(description),)
    assert out.content == description.strip()
