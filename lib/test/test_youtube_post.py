"""YouTube 社区帖子：同一个 parser 同时管视频与帖子。

帖子以前完全解析不了：``__match__`` 里的 ``(?!(live|post))`` 把它排除，链接落到
「不支持的平台」；而 yt-dlp 也拿不到帖子（``[youtube:tab] post: This channel does not
have a Ugk… tab``），所以帖子改读页面里的 ``ytInitialData``。

fixture 是从真实页面裁剪出来的（只留帖子本体 + JSON-LD 的 ``datePublished``）：

- ``youtube_post_single_image.html`` —— ANIPLUS Asia，单图 + 4 个标签
- ``youtube_post_multi_image.html`` —— ANIPLUS Asia，2 张图
- ``youtube_post_video.html`` —— ANIPLUS Asia，分享的视频（无配图）
- ``youtube_post_poll.html`` —— MrBeast，投票
- ``youtube_post_truncated_links.html`` —— Ani-One，**正文里的链接被 YouTube 截断**
  （显示文本 ``https://www.youtube.com/playlist?list...``），完整地址挂在 run 的
  ``navigationEndpoint`` 上（站内走 ``browseEndpoint.canonicalBaseUrl``、
  外链走 ``urlEndpoint.url`` 的 ``/redirect?…&q=``）
"""

import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from parsehub.parsers.parser.youtube import YtbParse
from parsehub.provider_api.youtube import (
    YoutubePostError,
    parse_post_page,
    post_id_from_url,
)
from parsehub.types import ImageRef, MultimediaParseResult

FIXTURES = Path(__file__).parent / "fixtures"

SINGLE_IMAGE_ID = "Ugkxzq0QbgtS1VgHo8hIjZwpiwshZSvd-JtO"
MULTI_IMAGE_ID = "UgkxzDiURbU2j82CIFPIKtSXoeL6-xTfw-Zj"
VIDEO_ID = "Ugkx684dnFKNtuPPWO3hhEL5YW_W8hpoB0IR"
VIDEO_VIDEO_ID = "iAwRpKe61PA"
POLL_ID = "UgkxZusu9I1Z-VuU5PGZNA2gclHi8V9CJVZk"


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --------------------------------------------------------------- 匹配与链接分派


def test_a_post_link_is_matched_by_the_youtube_parser():
    """核心回归：帖子链接必须被 YouTube parser 接住（以前被 ``(?!(live|post))`` 排除）。"""
    assert YtbParse.match(f"https://www.youtube.com/post/{SINGLE_IMAGE_ID}")


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://youtube.com/shorts/hnSeq_P3jAo",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
    ],
)
def test_video_links_still_match(url):
    assert YtbParse.match(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/@aniplusasia2014",
        "https://www.youtube.com/live/abcdefghijk",
    ],
)
def test_channel_and_live_links_are_still_rejected(url):
    """频道页与直播仍不接受（前者拿不到视频本体, 后者是直播）。"""
    assert not YtbParse.match(url)


def test_post_id_from_url():
    assert post_id_from_url(f"https://www.youtube.com/post/{SINGLE_IMAGE_ID}") == SINGLE_IMAGE_ID
    assert post_id_from_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == ""
    assert post_id_from_url("") == ""


def test_a_post_url_is_parsed_from_the_page_not_through_the_video_path():
    """帖子必须走页面数据 —— 若落到视频路径（player API）这里会抛 AssertionError。"""
    calls = {"fetch_post": 0}

    async def fake_fetch_post(url, **_kwargs):
        calls["fetch_post"] += 1
        return parse_post_page(_html("youtube_post_single_image.html"), url=url)

    def boom(*_args, **_kwargs):
        raise AssertionError("帖子不该走 innertube player（视频）路径")

    with (
        patch("parsehub.parsers.parser.youtube.fetch_post", new=fake_fetch_post),
        patch("parsehub.parsers.parser.youtube.fetch_video", new=boom),
    ):
        result = asyncio.run(YtbParse().parse(f"https://www.youtube.com/post/{SINGLE_IMAGE_ID}"))

    assert calls["fetch_post"] == 1
    assert isinstance(result, MultimediaParseResult)


# ------------------------------------------------------------------ 页面解析


