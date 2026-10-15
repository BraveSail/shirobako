"""YouTube 视频 / 音乐：自研 player API 解析与选流（不再走 yt-dlp）。

fixture:

- ``youtube_player_visionos.json`` —— 真实 ``/youtubei/v1/player`` 响应（**生产用的
  VISIONOS client**, videoId ``dQw4w9WgXcQ``）：27 条流全部带明文 url, 含 2160p/1440p/1080p
  （**全是分离流, 没有音视频合一档**）。
- ``youtube_player_android.json`` —— 真实响应（``ANDROID`` client, 同视频）：
  30 条里**只有 1 条**带明文 url（itag=18, 360p **合一档**）, 另留 1 条无 url 的
  adaptiveFormat 钉住"没有明文 url 的流会被丢弃"。合一是降级路径（环境没有 ffmpeg 时）。
- ``youtube_player_unavailable.json`` —— 非 ``OK`` 的 playability 响应（构造）。

覆盖: 明文直链提取 / 选流策略 / 元数据映射 / 非 OK 报错 / parser 产出 googlevideo 直链 /
分离流的 mux 下载接线 / 合一单文件回退 / 帖子路径回归。
"""

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from _fakes import FakeResponse

from parsehub.parsers.parser.youtube import YtbParse, YtbVideoParseResult
from parsehub.provider_api.youtube import parse_post_page
from parsehub.provider_api.youtube_video import (
    DEFAULT_MAX_HEIGHT,
    YoutubeVideoError,
    _extract_player_response,
    _reset_visitor_cache,
    fetch_video,
    parse_microformat,
    parse_player_response,
    select_streams,
    video_id_from_url,
    visitor_data_from_response,
)
from parsehub.types import MultimediaParseResult, ParseError, VideoParseResult, VideoRef

FIXTURES = Path(__file__).parent / "fixtures"
GOOGLEVIDEO_HOST = "googlevideo.com"
VIDEO_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _player(name: str = "youtube_player_visionos.json") -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _watch_html(data: dict | None = None) -> str:
    """造一份最小 watch 页 HTML（内嵌 ``ytInitialPlayerResponse``）。

    形态与真实页面一致：``var ytInitialPlayerResponse = {...};`` 前后有别的脚本。
    """
    payload = json.dumps(data if data is not None else _player())
    return (
        "<!DOCTYPE html><html><head><script>var ytcfg = {};</script></head><body>"
        f"<script>var ytInitialPlayerResponse = {payload};</script>"
        "<script>var ytInitialData = {};</script>"
        "</body></html>"
    )


def _watch_client(
    captured: dict | None = None,
    *,
    watch_data: dict | None = None,
    watch_status: int = 200,
    watch_html: str | None = None,
    watch_calls: list | None = None,
):
    """假客户端：``get`` 回 watch 页（主路），``post`` 回兜底路径。

    ``watch_status=401`` / ``watch_html="<html></html>"`` 用来模拟主路失败、逼出兜底。
    """

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, **kwargs):
            if watch_calls is not None:
                watch_calls.append((url, kwargs.get("headers", {})))
            if captured is not None:
                captured["watch_url"] = url
                captured["watch_headers"] = kwargs.get("headers", {})
            html = watch_html if watch_html is not None else _watch_html(watch_data)
            return FakeResponse(watch_status, text=html)

        async def post(self, url, **kwargs):
            if captured is not None:
                captured["headers"] = kwargs["headers"]
                captured["body"] = json.loads(kwargs["data"].decode())
            return FakeResponse(200, json_data=_player())

    return FakeClient


def _by_itag(video) -> dict:
    return {stream.itag: stream for stream in video.streams}


@pytest.fixture(autouse=True)
def _isolate_visitor_cache():
    """每个用例前后都清 visitorData 缓存 —— 它是模块级状态, 不隔离会串用例。"""
    _reset_visitor_cache()
    yield
    _reset_visitor_cache()


