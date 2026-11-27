"""Render documents are built from platform source, not compatibility Markdown."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_linuxdo import POLL_COOKIE_PLACEHOLDER, POLL_STRUCTURED, make_payload
from test_xiaoheihe_article_media import BODY_HTML, IMG_NODES, SHARE_URL

from parsehub.provider_api.bangumi import BangumiBlog, BangumiIndex, BangumiTopic
from parsehub.provider_api.douban import DoubanTopic
from parsehub.provider_api.linuxdo import LinuxDoTopic
from parsehub.provider_api.weixin import WX
from parsehub.provider_api.xiaoheihe import XiaoHeiHeAPI
from parsehub.provider_api.zhihu import ZhihuPin, ZhihuQA, ZhihuZhuanLan
from parsehub.types.document import visible_text

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_douban_keeps_literal_punctuation_with_dom_styles():
    topic = DoubanTopic.parse({"content": "<p>||literal|| <strong>a_b</strong></p>"})
    assert topic.document is not None
    assert [(run.text, run.styles) for run in topic.document.blocks[0].runs] == [
        ("||literal|| ", ()), ("a_b", ("bold",)),
    ]


def test_linuxdo_poll_is_a_block_at_the_source_position():
    payload = make_payload()
    payload["post_stream"]["posts"][0].update(
        cooked=POLL_COOKIE_PLACEHOLDER + "<p>after</p>", polls=POLL_STRUCTURED,
    )
    topic = LinuxDoTopic._from_payload(payload, "2985951")
    assert topic.document is not None
    before, poll, after = topic.document.blocks
    assert before.kind == "paragraph"
    assert (poll.kind, poll.index) == ("poll", 0)
    assert after.runs[0].text == "after"
    assert topic.polls[0].options[0].count == POLL_STRUCTURED[0]["options"][0]["votes"]


def test_linuxdo_real_floor_keeps_op_and_replied_floor_order():
    payload = json.loads(_read("linuxdo_floor_4.json"))
    topic = LinuxDoTopic._from_payload(payload, "2989140", "3")
    assert topic.document is not None
    op, parent, *body = topic.document.blocks
    assert (op.kind, op.role, parent.kind, parent.role) == ("quote", "reply", "quote", "reply")
    assert op.runs[-1].text.endswith("#1")
    assert parent.runs[-1].text.endswith("#2")
    assert body


def test_linuxdo_context_uses_absolute_media_and_poll_slots():
    payload = make_payload()
    posts = payload["post_stream"]["posts"]
    posts[0].update(cooked='<img src="/uploads/op.jpg">' + POLL_COOKIE_PLACEHOLDER, polls=POLL_STRUCTURED)
    posts.append({
        "post_number": 2, "reply_to_post_number": 1, "username": "reply",
        "cooked": '<img src="/uploads/current.jpg">' + POLL_COOKIE_PLACEHOLDER, "polls": POLL_STRUCTURED,
    })
    topic = LinuxDoTopic._from_payload(payload, "2985951", "2")
    assert topic.document is not None
    context, image, _, poll = topic.document.blocks
    assert context.role == "reply"
    assert [(b.kind, b.index) for b in context.children if b.kind in {"media", "poll"}] == [("media", 1), ("poll", 0)]
    assert (image.kind, image.index, image.url, poll.index) == ("media", 0, topic.images[0].url, 1)


def test_bangumi_real_blog_places_attachment_inside_body():
    blog = BangumiBlog._from_html(_read("bangumi_blog_381120.html"), "381120")
    assert blog.document is not None
    blocks = blog.document.blocks
    index = next(n for n, block in enumerate(blocks) if block.kind == "media")
    assert "这集有点怪" in visible_text(blocks[:index])
    assert (blocks[index].index, blocks[index].url) == (0, blog.images[0].url)
    assert "{{media:0}}" in blog.markdown_content


def test_bangumi_real_index_serializes_rank_and_link_as_runs():
    from parsehub.parsers.parser.bangumi import BangumiParser

    index = BangumiIndex._from_html(_read("bangumi_index_77872.html"), "77872")
    with patch.object(BangumiIndex, "parse", new=AsyncMock(return_value=index)):
        result = asyncio.run(BangumiParser()._do_parse("https://bgm.tv/index/77872"))
    assert result.document is not None
    first = next(block for block in result.document.blocks if block.role == "index_entry")
    assert (first.runs[0].text, first.runs[0].styles) == ("2.", ("bold",))
    assert first.runs[2].url == index.entries[0].url


def test_bangumi_real_topic_keeps_origin_out_of_body_and_signs_reply():
    topic = BangumiTopic._from_html(_read("bangumi_group_topic_472394.html"), "472394")
    picked = next(floor for floor in topic.floors if floor.label == "#2")
    reply = BangumiTopic._from_html(_read("bangumi_group_topic_472394.html"), "472394", floor_id=picked.dom_id)
    assert reply.document is not None
    assert reply.document.origin[0].url == reply.context_url
    assert reply.document.origin[-1].text == "讨论"
    assert reply.document.blocks[0].role == "reply"
    assert "#1" in "".join(run.text for run in reply.document.blocks[0].runs)


def test_douban_animated_attachment_keeps_absolute_slot_after_video():
    url = "https://img.example/animated.gif"
    topic = DoubanTopic.parse({
        "content": f'<p>before</p><img src="{url}"><p>after</p><img src="https://elsewhere.example/no.jpg">',
        "image_layout": "vertical",
        "video_info": {"video_url": "https://img.example/video.mp4"},
        "photos": [{"image": {"large": {"url": url}, "is_animated": True,
                              "video": {"url": "https://img.example/animated.mp4"}}}],
    })
    assert topic.document is not None
    assert [b.kind for b in topic.document.blocks] == ["paragraph", "media", "paragraph"]
    assert topic.document.blocks[1].index == 1
    assert topic.photos[0].is_animated
    assert topic.document.blocks[1].url == topic.photos[0].url


def test_coolapk_article_uses_only_selected_message_images(monkeypatch):
    from parsehub.parsers.parser.coolapk import CoolapkParser, CoolapkRichTextParseResult
    from parsehub.provider_api import coolapk

    html = (
        '<h1 class="message-title">Topic</h1><div class="feed-article-message">'
        '<p><a href="/feed/2">Next</a></p><img src="//img.example/icon.png">'
        '<img class="message-image" src="//img.example/body.gif"><p><b>After</b></p></div>'
    )
    monkeypatch.setattr(coolapk.http.AsyncClient, "get", AsyncMock(return_value=SimpleNamespace(text=html)))
    result = asyncio.run(CoolapkParser()._do_parse("https://www.coolapk.com/feed/1"))
    assert isinstance(result, CoolapkRichTextParseResult)
    assert not result.requires_media_download
    assert result.document is not None
    before, image, after = result.document.blocks
    assert before.runs[0].url == "https://www.coolapk.com/feed/2"
    assert (image.index, image.url) == (0, result.media[0].url)
    assert after.runs[0].styles == ("bold",)


def test_weixin_lazy_image_does_not_promote_external_src():
    source = ('<p>before</p><img src="https://img.example/not-selected.png">'
              '<img src="loading.png" data-src="//mmbiz.qpic.cn/actual.png?wx_fmt=png&amp;x=1"><p>after</p>')
    html = '<script>var cgiDataNew = {item_show_type: "0" * 1, content_noencode: ' + json.dumps(source) + '};</script>'
    article = WX._parse_data(html)
    assert article.document is not None
    before, image, after = article.document.blocks
    assert [before.runs[0].text, after.runs[0].text] == ["before", "after"]
    assert article.imgs == ["https://mmbiz.qpic.cn/actual.png?wx_fmt=png&x=1"]
    assert (image.index, image.url) == (0, article.imgs[0])


def test_weixin_photo_post_keeps_gallery_order_and_literal_caption():
    payload = {
        "item_show_type": 8, "content_noencode": "||literal||<br>a_b",
        "picture_page_info_list": [
            {"cdn_url": "https://img.example/placeholder", "watermark_info": {"cdn_url": "https://img.example/first"}},
            {"cdn_url": "https://img.example/second"},
        ],
    }
    photo = WX._parse_data(f"<script>var cgiDataNew = {json.dumps(payload)};</script>")
    assert photo.photos == ["https://img.example/first", "https://img.example/second"]
    assert photo.document is not None
    assert visible_text(photo.document.blocks) == "||literal||\na_b"


def test_xiaoheihe_article_aligns_body_urls_in_source_order():
    payload = json.loads(_read("xiaoheihe_link_tree.json"))["result"]
    # Reuse the observed article HTML/CDN pair; the captured full payload is a short post.
    payload["link"].update(text=json.dumps([{"type": "html", "text": BODY_HTML}, *IMG_NODES]), use_concept_type=False)
    with patch.object(XiaoHeiHeAPI, "link_tree", new=AsyncMock(return_value=payload)):
        post = asyncio.run(XiaoHeiHeAPI().parse(SHARE_URL))
    assert post.document is not None
    assert [b.kind for b in post.document.blocks] == ["paragraph", "media", "paragraph", "media"]
    assert [(b.index, b.url) for b in post.document.blocks if b.kind == "media"] == [
        (i, media.url) for i, media in enumerate(post.media)
    ]
    assert all("imgheybox.max-c.com" in m.url for m in post.media)


def test_zhihu_answer_and_column_keep_image_position_but_not_equations():
    body = ('<p><a href="/people/a"><strong>a_b</strong></a></p>'
            '<img src="data:image/svg+xml,example" alt="x^2"><img src="/equation?x=1">'
            '<figure><img src="//pic.example/body.jpg"><figcaption>Caption</figcaption></figure>')
    answer = ZhihuQA.parse({"question": {"title": "Question"}, "content": body})
    column = ZhihuZhuanLan.parse({"title": "Column", "content": body})
    for article in (answer, column):
        assert article.document is not None
        link, equation, picture, caption = article.document.blocks
        assert link.runs[0].styles == ("bold",)
        assert link.runs[0].url.endswith("/people/a")
        assert equation.runs[0].text == "x^2"
        assert (picture.index, picture.url) == (0, article.imgs[0])
        assert caption.runs[0].text == "Caption"


def test_zhihu_pin_does_not_select_inline_html_to_download():
    pin = ZhihuPin.parse({"content": [{"type": "text", "title": "Pin", "content": (
        '<p><em>||word||</em></p><img src="https://outside.example/unselected.jpg">'
    )}]})
    assert pin.document is not None
    assert not pin.media
    assert [b.kind for b in pin.document.blocks] == ["paragraph"]
    assert pin.document.blocks[0].runs[0].text == "||word||"