def test_single_image_post():
    post = parse_post_page(_html("youtube_post_single_image.html"))
    assert post.post_id == SINGLE_IMAGE_ID
    assert post.author_name == "ANIPLUS Asia"
    assert post.author_handle == "aniplusasia2014"
    assert post.channel_id == "UCLFfRCXon9T8C_QxgFchYOA"
    assert post.like_count == 9
    assert post.published_at == datetime.fromisoformat("2026-10-07T20:00:01.823371-07:00")
    assert post.hashtags == ["信者ゼロ", "ShinjaZero", "ZeroBelievers", "anime"]
    assert len(post.images) == 1
    assert post.images[0].width == 2480 and post.images[0].height == 3508
    assert post.text.startswith("Starting life in a dream-like fantasy world")
    assert post.video is None and post.poll is None


def test_single_image_url_keeps_the_size_suffix_off():
    """配图取原图 —— 页面给的最大档带 ``=sNNN…`` 尺寸/裁剪修饰, 拿它当原图会丢边。"""
    post = parse_post_page(_html("youtube_post_single_image.html"))
    assert "=" not in post.images[0].url
    assert post.images[0].thumb_url  # 最小档留着当缩略图


# ------------------------------------------------------- 正文里被截断的链接


TRUNCATED_POST_ID = "UgkxNoA6S6qRc2Yc7QXHcECdW9JHe7XLDpAB"


def test_truncated_internal_link_keeps_its_text_but_points_at_the_full_url():
    """**核心回归**：正文**保留 YouTube 给的省略号原文**，但锚点的 href 是完整地址
    （站内路径来自 ``browseEndpoint.canonicalBaseUrl``，要给相对路径补域名）。
    """
    post = parse_post_page(
        _html("youtube_post_truncated_links.html"),
        url=f"https://www.youtube.com/post/{TRUNCATED_POST_ID}",
    )
    assert (
        '<a href="https://www.youtube.com/playlist?list=PLcsS6p8iu5r4">'
        "https://www.youtube.com/playlist?list...</a>"
    ) in post.text
    # 显示形态不变：省略号还在（用户明确要求"保留原格式"）
    assert "list..." in post.text


def test_truncated_external_link_unwraps_the_youtube_redirect():
    """外链（X）的完整地址在 ``urlEndpoint.url`` 里，且包了一层
    ``youtube.com/redirect?…&q=<URL 编码的真实地址>`` —— href 要用解出来的真实地址，
    显示文本仍是原文的省略号形态。
    """
    post = parse_post_page(_html("youtube_post_truncated_links.html"))
    assert (
        '<a href="https://x.com/fxkurumi_info/status/2105266424730255588">'
        "https://x.com/fxkurumi_info/status/21...</a>"
    ) in post.text
    # 跳转壳不能出现在结果里（它带一堆追踪参数）
    assert "youtube.com/redirect" not in post.text


def test_an_untruncated_url_is_left_exactly_as_is():
    """文本与真实地址**相同**（没被截断）时不做任何包装 —— 原有行为不变。"""
    from parsehub.provider_api.youtube import _text_of

    node = {
        "runs": [
            {
                "text": "https://example.com/full/path",
                "navigationEndpoint": {"urlEndpoint": {"url": "https://example.com/full/path"}},
            }
        ]
    }
    assert _text_of(node) == "https://example.com/full/path"


def test_hashtags_are_not_touched_by_the_link_restore():
    """``#hashtag`` 有独立渲染通道，不能被替换成 URL（否则标签行/正文链接化全废）。"""
    post = parse_post_page(_html("youtube_post_truncated_links.html"))
    for tag in ("#AniOne", "#FX戰士久留美", "#線上看", "#Anime"):
        assert tag in post.text, tag


def test_other_prose_is_left_alone():
    """非 URL 形态的文本一个字都不改（还原只对 ``^https?://`` / ``^www.`` 生效）。"""
    post = parse_post_page(_html("youtube_post_truncated_links.html"))
    assert "👈發薪日：錢錢該如何花" in post.text
    assert "ℹ️ 來源 | X @ " in post.text


def test_a_url_without_a_navigation_endpoint_keeps_its_text():
    """run 没有 navigationEndpoint 时保持原样 —— 拿不到真地址就不动它。"""
    from parsehub.provider_api.youtube import _text_of

    node = {"runs": [{"text": "https://example.com/no-endpoint"}]}
    assert _text_of(node) == "https://example.com/no-endpoint"


def test_plain_text_runs_are_joined_verbatim():
    from parsehub.provider_api.youtube import _text_of

    node = {"runs": [{"text": "前"}, {"text": "中"}, {"text": "后"}]}
    assert _text_of(node) == "前中后"


