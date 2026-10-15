"""YouTube 视频 / 音乐解析 —— **自研, 不走 yt-dlp**。

背景
----
YouTube 的反爬本质是 **IP 判定**: 同一条 ``/youtubei/v1/player`` 请求, 从被标记的
出口发出去会被 ``LOGIN_REQUIRED`` / ``UNPLAYABLE`` 挡掉, 换一个干净出口就正常。
一旦出口没问题, 只要用一个**返回明文 ``url`` 的 innertube client** 请求 player API,
``streamingData.formats`` / ``adaptiveFormats`` 里每一项都带**可直接下载的明文直链**
(``https://…googlevideo.com/videoplayback?…``) —— 不需要 signature / nsig 解密,
也不需要 JS 解释器 (那是网页版 client 才要的东西)。

**client 的选择判据是"既要给明文 url, 又不要 PO token"** —— 两条都要满足, 缺一条就废:

| client | 带明文 url 的流 | 直链实测 |
| --- | --- | --- |
| ``ANDROID`` | 30 条里**只有 1 条** (itag=18, 360p) | ✅ 206 |
| ``ANDROID_VR`` | 27 条全部 | ❌ 403 (`GVS_PO_TOKEN_POLICY: required=True`) |
| ``VISIONOS`` | 27 条全部 (含 1080p avc1 / 2160p) | ✅ 206 / 830KB/s |

所以只用 ``VISIONOS`` (见 ``CLIENTS`` 的说明, 别把 ``ANDROID_VR`` 加回来当兜底)。
**选流策略**见 ``select_streams``: 默认取 ≤1080p 的最高画质 (同分辨率优先 H.264, 便于
mux 成 mp4), 音视频分离时由解析器层用 ffmpeg ``-c copy`` 合并
(见 ``parsers/parser/youtube.py``)。

本模块是"播放器逻辑"的归口: 纯函数 (吃 player JSON) + 唯一网络入口 (走 ``utils.http``)。
**不要**把它塞进 ``provider_api/youtube.py`` (那里是帖子/封面, 与播放器无关)。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from loguru import logger

from ..utils import http
from ..utils import nsig as nsig_mod
from ..utils.helpers import to_datetime, to_int

logger = logger.bind(name="YoutubeVideo")

# ── innertube client 常量 ──────────────────────────────────────────────────
#
# **这些版本号会过期**: YouTube 会周期性收紧老 client (要求更高 clientVersion、
# 换 deviceModel, 或干脆停止返回明文 url)。一旦解析集体失败, 第一件事就是照上游
# yt-dlp 的 ``INNERTUBE_CLIENTS`` 更新这里的字段:
#   https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/youtube/_base.py
# 常量集中在这一处, 更新时只改这里 —— 不要把版本号散落到函数体里。
#
# ``_user_agent`` / ``_x_client_name`` 是带下划线的**私有**键, 不会进 innertube
# ``context.client`` (见 ``_client_context``)。``X-Youtube-Client-Name`` 的数值来自
# 上游 ``INNERTUBE_CLIENTS`` 的 client_id。

VISIONOS: dict[str, Any] = {
    "clientName": "VISIONOS",
    "clientVersion": "1.02",
    "deviceMake": "Apple",
    "deviceModel": "RealityDevice17,1",
    "osName": "visionOS",
    "osVersion": "26.5.23O471",
    "hl": "en",
    "gl": "US",
    "_user_agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/26.0 Safari/605.1.15"
    ),
    "_x_client_name": "101",
}

_WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
"""网页版 UA (取 ``visitorData`` 的 guide 请求用; 与 yt-dlp 的 ``web`` client 一致)。"""

#: 按尝试顺序排列 (首选在前)。``fetch_video`` 依次尝试, 全失败才抛错。
#:
#: **只留 VISIONOS**（2026-10-08 实测定案）:
#:
#: | client | 带明文 url 的流 | 直链能否下载 |
#: | --- | --- | --- |
#: | ``ANDROID`` | 30 条里**只有 1 条**（itag=18, 640x360）| ✅ 206 |
#: | ``ANDROID_VR`` | 27 条全部 | ❌ **403** |
#: | ``VISIONOS`` | 27 条全部（含 1080p avc1 / 2160p）| ✅ 206 |
#:
#: - ``ANDROID`` 能下但**只有 360p** —— 其余 29 条既没有 ``url`` 也没有 ``cipher`` 字段,
#:   自研路径根本拿不到, 想要高清只能靠别的 client。
#: - ``ANDROID_VR`` 的 url 看着齐, 实际**必然 403**: 上游 ``INNERTUBE_CLIENTS`` 里
#:   ``android_vr`` 的 ``GVS_PO_TOKEN_POLICY`` 是 ``required=True``（要 PO token）,
#:   而 ``visionos`` 是 ``required=False``。**别再加回它当兜底** —— 它只会掩盖真问题。
#: - 所以这一类 client 的选择判据是: **既要给明文 url, 又不要 PO token**。两者都满足的
#:   目前只有 ``VISIONOS``。
CLIENTS: tuple[dict[str, Any], ...] = (VISIONOS,)

PLAYER_URL = "https://www.youtube.com/youtubei/v1/player"
"""innertube player 接口 (两种 client 共用)。"""

DEFAULT_MAX_HEIGHT = 1080
"""默认画质上限。

