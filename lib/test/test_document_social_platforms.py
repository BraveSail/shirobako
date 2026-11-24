"""Source fixtures and edge cases for social platform Documents."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from parsehub.parsers.parser.ptt import PttParser
from parsehub.parsers.parser.threads import ThreadsParser
from parsehub.parsers.parser.twitter import TwitterParser
from parsehub.parsers.parser.weibo import WeiboParser
from parsehub.provider_api.ptt import PttAPI
from parsehub.provider_api.threads import ThreadsPost
from parsehub.provider_api.twitter import TrendingCard, Twitter
from parsehub.provider_api.weibo import WeiboAPI, WeiboContent, WeiboTVContent
from parsehub.types.document import TextRun, visible_text

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def returning(value):
    async def receive(*args):
        return value
    return receive


def walk(blocks):
    for block in blocks:
        yield block
        yield from walk(block.children)
        for item in block.items:
            yield from walk(item)


def test_x_note_and_polls_use_original_runs_and_role_indexes():
    tweet = Twitter().parse(load("twitter_richtext_emoji.json"))
    poll = load("twitter_poll_card.json")
    tweet.reply_to = Twitter().parse(poll)
    tweet.quoted_status = Twitter().parse(poll)
    result = asyncio.run(TwitterParser.media_parse(tweet))
    assert result.document is not None
    reply, body, *_, quote = result.document.blocks
    assert (reply.role, quote.role) == ("reply", "quoted")
    assert body.runs == tweet.segments
    assert any("2027年1月7日(木)" in r.text and "bold" in r.styles for r in body.runs)
    assert [b.index for b in walk(result.document.blocks) if b.kind == "poll"] == [0, 1]
    assert "**2027年1月7日(木)**" in result.content


def test_x_document_reuses_status_translation_without_losing_media():
    from parsehub.types.x_document import replace_x_document_text

    tweet = Twitter().parse(load("twitter_richtext_emoji.json"))
    result = asyncio.run(TwitterParser.media_parse(tweet))
    status = result.x_status
    translated = replace(status, main=replace(status.main, segments=(TextRun("**literal**"),)))
    updated = replace_x_document_text(result.document, translated)
    assert updated.blocks[0].runs == (TextRun("**literal**"),)
    assert updated.blocks[1:] == result.document.blocks[1:]


def x_node(text, **extra):
    return {"rest_id": "1", "legacy": {"full_text": text, "entities": {}}, **extra}


def source_x(node):
    return Twitter().parse({"data": {"tweetResult": {"result": node}}})


def test_x_trending_cover_media_only_quote_and_youtube_link(monkeypatch):
    from parsehub.provider_api import youtube

    photo = "https://pbs.twimg.com/media/a.jpg"
    cover = "https://pbs.twimg.com/media/topic.jpg"
    card_url = "https://youtu.be/aaaaaaaaaaA"
    quote = x_node("https://t.co/abc")
    quote["legacy"]["entities"]["media"] = [{
        "type": "photo", "url": "https://t.co/abc", "media_url_https": photo, "indices": [0, 16],
    }]
    tweet = source_x(x_node("**literal** {{poll:0}}", quoted_status_result={"result": quote}))
    tweet.media = tweet.quoted_status.media
    tweet.reply_to = source_x(x_node(card_url))
    tweet.trending = TrendingCard(
        title="**topic**", subtitle="<summary>", url="https://x.com/i/trending/123", cover=cover,
    )
    monkeypatch.setattr(youtube, "fetch_card", returning(SimpleNamespace(title="T <3", url=card_url)))
    result = asyncio.run(TwitterParser.media_parse(tweet))
    assert result.document is not None
    reply, text, trend, media, quoted = result.document.blocks
    assert reply.role == "reply"
    assert TextRun("T <3", card_url, ("italic",)) in tuple(r for b in reply.children for r in b.runs)
    assert text.runs == (TextRun("**literal** {{poll:0}}"),)
    assert (media.kind, media.index, media.url) == ("media", 0, result.media[0].url)
    trend_media = next(b for b in trend.children if b.kind == "media")
    assert (trend_media.index, trend_media.url) == (1, cover)
    assert quoted.role == "quoted"
    assert [(b.index, b.url) for b in quoted.children if b.kind == "media"] == [(2, result.media[2].url)]


def article_node():
    image = "https://pbs.twimg.com/media/article.jpg"
    return x_node("preview", article={"article_results": {"result": {
        "title": "Article",
        "cover_media": {"media_info": {"original_img_url": "https://pbs.twimg.com/media/cover.jpg"}},
        "media_entities": [{"media_id": "42", "media_info": {"__typename": "ApiImage", "original_img_url": image}}],
        "content_state": {
            "blocks": [
                {"key": "1", "type": "header-two", "text": "🧪 **字**",
                 "inlineStyleRanges": [{"offset": 3, "length": 5, "style": "Bold"}],
                 "entityRanges": [{"offset": 5, "length": 1, "key": 0}]},
                {"key": "2", "type": "atomic", "text": " ", "entityRanges": [{"offset": 0, "length": 1, "key": 1}]},
                {"key": "3", "type": "ordered-list-item", "depth": 0, "text": "一"},
                {"key": "4", "type": "ordered-list-item", "depth": 0, "text": "二"},
            ],
            "entityMap": [
                {"key": "0", "value": {"type": "LINK", "data": {"url": "https://example.org/"}}},
                {"key": "1", "value": {"type": "MEDIA", "data": {"mediaItems": [{"mediaId": "42"}]}}},
            ],
        },
    }}})


@pytest.mark.parametrize("related", [False, True])
def test_x_article_draftjs_heading_ranges_and_media(related):
    node = article_node()
    if related:
        node = x_node("main", quoted_status_result={"result": node})
    result = asyncio.run(TwitterParser.media_parse(source_x(node)))
    assert result.document is not None
    blocks = result.document.blocks
    if related:
        assert blocks[-1].role == "quoted"
        blocks = blocks[-1].children
    cover, heading, image, items = list(blocks)[-4:]
    assert (heading.kind, heading.level) == ("heading", 2)
    assert heading.runs == (TextRun("🧪 "), TextRun("**", styles=("bold",)),
                            TextRun("字", "https://example.org/", ("bold",)), TextRun("**", styles=("bold",)))
    assert cover.index == 0 and cover.url == result.media[0].url
    assert image.index == 1 and image.url == result.media[1].url
    assert items.ordered and [visible_text(item) for item in items.items] == ["一", "二"]


def test_threads_real_poll_and_exact_spoiler_fragment_locations(monkeypatch):
    node = load("threads_poll.json")["data"]["data"]["edges"][0]["node"]["thread_items"][0]["post"]
    post = ThreadsPost.from_graphql(node)
    literal = "||literal|| secret / secret"
    source = {"caption": {"text": literal}, "media_type": 1,
              "image_versions2": {"candidates": [{"url": "https://example.org/photo.jpg"}]},
              "text_post_app_info": {"text_fragments": {"fragments": [
                  {"plaintext": "||literal|| secret / "}, {"plaintext": "secret", "styling_info": {"is_spoiler": True}},
              ]}}}
    post.reply_to = ThreadsPost.from_graphql(source)
    post.reply_to.poll = post.poll
    post.topic = "A/B & C"
    monkeypatch.setattr(ThreadsParser, "_parse", returning(post))
    result = asyncio.run(ThreadsParser()._do_parse(""))
    assert result.document is not None
    reply, *rest = result.document.blocks
    assert reply.role == "reply"
    assert next(b for b in reply.children if b.kind == "paragraph").runs == (
        TextRun("||literal|| secret / "), TextRun("secret", styles=("spoiler",)),
    )
    assert [b.index for b in walk(result.document.blocks) if b.kind == "poll"] == [0, 1]
    assert [(b.index, b.url) for b in reply.children if b.kind == "media"] == [
        (len(result.media) - 1, "https://example.org/photo.jpg"),
    ]
    assert result.document.topic == (TextRun("A/B & C", "https://www.threads.com/search?q=A%2FB%20%26%20C&serp_type=tags"),)
    assert "{{poll:1}}" in result.content


def test_real_threads_snippet_is_a_separate_quoted_source_block(monkeypatch):
    items = load("threads_snippet_post.json")["data"]["data"]["edges"][0]["node"]["thread_items"]
    node = next(item["post"] for item in items if item["post"]["code"] == "DeTKrdrGjrZ")
    monkeypatch.setattr(ThreadsParser, "_parse", returning(ThreadsPost.from_graphql(node)))
    result = asyncio.run(ThreadsParser()._do_parse(""))
    raw = node["text_post_app_info"]["snippet_attachment_info"]["text_fragments"]["fragments"][0]["plaintext"]
    assert result.document is not None
    quote = next(block for block in result.document.blocks if block.role == "quoted")
    assert quote.kind == "quote" and quote.children[0].runs == (TextRun(raw),)
    assert result.document.blocks[0].runs == (TextRun(node["caption"]["text"]),)


@pytest.mark.parametrize("name", [
    "weibo_annotations_vs_topic_struct.json", "weibo_video_post.json", "weibo_tv_post.json",
])
def test_weibo_uses_cleaned_source_value_and_known_topic_boundaries(monkeypatch, name):
    raw = load(name)
    tv = name == "weibo_tv_post.json"
    source = WeiboTVContent.parse(raw) if tv else WeiboContent.parse(raw)
    if "topic_struct" in raw and not tv:
        # No HTML anchor fallback: the platform's structured topic list alone suffices.
        source.data.text = None
    monkeypatch.setattr(WeiboAPI, "parse", returning(source))
    result = asyncio.run(WeiboParser()._do_parse(""))
    assert result.document is not None
    runs = result.document.blocks[0].runs
    assert "".join(r.text for r in runs) == (source.text.strip() if tv else source.data.content)
    if name == "weibo_annotations_vs_topic_struct.json":
        assert [r.text for r in runs if r.url] == [f"#{t['topic_title']}#" for t in raw["topic_struct"]]
        assert all(r.url.startswith("https://s.weibo.com/weibo?q=") for r in runs if r.url)
    elif name == "weibo_video_post.json":
        assert "http://t.cn/AX0eDSY3" not in visible_text(result.document.blocks)


def test_ptt_dom_retains_absolute_media_and_bad_image_fallback_url(monkeypatch):
    raw = (FIXTURES / "ptt_article_img.html").read_text()
    raw = raw.replace(
        '<div class="richcontent">', '  literal **[x]** <span><a href="/bbs/next">click</a></span>\n'
        '<div class="richcontent">', 1,
    )
    article = PttAPI.parse_html(raw)
    monkeypatch.setattr(PttAPI, "fetch", returning(article))
    result = asyncio.run(PttParser()._do_parse("https://www.ptt.cc/bbs/C_Chat/example.html"))
    assert result.document is not None
    body = result.document.blocks
    assert "  literal **[x]** " in body[0].runs[0].text
    assert TextRun("click", "https://www.ptt.cc/bbs/next") in body[0].runs
    assert (body[1].kind, body[1].index, body[1].url) == ("media", 0, result.media[0].url)
    assert "發信站" not in visible_text(body)
    assert "{{media:0}}" in result.markdown_content


def test_ptt_real_push_details_stay_at_body_level(monkeypatch):
    article = PttAPI.parse_html((FIXTURES / "ptt_article.html").read_text())
    monkeypatch.setattr(PttAPI, "fetch", returning(article))
    result = asyncio.run(PttParser()._do_parse("https://www.ptt.cc/bbs/C_Chat/example.html"))
    assert result.document is not None
    body = result.document.blocks
    assert body[-1].kind == "details" and body[-1].independent
    assert len(body[-1].children) == len(article.pushes) == 3
    assert all(child.kind == "quote" for child in body[-1].children)