def _fake_client(  # noqa: PLR0913 - 假客户端需要几个开关, 但都是可选的
    captured: dict | None = None,
    *,
    guide_visitor: str | None = "V-TEST",
    guide_error: Exception | None = None,
    player_data: dict | None = None,
    player_error: Exception | None = None,
    guide_calls: list | None = None,
    player_calls: list | None = None,
):
    """按 URL 分派的假 ``http.AsyncClient``: ``guide`` 回 ``responseContext.visitorData``。

    ``guide_visitor=None`` 模拟"取不到 visitor"; ``guide_error`` 模拟 guide 被限流/挂掉。
    """

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, **kwargs):
            """watch 页（主路）—— 这里刻意让它失败，好让这些用例**显式**走 POST 兜底。"""
            return FakeResponse(200, text="<html></html>")

        async def post(self, url, **kwargs):
            if "guide" in url:
                if guide_calls is not None:
                    guide_calls.append(url)
                if guide_error is not None:
                    raise guide_error
                return FakeResponse(200, json_data={"responseContext": {"visitorData": guide_visitor}})
            if player_calls is not None:
                player_calls.append(url)
            if player_error is not None:
                raise player_error
            if captured is not None:
                captured["headers"] = kwargs["headers"]
                captured["body"] = json.loads(kwargs["data"].decode())
            return FakeResponse(200, json_data=player_data if player_data is not None else _player())

    return FakeClient


# ── ① 明文直链提取 + 选流策略 ──────────────────────────────────────────────


def test_player_response_keeps_only_plain_url_streams():
    """每条流都带可直接下载的明文 url（不需要 signature/nsig 解密）。"""
    video = parse_player_response(_player())
    assert video.streams
    assert all(GOOGLEVIDEO_HOST in stream.url for stream in video.streams)
    assert all(stream.url.startswith("https://") for stream in video.streams)


def test_selection_prefers_highest_quality_capped_at_1080p_with_h264():
    """默认 ≤1080p 的最高画质, 同分辨率优先 H.264（便于 mux 成 mp4）。

    fixture 里 1080p 有 avc1(137) / av01(399) / vp9(248) 三档 —— 要挑 avc1 137;
    音频 mp4a(140) 与 opus(251) 里要挑 mp4a 140。
    """
    video = parse_player_response(_player())
    by_itag = _by_itag(video)
    selected = select_streams(video, max_height=DEFAULT_MAX_HEIGHT, allow_mux=True)

    assert selected.audio_url is not None, "1080p 是分离流, 应走 mux 路径"
    assert selected.height == 1080
    assert selected.video_url == by_itag[137].url  # avc1, 不是 an 399/248
    assert selected.audio_url == by_itag[140].url  # mp4a, 不是 opus 251
    assert selected.ext == "mp4"


def test_selection_without_a_cap_can_reach_4k():
    """放开上限（max_height=0）时选到 2160p —— 证明高画质确实拿得到。"""
    video = parse_player_response(_player())
    by_itag = _by_itag(video)
    selected = select_streams(video, max_height=0, allow_mux=True)
    assert selected.height == 2160
    assert selected.video_url == by_itag[313].url


def test_selection_without_mux_falls_back_to_the_muxed_stream():
    """没有 ffmpeg 时退回合一的 360p 单文件（audio_url=None）, 不输出无声视频。

    用 ``ANDROID`` 的真实响应：它正好只给一条合一档, 代表降级场景。
    """
    video = parse_player_response(_player("youtube_player_android.json"))
    by_itag = _by_itag(video)
    selected = select_streams(video, allow_mux=False)
    assert selected.audio_url is None
    assert selected.video_url == by_itag[18].url
    assert selected.height == 360


def test_selection_raises_when_only_split_streams_and_no_mux():
    """既不能 mux、又没有合一档 -> 明确报错, 绝不静默出无声视频。"""
    video = parse_player_response(_player())
    split_only = [s for s in video.streams if not s.is_muxed]
    video.streams = split_only
    with pytest.raises(YoutubeVideoError):
        select_streams(video, allow_mux=False)


# ── ② 元数据映射 ──────────────────────────────────────────────────────────


def test_metadata_is_mapped_from_video_details():
    video = parse_player_response(_player())
    assert video.video_id == "dQw4w9WgXcQ"
    assert video.title.startswith("Rick Astley")
    assert video.author_name == "Rick Astley"
    assert video.channel_id == "UCuAXFkgsw1L7xaCfnd5JJOw"
    assert video.duration == 213
    assert video.view_count == int(_player()["videoDetails"]["viewCount"])  # 实时变化, 别写死
    assert video.description.startswith("The official video")
    assert video.thumbnail.startswith("https://i.ytimg.com/")
    assert video.author_url == "https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw"