1080p 是 H.264 (``avc1``) 在 YouTube 上的**最高档** —— 再往上只有 AV1 / VP9,
体积大、mux 进 mp4 的通用性也差, 而在 Telegram 里 1080p 已经够看。想要 4K 就把
``select_streams(max_height=0)`` 放开 (0 = 不设上限)。
"""


class YoutubeVideoError(RuntimeError):
    """player API 请求失败, 或响应里没有可用的流 (playabilityStatus 非 OK)。"""


# ── 数据结构 ──────────────────────────────────────────────────────────────

_VIDEO_CODECS = ("avc1", "avc3", "av01", "vp9", "vp8", "hev1", "hvc1", "dvh1")
_AUDIO_CODECS = ("mp4a", "opus", "vorbis", "aac", "ec-3", "ac-3")
_CODECS_RE = re.compile(r'codecs="([^"]+)"')


@dataclass(frozen=True, slots=True)
class Stream:
    """player 响应里的一条流 (已确认带明文 ``url``)。"""

    itag: int
    url: str
    mime_type: str
    codec: str = ""
    bitrate: int = 0
    width: int = 0
    height: int = 0
    fps: int = 0
    content_length: int = 0
    quality_label: str = ""
    has_video: bool = False
    has_audio: bool = False

    @property
    def is_muxed(self) -> bool:
        """音视频合一 (``formats`` 里的 itag=18 那种) —— 单文件即可下载。"""
        return self.has_video and self.has_audio


@dataclass(slots=True)
class VideoMetadata:
    """与流无关的视频元数据（来自 ``microformat.playerMicroformatRenderer``）。"""

    published_at: datetime | None = None
    like_count: int | None = None
    author_handle: str = ""
    author_url: str = ""


def parse_microformat(data: Any) -> VideoMetadata:
    """``microformat.playerMicroformatRenderer`` → ``VideoMetadata``（纯函数）。

    - ``publishDate`` 是 ISO 带偏移（``2026-10-08T02:00:03-07:00``），交给 ``to_datetime``；
    - ``likeCount`` 是**字符串**（``"368"``），走 ``to_int``；
    - ``ownerProfileUrl`` 是 ``http://www.youtube.com/@handle``（**注意是 http**，要换 https）。
    取不到一律留空，**不抛错**（老响应形态 / 元数据缺失都不能影响取流）。
    """
    if not isinstance(data, dict):
        return VideoMetadata()
    renderer = data.get("microformat")
    if not isinstance(renderer, dict):
        return VideoMetadata()
    info = renderer.get("playerMicroformatRenderer")
    if not isinstance(info, dict):
        return VideoMetadata()

    handle = ""
    owner_url = str(info.get("ownerProfileUrl") or "")
    if owner_url:
        # http://www.youtube.com/@handle → https + 只留 @handle
        tail = owner_url.split("youtube.com/", 1)[-1].strip("/")
        if tail.startswith("@"):
            handle = tail[1:]

    return VideoMetadata(
        published_at=to_datetime(info.get("publishDate") or info.get("uploadDate")),
        like_count=to_int(info.get("likeCount")),
        author_handle=handle,
        author_url=owner_url.replace("http://", "https://", 1) if owner_url else "",
    )


@dataclass(slots=True)
class YoutubeVideo:
    """一条视频的解析结果 (元数据 + 全部可用流)。"""

    video_id: str
    title: str = ""
    description: str = ""
    author_name: str = ""
    channel_id: str = ""
    thumbnail: str = ""
    duration: int = 0
    view_count: int | None = None
    user_agent: str = ""
    """请求 player 所用 client 的 User-Agent —— 下载直链时一并带上。"""
    metadata: VideoMetadata = field(default_factory=VideoMetadata)
    """视频元数据（发布时间/点赞/上传者）。由 ``fetch_video`` 从 WEB client 的
    ``microformat`` 填入 —— 取流的 client 从不返回它，见 ``MICROFORMAT_CLIENT``。"""
    streams: list[Stream] = field(default_factory=list)

    @property
    def author_url(self) -> str:
        """作者主页。

        只有 ``videoDetails.channelId`` 可用 —— 拼 ``/channel/<id>`` 是**平台自己
        的规范地址** (一定能打开), 而 handle 不在 player 响应里, 拼不了
        (``/@{channel_id}`` 打不开, 上游 yt-dlp 也是这么说的)。
        """
        return f"https://www.youtube.com/channel/{self.channel_id}" if self.channel_id else ""


@dataclass(frozen=True, slots=True)
class SelectedStreams:
    """选流结果。

    ``audio_url`` 为 ``None`` 表示单文件 (音视频合一) —— 用项目下载器直接下次即可;
    非 ``None`` 表示音视频分离, 需要分别下载后用 ffmpeg 合并。
    """

    video_url: str
    audio_url: str | None
    width: int
    height: int
    duration: int
    ext: str = "mp4"


# ── 纯函数: player JSON -> 结构化 ─────────────────────────────────────────

def video_id_from_url(url: str) -> str:
    """从链接里取 11 位视频 ID；取不到返回空串。"""
    from .youtube import VIDEO_ID_RE  # 复用既有正则, 不重复维护

    match = VIDEO_ID_RE.search(url or "")
    return match.group(1) if match else ""


def _stream_codecs(mime_type: str) -> list[str]:
    """``video/mp4; codecs="avc1.640028, mp4a.40.2"`` -> ``["avc1", "mp4a"]``。"""
    codecs: list[str] = []
    for block in _CODECS_RE.findall(mime_type):
        for token in block.split(","):
            name = token.strip().split(".")[0].lower()
            if name:
                codecs.append(name)
    return codecs


def _stream_from_raw(raw: Any) -> Stream | None:
    """一条 ``formats`` / ``adaptiveFormats`` 条目 -> ``Stream``；没有明文 url 则丢弃。"""
    if not isinstance(raw, dict):
        return None
    url = raw.get("url")
    if not url:
        return None
    mime_type = str(raw.get("mimeType") or "")
    codecs = _stream_codecs(mime_type)
    has_video = mime_type.startswith("video/") or any(codec in _VIDEO_CODECS for codec in codecs)
    has_audio = mime_type.startswith("audio/") or any(codec in _AUDIO_CODECS for codec in codecs)
    primary = next(
        (codec for codec in codecs if codec in _VIDEO_CODECS),
        next((codec for codec in codecs if codec in _AUDIO_CODECS), ""),
    )
    return Stream(
        itag=to_int(raw.get("itag")) or 0,
        url=str(url),
        mime_type=mime_type,
        codec=primary,
        bitrate=to_int(raw.get("bitrate")) or 0,
        width=to_int(raw.get("width")) or 0,
        height=to_int(raw.get("height")) or 0,
        fps=to_int(raw.get("fps")) or 0,
        content_length=to_int(raw.get("contentLength")) or 0,
        quality_label=str(raw.get("qualityLabel") or ""),
        has_video=has_video,
        has_audio=has_audio,
    )


def _playability_error(data: dict[str, Any]) -> str | None:
    """``playabilityStatus`` 非 OK 时返回可读原因；OK 返回 ``None``。"""
    status = data.get("playabilityStatus") or {}
    code = str(status.get("status") or "")
    if code in ("", "OK"):
        return None
    detail = status.get("reason") or (status.get("errorScreen") or {}).get("playerErrorMessageRenderer", {}).get(
        "reason", {}
    )
    if isinstance(detail, dict):
        detail = detail.get("simpleText") or "".join(run.get("text", "") for run in detail.get("runs", []))
    return f"{code}{(': ' + str(detail)) if detail else ''}"


def apply_solved_n(urls: list[str], solved: dict[str, str]) -> dict[str, str]:
    """把解出的 ``n`` 写回 URL，返回 ``{原 url: 新 url}``（纯函数，便于离线测试）。

    只替换 URL 里 ``n=`` 的**值**，其余参数一字不动 —— 实测**任何其它参数的顺序/取值
    都不能改**（``sig`` 覆盖了 ``sparams`` 列出的那组参数，改一个就 403）。

    解不出（或结果相同）的项**不放进返回值** —— 调用方据此保持原 URL，
    行为与"没有 n 解算"完全一致（不劣化）。
    """
    out: dict[str, str] = {}
    for url in urls:
        raw = _param_value(url, "n")
        solved_value = solved.get(raw) if raw else None
        if not raw or not solved_value or solved_value == raw:
            continue
        head, sep, query = url.partition("?")
        if not sep:
            continue
        parts = []
        for kv in query.split("&"):
            key, eq, value = kv.partition("=")
            parts.append(f"{key}={solved_value}" if eq and key == "n" else kv)
        out[url] = head + sep + "&".join(parts)
    return out


def _param_value(url: str, key: str) -> str:
    """取 URL query 里某个参数的值（取不到返回空串）。"""
    _, sep, query = url.partition("?")
    if not sep:
        return ""
    for kv in query.split("&"):
        name, eq, value = kv.partition("=")
        if eq and name == key:
            return value
    return ""


def parse_player_response(data: dict[str, Any], *, video_id: str = "") -> YoutubeVideo:
    """把 innertube player 响应转成 ``YoutubeVideo``（纯函数, 便于离线测试）。

    :raises YoutubeVideoError: playabilityStatus 非 OK, 或缺 ``videoDetails`` /
        没有任何带明文 url 的流 —— **不静默返回空结果**。
    """
    if error := _playability_error(data):
        raise YoutubeVideoError(error)

    details = data.get("videoDetails")
    if not isinstance(details, dict):
        raise YoutubeVideoError("player 响应缺少 videoDetails")

    streaming = data.get("streamingData") or {}
    raw_streams = list(streaming.get("formats") or []) + list(streaming.get("adaptiveFormats") or [])
    streams = [stream for raw in raw_streams if (stream := _stream_from_raw(raw)) is not None]
    if not streams:
        raise YoutubeVideoError("player 响应里没有带明文 url 的流")

    thumbnails = (details.get("thumbnail") or {}).get("thumbnails") or []
    thumbnail = ""
    if thumbnails:
        biggest = max(thumbnails, key=lambda t: int(t.get("width") or 0))
        thumbnail = str(biggest.get("url") or "")

    return YoutubeVideo(
        video_id=str(details.get("videoId") or video_id),
        title=str(details.get("title") or "").strip(),
        description=str(details.get("shortDescription") or "").strip(),
        author_name=str(details.get("author") or "").strip(),
        channel_id=str(details.get("channelId") or "").strip(),
        thumbnail=thumbnail,
        duration=to_int(details.get("lengthSeconds")) or 0,
        view_count=to_int(details.get("viewCount")),
        streams=streams,
    )


# ── 纯函数: 选流 ──────────────────────────────────────────────────────────

#: 同分辨率下优先 H.264 (avc1) —— mux 成 mp4 最通用; AV1/VP9 次之。
_VIDEO_RANK = {"avc1": 0, "avc3": 0, "av01": 1, "vp9": 2, "vp8": 3}
_VIDEO_RANK_DEFAULT = 9
#: 音频优先 AAC (mp4a), 与 mp4 容器最搭; opus 次之。
_AUDIO_RANK = {"mp4a": 0, "opus": 1}
_AUDIO_RANK_DEFAULT = 9


def _video_sort_key(stream: Stream) -> tuple:
    # 分辨率优先, 再看编码 (小值优先), 然后帧率 / 码率
    return (
        -stream.height,
        _VIDEO_RANK.get(stream.codec, _VIDEO_RANK_DEFAULT),
        -stream.fps,
        -stream.bitrate,
    )


def _audio_sort_key(stream: Stream) -> tuple:
    return (_AUDIO_RANK.get(stream.codec, _AUDIO_RANK_DEFAULT), -stream.bitrate)


def _ext_of(mime_type: str) -> str:
    if "webm" in mime_type:
        return "webm"
    return "mp4"


def select_streams(
    video: YoutubeVideo,
    *,
    max_height: int = DEFAULT_MAX_HEIGHT,
    allow_mux: bool = True,
) -> SelectedStreams:
    """挑一条可下载的流（纯函数）。

    策略:

    1. ``allow_mux`` 且存在分离的视频流与音频流 -> **最高画质**: ``≤max_height`` 里
       最高的一条 (同分辨率优先 H.264) + 最优音频 (优先 AAC), 交给上层 mux。
    2. 否则退回**音视频合一**的单文件流 (最高档)。
    3. 都没有 -> 抛 ``YoutubeVideoError``（不静默出无声/空结果）。

    :param max_height: 画质上限, ``0`` 表示不设上限。
    :param allow_mux: 环境能否 mux (有 ffmpeg 才为 True)。False 时只用合一单文件,
        若又没有合一档则明确报错 —— 绝不输出无声视频。
    """
    video_streams = [s for s in video.streams if s.has_video and not s.has_audio and s.height > 0]
    audio_streams = [s for s in video.streams if s.has_audio and not s.has_video]
    muxed_streams = [s for s in video.streams if s.is_muxed]

    if max_height > 0:
        capped = [s for s in video_streams if s.height <= max_height]
        if capped:
            video_streams = capped

    if allow_mux and video_streams and audio_streams:
        best_video = min(video_streams, key=_video_sort_key)
        best_audio = min(audio_streams, key=_audio_sort_key)
        return SelectedStreams(
            video_url=best_video.url,
            audio_url=best_audio.url,
            width=best_video.width,
            height=best_video.height,
            duration=video.duration,
            ext=_ext_of(best_video.mime_type),
        )

    if muxed_streams:
        best = min(muxed_streams, key=_video_sort_key)
        return SelectedStreams(
            video_url=best.url,
            audio_url=None,
            width=best.width,
            height=best.height,
            duration=video.duration,
            ext=_ext_of(best.mime_type),
        )

    if video_streams and audio_streams:
        raise YoutubeVideoError("需要 ffmpeg 合并音视频, 但当前环境没有 ffmpeg")
    raise YoutubeVideoError("响应里没有可下载的流")


# ── 唯一网络入口 ──────────────────────────────────────────────────────────

GUIDE_URL = "https://www.youtube.com/youtubei/v1/guide?prettyPrint=false"
"""取 ``visitorData`` 的轻量 innertube 接口 (导航数据)。

