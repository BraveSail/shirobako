"""Offline #t tests for native X DTOs and their dedicated translation cache."""

import asyncio
import json
from dataclasses import replace

import pytest
from parsehub.errors import TranslationUnavailable
from parsehub.types import ImageRef, MultimediaParseResult, Poll, PollOption
from parsehub.types.x_status import TextRun, XPost, XStatus, XTrending

from plugins import translate
from services.cache import CacheEntry, CacheMedia, CacheMediaType, CacheParseResult
from services.redis_client import cache_key

URL = "https://x.com/main/status/100"
LANG = "zh-hans"
LITERAL = "    原文 **粗體** [_連結](字面) ||\n> 不是引用 &amp;"
TRANSLATED = "    😀 **译文** [_链接](字面) ||\n> 不是引用 &amp; @author"
TRANSLATED_RUNS = (
    TextRun(TRANSLATED.removesuffix("@author")),
    TextRun("@author", "https://x.com/author"),
)


class TranslationClient:
    def __init__(self):
        self.calls = []
        self.responses = {
            "100": (
                TRANSLATED,
                {"user_mentions": [{"indices": [TRANSLATED.index("@"), len(TRANSLATED)], "screen_name": "author"}]},
            ),
            "200": ("回复译文", {}),
            "300": ("引用译文", {}),
        }

    async def translate_tweet(self, tweet_id, dst_lang):
        self.calls.append((tweet_id, dst_lang))
        await asyncio.sleep(0)  # Without deduplication both same-ID roles can miss together.
        return self.responses[tweet_id]


@pytest.fixture
def backend(monkeypatch):
    store = {}
    client = TranslationClient()

    async def get(key):
        return store.get(key)

    async def set_with_ttl(key, payload, ttl):
        assert isinstance(payload, str)
        assert ttl == 7 * 24 * 60 * 60
        store[key] = payload
        return True

    monkeypatch.setattr(translate, "get_optional", get)
    monkeypatch.setattr(translate, "set_with_ttl", set_with_ttl)
    monkeypatch.setattr(translate, "_build_twitter_client", lambda: client)
    return store, client


@pytest.fixture
def source():
    return MultimediaParseResult(
        content="旧通道的文本应原样保留",
        x_status=XStatus(
            main=XPost((TextRun(LITERAL, styles=("bold",)),)),
            quoted=XPost(
                (TextRun("引用原文"),),
                author_name="引用作者",
                author_handle="quoted",
                author_url="https://x.com/quoted",
                poll_index=0,
                youtube=TextRun("YouTube 卡片", "https://youtu.be/example"),
            ),
            trending=XTrending("趋势", "摘要", "https://x.com/i/trending/1", cover_index=0),
        ),
        media=[ImageRef(url="https://example.com/trending.jpg"), ImageRef(url="https://example.com/quoted.jpg")],
        quoted_media_count=1,
        quote_roles=["quoted"],
        quote_ids={"quoted": "300"},
        polls=[Poll(options=[PollOption("A", 2), PollOption("B", 1)], total_votes=3)],
    )


def test_display_translates_only_native_text_runs(source, backend):
    _, client = backend
    status = source.x_status
    original_content = source.content
    displayed = asyncio.run(translate.translate_for_display(source, URL, LANG))

    assert displayed is not source
    assert displayed.x_status == replace(
        status,
        main=replace(status.main, segments=TRANSLATED_RUNS),
        quoted=replace(status.quoted, segments=(TextRun("引用译文"),)),
    )
    assert displayed.content == source.content == original_content
    assert source.x_status is status
    assert status.main.segments == (TextRun(LITERAL, styles=("bold",)),)
    assert status.quoted.segments == (TextRun("引用原文"),)
    assert displayed.polls == source.polls
    assert displayed.media == source.media
    assert displayed.quoted_media_count == 1
    assert sorted(client.calls) == [("100", "zh-cn"), ("300", "zh-cn")]


def test_quote_translates_independently_when_main_fails(source, backend, monkeypatch):
    _, client = backend
    source.x_status = replace(source.x_status, reply=XPost((TextRun("回复原文"),)))
    source.quote_ids["reply"] = "200"
    initial = source.x_status

    async def run():
        all_started = asyncio.Event()
        seen = set()

        request = client.translate_tweet

        async def translate_tweet(tweet_id, dst_lang):
            seen.add(tweet_id)
            if seen == {"100", "200", "300"}:
                all_started.set()
            await all_started.wait()
            if tweet_id == "100":
                raise TranslationUnavailable("not available")
            return await request(tweet_id, dst_lang)

        monkeypatch.setattr(client, "translate_tweet", translate_tweet)
        return await asyncio.wait_for(translate.translate_for_display(source, URL, LANG), timeout=2)

    displayed = asyncio.run(run())
    assert displayed.x_status.main is initial.main
    assert displayed.x_status.reply == replace(initial.reply, segments=(TextRun("回复译文"),))
    assert displayed.x_status.quoted == replace(initial.quoted, segments=(TextRun("引用译文"),))
    assert source.x_status is initial