def test_missing_video_details_raises():
    with pytest.raises(YoutubeVideoError):
        parse_player_response({"playabilityStatus": {"status": "OK"}})


# ── ③ 非 OK 时报错 ────────────────────────────────────────────────────────


def test_unplayable_response_raises():
    with pytest.raises(YoutubeVideoError) as exc:
        parse_player_response(_player("youtube_player_unavailable.json"))
    assert "UNPLAYABLE" in str(exc.value)


def test_ok_response_without_any_stream_raises():
    data = {
        "playabilityStatus": {"status": "OK"},
        "videoDetails": {"videoId": "x", "title": "t"},
        "streamingData": {"formats": [{"itag": 18}], "adaptiveFormats": []},  # 无 url
    }
    with pytest.raises(YoutubeVideoError):
        parse_player_response(data)


# ── ④ parser 产出的 VideoRef.url 是 googlevideo 直链 ──────────────────────


def test_video_id_from_url():
    assert video_id_from_url(VIDEO_URL) == "dQw4w9WgXcQ"
    assert video_id_from_url("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert video_id_from_url("https://www.youtube.com/shorts/hnSeq_P3jAo") == "hnSeq_P3jAo"
    assert video_id_from_url("https://www.youtube.com/post/abc") == ""


def _parse_video_with(player: dict):
    async def fake_fetch(video_id, **_kwargs):
        return parse_player_response(player, video_id=video_id)

    with (
        patch("parsehub.parsers.parser.youtube.fetch_video", new=fake_fetch),
        patch.object(YtbParse, "_ffmpeg_available", staticmethod(lambda: True)),
    ):
        return asyncio.run(YtbParse().parse(VIDEO_URL))


def test_parser_video_ref_url_is_a_googlevideo_direct_link():
    result = _parse_video_with(_player())
    assert isinstance(result, VideoParseResult)
    assert isinstance(result.media, VideoRef)
    assert GOOGLEVIDEO_HOST in result.media.url
    assert "youtube.com/watch" not in result.media.url
    assert result.media.width == 1920 and result.media.height == 1080
    assert result.media.duration == 213
    # 元数据也带上了
    assert result.title.startswith("Rick Astley")
    assert result.author_name == "Rick Astley"
    assert result.author_url.startswith("https://www.youtube.com/channel/")
    assert result.view_count == int(_player()["videoDetails"]["viewCount"])
    assert result.content.startswith("The official video")


def test_parser_falls_back_to_muxed_when_ffmpeg_is_missing():
    async def fake_fetch(video_id, **_kwargs):
        return parse_player_response(_player("youtube_player_android.json"), video_id=video_id)

    with (
        patch("parsehub.parsers.parser.youtube.fetch_video", new=fake_fetch),
        patch.object(YtbParse, "_ffmpeg_available", staticmethod(lambda: False)),
    ):
        result = asyncio.run(YtbParse().parse(VIDEO_URL))
    assert result.media.height == 360  # itag 18 合一档


def test_parser_raises_parse_error_when_player_is_unplayable():
    async def fake_fetch(video_id, **_kwargs):
        return parse_player_response(_player("youtube_player_unavailable.json"), video_id=video_id)

    with patch("parsehub.parsers.parser.youtube.fetch_video", new=fake_fetch):
        with pytest.raises(ParseError):
            asyncio.run(YtbParse().parse(VIDEO_URL))


def test_parser_rejects_a_url_without_a_video_id():
    with pytest.raises(ParseError):
        asyncio.run(YtbParse().parse("https://www.youtube.com/watch"))
    assert YtbParse.match("https://www.youtube.com/watch")


# ── fetch_video: 网络入口 ─────────────────────────────────────────────────


# ── ① 主路：GET watch 页 ──────────────────────────────────────────────────
#
# 反爬链路（2026-10-15 实测定案）: 浏览器**只 GET watch 页**, 服务端把
# ``ytInitialPlayerResponse`` 内嵌进 HTML —— 它从不 POST player。
# 受控对照: 同一视频/出口/cookie, POST player 一律 LOGIN_REQUIRED, GET watch 给 31 条明文流。
# 且 watch 页一次就给全 **流 + microformat**, 所以稳态只发 1 次请求。


def test_fetch_video_gets_the_watch_page_and_makes_only_one_request():
    """**核心契约**: 主路只发 1 次请求（GET watch 页）, 且不再需要 visitor。"""
    watch_calls: list = []
    post_calls: list = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, **kwargs):
            watch_calls.append((url, kwargs.get("headers", {})))
            return FakeResponse(200, text=_watch_html())

        async def post(self, url, **kwargs):  # 主路成功就不该走到这里
            post_calls.append(url)
            return FakeResponse(200, json_data=_player())

    with patch("parsehub.provider_api.youtube_video.http.AsyncClient", FakeClient):
        video = asyncio.run(fetch_video("dQw4w9WgXcQ", proxy="socks5h://127.0.0.1:1085"))

    assert len(watch_calls) == 1, "稳态只发 1 次请求（watch 页）"
    assert watch_calls[0][0] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert post_calls == [], "主路成功就不该再 POST player"
    assert video.video_id == "dQw4w9WgXcQ"
    assert video.streams, "流来自内嵌响应"
    assert video.user_agent, "要留下载用的 UA"