⚠️ **不要改成抓 ``/watch`` 页 HTML** —— 实测该页面会返回 **HTTP 429**(连打几次就限流),
一失败就退化成 ``LOGIN_REQUIRED``; guide 稳定得多, 且不需要身份、不会被 bot 检查拦。
"""

_WEB_VERSION = "2.20260708.00.00"
"""guide 请求用的网页版 clientVersion (与 yt-dlp 的 ``web`` 一致)。"""

_VISITOR_TTL = 3600.0
"""``visitorData`` 的缓存时长 (秒)。"""

_visitor_cache: dict[str, Any] = {"value": "", "expires_at": 0.0}
"""进程内缓存的 ``visitorData``。

它是**访客身份** (不是视频级凭证) ⇒ 可跨视频复用: 实测同一个 visitor 连用 3 条视频
全部 ``OK``。缓存能省掉每个视频一次额外请求 (也少一次被限流的机会)。
"""


def visitor_data_from_response(data: Any) -> str:
    """从 innertube 响应的 ``responseContext.visitorData`` 取访客标识 (纯函数)。

    取不到返回空串 —— 调用方据此走"不带 visitor"的降级路径, 不抛错。
    """
    if not isinstance(data, dict):
        return ""
    context = data.get("responseContext")
    if not isinstance(context, dict):
        return ""
    value = context.get("visitorData")
    return value if isinstance(value, str) else ""


async def _fetch_visitor_data(*, proxy: str | http.Proxy | None, cookie: dict[str, str] | None) -> str:
    """请求 guide 接口取 ``visitorData``; 任何失败都返回空串 (由调用方降级)。

    只在 ``_fetch_web_metadata`` 拿不到 visitor 时兜底用 —— 正常路径下 visitor 由
    ``_fetch_web_metadata`` 的 WEB player 响应顺手带回（**不额外发请求**）。
    """
    body = {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": _WEB_VERSION,
                "hl": "en",
                "gl": "US",
            }
        }
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": _WEB_USER_AGENT,
        "Origin": "https://www.youtube.com",
        "X-Youtube-Client-Name": "1",
        "X-Youtube-Client-Version": _WEB_VERSION,
    }
    try:
        async with http.AsyncClient(proxy=proxy, cookies=cookie, timeout=30) as client:
            response = await client.post(GUIDE_URL, data=json.dumps(body).encode(), headers=headers)
        if response.status_code != 200:
            return ""
        return visitor_data_from_response(response.json())
    except Exception:  # noqa: BLE001 - 网络/JSON 异常都并入降级路径
        return ""


async def _get_visitor_data(*, proxy: str | http.Proxy | None, cookie: dict[str, str] | None) -> str:
    """带缓存的 ``visitorData`` 取用: 命中缓存直接返回, 否则请求一次并按 TTL 存起来。"""
    now = time.monotonic()
    cached = str(_visitor_cache["value"])
    if cached and now < float(_visitor_cache["expires_at"]):
        return cached

    value = await _fetch_visitor_data(proxy=proxy, cookie=cookie)
    if value:
        _visitor_cache["value"] = value
        _visitor_cache["expires_at"] = now + _VISITOR_TTL
        return value
    # 本次没取到: 若手上有过期值, 宁可用旧的也比空着强
    return cached


def _reset_visitor_cache() -> None:
    """清空 visitorData 缓存 (测试用)。"""
    _visitor_cache["value"] = ""
    _visitor_cache["expires_at"] = 0.0


def _client_context(spec: dict[str, Any]) -> dict[str, Any]:
    """去掉私有键 (``_`` 开头), 只留 innertube ``context.client`` 认得的字段。"""
    return {key: value for key, value in spec.items() if not key.startswith("_")}


# ── 视频元数据（发布时间 / 点赞 / 上传者）─────────────────────────────────
#
# ⚠️ **实测结论（别再试别的路）**：**没有一个 client 能一次同时给「明文流」和
# ``microformat``** —— 25 个 client + player/next 两个接口 + JSON/protobuf 两种格式
# + 8 个内置 key + 两个域名都试过：
#
# | 能出明流的 client | ``microformat``（发布时间） |
# | --- | --- |
# | ``VISIONOS`` / ``ANDROID`` / ``ANDROID_VR`` / ``IOS`` | **无**（连键都不存在；3.8MB 的
# |   next protobuf 逐字节搜发布日期戳, 0 命中）|
# | ``WEB`` / ``MWEB``（带不带 PO token 都一样）| **有**（``publishDate`` / ``dateText``）|
#
# app 的发布时间来自服务端下发的 protobuf 字段
# （``OfflineVideoCursorReader`` → ``bjjy.f115999h`` → ``new Date(SECONDS.toMillis(...))``
#  → ``VideoMetadata.publishedDateText``），**匿名请求拿不到**（疑似要登录态）。
#
# ⇒ 所以是**两个 client 分工**：``VISIONOS`` 取流（唯一满足「给明流 + 不要 PO token」的），
# ``WEB`` 取元数据。但这是**同一个请求位置**上的分工 —— ``fetch_video`` 只发两次请求，
# 与之前「guide 取 visitor + VISIONOS 取流」**请求数完全相同**，只是把信息更少的 guide
# 换成了 WEB player（它还顺手带回 visitorData）。别再加第三次请求。

MICROFORMAT_CLIENT: dict[str, Any] = {
    "clientName": "WEB",
    "clientVersion": "2.20260708.00.00",
    "hl": "en",
    "gl": "US",
    "timeZone": "UTC",
    "utcOffsetMinutes": 0,
    "_user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/605.1.15"
    ),
    "_x_client_name": "1",
}
"""取 ``microformat``（发布时间/点赞/上传者）用的 client。

