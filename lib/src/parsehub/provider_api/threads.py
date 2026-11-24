from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from ..types.document import TextRun
from ..utils import http
from ..utils.helpers import UA, get_author_name, to_datetime, to_int


class ThreadsAPIError(Exception):
    """Threads API 相关错误"""


class ThreadsAPI:
    GRAPHQL_URL = "https://www.threads.com/graphql/query"
    THREADS_URL = "https://www.threads.com/"
    # BarcelonaPostPageDirectQuery, 通过帖子 ID 获取帖子内容
    POST_DOC_ID = "27419285281047858"
    X_IG_APP_ID = "238260118697367"
    # shortcode <-> pk 使用与 Instagram 相同的 base64 字母表
    SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

    # Threads 与 Instagram 共用 Meta 的登录后端, 因此使用同一套 Cookie 键
    DEFAULT_COOKIES = {
        "sessionid": "",
        "ds_user_id": "",
        "csrftoken": "",
        "mid": "",
        "ig_did": "",
    }

    # GraphQL 查询强制要求的 relay provider 变量: 必须全部传入 (值统一给 False 即可),
    # 缺失或只传部分都会导致查询返回 execution error.
    # 与 Instagram 的 doc_id 一样, 该列表会随 Threads 前端更新而变化, 需要时同步维护.
    RELAY_PROVIDERS = (
        "BarcelonaHasPermalinkIndentation",
        "BarcelonaIsLoggedIn",
        "BarcelonaHasPostAuthorNotifControls",
        "BarcelonaShouldShowFediverseM1Features",
        "BarcelonaHasPermalinkPodcastCard",
        "BarcelonaHasDearAlgoConsumption",
        "BarcelonaHasEventBadge",
        "BarcelonaGenAIRepliesEnabled",
        "BarcelonaIsSearchDiscoveryEnabled",
        "BarcelonaHasCommunities",
        "BarcelonaHasGameScoreShare",
        "BarcelonaHasPublicViewCountCard",
        "BarcelonaHasCommunityEntityCard",
        "BarcelonaHasScorecardCommunity",
        "BarcelonaHasSportTeamAllegianceCard",
        "BarcelonaHasMusic",
        "BarcelonaHasNewspaperLinkStyle",
        "BarcelonaHasMessaging",
        "BarcelonaHasPodcastTextFragments",
        "BarcelonaShouldFulfillLightboxQuery",
        "BarcelonaHasViewerReplied",
        "BarcelonaHasPrivateRepliesDeprecation",
        "BarcelonaHasGhostPostEmojiActivation",
        "BarcelonaOptionalCookiesEnabled",
        "BarcelonaHasDearAlgoWebProduction",
        "BarcelonaHasWebFavicons",
        "BarcelonaIsCrawler",
        "BarcelonaHasCommunityTopContributors",
        "BarcelonaCanSeeSponsoredContent",
        "BarcelonaShouldShowFediverseM075Features",
        "BarcelonaIsInternalUser",
    )

    def __init__(
        self,
        proxy: str | None = None,
        cookie: dict[str, str] | None = None,
        timeout: float = 30,
    ):
        self.proxy = proxy
        self.cookie = cookie or {}
        self.timeout = timeout

    async def parse(self, url: str) -> ThreadsPost:
        code = self.get_post_id_by_url(url)
        payload = await self._post_graphql(
            doc_id=self.POST_DOC_ID,
            variables=self._build_variables(code),
        )
        post, reply_to = self._extract_post(payload, code)
        if post is None:
            raise ThreadsAPIError("Fetching Post metadata failed.")
        result = ThreadsPost.from_graphql(post)
        if not result.author_name:
            try:
                result.author_name = self.get_username_by_url(url).removeprefix("@")
            except ValueError:
                pass
        if reply_to is not None:
            try:
                result.reply_to = ThreadsPost.from_graphql(reply_to)
            except Exception:  # noqa: BLE001 - 父帖解析失败不应阻断主帖
                pass
        return result

    def _build_variables(self, code: str) -> dict[str, Any]:
        variables: dict[str, Any] = {"postID": str(self.shortcode_to_pk(code))}
        for name in self.RELAY_PROVIDERS:
            variables[f"__relay_internal__pv__{name}relayprovider"] = False
        return variables

    async def _post_graphql(self, *, doc_id: str, variables: dict[str, Any]) -> dict[str, Any]:
        async with self._new_client() as client:
            await self._ensure_csrf_token(client)
            data = {
                "variables": json.dumps(variables, separators=(",", ":")),
                "doc_id": doc_id,
                "server_timestamps": "true",
            }
            try:
                response = await client.post(self.GRAPHQL_URL, data=data, follow_redirects=False)
            except http.HTTPError as exc:
                raise ThreadsAPIError(f"请求 Threads GraphQL 失败: {exc}") from exc

        if response.status_code != 200:
            raise ThreadsAPIError(f"Threads GraphQL 返回 HTTP {response.status_code}: {response.text[:500]}")

        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            # 未登录时会返回 HTML 登录页
            raise ThreadsAPIError("Threads GraphQL 返回非 JSON 响应(可能需要登录)") from exc

        if payload.get("errors"):
            raise ThreadsAPIError(f"Threads GraphQL 返回错误: {payload['errors']}")
        return payload

    @staticmethod
    def _extract_post(payload: dict[str, Any], code: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """返回 (目标帖, 被回复的父帖); 找不到精确匹配时退回第一条.

        回复链与目标帖同处一个 thread_items 数组, 且按时间顺序排列;
        目标帖紧邻的前一条即它回复的对象 (多层链中取直接回复对象).
        仅当目标帖本身是回复 (is_reply) 时, 前一条才视为被回复帖.
        """
        data = ((payload.get("data") or {}).get("data")) or {}
        edges = data.get("edges") or []
        fallback: dict[str, Any] | None = None
        for edge in edges:
            items = (edge.get("node") or {}).get("thread_items") or []
            prev: dict[str, Any] | None = None
            for item in items:
                post = item.get("post")
                if not isinstance(post, dict):
                    continue
                if fallback is None:
                    fallback = post
                if post.get("code") == code:
                    is_reply = bool((post.get("text_post_app_info") or {}).get("is_reply"))
                    return post, (prev if is_reply else None)
                prev = post
        # 找不到精确匹配时退回第一条 (通常即目标帖子本身)
        return fallback, None

    def _new_client(self) -> http.AsyncClient:
        cookies = self.DEFAULT_COOKIES | self.cookie
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": self.THREADS_URL,
            "User-Agent": UA,
            "X-IG-App-ID": self.X_IG_APP_ID,
        }
        return http.AsyncClient(
            cookies=cookies,
            headers=headers,
            proxy=self.proxy,
            timeout=self.timeout,
        )

    async def _ensure_csrf_token(self, client: http.AsyncClient) -> None:
        csrf_token = self._get_cookie_value(client, "csrftoken")
        if not csrf_token:
            try:
                await client.get(self.THREADS_URL, follow_redirects=True)
            except http.HTTPError as exc:
                raise ThreadsAPIError(f"获取 Threads csrftoken 失败: {exc}") from exc
            csrf_token = self._get_cookie_value(client, "csrftoken")
        if csrf_token:
            client.headers["x-csrftoken"] = csrf_token

    @staticmethod
    def _get_cookie_value(client: http.AsyncClient, name: str) -> str:
        values = [cookie.value for cookie in client.cookies.jar if cookie.name == name and cookie.value]
        return values[-1] if values else ""

    @classmethod
    def shortcode_to_pk(cls, code: str) -> int:
        pk = 0
        for ch in code:
            try:
                pk = pk * 64 + cls.SHORTCODE_ALPHABET.index(ch)
            except ValueError as exc:
                raise ValueError(f"无效的 Threads 帖子 ID: {code}") from exc
        return pk

    @staticmethod
    def get_username_by_url(url: str) -> str:
        u = re.search(r"/(@[\w.]+)/post/", url)
        if not u:
            raise ValueError("从 URL 中获取用户名失败")
        return u[1]

    @staticmethod
    def get_post_id_by_url(url: str) -> str:
        p = re.search(r"/post/([\w-]+)", url)
        if not p:
            raise ValueError("从 URL 中获取帖子 ID 失败")
        return p[1]