def test_watch_page_request_looks_like_a_browser():
    """watch 页请求必须像浏览器 —— 实测非浏览器 UA 会被判 LOGIN_REQUIRED。"""
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["proxy"] = kwargs.get("proxy")
            captured["cookies"] = kwargs.get("cookies")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, **kwargs):
            captured["headers"] = kwargs.get("headers", {})
            return FakeResponse(200, text=_watch_html())

    with patch("parsehub.provider_api.youtube_video.http.AsyncClient", FakeClient):
        asyncio.run(
            fetch_video("dQw4w9WgXcQ", proxy="socks5h://127.0.0.1:1085", cookie={"SID": "x"})
        )

    assert "Mozilla/5.0" in captured["headers"]["User-Agent"]
    assert "text/html" in captured["headers"]["Accept"]
    assert captured["headers"]["Accept-Language"].startswith("en")
    assert captured["proxy"] == "socks5h://127.0.0.1:1085", "解析代理要透传"
    assert captured["cookies"] == {"SID": "x"}, "cookie 必须带上（缺它两条路一起挂）"


def test_metadata_comes_from_the_same_watch_response():
    """**请求数从 2 降到 1 的依据**: watch 页同时给流与 microformat。"""
    data = {**_player(), **MICROFORMAT}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, **kwargs):
            return FakeResponse(200, text=_watch_html(data))

    with patch("parsehub.provider_api.youtube_video.http.AsyncClient", FakeClient):
        video = asyncio.run(fetch_video("jTbsEsYSnpM"))

    assert video.metadata.like_count == 368, "同一个响应里的 microformat 要填进元数据"
    assert video.metadata.published_at is not None
    assert video.streams


def test_watch_page_failure_falls_back_to_post_player():
    """watch 页拿不到流（改版/被拦）⇒ 降级到 POST player 老路, 不能整条挂。"""
    posted: list = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, **kwargs):
            return FakeResponse(200, text="<html><body>no player response here</body></html>")

        async def post(self, url, **kwargs):
            posted.append(url)
            return FakeResponse(200, json_data=_player())

    with patch("parsehub.provider_api.youtube_video.http.AsyncClient", FakeClient):
        video = asyncio.run(fetch_video("dQw4w9WgXcQ"))

    assert posted, "应降级发 POST player"
    assert video.streams


def test_non_200_watch_page_falls_back():
    """watch 页返回非 200（限流/封禁）同样要降级。"""
    posted: list = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, **kwargs):
            return FakeResponse(429, text="")

        async def post(self, url, **kwargs):
            posted.append(url)
            return FakeResponse(200, json_data=_player())

    with patch("parsehub.provider_api.youtube_video.http.AsyncClient", FakeClient):
        video = asyncio.run(fetch_video("dQw4w9WgXcQ"))

    assert posted
    assert video.streams


@pytest.mark.parametrize(
    "html",
    [
        "",
        "<html></html>",
        "<script>var ytInitialPlayerResponse = null;</script>",   # 不是对象
        "<script>var ytInitialPlayerResponse = {broken json;</script>",
        "<script>var ytInitialPlayerResponse={};</script>",       # 空对象
    ],
)
def test_watch_page_extraction_tolerates_bad_html(html):
    """页面改版/字段缺失 ⇒ 空 dict（由调用方降级）, **不抛异常**。"""
    assert _extract_player_response(html) == {} or isinstance(_extract_player_response(html), dict)