网页版即使播放被拦（``playabilityStatus`` 非 OK）**仍照常返回 ``microformat``** ——
我们要的只是元数据，所以这个"播放不可用"不影响用途。
"""


async def _request_player(
    video_id: str,
    spec: dict[str, Any],
    *,
    proxy: str | None,
    cookie: dict[str, str] | None,
    visitor: str = "",
) -> dict[str, Any]:
    """请求 player API 并返回原始 JSON。

    ``visitor`` 必须带上 —— **实测缺它就会 ``LOGIN_REQUIRED``**（同一视频、同一出口、
    同一 client, 只加 ``X-Goog-Visitor-Id`` 就从"要登录"变成 23 条明文流 / 1080p）。
    同时写进 ``context.client.visitorData``（与 yt-dlp 的做法对齐, 有些 client 会读它）。
    """
    client = _client_context(spec)
    if visitor:
        client["visitorData"] = visitor
    body = {
        "context": {"client": client},
        "videoId": video_id,
        "contentCheckOk": True,
        "racyCheckOk": True,
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": spec["_user_agent"],
        "X-Youtube-Client-Name": spec["_x_client_name"],
        "X-Youtube-Client-Version": spec["clientVersion"],
        "Accept-Language": "en-US,en;q=0.9",
    }
    if visitor:
        headers["X-Goog-Visitor-Id"] = visitor
    async with http.AsyncClient(proxy=proxy, cookies=cookie, timeout=30) as client_obj:
        response = await client_obj.post(PLAYER_URL, data=json.dumps(body).encode(), headers=headers)
    if response.status_code != 200:
        raise YoutubeVideoError(f"player API 返回 HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise YoutubeVideoError("player API 返回的不是 JSON") from exc


async def _fetch_web_metadata(
    video_id: str, *, proxy: str | http.Proxy | None, cookie: dict[str, str] | None
) -> tuple[VideoMetadata, str]:
    """一次 WEB player 请求, 同时取回 **视频元数据** 与 **visitorData**。

    返回 ``(metadata, visitor)``；任何失败都返回空值（由调用方走兜底/降级）。

    ⚠️ **这次请求就是原来 ``guide`` 的位置** —— 请求数不变（仍是 2: 本函数 + VISIONOS 取流），
    只是把「只带回 visitor 的 guide」换成「顺带带回发布时间/点赞/上传者 的 WEB player」。
    """
    body = {
        "context": {"client": _client_context(MICROFORMAT_CLIENT)},
        "videoId": video_id,
        "contentCheckOk": True,
        "racyCheckOk": True,
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": MICROFORMAT_CLIENT["_user_agent"],
        "X-Youtube-Client-Name": MICROFORMAT_CLIENT["_x_client_name"],
        "X-Youtube-Client-Version": MICROFORMAT_CLIENT["clientVersion"],
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with http.AsyncClient(proxy=proxy, cookies=cookie, timeout=30) as client:
            response = await client.post(PLAYER_URL, data=json.dumps(body).encode(), headers=headers)
        if response.status_code != 200:
            return VideoMetadata(), ""
        data = response.json()
    except Exception:  # noqa: BLE001 - 网络/JSON 异常都走降级
        return VideoMetadata(), ""

    return parse_microformat(data), visitor_data_from_response(data)


async def fetch_video(
    video_id: str,
    *,
    proxy: str | None = None,
    cookie: dict[str, str] | None = None,
) -> YoutubeVideo:
    """请求 player API 并解析。

    ``CLIENTS`` 只有 ``VISIONOS``（取流）；元数据另走一次 WEB player —— 见
    ``MICROFORMAT_CLIENT`` 的说明：**没有一个 client 能同时给流和发布时间**，
    所以是两个 client 分工，但请求总数仍是 2（WEB + VISIONOS），没有额外开销。

    取不到 visitor 时降级为不带它（回到"可能被 bot 检查拦住"的旧行为，不会更差）；
    取不到元数据时时间/点赞留空（渲染层不显示空项）。
    """
    if not video_id:
        raise YoutubeVideoError("缺少视频 ID")

    metadata, visitor = await _fetch_web_metadata(video_id, proxy=proxy, cookie=cookie)
    if not visitor:
        # WEB 请求失败/没带 visitor ⇒ 回退到 guide（visitor 是必需项：缺它会 LOGIN_REQUIRED）
        visitor = await _get_visitor_data(proxy=proxy, cookie=cookie)

    errors: list[str] = []
    for spec in CLIENTS:
        try:
            data = await _request_player(video_id, spec, proxy=proxy, cookie=cookie, visitor=visitor)
            video = parse_player_response(data, video_id=video_id)
        except YoutubeVideoError as exc:
            errors.append(f"{spec['clientName']}: {exc}")
        except Exception as exc:  # noqa: BLE001 - 网络/解析异常都要并入原因列表
            errors.append(f"{spec['clientName']}: {exc}")
        else:
            video.user_agent = str(spec["_user_agent"])
            video.metadata = metadata
            # n-sig: 直链里原样的 ``n`` 会被服务端 403, 必须解一遍（解不出就保持原样）
            await _apply_nsig(video, data, proxy=proxy, cookie=cookie)
            return video

    raise YoutubeVideoError("player API 解析失败: " + "; ".join(errors))


async def _apply_nsig(
    video: YoutubeVideo,
    data: dict[str, Any],
    *,
    proxy: str | None,
    cookie: dict[str, str] | None,
) -> None:
    """把 ``video.streams`` 里所有 URL 的 n 参数解成有效值（原地改写）。

    **为什么必须做**: 直链里的 ``n`` 是需 JS 变换的令牌 —— 原样请求会被 ``403``。
    实测同一条视频: n 原样 → 403；解过后 → 206 / 可下载。

    降级策略（每一层失败都不抛）:
    - 没有 JS 运行时 / 拿不到播放器 JS / 求解器无输出 ⇒ **保持原 URL**（行为同改动前）
    - 只有``n`` 还留在 URL 上的流才参与（其它流无 n，无需处理）
    """
    if not nsig_mod.find_runtime():
        return
    targets = [s.url for s in video.streams if _param_value(s.url, "n")]
    if not targets:
        return

    player_id = nsig_mod.player_id_from_response(data)
    if not player_id:
        logger.debug("播放器响应里没有 jsUrl, 跳过 n-sig")
        return

    player_js = await nsig_mod.get_player_js(player_id, proxy=proxy)
    if not player_js:
        return

    raw_values = list({_param_value(u, "n") for u in targets})
    solved = nsig_mod.solve_n(player_js, raw_values)
    if not solved:
        return

    mapping = apply_solved_n(targets, solved)
    if not mapping:
        return

    # ``Stream`` 是 frozen dataclass ⇒ 用 ``replace`` 造新对象；``YoutubeVideo`` 不是
    # frozen，直接改 ``streams`` 字段即可。
    video.streams = [replace(s, url=mapping.get(s.url, s.url)) for s in video.streams]
    logger.debug(f"n-sig 已解: {len(mapping)}/{len(targets)} 条流")


__all__ = [
    "CLIENTS",
    "DEFAULT_MAX_HEIGHT",
    "GUIDE_URL",
    "MICROFORMAT_CLIENT",
    "PLAYER_URL",
    "VISIONOS",
    "SelectedStreams",
    "Stream",
    "VideoMetadata",
    "YoutubeVideo",
    "YoutubeVideoError",
    "apply_solved_n",
    "fetch_video",
    "parse_microformat",
    "parse_player_response",
    "select_streams",
    "video_id_from_url",
    "visitor_data_from_response",
]