def test_skips_roles_without_text_even_if_their_ids_exist(source, backend):
    _, client = backend
    source.x_status = replace(
        source.x_status,
        main=XPost((TextRun("  \n"),)),
        quoted=replace(source.x_status.quoted, segments=()),
    )
    source.quote_ids["reply"] = "200"  # No corresponding reply DTO.

    assert asyncio.run(translate.translate_for_display(source, URL, LANG)) is source
    assert client.calls == []


def test_distinct_roles_share_one_request_when_their_actual_id_matches(source, backend):
    _, client = backend
    source.x_status = replace(source.x_status, reply=XPost((TextRun("回复原文"),), author_handle="reply"))
    source.quote_ids["reply"] = source.quote_ids["quoted"]

    displayed = asyncio.run(translate.translate_for_display(source, URL, LANG))
    assert displayed.x_status.reply.segments == displayed.x_status.quoted.segments == (TextRun("引用译文"),)
    assert displayed.x_status.reply.author_handle == "reply"
    assert displayed.x_status.quoted.author_handle == "quoted"
    assert sorted(client.calls) == [("100", "zh-cn"), ("300", "zh-cn")]


def test_runs_cache_round_trips_without_reading_or_overwriting_legacy_markdown(source, backend):
    store, client = backend
    old_key = cache_key("translate", "100", "zh-cn")
    retired_key = cache_key("translate-runs", "v0", "100", "zh-cn")
    other_language = cache_key("translate-runs", "v1", "100", "zh-tw")
    untouched = {
        old_key: "已有 Markdown 译文",
        retired_key: '[{"text":"过期结构"}]',
        other_language: '[{"text":"繁中譯文"}]',
    }
    store.update(untouched)

    first = asyncio.run(translate.translate_for_display(source, URL, LANG))
    assert store.keys() - untouched.keys() == {
        cache_key("translate-runs", "v1", "100", "zh-cn"),
        cache_key("translate-runs", "v1", "300", "zh-cn"),
    }
    assert [
        {"text": run.text, "url": run.url, "styles": list(run.styles)} for run in TRANSLATED_RUNS
    ] == json.loads(store[cache_key("translate-runs", "v1", "100", "zh-cn")])
    assert first.x_status.main.segments == TRANSLATED_RUNS

    second = asyncio.run(translate.translate_for_display(source, URL, LANG))
    assert second.x_status == first.x_status
    assert sorted(client.calls) == [("100", "zh-cn"), ("300", "zh-cn")]
    assert asyncio.run(translate.translate_tweet_markdown("https://x.com/i/article/100", LANG)) == store[old_key]
    assert {key: store[key] for key in untouched} == untouched
    assert len(client.calls) == 2


@pytest.mark.parametrize("stale", ['[broken JSON', '[{"url":"https://x.com","styles":[]} ]'])
def test_bad_native_cache_is_a_local_miss(source, backend, stale):
    store, client = backend
    legacy_key = cache_key("translate", "100", "zh-cn")
    store[legacy_key] = "旧方案"
    asyncio.run(translate.translate_for_display(source, URL, LANG))
    main_key = cache_key("translate-runs", "v1", "100", "zh-cn")
    assert main_key in store
    store[main_key] = stale
    client.calls.clear()

    displayed = asyncio.run(translate.translate_for_display(source, URL, LANG))
    assert displayed.x_status.main.segments == TRANSLATED_RUNS
    assert displayed.x_status.quoted.segments == (TextRun("引用译文"),)
    assert client.calls == [("100", "zh-cn")]
    assert store[legacy_key] == "旧方案"


class NativeCacheParseResult(CacheParseResult):
    # The base cache model gets this same dataclass field in the next phase.
    x_status: XStatus | None = None


def test_warm_file_id_entry_translates_a_copy_from_cached_runs(source, backend):
    _, client = backend
    cold = asyncio.run(translate.translate_for_display(source, URL, LANG))
    entry = CacheEntry(
        parse_result=NativeCacheParseResult(
            x_status=source.x_status,
            content=source.content,
            quote_ids=source.quote_ids,
            quoted_media_count=1,
            polls=[{"options": [{"text": "A", "count": 2}]}],
        ),
        media=[
            CacheMedia(type=CacheMediaType.PHOTO, file_id="trending"),
            CacheMedia(type=CacheMediaType.PHOTO, file_id="quoted"),
        ],
        rich=True,
    )
    original = entry.parse_result.model_dump()
    client.calls.clear()

    warm = asyncio.run(translate.translate_cache_entry(entry, URL, LANG))
    assert warm is not entry and warm.parse_result is not entry.parse_result
    assert isinstance(warm.parse_result, NativeCacheParseResult)
    assert warm.parse_result.x_status == cold.x_status
    assert warm.parse_result.content == entry.parse_result.content
    assert warm.parse_result.polls == entry.parse_result.polls
    assert warm.parse_result.quoted_media_count == 1
    assert warm.media == entry.media
    assert entry.parse_result.model_dump() == original
    assert client.calls == []