def test_watch_page_extraction_handles_nested_braces_and_strings():
    """括号截取必须扛住嵌套对象与字符串里的花括号（否则会截断成非法 JSON）。"""
    data = {"a": {"b": {"c": "}{\\\" 干扰"}}, "d": [1, 2, 3]}
    html = f"<script>var ytInitialPlayerResponse = {json.dumps(data)}; </script>"
    assert _extract_player_response(html) == data


def test_player_response_fixture_is_reachable_from_the_watch_path():
    """真实 fixture 走一遍主路 —— 钉住"内嵌响应能被完整解析"。"""
    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, **kwargs):
            return FakeResponse(200, text=_watch_html())

    with patch("parsehub.provider_api.youtube_video.http.AsyncClient", FakeClient):
        video = asyncio.run(fetch_video("dQw4w9WgXcQ"))

    itags = {s.itag for s in video.streams}
    assert 18 in itags or itags, "至少要拿到流"
    assert video.title.startswith("Rick Astley")


# ── ② 兜底：POST player 老路 ──────────────────────────────────────────────


def test_fetch_video_uses_the_visionos_client():
    """只请求 VISIONOS（#101）—— 它是唯一"给明文 url 且不要 PO token"的 client。

    ``ANDROID_VR``（#28）虽然也给全部明文 url, 但直链实测 403（要 PO token）,
    所以**不能**再拿它当兜底：player 请求"成功"会掩盖真正的下载失败。
    """
    captured: dict = {}

    with patch("parsehub.provider_api.youtube_video.http.AsyncClient", _fake_client(captured)):
        video = asyncio.run(fetch_video("dQw4w9WgXcQ", proxy="http://127.0.0.1:1085"))

    assert captured["headers"]["X-Youtube-Client-Name"] == "101"
    assert video.video_id == "dQw4w9WgXcQ"
    assert video.user_agent  # 带下载时用的 UA


def test_fetch_video_raises_when_every_client_fails():
    fake = _fake_client(player_error=RuntimeError("network down"))

    with patch("parsehub.provider_api.youtube_video.http.AsyncClient", fake):
        with pytest.raises(YoutubeVideoError):
            asyncio.run(fetch_video("dQw4w9WgXcQ"))


# ── visitorData：缺它就被 bot 检查拦住（2026-10-08 定案）─────────────────
#
# 报障 ``jTbsEsYSnpM``: 同一视频、同一出口、同一 client, 只加 ``X-Goog-Visitor-Id``
# 就从 ``LOGIN_REQUIRED`` 变成 ``OK`` + 23 条明文流 / 1080p。所以这个头是**必需**的,
# 而 代理 与 它 **缺一不可**（161 直连时带上它也仍被拦）。


def test_visitor_data_from_response_reads_the_response_context():
    assert visitor_data_from_response({"responseContext": {"visitorData": "abc"}}) == "abc"


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"responseContext": {}},
        {"responseContext": None},
        {"responseContext": {"visitorData": None}},
        {"responseContext": {"visitorData": 123}},  # 类型不对也不能当字符串用
        None,
        "not-a-dict",
    ],
)
def test_visitor_data_from_response_tolerates_odd_shapes(data):
    """拿不到就是空串 —— 调用方据此降级, 不抛错。"""
    assert visitor_data_from_response(data) == ""


def test_player_request_carries_the_visitor_header_and_context():
    """**核心回归**：player 请求必须带 ``X-Goog-Visitor-Id``（缺它就 LOGIN_REQUIRED）。"""
    captured: dict = {}

    with patch("parsehub.provider_api.youtube_video.http.AsyncClient", _fake_client(captured, guide_visitor="V-123")):
        asyncio.run(fetch_video("jTbsEsYSnpM"))

    assert captured["headers"]["X-Goog-Visitor-Id"] == "V-123"
    assert captured["body"]["context"]["client"]["visitorData"] == "V-123"


