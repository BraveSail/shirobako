from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

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
        snippet = cls._snippet_text(post)
        # 文字级遮罩: threads 的 `styling_info.is_spoiler` 标在某一片段上
        # (用户报「识别不到遮罩文字」——那条帖子里有一整行是遮罩的)。
        # 映射成富文本的行内遮罩语法 `||…||` (实测服务端解析成 RichTextSpoiler)。
        # caption 与 snippet 两处的片段都要看（长文里也可能有遮罩行）。
        content = cls._apply_text_spoilers(content, post)
        if snippet:
            snippet = cls._apply_text_spoilers(snippet, post)
        # taken_at 是 unix 秒; 浏览量不在该 GraphQL 响应里 (页面上的 views 另走接口), 因此留 None
        return cls(
            content=content,
            media=cls._fetch_media(post),
            author_name=get_author_name(post.get("user")),
            author_handle=str((post.get("user") or {}).get("username") or ""),
            published_at=to_datetime(post.get("taken_at")),
            view_count=to_int(post.get("view_count")),
            like_count=to_int(post.get("like_count")),
            snippet=snippet,
            poll=cls._parse_poll(post),
        )

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
    def _snippet_text(post: dict[str, Any]) -> str:
        """帖子附带的**长文块**（caption 底下的"阅读更多"内容）。

        实测（2026-10-10）: 长文在
        ``text_post_app_info.snippet_attachment_info.text_fragments.fragments[].plaintext``,
        而 ``caption.text`` 只是开头一句（页面上 6 行截断 + "Read more"）。匿名请求该字段
        为 ``null``（登录态/cookie 的 GraphQL 才返回）⇒ 拿不到就返回空串, 行为与之前一致。
        """
        tpa = post.get("text_post_app_info") or {}
        snippet_info = tpa.get("snippet_attachment_info") or {}
        if not isinstance(snippet_info, dict):
            return ""
        fragments = (snippet_info.get("text_fragments") or {}).get("fragments") or []
        parts = [str(f.get("plaintext") or "") for f in fragments if isinstance(f, dict)]
        return "\n\n".join(p for p in parts if p).strip()

    @staticmethod
    def _apply_text_spoilers(content: str, post: dict[str, Any]) -> str:
        """把 ``text_fragments`` 里被标为遮罩的文字包成 ``||…||``。

        ``caption.text`` 是**不带遮罩标记的纯文字**，遮罩信息只在
        ``text_post_app_info.text_fragments.fragments[].styling_info.is_spoiler`` ——
        只看 caption 就会把遮罩文字当普通文字发出去（这就是用户报的问题）。

        按 ``plaintext`` 在正文里定位并逐个包裹（逐次从上次结束处往后找，避免同一段文字
        被重复包裹；找不到就跳过，**绝不臆造位置**）。

        caption 与 snippet（长文块）两处片段都看 —— 内容可能出现在任意一处。
        """
        if not content:
            return content
        tpa = post.get("text_post_app_info") or {}
        sources = [
            tpa.get("text_fragments"),
            (tpa.get("snippet_attachment_info") or {}).get("text_fragments"),
        ]
        fragments = [f for s in sources if isinstance(s, dict) for f in (s.get("fragments") or [])]
        out = content
        cursor = 0
        for fragment in fragments:
            if not isinstance(fragment, dict):
                continue
            styling = fragment.get("styling_info") or {}
            if not styling.get("is_spoiler"):
                continue
            text = fragment.get("plaintext")
            if not text:
                continue
            start = out.find(text, cursor)
            if start < 0:
                continue
            end = start + len(text)
            out = f"{out[:start]}||{text}||{out[end:]}"
            cursor = end + 4  # 让过刚插入的 ||
        return out

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
