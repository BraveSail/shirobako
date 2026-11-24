"""YouTube 频道/视频的标题与封面。

**不是解析 YouTube 视频本身**（那是 ``parsers/parser/youtube.py`` + yt-dlp 的活），
而是给**别处正文里提到的 YouTube 链接**配一张封面（用户要求：正文引用的 YouTube
链接要像 bilibili 引用的视频那样，有封面、标题可点）。

两条路：

- **视频**（``watch?v=`` / ``youtu.be/`` / ``shorts/``）走 oembed，返回 JSON，
  ``thumbnail_url`` 就是视频缩略图 —— 又快又稳。
- **频道**（``/@handle`` 等）没有 oembed，只能抓频道页的 ``og:title`` / ``og:image``
  （拿到的是频道头像/主视觉，频道本来就没有"视频缩略图"）。

封面抓不到**不该影响解析**：任何异常都返回 ``None``，调用方跳过即可。
"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..types.document import TextRun
from ..utils import http
from ..utils.helpers import to_datetime

#: 视频 ID: watch?v= / youtu.be/ / embed/ / shorts/ / live/
VIDEO_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|embed/|shorts/|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)

#: 频道页 (拿不到视频 ID 时按频道处理)
CHANNEL_RE = re.compile(
    r"youtube\.com/(?:@[A-Za-z0-9_.\-]+|channel/[A-Za-z0-9_-]+|c/[A-Za-z0-9_.\-]+|user/[A-Za-z0-9_.\-]+)"
)

#: 正文里出现的 YouTube 链接 (含裸域名与 http/https)
_LINK_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?[^\s<>\"']+|@[A-Za-z0-9_.\-]+"
    r"|channel/[A-Za-z0-9_-]+|c/[A-Za-z0-9_.\-]+|user/[A-Za-z0-9_.\-]+|shorts/[A-Za-z0-9_-]+"
    r"|live/[A-Za-z0-9_-]+|embed/[A-Za-z0-9_-]+)"
    r"|youtu\.be/[A-Za-z0-9_-]+)"
)

_OEMBED = "https://www.youtube.com/oembed?format=json&url={url}"

_OG_TITLE_RE = re.compile(r'<meta\s+property="og:title"\s+content="([^"]*)"', re.I)
_OG_IMAGE_RE = re.compile(r'<meta\s+property="og:image"\s+content="([^"]*)"', re.I)




@dataclass
class YoutubeCard:
    """一个 YouTube 链接的卡片信息。"""

    url: str
    title: str
    cover_url: str


def find_youtube_links(text: str | None) -> list[str]:
    """正文里出现的 YouTube 链接 (去重, 保持出现顺序)。"""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for link in _LINK_RE.findall(text):
        link = link.rstrip("。，、）)】].,")
        if link not in seen:
            seen.add(link)
            out.append(link)
    return out


def trim_cover(url: str) -> str:
    """把 ``yt3.ggpht.com`` 的尺寸修饰换成 **``=s0``（原图）**。

    ⚠️ **不能简单截掉 ``=`` 之后的部分**（旧实现就是那么做的）：
    实测同一条帖子配图
    - 无后缀 → **512×288 / 241 KB**（服务端默认小图，比原图小得多）
    - ``=s0`` → **1280×720 / 1.13 MB**（原图）
    - ``=s288-rw-nd-v1``（页面给的 thumb）→ 288×162

    也就是说「去掉修饰」拿到的是默认缩略图，不是原图 —— 用户报「抓的不是高清图」
    就是这条。正确做法是保留 ``=`` 并把尺寸段换成 ``s0``。

    顺带解决方形裁剪：``=s1280-c`` 会裁成 1280×1280，``=s0`` 不带 ``-c``，
    拿到的是未裁剪的原比例图。

    非 ggpht 域名（其它图床）原样返回 —— 它们的 ``=`` 未必是尺寸语义。
    """
    if "yt3.ggpht.com" not in url and "yt3.googleusercontent.com" not in url:
        return url
    base = url.split("=", 1)[0]
    return f"{base}=s0"


async def _fetch_page(url: str, *, proxy=None) -> str:
    """抓频道页。

    频道页约 1.1MB, 但 **og 标签不在开头** —— 实测它落在 400KB 之后 (前面是内联
    脚本与数据), 所以不能"只读前 N KB"就去找, 必须拿完整文本。
    """
    async with http.AsyncClient(proxy=proxy, follow_redirects=True) as cli:
        resp = await cli.get(url)
        if resp.status_code != 200:
            return ""
        return resp.text or ""


async def _fetch_video(video_url: str, *, proxy=None) -> YoutubeCard | None:
    async with http.AsyncClient(proxy=proxy, follow_redirects=True) as cli:
        resp = await cli.get(_OEMBED.format(url=video_url))
        if resp.status_code != 200:
            return None
        data = json.loads(resp.text)
    title = (data.get("title") or "").strip()
    cover = trim_cover((data.get("thumbnail_url") or "").strip())
    if not title or not cover:
        return None
    return YoutubeCard(url=video_url, title=title, cover_url=cover)


async def _fetch_channel(channel_url: str, *, proxy=None) -> YoutubeCard | None:
    html = await _fetch_page(channel_url, proxy=proxy)
    if not html:
        return None
    title_m = _OG_TITLE_RE.search(html)
    image_m = _OG_IMAGE_RE.search(html)
    if not (title_m and image_m):
        return None
    title = title_m.group(1).strip()
    cover = trim_cover(image_m.group(1).strip())
    if not title or not cover:
        return None
    return YoutubeCard(url=channel_url, title=title, cover_url=cover)


async def fetch_card(url: str, *, proxy=None) -> YoutubeCard | None:
    """取一个 YouTube 链接的标题与封面。

    视频优先走 oembed; 频道 (或 oembed 失败) 退回抓页面。**失败一律返回 None** ——
    封面是锦上添花, 不该让整条解析失败。
    """
    if not url:
        return None
    try:
        if VIDEO_ID_RE.search(url):
            if card := await _fetch_video(url, proxy=proxy):
                return card
        if CHANNEL_RE.search(url) or VIDEO_ID_RE.search(url):
            return await _fetch_channel(url, proxy=proxy)
    except Exception:  # noqa: BLE001 - 网络/解析异常都不该冒泡到解析流程
        return None
    return None


# ─────────────────────────────────────────────────────────── 社区帖子 (community post)
#
# 帖子不是视频: yt-dlp 拿不到它 —— ``[youtube:tab] post: This channel does not have a
# Ugk… tab``（它把 ``/post/<id>`` 当成频道 tab）。只能读页面里的 ``ytInitialData``
# （匿名可访问, 实测整页 ~814 KB）, 帖子本体在
# ``…sectionListRenderer → itemSectionRenderer → backstagePostThreadRenderer
# → post.backstagePostRenderer``。
#
# 拿得到: 正文 / 作者（显示名 + ``@handle`` + channelId）/ 附件（单图、多图、分享的视频、
# 投票）/ 点赞数 / 正文里的标签; **绝对发布时间只在页头 JSON-LD 的 ``datePublished``**
# （``publishedTimeText`` 是 "50 minutes ago" 这种相对时间, 不能当发布时间用）。
# 拿不到: 投票**每项**的票数与占比 —— 匿名只给选项文案与总票数（choice 里只有
# ``signinEndpoint``）, 要登录才有。

POST_URL_RE = re.compile(r"youtube\.com/post/([A-Za-z0-9_-]+)")
"""帖子链接里的 ID（``/post/<id>``）。"""

_YT_INITIAL_DATA_MARKER = "var ytInitialData = "
_DATE_PUBLISHED_RE = re.compile(r'"(?:datePublished|publishDate)"\s*:\s*"([^"]+)"')
_HASHTAG_BROWSE_ID = "FEhashtag"


class YoutubePostError(Exception):
    """帖子取数失败（页面没有数据 / 帖子已删除 / 需要登录）。"""

    def __init__(self, msg: str):
        self.msg = msg
        super().__init__(msg)


@dataclass
class YoutubePostImage:
    """帖子配图 —— ``url`` 是去掉尺寸后缀的**原图**，``thumb_url`` 是页面给的最小档。"""

    url: str
    thumb_url: str = ""
    width: int = 0
    height: int = 0


@dataclass
class YoutubePostVideo:
    """帖子里分享的视频（只给封面与标题, 解析器只渲染链接, 不下载）。"""

    video_id: str
    title: str = ""
    cover_url: str = ""
    title_runs: tuple[TextRun, ...] = ()


@dataclass
class YoutubePostPoll:
    """投票 —— 匿名只能拿到选项文案与总票数（每项票数需要登录）。"""

    choices: list[str] = field(default_factory=list)
    total_votes: int | None = None
    total_votes_text: str = ""


@dataclass
class YoutubePost:
    post_id: str
    text: str = ""
    author_name: str = ""
    author_handle: str = ""
    channel_id: str = ""
    published_at: datetime | None = None
    like_count: int | None = None
    hashtags: list[str] = field(default_factory=list)
    images: list[YoutubePostImage] = field(default_factory=list)
    video: YoutubePostVideo | None = None
    poll: YoutubePostPoll | None = None
    text_runs: tuple[TextRun, ...] = ()


def post_id_from_url(url: str) -> str:
    """从链接里取帖子 ID；不是帖子链接时返回空串。"""
    match = POST_URL_RE.search(url or "")
    return match.group(1) if match else ""


def _extract_json_object(text: str, start: int) -> dict[str, Any] | None:
    """从 ``start``（指向 ``{``）开始按**花括号配平**取一个 JSON 对象。

    不用正则匹配到 ``};</script>``: 页面里这类内联数据的结尾形态会变, 配平法只看括号,
    字符串与转义都跳过, 与 yt-dlp 之外的实现无关。
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : index + 1])
                except ValueError:
                    return None
    return None