def test_visitor_comes_from_the_web_response_without_a_guide_request():
    """visitorData 由 **WEB player 的响应**顺手带回 ⇒ 正常路径下**不再需要 guide 请求**。
    （visitor 是必需项：缺它 VISIONOS 会 LOGIN_REQUIRED。）"""
    guide_calls: list = []
    visitor_headers: list = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **kwargs):
            if "guide" in url:
                guide_calls.append(url)
                return FakeResponse(200, json_data={"responseContext": {"visitorData": "V-GUIDE"}})
            visitor_headers.append(kwargs["headers"].get("X-Goog-Visitor-Id"))
            if kwargs["headers"]["X-Youtube-Client-Name"] == "1":
                return FakeResponse(200, json_data={**MICROFORMAT,
                                                    "responseContext": {"visitorData": "V-WEB"}})
            return FakeResponse(200, json_data=_player())

    with patch("parsehub.provider_api.youtube_video.http.AsyncClient", FakeClient):
        asyncio.run(fetch_video("dQw4w9WgXcQ"))

    assert guide_calls == [], "正常路径不该再发 guide"
    assert "V-WEB" in visitor_headers, "VISIONOS 请求要带上 WEB 给回的那个 visitor"


def test_guide_is_the_fallback_when_the_web_response_has_no_visitor():
    """WEB 没给 visitor 时**必须**兜底到 guide —— 否则 VISIONOS 会 LOGIN_REQUIRED
    （那次失败的代价是整条视频解析不了）。"""
    guide_calls: list = []
    visitor_headers: list = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **kwargs):
            if "guide" in url:
                guide_calls.append(url)
                return FakeResponse(200, json_data={"responseContext": {"visitorData": "V-GUIDE"}})
            visitor_headers.append(kwargs["headers"].get("X-Goog-Visitor-Id"))
            if kwargs["headers"]["X-Youtube-Client-Name"] == "1":
                # WEB 响应没有 responseContext.visitorData
                return FakeResponse(200, json_data=MICROFORMAT)
            return FakeResponse(200, json_data=_player())

    with patch("parsehub.provider_api.youtube_video.http.AsyncClient", FakeClient):
        asyncio.run(fetch_video("dQw4w9WgXcQ"))

    assert len(guide_calls) == 1, "应兜底发一次 guide"
    assert "V-GUIDE" in visitor_headers


def test_all_requests_go_over_the_same_proxy():
    """一次解析里**所有**请求必须走同一个出口（代理不能中途换）。

    新链路: 主路 1 发（watch 页）；这里让 watch 页失败以逼出兜底，
    从而同时覆盖"watch + WEB + VISIONOS"三发都走同一代理。
    """
    seen: list = []

    class FakeClient:
        def __init__(self, **kwargs):
            seen.append(kwargs.get("proxy"))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, **kwargs):
            return FakeResponse(200, text="<html></html>")  # 主路失败 -> 走兜底

        async def post(self, url, **kwargs):
            if kwargs["headers"]["X-Youtube-Client-Name"] == "1":
                return FakeResponse(200, json_data={**MICROFORMAT,
                                                    "responseContext": {"visitorData": "V"}})
            return FakeResponse(200, json_data=_player())

    with patch("parsehub.provider_api.youtube_video.http.AsyncClient", FakeClient):
        asyncio.run(fetch_video("dQw4w9WgXcQ", proxy="socks5h://127.0.0.1:1085"))

    assert seen, "至少发过一次请求"
    assert set(seen) == {"socks5h://127.0.0.1:1085"}, f"所有请求必须同一出口, 实际 {seen}"
    assert len(seen) >= 2, "兜底路径会有 watch + WEB + VISIONOS 多发, 全部都要带代理"


def test_watch_page_alone_uses_the_proxy():
    """主路（watch 页）单独验证: 只发 1 次, 且带上传入的代理。"""
    seen: list = []

    class FakeClient:
        def __init__(self, **kwargs):
            seen.append(kwargs.get("proxy"))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, **kwargs):
            return FakeResponse(200, text=_watch_html())

    with patch("parsehub.provider_api.youtube_video.http.AsyncClient", FakeClient):
        asyncio.run(fetch_video("dQw4w9WgXcQ", proxy="socks5h://127.0.0.1:1085"))

    assert seen == ["socks5h://127.0.0.1:1085"]