def test_multi_image_post():
    post = parse_post_page(_html("youtube_post_multi_image.html"))
    assert post.post_id == MULTI_IMAGE_ID
    assert post.like_count == 46
    assert len(post.images) == 2
    assert post.published_at == datetime.fromisoformat("2026-10-02T21:00:39.792464-07:00")


def test_video_post_has_the_shared_video_but_no_pictures():
    post = parse_post_page(_html("youtube_post_video.html"))
    assert post.post_id == VIDEO_ID
    assert post.images == []
    assert post.video is not None
    assert post.video.video_id == VIDEO_VIDEO_ID
    assert post.video.title.startswith("The Cold Sato-san")
    assert post.video.cover_url.startswith("https://i.ytimg.com/vi/")


def test_poll_post_only_has_the_choices_and_the_total():
    """匿名只拿得到选项与总票数（每项票数要登录 —— 页面只给 signinEndpoint）。"""
    post = parse_post_page(_html("youtube_post_poll.html"))
    assert post.post_id == POLL_ID
    assert post.author_name == "MrBeast"
    assert post.like_count == 104_000  # 页面写的是 "104K"
    assert post.poll is not None
    assert post.poll.choices == ["100 Years Of No Aging", "1 Billion Dollars"]
    assert post.poll.total_votes == 1_800_000  # 页面写的是 "1.8M votes"
    assert post.poll.total_votes_text == "1.8M votes"


def test_a_page_without_the_post_raises():
    with pytest.raises(YoutubePostError):
        parse_post_page('<html><script>var ytInitialData = {"contents":{}};</script></html>')


def test_a_page_without_yt_initial_data_raises():
    with pytest.raises(YoutubePostError):
        parse_post_page("<html><body>nope</body></html>")


# ------------------------------------------------------------- parser 渲染层


def _parse_fixture(name: str, post_id: str):
    async def fake_fetch_post(_url, **_kwargs):
        return parse_post_page(_html(name), url=f"https://www.youtube.com/post/{post_id}")

    with patch("parsehub.parsers.parser.youtube.fetch_post", new=fake_fetch_post):
        return asyncio.run(YtbParse().parse(f"https://www.youtube.com/post/{post_id}"))


def test_parser_exposes_author_and_metadata():
    result = _parse_fixture("youtube_post_single_image.html", SINGLE_IMAGE_ID)
    assert result.author_name == "ANIPLUS Asia"
    assert result.author_handle == "aniplusasia2014"
    assert result.author_url == "https://www.youtube.com/@aniplusasia2014"
    assert result.like_count == 9
    assert result.hashtags == ["信者ゼロ", "ShinjaZero", "ZeroBelievers", "anime"]
    assert isinstance(result.media, list)
    assert all(isinstance(item, ImageRef) for item in result.media)
    assert result.content.startswith("Starting life in a dream-like fantasy world")


def test_parser_renders_the_poll_as_a_placeholder_with_structured_data():
    """投票在正文里只留占位符行; 头部「投票 · 共 N 票」与单列表格由渲染层本地化拼装。"""
    result = _parse_fixture("youtube_post_poll.html", POLL_ID)
    assert "{{poll:0}}" in result.content, result.content
    assert "投票" not in result.content, result.content
    assert "| 选项 |" not in result.content, result.content
    # 位置: 正文之后
    assert result.content.index("Would you rather") < result.content.index("{{poll:0}}")
    # 结构化数据: 匿名拿不到单项票数 ⇒ count 一律 None, 只有总票数
    assert len(result.polls) == 1
    poll = result.polls[0]
    assert [o.text for o in poll.options] == ["100 Years Of No Aging", "1 Billion Dollars"]
    assert all(o.count is None for o in poll.options)
    assert poll.total_votes == 1_800_000
    assert poll.voters is None
    assert result.media == []


def test_parser_renders_the_shared_video_as_a_link_plus_cover():
    result = _parse_fixture("youtube_post_video.html", VIDEO_ID)
    assert f'<a href="https://www.youtube.com/watch?v={VIDEO_VIDEO_ID}">' in result.content
    assert len(result.media) == 1
    assert isinstance(result.media[0], ImageRef)
    assert result.media[0].url.startswith("https://i.ytimg.com/vi/")


def test_parser_keeps_multiple_pictures_in_order():
    result = _parse_fixture("youtube_post_multi_image.html", MULTI_IMAGE_ID)
    assert len(result.media) == 2
    assert result.media[0].url != result.media[1].url
