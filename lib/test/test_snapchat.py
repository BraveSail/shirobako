"""Snapchat Spotlight: 自研 provider 从页面 ``__NEXT_DATA__`` 取明文直链 + 元数据。

fixture ``fixtures/snapchat_spotlight.html`` 从**真实页面**裁剪（只留 ``__NEXT_DATA__``
那段与最小外壳；feed 里保留目标条目 + 两条真实干扰条目，目标**不在首位**）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date
from pathlib import Path

import pytest
from _fakes import FakeResponse, patch_async_get

from parsehub.parsers.parser.snapchat import Snapchatarse
from parsehub.provider_api.snapchat import (
    SnapchatError,
    SnapchatVideo,
    extract_next_data,
    fetch_video,
    get_video_id,
    parse_spotlight,
)
from parsehub.types import ParseError, Platform, VideoParseResult, VideoRef

FIXTURE = Path(__file__).parent / "fixtures" / "snapchat_spotlight.html"
HTML = FIXTURE.read_text(encoding="utf-8")

TARGET_ID = "W7_EDlXWTBiXAEEniNoMPwAAYYWtidGhudGZpAX1TKn0JAX1TKnXJAAAAAA"
#: fixture 里两条**真实干扰条目**（feed 顺序: 干扰1、目标、干扰2 —— 目标不在首位）
DECOY0_ID = "W7_EDlXWTBiXAEEniNoMPwAAYbW94aXltanlqAaDSdDxzAaDSdDxhAAAAAQ"
DECOY1_ID = "W7_EDlXWTBiXAEEniNoMPwAAYd2lxdWRoaW1iAaAgVcKlAaAgUyLnAAAAAQ"
PAGE_URL = f"https://www.snapchat.com/spotlight/{TARGET_ID}"
TARGET_DIRECT_URL = (
    "https://cf-st.sc-cdn.net/d/kKJHIR1QAznRKK9jgYYDq.1034.IRZXSOY"
    "?mo=GkAaFRoAGgAiBgin0YOrBjIBBEgCUC5gAaIBJgiKCBIUChIgAUoOCgleMTkwQzZGOkYQ9AMiCxIAKgdJUlpYU09Z&uc=46"
)


# ── 页面解析（纯函数，离线）────────────────────────────────────────────


def test_parse_spotlight_reads_direct_url_and_metadata():
    video = parse_spotlight(HTML, TARGET_ID)
    assert isinstance(video, SnapchatVideo)
    # 明文直链: 命中 CDN 直链，而不是页面 URL
    assert video.url == TARGET_DIRECT_URL
    assert video.url.startswith("https://cf-st.sc-cdn.net/d/")
    assert video.title == "Views 💕"
    assert video.duration == 5  # durationMs=4665
    assert video.width == 0
    assert video.height == 0
    assert video.author_name == "Shrey"
    assert video.author_handle == "shreypatel57"
    assert video.author_url == "https://www.snapchat.com/@shreypatel57"
    assert video.thumbnail_url.startswith("https://cf-st.sc-cdn.net/")
    # uploadDateMs=1637777831369 → 2021-11-24 UTC
    assert video.published_at is not None
    assert video.published_at.tzinfo is not None
    assert video.published_at.astimezone(UTC).date() == date(2021, 11, 24)
    # viewCount="-1" 表示不可用 → 留空 (不显示)
    assert video.view_count is None
    # videoMetadata.description 是固定模板, 不当正文；视频级 description 为空
    assert video.description == ""


def test_selects_story_by_id_not_by_position():
    """按 ``storyId`` 选中, 而不是拿 feed 第一条（目标在 fixture 中间）。"""
    target = parse_spotlight(HTML, TARGET_ID)
    decoy0 = parse_spotlight(HTML, DECOY0_ID)
    # 干扰条目有自己的直链/作者/时长/尺寸, 与目标不同
    assert decoy0.url != target.url
    assert decoy0.title == "Spotlight Snap"
    assert decoy0.author_name == "KHADIJA"
    assert decoy0.author_handle == "khadija44507"
    assert decoy0.view_count == 1688382
    assert decoy0.duration == 7
    assert (decoy0.width, decoy0.height) == (540, 960)
    # 目标确实不在首位
    stories = extract_next_data(HTML)["props"]["pageProps"]["spotlightFeed"]["spotlightStories"]
    order = [s["story"]["storyId"]["value"] for s in stories]
    assert order == [DECOY0_ID, TARGET_ID, DECOY1_ID]


def test_engagement_stats_fall_back_for_view_count():
    """videoMetadata.viewCount 缺失时用 engagementStats.viewCount。"""
    metadata = {
        "videoMetadata": {"contentUrl": "https://cdn.example/d/x.1.IRZXSOY", "durationMs": "1000"},
        "engagementStats": {"viewCount": "42"},
    }
    video = SnapchatVideo.from_metadata(metadata, "x")
    assert video.view_count == 42


# ── 视频 ID 提取与匹配规则 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://www.snapchat.com/spotlight/{TARGET_ID}", TARGET_ID),
        (f"https://www.snapchat.com/@snapchat/spotlight/{TARGET_ID}", TARGET_ID),
        (f"https://www.snapchat.com/@some.user/{TARGET_ID}", TARGET_ID),
    ],
)
def test_get_video_id(url, expected):
    assert get_video_id(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.snapchat.com/spotlight/{TARGET_ID}",
        "https://www.snapchat.com/@snapchat/spotlight/W7_EDlXWTBiXAEEniNoMPwAAYbHBpemNsYmlyAZ7mTxgqAZ7mTuxMAAAAAw",
        "https://www.snapchat.com/@creativemindsho/gBRYnSexSxSBqXdq2Y6bhAAAga2djanpnd3JlAZ8fYED8AZ8fYD7pAAAAAA",
    ],
)
def test_match_accepts_real_url_forms(url):
    assert Snapchatarse.match(url)
    assert Snapchatarse.__platform__ is Platform.SNAPCHAT


@pytest.mark.parametrize(
    "url",
    [
        "https://www.snapchat.com/download",
        "https://www.snapchat.com/@user",
        "https://example.com/spotlight/whatever",
    ],
)
def test_match_rejects_non_spotlight(url):
    assert not Snapchatarse.match(url)


# ── 拿不到数据时抛错 ───────────────────────────────────────────────────


def test_missing_next_data_raises():
    with pytest.raises(SnapchatError):
        extract_next_data("<html><body>no data here</body></html>")


def test_malformed_next_data_raises():
    with pytest.raises(SnapchatError):
        extract_next_data('<script id="__NEXT_DATA__">{not json</script>')


def test_unknown_video_id_raises():
    """feed 存在但没有该 ID → 报错, 不悄悄退回别的视频。"""
    with pytest.raises(SnapchatError):
        parse_spotlight(HTML, "W7_not_a_real_story_id_0000")


def test_missing_content_url_raises():
    with pytest.raises(SnapchatError):
        SnapchatVideo.from_metadata({"videoMetadata": {"name": "x"}}, "x")


# ── 解析器：VideoRef.url 是明文直链 ─────────────────────────────────────


def test_parser_returns_direct_url_in_videoref():
    with patch_async_get(FakeResponse(text=HTML)):
        result = asyncio.run(Snapchatarse()._do_parse(PAGE_URL))
    assert isinstance(result, VideoParseResult)
    assert isinstance(result.media, VideoRef)
    assert result.media.url == TARGET_DIRECT_URL
    # 关键：是 CDN 直链，不是页面 URL
    assert result.media.url != PAGE_URL
    assert "snapchat.com/spotlight" not in result.media.url
    assert result.media.duration == 5
    assert result.title == "Views 💕"
    assert result.author_name == "Shrey"
    assert result.author_handle == "shreypatel57"
    assert result.author_url == "https://www.snapchat.com/@shreypatel57"
    assert result.published_at is not None
    assert Snapchatarse.__platform__ is Platform.SNAPCHAT


def test_parser_wraps_snapchat_error_as_parse_error():
    with patch_async_get(FakeResponse(text="<html>no next data</html>")):
        with pytest.raises(ParseError):
            asyncio.run(Snapchatarse()._do_parse(PAGE_URL))


def test_real_spotlight_link_replays_the_captured_response():
    """The network boundary is recorded; the real fetch/parser path still runs."""
    with patch_async_get(FakeResponse(text=HTML)) as get:
        video = asyncio.run(fetch_video(PAGE_URL))
    get.assert_awaited_once()
    call = get.await_args
    assert call is not None and call.args[0] == PAGE_URL
    assert video.url == TARGET_DIRECT_URL
    assert video.title == "Views 💕" and video.author_handle == "shreypatel57"