def test_missing_visitor_degrades_instead_of_failing():
    """guide 挂掉/被 429 时不能连解析一起挂：退回不带该头的旧行为（不抛错）。"""
    captured: dict = {}
    fake = _fake_client(captured, guide_error=RuntimeError("HTTP 429"))

    with patch("parsehub.provider_api.youtube_video.http.AsyncClient", fake):
        video = asyncio.run(fetch_video("dQw4w9WgXcQ"))

    assert video.video_id == "dQw4w9WgXcQ"
    assert "X-Goog-Visitor-Id" not in captured["headers"]
    assert "visitorData" not in captured["body"]["context"]["client"]


# ── 发布时间/点赞/上传者：WEB client 的 microformat ───────────────────────
#
# 实测（25 个 client × player/next × JSON/protobuf，见 MICROFORMAT_CLIENT 的注释）：
# **没有一个 client 能同时给「明文流」和 ``microformat``**。取流的 VISIONOS 连
# ``microformat`` 键都没有；有 ``microformat`` 的 WEB/MWEB 不出流。
# ⇒ 两个 client 分工，但**请求数不变**（WEB player 顶掉原来的 guide 那一发）。

MICROFORMAT = {
    "microformat": {
        "playerMicroformatRenderer": {
            "publishDate": "2026-10-08T02:00:03-07:00",
            "uploadDate": "2026-10-08T02:00:03-07:00",
            "likeCount": "368",
            "ownerProfileUrl": "http://www.youtube.com/@KADOKAWAanime",
            "ownerChannelName": "KADOKAWAanime",
        }
    }
}


def test_parse_microformat_maps_all_three_fields():
    meta = parse_microformat(MICROFORMAT)

    assert meta.published_at is not None
    assert meta.published_at.isoformat().startswith("2026-10-08T02:00:03")
    assert meta.like_count == 368, "likeCount 是字符串, 必须转成 int"
    assert meta.author_handle == "KADOKAWAanime", "从 ownerProfileUrl 取 @handle"
    assert meta.author_url == "https://www.youtube.com/@KADOKAWAanime", "http 要换成 https"


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"microformat": {}},
        {"microformat": None},
        {"microformat": {"playerMicroformatRenderer": {}}},   # 字段全缺
        {"microformat": {"playerMicroformatRenderer": {"publishDate": None, "likeCount": None}}},
        None,
        "not-a-dict",
    ],
)
def test_parse_microformat_tolerates_missing_fields(data):
    """元数据缺失/形态不对都不能抛错（老响应、取流不受影响）。"""
    meta = parse_microformat(data)
    assert meta.published_at is None
    assert meta.like_count is None
    assert meta.author_handle == ""


def test_fetch_video_fills_metadata_from_the_web_client_and_keeps_two_requests():
    """**核心契约**：元数据来自 WEB、流来自 VISIONOS，且**总共只发 2 次请求**
    （WEB player 一发 + VISIONOS player 一发；不能变成 3 发）。"""
    calls: list[tuple[str, str]] = []

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **kwargs):
            client_name = kwargs["headers"]["X-Youtube-Client-Name"]
            calls.append((url, client_name))
            if client_name == "1":      # WEB → 元数据 + visitor
                return FakeResponse(200, json_data={**MICROFORMAT,
                                                    "responseContext": {"visitorData": "V-WEB"}})
            return FakeResponse(200, json_data=_player())

    with patch("parsehub.provider_api.youtube_video.http.AsyncClient", FakeClient):
        video = asyncio.run(fetch_video("jTbsEsYSnpM"))

    assert [c[1] for c in calls] == ["1", "101"], "WEB 取元数据 → VISIONOS 取流"
    assert len(calls) == 2, "请求数必须保持 2（与原来的 guide+player 相同）"
    assert video.metadata.like_count == 368
    assert video.metadata.published_at is not None
    assert video.streams, "取流不受影响"


def test_missing_microformat_still_parses_the_streams():
    """WEB 那发失败/没元数据时：时间与点赞留空, 但**流必须照常拿到**。"""
    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **kwargs):
            if kwargs["headers"]["X-Youtube-Client-Name"] == "1":
                raise RuntimeError("web client down")
            return FakeResponse(200, json_data=_player())

    with patch("parsehub.provider_api.youtube_video.http.AsyncClient", FakeClient):
        video = asyncio.run(fetch_video("jTbsEsYSnpM"))

    assert video.streams
    assert video.metadata.published_at is None
    assert video.metadata.like_count is None


# ── 分离流的 mux 下载接线（离线, 打桩下载与 mux） ─────────────────────────