class ThreadsMediaType(Enum):
    IMAGE = "image"
    VIDEO = "video"


@dataclass
class ThreadsMedia:
    type: ThreadsMediaType
    url: str
    thumb_url: str | None = None
    width: int = 0
    height: int = 0


@dataclass
class ThreadsPoll:
    """帖子附带的投票（``caption_add_on.poll`` 里的 ``XDTPollSticker``）。

    实测结构（2026-10-12, 真实响应见 ``test/fixtures/threads_poll.json``）::

        post["caption_add_on"]["poll"] = {
            "__typename": "XDTPollSticker",
            "tallies": [{"count": 11, "text": "あいはんする"},
                        {"count": 8,  "text": "そうはんする"}],
            "finished": false,
            "viewer_can_vote": false,
            ...
        }

    票数就在同一次响应里（``tallies[].count``）—— 不需要额外请求。
    """

    choices: list[tuple[str, int]]
    is_finished: bool = False


@dataclass
class ThreadsPost:
    content: str
    media: ThreadsMedia | list[ThreadsMedia] | None = None
    author_name: str = ""
    author_handle: str = ""
    reply_to: ThreadsPost | None = None
    published_at: datetime | None = None
    view_count: int | None = None
    like_count: int | None = None
    #: 帖子附带的**长文块**（caption 底下的"阅读更多"内容）—— 与 caption 分开存，
    #: 让 parser 决定它的渲染形态（现在是引用块，见 threads parser）。
    snippet: str = ""
    #: 帖子附带的**投票**（caption 下方的贴纸）—— 渲染形态（表格）由 parser 决定。
    poll: ThreadsPoll | None = None
    #: 帖子所属的**话题**（``text_post_app_info.tag_header.display_name``）——
    #: 页面上显示在帖子顶部。渲染形态由 parser 决定（现在是归属行：标题与作者之间）。
    topic: str = ""
    caption_runs: tuple[TextRun, ...] | None = None
    snippet_runs: tuple[TextRun, ...] | None = None

    @classmethod
    def from_graphql(cls, post: dict[str, Any]) -> ThreadsPost:
        caption = post.get("caption")
        content = caption.get("text") if isinstance(caption, dict) else caption
        content = str(content or "")
        # 帖子"下方内容"（caption 底下的长文块）: threads 把附加长文放在
        # ``snippet_attachment_info`` 里, 而 ``caption.text`` 只是开头一句 ——
        # 页面上表现为 6 行截断 + "Read more"。不读它就会静默丢正文的后半段
        # 甚至只丢那一句之外的全部（用户报「抓不到下方内容」）。
        # 与 caption **分开存**（``snippet`` 字段）：渲染形态（引用块）由 parser 决定，
        # provider 只负责把两块文字都取出来。
        info = post.get("text_post_app_info") or {}
        caption_runs = cls._caption_runs(content, cls._fragments(info))
        snippet_runs = cls._snippet_runs(cls._fragments(info.get("snippet_attachment_info")))
        # taken_at 是 unix 秒; 浏览量不在该 GraphQL 响应里 (页面上的 views 另走接口), 因此留 None
        return cls(
            content=cls._legacy_text(caption_runs),
            media=cls._fetch_media(post),
            author_name=get_author_name(post.get("user")),
            author_handle=str((post.get("user") or {}).get("username") or ""),
            published_at=to_datetime(post.get("taken_at")),
            view_count=to_int(post.get("view_count")),
            like_count=to_int(post.get("like_count")),
            snippet=cls._legacy_text(snippet_runs).strip(),
            poll=cls._parse_poll(post),
            topic=cls._parse_topic(post),
            caption_runs=caption_runs,
            snippet_runs=snippet_runs,
        )

    @staticmethod
    def _parse_topic(post: dict[str, Any]) -> str:
        """帖子所属的**话题** —— ``text_post_app_info.tag_header.display_name``。

        实测结构（2026-10-20）::

            "tag_header": {"display_name": "申請審判", "id": "18626374978065524",
                           "is_community": false, "tag_cluster_name": "申請審判",
                           "community_emoji": null}

        没有话题时该字段是 ``null`` ⇒ 返回空串。取 ``display_name``（页面显示的那份），
        不取 ``tag_cluster_name``（实测两者相同，但前者才是给用户看的）。
        """
        header = (post.get("text_post_app_info") or {}).get("tag_header")
        if not isinstance(header, dict):
            return ""
        return str(header.get("display_name") or "").strip()

    @staticmethod
    def _parse_poll(post: dict[str, Any]) -> ThreadsPoll | None:
        """投票 —— 数据在 ``post["caption_add_on"]["poll"]`` 里, 不在 caption 上。

        与 ``snippet_attachment_info`` 同属"caption 底下的附加内容", 但键不同:
        投票是 ``caption_add_on.poll``（实测 ``__typename`` = ``XDTPollSticker``）。
        ``tallies`` 是 ``[{"count": N, "text": "选项"}]`` —— **票数就在同一次
        响应里**（不需要额外请求, 规则同 linux.do 的 ``post["polls"]``）。

        缺失票数按 0 算（选项本身仍要显示）；没有投票、结构异常时返回 ``None``。
        """
        add_on = post.get("caption_add_on")
        if not isinstance(add_on, dict):
            return None
        poll = add_on.get("poll")
        if not isinstance(poll, dict):
            return None
        tallies = poll.get("tallies") or []
        choices: list[tuple[str, int]] = []
        for item in tallies:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            choices.append((text, to_int(item.get("count")) or 0))
        if not choices:
            return None
        return ThreadsPoll(choices=choices, is_finished=bool(poll.get("finished")))

    @staticmethod
    def _fragments(info: object) -> tuple[TextRun, ...]:
        fragments = info.get("text_fragments") if isinstance(info, dict) else None
        if not isinstance(fragments, dict):
            return ()
        out = []
        for part in fragments.get("fragments") or ():
            if not isinstance(part, dict) or not isinstance(text := part.get("plaintext"), str) or not text:
                continue
            styling = part.get("styling_info") or {}
            styles = ("spoiler",) if isinstance(styling, dict) and styling.get("is_spoiler") else ()
            out.append(TextRun(text, styles=styles))
        return tuple(out)

    @staticmethod
    def _caption_runs(content: str, fragments: tuple[TextRun, ...]) -> tuple[TextRun, ...]:
        """Align all fragments, not only spoilers, so repeated words keep their source styling."""
        out = []
        cursor = plain_start = 0
        for fragment in fragments:
            start = content.find(fragment.text, cursor)
            if start < 0:
                continue
            cursor = start + len(fragment.text)
            if fragment.styles:
                if plain_start < start:
                    out.append(TextRun(content[plain_start:start]))
                out.append(fragment)
                plain_start = cursor
        if plain_start < len(content):
            out.append(TextRun(content[plain_start:]))
        return tuple(out)

    @staticmethod
    def _snippet_runs(fragments: tuple[TextRun, ...]) -> tuple[TextRun, ...]:
        return tuple(run for i, fragment in enumerate(fragments)
                     for run in ((TextRun("\n\n"), fragment) if i else (fragment,)))

    @staticmethod
    def _legacy_text(runs: tuple[TextRun, ...]) -> str:
        return "".join(f"||{run.text}||" if "spoiler" in run.styles else run.text for run in runs)

    @classmethod
    def _fetch_media(cls, d: dict[str, Any]) -> ThreadsMedia | list[ThreadsMedia]:
        media: ThreadsMedia | list[ThreadsMedia]
        match d.get("media_type"):
            case 1:  # 单张图片
                image = d["image_versions2"]["candidates"][0]
                media = ThreadsMedia(
                    type=ThreadsMediaType.IMAGE,
                    url=image["url"],
                    thumb_url=image["url"],
                    width=image.get("width", 0),
                    height=image.get("height", 0),
                )
            case 2:  # 单个视频
                thumb = d["image_versions2"]["candidates"][0]["url"]
                video = d["video_versions"][0]["url"]
                media = ThreadsMedia(
                    type=ThreadsMediaType.VIDEO,
                    url=video,
                    thumb_url=thumb,
                    width=d.get("original_width", 0),
                    height=d.get("original_height", 0),
                )
            case 8:  # 多图/视频
                media = []
                for m in d.get("carousel_media") or []:
                    if m.get("video_versions"):
                        thumb = m["image_versions2"]["candidates"][0]["url"]
                        media.append(
                            ThreadsMedia(
                                type=ThreadsMediaType.VIDEO,
                                url=m["video_versions"][0]["url"],
                                thumb_url=thumb,
                                width=m.get("original_width", 0),
                                height=m.get("original_height", 0),
                            )
                        )
                    else:
                        image = m["image_versions2"]["candidates"][0]
                        media.append(
                            ThreadsMedia(
                                type=ThreadsMediaType.IMAGE,
                                url=image["url"],
                                thumb_url=image["url"],
                                width=m.get("original_width", 0),
                                height=m.get("original_height", 0),
                            )
                        )
            case 19:  # 纯文本/外部链接
                linked = (d.get("text_post_app_info") or {}).get("linked_inline_media")
                media = cls._fetch_media(linked) if linked else []
            case _:
                media = []
        return media


__all__ = [
    "ThreadsAPI",
    "ThreadsAPIError",
    "ThreadsMedia",
    "ThreadsMediaType",
    "ThreadsPoll",
    "ThreadsPost",
]
