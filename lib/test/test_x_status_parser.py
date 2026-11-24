"""Integration checks from the X response to native status and legacy public output."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from parsehub.parsers.parser.twitter import TwitterArticleParseResult, TwitterParser
from parsehub.provider_api.twitter import TrendingCard, Twitter, TwitterTweet

FIXTURES = Path(__file__).parent / "fixtures"
MAIN_URL = "https://x.com/author/status/1"


def _node(text, *, entities=None, tweet_id="1", handle="author", **extra):
    legacy = {"full_text": text, "entities": {} if entities is None else entities}
    return {
        "rest_id": tweet_id,
        "legacy": legacy,
        "core": {"user_results": {"result": {"legacy": {"name": f"Name {handle}", "screen_name": handle}}}},
        **extra,
    }


def _payload(node):
    return {"data": {"tweetResult": {"result": {"tweet": node}}}}


def _url_entity(text, short, target, *, start=0):
    start = text.index(short, start)
    return {"indices": [start, start + len(short)], "url": short, "expanded_url": target}


def _photo(short, name):
    return {"type": "photo", "url": short, "media_url_https": f"https://pbs.twimg.com/media/{name}.jpg"}


def _parse(node):
    return asyncio.run(TwitterParser.media_parse(Twitter().parse(_payload(node))))


def test_real_note_fixture_uses_codepoint_entities_and_utf16_styles():
    raw = json.loads((FIXTURES / "twitter_richtext_emoji.json").read_text())
    note = raw["data"]["tweetResult"]["result"]["note_tweet"]["note_tweet_results"]["result"]
    tweet = Twitter().parse(raw)
    result = asyncio.run(TwitterParser.media_parse(tweet))

    main = result.x_status.main
    assert main.segments == tweet.segments
    assert any("2027年1月7日(木)" in run.text and "bold" in run.styles for run in main.segments)
    assert note["entity_set"]["urls"][0]["expanded_url"] in {run.url for run in main.segments}
    assert any(run.text == "#鈴代紗弓" and run.url for run in main.segments)
    assert main.author_handle == "chihara_anime"
    assert main.author_url == "https://x.com/chihara_anime"
    assert "**2027年1月7日(木)**" in result.content
    assert "x_status" not in result.to_dict()


def test_legacy_source_decodes_text_after_slicing_and_preserves_literal_markup():
    short = "https://t.co/abc"
    expanded = "https://example.org/long?a=1&b=2"
    literal = "  &lt;tg-collage&gt; **字** {{media:0}} &amp; 🧪  "
    text = f"{literal}{short} #話題」 @reader"
    tag_start = text.index("#話題")
    mention_start = text.index("@reader")
    entities = {
        "urls": [_url_entity(text, short, expanded)],
        "hashtags": [{"indices": [tag_start, tag_start + len("#話題")], "text": "話題"}],
        "user_mentions": [{"indices": [mention_start, len(text)], "screen_name": "reader"}],
    }
    # An empty note falls back to the legacy text *and* its entities.
    note = {"note_tweet_results": {"result": {"text": "", "entity_set": {"urls": []}}}}
    result = _parse(_node(text, entities=entities, note_tweet=note))
    runs = result.x_status.main.segments

    assert runs[0].text == "  <tg-collage> **字** {{media:0}} & 🧪  "
    assert all(not run.styles for run in runs)
    url_run, hashtag_run, mention_run = [run for run in runs if run.url]
    assert url_run.url == expanded
    assert hashtag_run.text == "#話題"
    assert (mention_run.text, mention_run.url) == ("@reader", "https://x.com/reader")
    assert any(run.text == "」 " and not run.url for run in runs)
    assert result.content == text.replace(short, expanded).strip()


def test_bad_entity_spans_do_not_replace_literal_text():
    text = " literal &lt;b&gt;  \nhttps://t.co/raw"
    entities = {"urls": [{"indices": [0, 400], "url": "https://t.co/raw", "expanded_url": "https://wrong.test/"}]}
    result = _parse(_node(text, entities=entities))

    assert "".join(run.text for run in result.x_status.main.segments) == " literal <b>  \nhttps://t.co/raw"
    assert not any(run.url for run in result.x_status.main.segments)


def test_missing_entities_keep_original_body():
    node = _node("  first\n\n    second")
    del node["legacy"]["entities"]
    result = _parse(node)

    assert "".join(run.text for run in result.x_status.main.segments) == node["legacy"]["full_text"]


def test_related_media_only_posts_keep_their_typed_cards_in_media_order(monkeypatch):
    def photo_node(tweet_id, handle, prefix=""):
        short = f"https://t.co/{tweet_id}"
        text = prefix + short
        media = _photo(short, handle) | {"indices": [len(prefix), len(text)]}
        return _node(text, entities={"media": [media]}, tweet_id=tweet_id, handle=handle)

    reply = photo_node("2", "reply")
    quoted = photo_node("3", "quoted")
    main = photo_node("1", "author", "正文 ")
    main["legacy"].update(in_reply_to_status_id_str="2", quoted_status_id_str="3")
    main["quoted_status_result"] = {"result": quoted}
    fetched = []

    async def fetch(_self, tweet_id):
        fetched.append(tweet_id)
        return _payload({"1": main, "2": reply}[tweet_id])

    monkeypatch.setattr(Twitter, "_fetch_result", fetch)
    result = asyncio.run(TwitterParser().parse(MAIN_URL))

    assert fetched == ["1", "2"]
    assert result.x_status.reply is not None and result.x_status.reply.segments == ()
    assert result.x_status.quoted is not None and result.x_status.quoted.segments == ()
    assert result.x_status.reply.author_url == "https://x.com/reply"
    assert result.x_status.quoted.author_url == "https://x.com/quoted"
    assert [urlsplit(media.url).path for media in result.media] == [
        "/media/author.jpg", "/media/reply.jpg", "/media/quoted.jpg",
    ]
    assert (result.reply_media_count, result.quoted_media_count) == (1, 1)
    assert result.quote_roles == ["reply", "quoted"]
    assert result.quote_ids == {"reply": "2", "quoted": "3"}


def test_native_poll_indices_use_reply_main_quote_order():
    raw = json.loads((FIXTURES / "twitter_poll_card.json").read_text())
    main, reply, quoted = [Twitter().parse(raw) for _ in range(3)]
    main.reply_to = reply
    main.quoted_status = quoted
    result = asyncio.run(TwitterParser.media_parse(main))

    status = result.x_status
    assert [status.reply.poll_index, status.main.poll_index, status.quoted.poll_index] == [0, 1, 2]
    assert len(result.polls) == 3
    content = result.content
    assert content.index("{{poll:0}}") < content.index("{{poll:1}}") < content.index("{{poll:2}}")


def test_trending_only_hides_matching_link_entity_and_indexes_actual_cover():
    trend = "https://x.com/i/trending/123?src=card"
    other = "https://example.org/i/trending/123"
    neighboring = "https://x.com/i/trending/1234"
    literal = "https://x.com/i/trending/123"
    text = f"  before {literal} https://t.co/trend  after https://t.co/other https://t.co/near"
    entities = {"urls": [
        _url_entity(text, "https://t.co/trend", "https://twitter.com/i/trending/123/?src=post"),
        _url_entity(text, "https://t.co/other", other),
        _url_entity(text, "https://t.co/near", neighboring),
    ], "media": [_photo("https://t.co/image", "main")]}
    tweet = Twitter().parse(_payload(_node(text, entities=entities)))
    cover = "https://pbs.twimg.com/media/trend.jpg"
    tweet.trending = TrendingCard(title="**topic**", subtitle="<summary>", url=trend, cover=cover)
    result = asyncio.run(TwitterParser.media_parse(tweet))

    status = result.x_status
    assert status.trending.title == "**topic**"
    assert status.trending.subtitle == "<summary>"
    assert status.trending.url == trend
    assert status.trending.cover_index == 1
    assert result.media[status.trending.cover_index].url == cover
    assert [run.url for run in status.main.segments if run.url] == [other, neighboring]
    assert "".join(run.text for run in status.main.segments).startswith(f"  before {literal}   after ")


def test_youtube_metadata_is_typed_and_reuses_the_legacy_lookup(monkeypatch):
    from parsehub.provider_api import youtube

    video = "https://youtu.be/aaaaaaaaaaA"
    main_link = "https://youtu.be/bbbbbbbbbbB"
    short = "https://t.co/quoted"
    quoted_text = f"read {short}"
    quoted = _node(quoted_text, tweet_id="2", entities={"urls": [_url_entity(quoted_text, short, video)]})
    fetched = []

    async def fetch_card(link):
        fetched.append(link)
        return SimpleNamespace(url=video, title='Title <b> & "x"', cover_url="https://example.org/cover.jpg")

    monkeypatch.setattr(youtube, "fetch_card", fetch_card)
    result = _parse(_node(f"main {main_link}", quoted_status_result={"result": quoted}))

    assert fetched == [video]
    assert result.x_status.main.youtube is None
    card = result.x_status.quoted.youtube
    assert (card.text, card.url, card.styles) == ('Title <b> & "x"', video, ("italic",))
    assert '&lt;b&gt; &amp; &quot;x&quot;' in result.content
    assert not result.media


@pytest.mark.parametrize("quote_article", [False, True])
def test_article_does_not_use_markdown_as_typed_status_source(quote_article):
    article = _node("legacy article preview", article={"article_results": {"result": {
        "title": "Article",
        "content_state": {"blocks": [{"key": "1", "text": "Article body", "type": "unstyled"}]},
    }}})
    if quote_article:
        result = _parse(_node("main body", quoted_status_result={"result": article}))
        assert result.content == "main body"
        assert result.x_status.quoted is None
    else:
        result = _parse(article)
        assert isinstance(result, TwitterArticleParseResult)
        assert not getattr(result, "x_status", None)
        assert result.markdown_content == "Article body"
    assert "x_status" not in result.to_dict()


def test_constructed_tweet_fallback_is_literal_not_a_markdown_parse():
    text = "**bold** [link](https://example.org) {{media:0}}"
    tweet = TwitterTweet("1", full_text=text)
    result = asyncio.run(TwitterParser.media_parse(tweet))

    assert [(run.text, run.url, run.styles) for run in result.x_status.main.segments] == [(text, "", ())]