def _yt_initial_data(html: str) -> dict[str, Any] | None:
    index = html.find(_YT_INITIAL_DATA_MARKER)
    if index < 0:
        return None
    brace = html.find("{", index + len(_YT_INITIAL_DATA_MARKER))
    if brace < 0:
        return None
    return _extract_json_object(html, brace)


def _find_first(node: Any, key: str) -> Any:
    """深度优先找第一个出现的 ``key``（帖子数据嵌在很深的固定层级里）。"""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if key in current:
                return current[key]
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return None


_URL_ISH_RE = re.compile(r"^(?:https?://|www\.)", re.I)

#: YouTube 的外链包一层跳转: ``youtube.com/redirect?…&q=<URL 编码的真实地址>``。
_REDIRECT_HOSTS = ("www.youtube.com", "youtube.com", "m.youtube.com")


def _unwrap_redirect(url: str) -> str:
    """把 YouTube 的 ``/redirect?…&q=<真实地址>`` 还原成真实地址（去掉追踪 token）。

    不是 redirect 形态时原样返回。
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return url
    if parsed.netloc not in _REDIRECT_HOSTS or not parsed.path.startswith("/redirect"):
        return url
    target = urllib.parse.parse_qs(parsed.query).get("q")
    if not target or not target[0]:
        return url
    return target[0]


def _run_link_url(run: dict[str, Any]) -> str:
    """从一个 run 的 ``navigationEndpoint`` 取**真实链接**。

    YouTube 会把 run 的**显示文本**截断（``https://www.youtube.com/playlist?list...``），
    但完整地址在同一条 run 里，有三种载体（按出现顺序取）:

    1. ``urlEndpoint.url`` —— 外链，通常包一层 ``youtube.com/redirect?...&q=<真实地址>``
    2. ``commandMetadata.webCommandMetadata.url`` —— 同上的另一种出现形态
    3. ``browseEndpoint.canonicalBaseUrl`` —— 站内路径（``/playlist?list=…``）

    ⚠️ 三个载体都可能是**相对路径**（``/playlist?list=…``），所以最后统一补域名 ——
    实测 run[1] 的 ② 就是相对路径，只在 ② 返回会导致正文里出现 ``/playlist?list=…``
    这种半截链接。

    取不到返回空串（调用方保持原显示文本）。
    """
    endpoint = run.get("navigationEndpoint")
    if not isinstance(endpoint, dict):
        return ""
    url = (endpoint.get("urlEndpoint") or {}).get("url")
    if not url:
        commands = endpoint.get("commandMetadata") or {}
        url = (commands.get("webCommandMetadata") or {}).get("url")
    if not url:
        url = (endpoint.get("browseEndpoint") or {}).get("canonicalBaseUrl")
    if not url:
        return ""
    text = str(url).strip()
    if text.startswith("/"):
        # 站内相对路径（/playlist?list=…、/hashtag/…）—— 补域名为绝对地址
        return f"https://www.youtube.com{text}"
    return _unwrap_redirect(text)


def _text_of(node: Any) -> str:
    """``{"runs": [{"text": ...}]}`` 或 ``{"simpleText": ...}`` → 纯文本。

    **显示文本被平台截断时**（``…list...`` / ``…/status/21...``），用 HTML 锚点把**原显示文本**
    当成链接文字、`href` 指向 navigationEndpoint 里的完整地址 —— **保留平台原本的样子**，
    只是让它可以点、且点到的是真实目标（用户要求：「保留原格式，省略号但是链接是完整的」）。

    用 HTML ``<a>`` 而不是 markdown 链接: 正文既可能落在引用块里（块内 markdown 行内语法不解析），
    也可能落在正文，HTML 标签两处都有效（与 bilibili 的标签处理同一形态）。渲染层本来就跳过
    ``<a>`` 段内的标签链接化，并专门保护 href 里的 markdown 定界符。

    只对 ``^https?://`` / ``^www.`` 开头的 run 生效, 所以 ``#hashtag`` 不受影响（它有独立的
    渲染通道, 且渲染层按名字做链接化）。文本与真实地址**相同**时不做任何包装（未截断的链接
    保持原有行为）。
    """
    if not isinstance(node, dict):
        return ""
    if isinstance(node.get("simpleText"), str):
        return str(node["simpleText"])
    runs = node.get("runs")
    if not isinstance(runs, list):
        return ""
    parts: list[str] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        text = str(run.get("text", ""))
        if _URL_ISH_RE.match(text.strip()):
            real = _run_link_url(run)
            if real and real != text.strip():
                text = f'<a href="{html.escape(real, quote=True)}">{html.escape(text)}</a>'
        parts.append(text)
    return "".join(parts)


def _source_runs(node: Any) -> tuple[TextRun, ...]:
    if not isinstance(node, dict):
        return ()
    if isinstance(text := node.get("simpleText"), str):
        return (TextRun(text),) if text else ()
    if not isinstance(source := node.get("runs"), list):
        return ()
    runs: list[TextRun] = []
    for run in source:
        if not isinstance(run, dict) or not isinstance(text := run.get("text"), str) or not text:
            continue
        try:
            url = _unwrap_redirect(_run_link_url(run))
            target = urllib.parse.urlsplit(url)
            if target.scheme not in {"http", "https"} or not target.netloc:
                url = ""
        except ValueError:
            url = ""
        runs.append(TextRun(text, url))
    return tuple(runs)


_COMPACT_COUNT_RE = re.compile(r"\s*([\d.,]+)\s*([KMB]?)", re.I)


def _compact_count(text: str) -> int | None:
    """``"1.1K"`` / ``"1.8M"`` / ``"9"`` → int（YouTube 的点赞与票数是缩写形式）。"""
    match = _COMPACT_COUNT_RE.match(text or "")
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    factor = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[match.group(2).lower()]
    return int(number * factor)


def _image_from_renderer(renderer: Any) -> YoutubePostImage | None:
    if not isinstance(renderer, dict):
        return None
    thumbnails = (renderer.get("image") or {}).get("thumbnails")
    if not isinstance(thumbnails, list) or not thumbnails:
        return None
    biggest = max(thumbnails, key=lambda t: int(t.get("width") or 0))
    smallest = min(thumbnails, key=lambda t: int(t.get("width") or 0))
    url = trim_cover(str(biggest.get("url") or ""))
    if not url:
        return None
    return YoutubePostImage(
        url=url,
        thumb_url=str(smallest.get("url") or url),
        width=int(biggest.get("width") or 0),
        height=int(biggest.get("height") or 0),
    )


def _collect_attachment(
    attachment: Any,
) -> tuple[list[YoutubePostImage], YoutubePostVideo | None, YoutubePostPoll | None]:
    images: list[YoutubePostImage] = []
    video: YoutubePostVideo | None = None
    poll: YoutubePostPoll | None = None
    if not isinstance(attachment, dict):
        return images, video, poll

    if single := attachment.get("backstageImageRenderer"):
        if image := _image_from_renderer(single):
            images.append(image)

    multi = (attachment.get("postMultiImageRenderer") or {}).get("images")
    for item in multi if isinstance(multi, list) else []:
        if isinstance(item, dict):
            if image := _image_from_renderer(item.get("backstageImageRenderer")):
                images.append(image)

    if renderer := attachment.get("videoRenderer"):
        video_id = str(renderer.get("videoId") or "")
        thumbnails = (renderer.get("thumbnail") or {}).get("thumbnails") or []
        cover_url = ""
        if thumbnails:
            biggest = max(thumbnails, key=lambda t: int(t.get("width") or 0))
            cover_url = str(biggest.get("url") or "")
        if video_id:
            video = YoutubePostVideo(
                video_id=video_id,
                title=_text_of(renderer.get("title")).strip(),
                cover_url=cover_url,
                title_runs=_source_runs(renderer.get("title")),
            )

    if renderer := attachment.get("pollRenderer"):
        choices = [
            _text_of(choice.get("text")).strip()
            for choice in (renderer.get("choices") or [])
            if isinstance(choice, dict)
        ]
        choices = [choice for choice in choices if choice]
        total_text = str((renderer.get("totalVotes") or {}).get("simpleText") or "")
        if choices:
            poll = YoutubePostPoll(
                choices=choices,
                total_votes=_compact_count(total_text),
                total_votes_text=total_text,
            )
    return images, video, poll


def _parse_hashtags(text_runs: Any) -> list[str]:
    """正文里的标签（``navigationEndpoint.browseEndpoint.browseId`` 为 ``FEhashtag``）。

    名字取 run 的文本（``#信者ゼロ``）去掉 ``#`` —— 标签页的 ``params`` 是编码后的
    二进制, 解不出名字；文本本身就是用户看到的标签。
    """
    tags: list[str] = []
    for run in text_runs if isinstance(text_runs, list) else []:
        if not isinstance(run, dict):
            continue
        browse = ((run.get("navigationEndpoint") or {}).get("browseEndpoint")) or {}
        if browse.get("browseId") != _HASHTAG_BROWSE_ID:
            continue
        name = str(run.get("text") or "").strip().lstrip("#").strip()
        if name and name not in tags:
            tags.append(name)
    return tags


def _post_renderer(data: dict[str, Any]) -> dict[str, Any] | None:
    thread = _find_first(data, "backstagePostThreadRenderer")
    if isinstance(thread, dict):
        renderer = (thread.get("post") or {}).get("backstagePostRenderer")
        if isinstance(renderer, dict):
            return renderer
    fallback = _find_first(data, "backstagePostRenderer")
    return fallback if isinstance(fallback, dict) else None


def parse_post_page(html: str, *, url: str = "") -> YoutubePost:
    """把帖子页面的 HTML 解析成 ``YoutubePost``（纯函数, 便于离线测试）。"""
    data = _yt_initial_data(html)
    if data is None:
        raise YoutubePostError("页面里没有 ytInitialData（可能被地区限制或需要登录）")
    renderer = _post_renderer(data)
    if renderer is None:
        raise YoutubePostError("帖子不存在或已删除")

    post_id = str(renderer.get("postId") or post_id_from_url(url))
    author_endpoint = renderer.get("authorEndpoint") or {}
    browse = author_endpoint.get("browseEndpoint") or {}
    canonical = str(browse.get("canonicalBaseUrl") or "")
    handle = canonical.rstrip("/").rsplit("/", 1)[-1].lstrip("@") if canonical else ""
    if not handle:
        handle = str(browse.get("browseId") or "")

    published_at = None
    if match := _DATE_PUBLISHED_RE.search(html):
        published_at = to_datetime(match.group(1))

    runs = (renderer.get("contentText") or {}).get("runs")
    images, video, poll = _collect_attachment(renderer.get("backstageAttachment"))

    return YoutubePost(
        post_id=post_id,
        text=_text_of(renderer.get("contentText")),
        author_name=_text_of(renderer.get("authorText")).strip(),
        author_handle=handle,
        channel_id=str(browse.get("browseId") or ""),
        published_at=published_at,
        like_count=_compact_count(str((renderer.get("voteCount") or {}).get("simpleText") or "")),
        hashtags=_parse_hashtags(runs),
        images=images,
        video=video,
        poll=poll,
        text_runs=_source_runs(renderer.get("contentText")),
    )


async def fetch_post(
    url: str,
    *,
    proxy: str | None = None,
    cookie: dict[str, str] | None = None,
) -> YoutubePost:
    """抓帖子页面并解析。失败抛 ``YoutubePostError``。"""
    async with http.AsyncClient(
        proxy=proxy, cookies=cookie, timeout=30, follow_redirects=True
    ) as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        raise YoutubePostError(f"获取帖子页面失败: HTTP {resp.status_code}")
    return parse_post_page(resp.text or "", url=url)


__all__ = [
    "YoutubeCard",
    "YoutubePost",
    "YoutubePostError",
    "YoutubePostImage",
    "YoutubePostPoll",
    "YoutubePostVideo",
    "fetch_card",
    "fetch_post",
    "find_youtube_links",
    "parse_post_page",
    "post_id_from_url",
    "trim_cover",
]