def test_split_stream_download_fetches_video_and_audio_then_muxes(tmp_path):
    video = parse_player_response(_player())
    selected = select_streams(video, allow_mux=True)
    result = YtbVideoParseResult(info=video, selected=selected)

    requested: list[str] = []

    async def fake_download(url, save_path, **_kwargs):
        requested.append(url)
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        Path(save_path).write_bytes(b"x" * 128)
        return str(save_path)

    muxed: dict = {}

    async def fake_mux(video_path, audio_path, output_path):
        assert Path(video_path).exists() and Path(audio_path).exists()
        muxed["video"] = str(video_path)
        muxed["audio"] = str(audio_path)
        Path(output_path).write_bytes(b"v" * 4096)

    with (
        patch("parsehub.parsers.parser.youtube.download", new=fake_download),
        patch.object(YtbVideoParseResult, "_mux", staticmethod(fake_mux)),
    ):
        download_result = asyncio.run(result.download(tmp_path))

    assert requested == [selected.video_url, selected.audio_url]
    assert muxed["video"] != muxed["audio"]
    assert download_result.media.path.endswith(".mp4")
    assert Path(download_result.media.path).stat().st_size == 4096
    assert download_result.media.duration == 213


def test_muxed_stream_download_uses_the_single_file_path(tmp_path):
    """合一档 (itag 18) -> 直接下单个文件, 不经过 ffmpeg。"""
    video = parse_player_response(_player("youtube_player_android.json"))
    selected = select_streams(video, allow_mux=False)
    assert selected.audio_url is None
    result = YtbVideoParseResult(info=video, selected=selected)

    requested: list[str] = []

    async def fake_download(url, save_path, **_kwargs):
        requested.append(url)
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        Path(save_path).write_bytes(b"x" * 2048)
        return str(save_path)

    # 合一档走基类 `ParseResult._do_download`, 它用的是 `types.result` 里绑定的 download
    with patch("parsehub.types.result.download", new=fake_download):
        download_result = asyncio.run(result.download(tmp_path))

    assert requested == [selected.video_url]
    assert download_result.media.height == 360
    assert Path(download_result.media.path).stat().st_size == 2048


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 ffmpeg")
def test_real_ffmpeg_mux_produces_a_playable_mp4(tmp_path):
    """真跑一次 ffmpeg 合并: 生成的 mp4 里确实有一条视频流 + 一条音频流。"""
    video_in = tmp_path / "in_video.mp4"
    audio_in = tmp_path / "in_audio.m4a"
    out = tmp_path / "out.mp4"
    for cmd in (
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10", "-t", "1", "-y", str(video_in)],
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1", "-y", str(audio_in)],
    ):
        subprocess.run(cmd, check=True, capture_output=True)

    asyncio.run(YtbVideoParseResult._mux(video_in, audio_in, out))

    assert out.stat().st_size > 0
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(out)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "video" in probe and "audio" in probe


# ── ⑤ 帖子路径不受影响（回归） ────────────────────────────────────────────


def test_post_links_still_use_the_page_path_not_the_video_path():
    video_calls = {"n": 0}

    async def fake_fetch_video(*_args, **_kwargs):
        video_calls["n"] += 1
        raise AssertionError("帖子不该走视频路径")

    async def fake_fetch_post(url, **_kwargs):
        html = (FIXTURES / "youtube_post_single_image.html").read_text(encoding="utf-8")
        return parse_post_page(html, url=url)

    post_url = "https://www.youtube.com/post/Ugkxzq0QbgtS1VgHo8hIjZwpiwshZSvd-JtO"
    with (
        patch("parsehub.parsers.parser.youtube.fetch_video", new=fake_fetch_video),
        patch("parsehub.parsers.parser.youtube.fetch_post", new=fake_fetch_post),
    ):
        result = asyncio.run(YtbParse().parse(post_url))

    assert video_calls["n"] == 0
    assert isinstance(result, MultimediaParseResult)
    assert result.author_name == "ANIPLUS Asia"


@pytest.mark.parametrize(
    "url",
    [
        VIDEO_URL,
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/hnSeq_P3jAo",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/post/Ugkxzq0QbgtS1VgHo8hIjZwpiwshZSvd-JtO",
    ],
)
def test_youtube_links_still_match(url):
    assert YtbParse.match(url)
