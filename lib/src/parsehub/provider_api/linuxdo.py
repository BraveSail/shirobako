"""linux.do（Discourse 论坛）解析。

数据走 Discourse 自带的 JSON 接口: ``/t/topic/<id>.json``（比抓 HTML 稳）。

⚠️ 该站点前置 Cloudflare，**必须同时满足两点**才能拿到数据（2026-10 实测）：

1. **浏览器 TLS 指纹** —— 本项目统一走 ``parsehub.utils.http``（curl_cffi, chrome150）。
   实测同一份 cookie 下 httpx 得到 403 挑战页、curl_cffi 得到 200。
2. **携带 cookie** —— 至少要有 ``cf_clearance``（Cloudflare 放行票）与 ``_forum_session``
   （Discourse 会话）。请在 ``platform_config.yaml`` 的 ``platforms.linuxdo.cookies`` 里配置
   浏览器里复制的完整 Cookie 头；``cf_clearance`` 会过期，失效后表现为 403。

不需要 cookie 的接口不存在 —— 匿名访问同样被 Cloudflare 拦。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup
from loguru import logger
from markdownify import MarkdownConverter

from ..types.platform import Platform
from ..utils import http
from ..utils.helpers import (
    MEDIA_PLACEHOLDER_RE,
    format_author_link,
    format_quote_block,
    media_placeholder,
    profile_url,
    to_int,
)

TOPIC_API = "https://linux.do/t/topic/{topic_id}.json"
#: 按 post id **精确**取指定楼层 —— 体积比楼层窗口小一个数量级 (实测单层 4.5KB vs 窗口 54KB)
POSTS_API = "https://linux.do/t/{topic_id}/posts.json?post_ids[]={post_id}"

# ⛔ **不要**用带楼层号的 ``/t/<id>/<n>.json``（那是"以该层为中心的 20 层窗口"）:
# 它不给全部楼层、主楼常在窗口外, 且一次要下 50KB+。要哪一层就用 ``POSTS_API`` 按 id 取。

#: /t/topic/<id>[/<楼层>]、/t/<slug>/<id>[/<楼层>]、/t/<id>[/<楼层>]
#: slug 不以数字开头, 否则 /t/2979226/11 会被误当成 slug=2979226、id=11
_TOPIC_URL_RE = re.compile(r"^https?://linux\.do/t/(?:[^\d/][^/]*/)?(\d+)(?:/(\d+))?")


class LinuxDoError(Exception):
    """linux.do 解析失败。"""


@dataclass
class LinuxDoImage:
    """帖子里的图片。"""

    url: str
    width: int = 0
    height: int = 0


@dataclass
class LinuxDoPollOption:
    """投票的一个选项。"""

    text: str
    count: int | None = None
    """该选项票数; None = 拿不到结构化数据 (老站点) 或平台不给单项票数。"""


@dataclass
class LinuxDoPoll:
    """帖子里的一个投票 (**结构化**, 不含任何拼好的文案)。

    lib 在正文对应位置只放占位符行 ``{{poll:N}}``; 「选项/票数/占比/人参与」那套
    文案由渲染层按用户语言拼装 (索引 N 与 ``LinuxDoTopic.polls`` 一致)。
    """

    options: list[LinuxDoPollOption] = field(default_factory=list)
    total_votes: int | None = None
    voters: int | None = None
    title: str = ""


@dataclass
class LinuxDoTopic:
    """一个主题（楼主帖 + 主题级统计）。"""

    topic_id: str = ""
    title: str = ""
    markdown_content: str = ""
    text_content: str = ""
    author_name: str = ""
    author_handle: str = ""
    published_at: str = ""
    view_count: int | None = None
    like_count: int | None = None
    reply_count: int | None = None
    tags: list[str] = field(default_factory=list)
    images: list[LinuxDoImage] = field(default_factory=list)
    #: 帖子里的**投票** (结构化, 不含任何拼好的文案), 顺序 = 正文里 ``{{poll:N}}``
    #: 占位符的出现顺序。渲染层按索引取数据再本地化。
    polls: list[LinuxDoPoll] = field(default_factory=list)
    #: images 末尾有多少张属于**上下文引用块** (主楼/被回复楼层), bot 侧据此把它们
    #: 放进引用块内部而不是正文媒体。块在正文**前** ⇒ 走 ``reply_media_count`` 那一档。
    reply_media_count: int = 0
    is_sensitive: bool = False
    #: **当前解析的这一层的楼层号** (主楼是 1)。渲染层用它给作者行标 ``#N`` ——
    #: 引用块里的其它层早就标了 (`_post_to_quote` 的 `` · #N``), 本层不标就不对称。
    post_number: int | None = None
    #: 上下文引用块的**角色** (按出现顺序)。它们排在正文**最前**、媒体算在
    #: ``reply_media_count`` 里 —— 声明出来渲染层就不用靠位置猜 (以前靠"两个块互相
    #: 兜底", 正文里一插行就可能错位)。
    quote_roles: list[str] = field(default_factory=list)

    @staticmethod
    def _split_url(url: str) -> tuple[str, str]:
        """返回 (话题 ID, 楼层号)；楼层号为空串表示没指定。"""
        if match := _TOPIC_URL_RE.match(url):
            return match.group(1), match.group(2) or ""
        raise LinuxDoError("暂不支持该 linux.do 链接, 目前仅支持话题页 (/t/<话题>/<id>)")

    @staticmethod
    def get_topic_id(url: str) -> str:
        return LinuxDoTopic._split_url(url)[0]

    @staticmethod
    def get_post_number(url: str) -> str:
        """URL 里指定的楼层号（``/t/topic/<id>/11`` → ``11``）；没指定返回空串。

        带楼层号时解析那一层（用户分享的常常是某个回复），不带则解析楼主帖。
        """
        return LinuxDoTopic._split_url(url)[1]

    @classmethod
    async def parse(
        cls,
        url: str,
        proxy: str | None = None,
        cookie: dict[str, str] | None = None,
    ) -> LinuxDoTopic:
        topic_id, post_number = cls._split_url(url)
        # **总是请求主题端点**（不带楼层号）: 它一次给齐三样 —— 主题元数据、``stream``
        # （话题里所有可见层的 post id, 有序, 按 id 取单层靠它）、以及**前 20 层**。
        # 需要的那一层若不在这 20 层里, 再按 id 精确取（见 ``_with_required_floors``）。
        #
        # 以前带楼层号时会请求 ``/t/<id>/<n>.json`` —— 那是"以该层为中心的 20 层窗口",
        # 主楼在靠后的楼层里会落到窗口外, 而且一次多下 50KB。现在统一成"主题 + 按 id 取"。
        api = TOPIC_API.format(topic_id=topic_id)
        try:
            async with http.AsyncClient(proxy=proxy, cookies=cookie, timeout=30) as client:
                response = await client.get(api, headers={"Accept": "application/json"})
        except http.HTTPError as e:
            raise LinuxDoError(f"请求 linux.do 失败: {e}") from e

        if response.status_code == 403:
            raise LinuxDoError("linux.do 需要有效的 cookie (含 cf_clearance), 请在平台配置里更新")
        if response.status_code == 404:
            raise LinuxDoError(f"话题 {topic_id} 不存在或不可见")
        response.raise_for_status()

        try:
            payload: dict[str, Any] = response.json()
        except Exception as e:  # noqa: BLE001 - 被 Cloudflare 拦时会返回 HTML
            raise LinuxDoError("linux.do 返回的不是 JSON, 多半是 Cloudflare 挑战页面 (cookie 失效或缺少指纹)") from e

        # 目标层或上下文层不在这 20 层里 -> 按 id 取回来
        payload = await cls._with_required_floors(
            payload, topic_id, post_number, proxy=proxy, cookie=cookie
        )
        return cls._from_payload(payload, topic_id, post_number=post_number)

    @staticmethod
    def _wanted_floor(post_number: str) -> int:
        """URL 里的楼层号 → int；没指定则是主楼（1）。"""
        return int(post_number) if post_number.isdigit() else 1

    @classmethod
    def _missing_context_floors(
        cls, posts: list[dict[str, Any]], wanted: int, reply_to: int | None
    ) -> list[int]:
        """手上**缺**的上下文楼层 (主楼 / 被回复的层), 需要另取。

        缺了不补的后果是**静默**的: ``_context_quotes`` 只是在手上的 posts 里找那一层,
        找不到就跳过, 「分享楼层时带上主楼」这条规则就悄悄失效了 (用户报「怎么没有主楼了」)。

        为什么要另取: 主题端点只回**前 20 层**, 而分享的楼层可以在很后面 (第 24 楼、第 50 楼),
        主楼/被回复的层根本不在这 20 层里。
        """
        present = {p.get("post_number") for p in posts}
        missing: list[int] = []
        for floor in (1, reply_to):
            if floor and floor != wanted and floor not in present and floor not in missing:
                missing.append(floor)
        return missing

    @staticmethod
    def _post_id_for_floor(payload: dict[str, Any], floor: int) -> int | None:
        """楼层号 → post id (话题的 ``post_stream.stream`` 就是**所有可见层的 id 列表**, 有序)。

        拿得到 id 就能按 id **精确**取那一层 (``POSTS_API``), 体积比楼层窗口小一个数量级
        (实测单层 4.5KB vs 窗口 53.6KB)。位置可能有偏差 (话题里删过层时 stream 会变短),
        所以调用方**必须校验取回来的 ``post_number``**。
        """
        stream = (payload.get("post_stream") or {}).get("stream") or []
        if 1 <= floor <= len(stream):
            return stream[floor - 1]
        return None

    @classmethod
    async def _fetch_floor(
        cls, client: Any, payload: dict[str, Any], topic_id: str, floor: int
    ) -> list[dict[str, Any]]:
        """取**某一层**的 posts —— **只走 ``posts.json?post_ids[]=<id>``**。

        一次只回那一层 (实测 4.5KB), 是按 id 精确取, 不会顺带拖回一堆用不上的楼层。

        取回来后**校验楼层号**: ``stream`` 的位置在话题删过层 (或被过滤) 时**会漂**,
        那时按 id 取回来的是另一层 —— 宁可不要这一段上下文, 也不能把别人的话当成
        "被回复的内容"渲染进引用块。
        """
        post_id = cls._post_id_for_floor(payload, floor)
        if post_id is None:
            raise LinuxDoError(f"话题的 stream 里没有第 {floor} 楼 (话题可能被删过层)")
        response = await client.get(
            POSTS_API.format(topic_id=topic_id, post_id=post_id),
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        posts = (response.json().get("post_stream") or {}).get("posts") or []
        if not any(p.get("post_number") == floor for p in posts):
            raise LinuxDoError(f"第 {floor} 楼的 id 取回来的是别的楼层 (stream 位置漂了)")
        return posts

    @classmethod
    async def _with_required_floors(
        cls,
        payload: dict[str, Any],
        topic_id: str,
        post_number: str,
        *,
        proxy: str | None = None,
        cookie: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """把**目标层与上下文层**取齐, 合进 ``payload`` 的帖子流。

        主题端点只给前 20 层, 而分享的楼层可以在很后面 —— 缺的按 id 精确取 (POSTS_API)。

        顺序上**必须先保证目标层在手**: 后面要取哪些上下文层, 取决于目标层的
        ``reply_to_post_number``, 而那个字段只有拿到了目标层才知道。

        用与主请求相同的条件 (proxy / cookie); 上下文层取不到只**记 warning**
        (少一个上下文块, 总好过整条打不开); 目标层取不到则由 ``_from_payload``
        报"话题里没有第 N 楼" —— 那是**必须**有的东西, 不能静默换一个层给用户。
        """
        stream = payload.get("post_stream") or {}
        posts: list[dict[str, Any]] = list(stream.get("posts") or [])
        wanted = cls._wanted_floor(post_number)

        # 目标层在手时也能看出该补哪些上下文层; 两者都不缺就一次请求都不发
        current = next((p for p in posts if p.get("post_number") == wanted), None)
        missing_context = cls._missing_context_floors(
            posts, wanted, (current or {}).get("reply_to_post_number")
        )
        if current is not None and not missing_context:
            return payload

        async with http.AsyncClient(proxy=proxy, cookies=cookie, timeout=30) as client:
            # ① 目标层 (它必须在, 后面的 reply_to 要从它读)
            if current is None:
                try:
                    posts.extend(await cls._fetch_floor(client, payload, topic_id, wanted))
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"linux.do 取第 {wanted} 楼失败: {e}")

            # ② 上下文层 (主楼 + 被回复的层)
            current = next((p for p in posts if p.get("post_number") == wanted), None)
            reply_to = (current or {}).get("reply_to_post_number")
            for floor in cls._missing_context_floors(posts, wanted, reply_to):
                try:
                    posts.extend(await cls._fetch_floor(client, payload, topic_id, floor))
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"linux.do 取第 {floor} 楼失败, 该层上下文将缺失: {e}")

        # 去重 (窗口与补取回来的两段会重叠) 并按楼层排序
        by_number: dict[int, dict[str, Any]] = {}
        for post in posts:
            number = post.get("post_number")
            if number is not None and number not in by_number:
                by_number[number] = post
        if by_number:
            stream["posts"] = [by_number[n] for n in sorted(by_number)]
            payload["post_stream"] = stream
        return payload

    @classmethod
    def _from_payload(cls, payload: dict[str, Any], topic_id: str, post_number: str = "") -> LinuxDoTopic:
        """组装主题。

        ``post_number`` 指定楼层时取那一层（用户分享的链接常带楼层号，那一层才是他要看的内容）；
        没指定则取楼主帖。带楼层号的 URL 返回的帖子流**不含 1 楼**，所以不能盲目回落到第一项 ——
        那会取到窗口里的其它楼层（症状：明明楼层里有图，解析出来没媒体）。
        """
        posts: list[dict[str, Any]] = (payload.get("post_stream") or {}).get("posts") or []
        wanted = int(post_number) if post_number.isdigit() else 1
        first = next((p for p in posts if p.get("post_number") == wanted), None)
        if first is None and post_number:
            raise LinuxDoError(f"话题 {topic_id} 里没有第 {post_number} 楼")
        if first is None:
            first = posts[0] if posts else None
        if first is None:
            raise LinuxDoError("话题里没有可解析的内容")

        cooked: str = first.get("cooked") or ""
        soup = BeautifulSoup(cooked, "lxml")
        # 投票: cooked 里的 poll 只是个**占位壳**（人数恒为 0），真实数据在同一次响应的
        # ``post["polls"]`` 结构化字段里（`options[].votes` / `voters`）—— 不需要额外请求。
        raw_polls = [p for p in (first.get("polls") or []) if isinstance(p, dict)]
        # 上下文引用块先算: 它们排在正文**最前**, 自带的投票先占占位符索引 (0..., 与
        # 正文出现顺序一致)。图片与纯文本兜底也在这里接上 (见下)。
        images = cls._extract_images(soup)
        # 图片 URL → 下标: ``_strip_lightbox`` 把正文里的图换成 ``{{media:N}}`` 原位标记，
        # 渲染层再换成媒体引用 —— 图出现在**文章原位**，而不是堆到消息末尾。
        # （本层图排在 media 列表最前，所以本层下标就是最终下标；上下文层的标记下标
        #  从 ``len(images)`` 起 —— 见 ``_context_quotes(image_offset=...)``。）
        context_quotes, context_images, context_polls = cls._context_quotes(
            posts, first, image_offset=len(images)
        )
        image_index = {i.url: n for n, i in enumerate(images)}
        # ⚠️ 必须在取纯文本**之前**清 lightbox: 它的包裹里除了 img 还有一段**说明文字**
        # (``span.filename`` 文件名 + ``span.informations`` 的"分辨率 体积", 如 ``746×330 24.3 KB``)。
        # 纯图楼层(markdown 为空)拿 text_content 当正文, 晚清理这些元信息就会跟图一起显示
        # (用户报「正文多了个 image 分辨率体积」)。
        cls._strip_lightbox(soup, image_index)
        # 纯文本兜底里不能留原位标记（那是 lib ↔ 渲染层的协议，不是给用户看的文字）
        text_content = MEDIA_PLACEHOLDER_RE.sub("", soup.get_text("\n", strip=True))
        # 引用块先抽出来 (改写成占位符), 转完 markdown 再回填公共 helper 渲染的结果
        rendered_quotes = cls._extract_quotes(soup)
        # 本层投票: ``_simplify`` 原地把 ``div.poll`` 改写成占位符行, 结构化数据
        # 由返回值带出来。索引从上下文之后继续 (它们在正文里排在上下文块之后)。
        pool = cls._simplify(soup, polls=raw_polls, offset=len(context_polls))
        polls = [*context_polls, *pool]
        markdown_content = cls._to_markdown(str(soup))
        for placeholder, block in rendered_quotes:
            markdown_content = markdown_content.replace(placeholder, block)

        # 上下文引用块放正文**之前**, 从远到近: 主楼 → 被回复的楼层 → 当前楼层正文
        # (用户要求: 分享楼层时把主楼做成回复; 楼层互回时被回复的那层也要带上)
        # 上下文层的图已在本层块内留原位标记 ⇒ 不再走"引用块媒体"尾部通道
        # （否则同一张图会出现两次：块内一次、块尾/消息尾一次）
        reply_media_count = 0
        if context_images:
            images = [*images, *context_images]
        if context_quotes:
            # strip 每个块: format_quote_block 末尾自带空行, 直接 join 会堆出多余空行
            parts = [*[q.strip() for q in context_quotes], markdown_content.strip()]
            markdown_content = "\n\n".join(p for p in parts if p)
            # 纯图楼层走 text_content 兜底, 上下文同样要带上（标记是协议, 不能给用户看）
            plain_parts = [
                *[MEDIA_PLACEHOLDER_RE.sub("", cls._quote_to_plain(q)) for q in context_quotes],
                text_content.strip(),
            ]
            text_content = "\n\n".join(p for p in plain_parts if p)

        tags = [str(t.get("name")) for t in (payload.get("tags") or []) if isinstance(t, dict) and t.get("name")]
        created_by = (payload.get("details") or {}).get("created_by") or {}
        # 作者只认**这一层**的字段; created_by 是主题创建者 (楼主), 指定楼层时用它
        # 会把楼层的作者显示成楼主 (用户报过同类问题: 时间/点赞也踩过)。只有解析的
        # 就是主楼时, 才允许回落到 created_by (楼层的 name 字段可能缺失)。
        is_opening = first.get("post_number") == 1
        author_handle = str(first.get("username") or (created_by.get("username") if is_opening else "") or "")
        author_name = str(
            first.get("name")
            or first.get("username")          # 楼层可能没有 name, 用 username 而不是楼主名
            or (created_by.get("name") if is_opening else "")
            or ""
        )

        return cls(
            topic_id=str(payload.get("id") or topic_id),
            title=str(payload.get("title") or payload.get("fancy_title") or ""),
            markdown_content=markdown_content,
            text_content=text_content,
            author_name=author_name,
            author_handle=author_handle,
            # 时间与点赞取**该楼层**的: 主题级 created_at / like_count 是楼主帖与全话题的,
            # 指定楼层时用它们会显示成别人的时间与赞数
            published_at=str(first.get("created_at") or payload.get("created_at") or ""),
            view_count=to_int(payload.get("views")),
            like_count=to_int(
                first.get("reaction_users_count")
                if first.get("reaction_users_count") is not None
                else payload.get("like_count")
            ),
            reply_count=to_int(payload.get("reply_count")),
            tags=tags,
            images=images,
            # 投票 (结构化): 顺序 = 正文里 ``{{poll:N}}`` 占位符的出现顺序
            # (上下文引用块里的在前, 本层的在后)。渲染层按索引本地化。
            polls=polls,
            reply_media_count=reply_media_count,
            # 上下文引用块的媒体已改为块内原位标记 ⇒ 不再声明 reply 角色
            # （声明了渲染层会去 reply 桶里找媒体，找不到就是静默降级）
            quote_roles=[],
            # 只认平台自己的标记: 标签里的 NSFW
            is_sensitive=any(t.casefold() == "nsfw" for t in tags),
            # 当前这一层的楼层号 —— 分享楼层时用它标出"这是第几楼"
            post_number=to_int(first.get("post_number")),
        )

    #: 引用块在正文里的占位标记 (转 markdown 后再替换成渲染结果)
    _QUOTE_PLACEHOLDER = "@@linuxdo-quote-{}@@"

    @classmethod
    def _post_to_quote(
        cls, post: dict[str, Any], poll_offset: int, image_offset: int = 0
    ) -> tuple[str, list[LinuxDoImage], list[LinuxDoPoll]]:
        """把**另一层**的内容渲染成引用块 + 它自己的图片 + 它自己的投票。

        返回 ``(引用块 markdown, 该层的图片, 该层的投票)``：

        - **该层的图片留原位标记**（``{{media:N}}``，``N`` 从 ``image_offset`` 起）——
          ``blockquote`` 是 ``blocks`` 形态、**能含图**（见 rich-rendering-pitfalls 的能力表），
          所以图直接出现在这一层正文的对应位置，而不是堆到消息末尾
          （用户报「这里面的图没在原位」）。
        - **纯图楼层也要返回引用块**(只有作者行 + 标记行): 这层确实在回应它,
          直接把整层丢掉是**有损**的 —— 用户看不到"在回复谁"。
        - 投票在引用块里放占位符行, 索引从 ``poll_offset`` 起 (上下文块在正文前 ⇒
          先占索引, 与「占位符出现顺序」一致)。
        """
        cooked = post.get("cooked") or ""
        username = str(post.get("username") or "")
        author = format_author_link(
            str(post.get("name") or ""), username, profile_url(Platform.LINUXDO, username)
        )
        floor = post.get("post_number")
        head = f"{author} · #{floor}" if floor else author
        if not cooked:
            return "", [], []

        soup = BeautifulSoup(cooked, "lxml")
        images = cls._extract_images(soup)  # 图片先取出来 (下标按块内顺序分配)
        image_index = {i.url: image_offset + n for n, i in enumerate(images)}
        # 先摘掉引用块与媒体外壳, 只要这一层自己的文字
        for aside in soup.find_all("aside", class_="quote"):
            aside.decompose()
        raw_polls = [p for p in (post.get("polls") or []) if isinstance(p, dict)]
        # ⚠️ 必须在取纯文本**之前**把 lightbox 换成标记: 包裹里还有文件名/分辨率文字
        cls._strip_lightbox(soup, image_index)
        polls = cls._simplify(soup, polls=raw_polls, offset=poll_offset)
        text = soup.get_text("\n", strip=True)
        # 没文字也出引用块 (sign_only) —— 纯图楼层只有署名 + 标记行
        return format_quote_block(text, head, sign_only=True), images, polls

    @staticmethod
    def _quote_to_plain(block: str) -> str:
        """引用块 markdown → 纯文本 (给 text_content 兜底路径用)。"""
        text = re.sub(r"<[^>]+>", "", block or "")
        lines = [re.sub(r"^>\s*", "", ln).strip() for ln in text.splitlines()]
        return "\n".join(ln for ln in lines if ln)

    @classmethod
    def _context_quotes(
        cls, posts: list[dict[str, Any]], current: dict[str, Any], image_offset: int = 0
    ) -> tuple[list[str], list[LinuxDoImage], list[LinuxDoPoll]]:
        """当前楼层的上下文: 引用块 (**从远到近**) + 这些层的图片 + 这些层的投票。

        用户要求: 分享某一层时要把**主楼**也带上 (那一层常是在回应主楼/前文);
        楼层回复了别的楼层时, 被回复的那层也要带上。

        被带上的那些层自己的图片也一并返回 —— 但图**在这一层正文里留原位标记**
        （``{{media:N}}``），调用方只需把图片按顺序并进 media 列表。``image_offset``
        是本层图片的数量（media 顺序 = 本层 → 上下文层）。

        这些层自带的投票同样返回 —— 它们的占位符在引用块里 (正文**最前**) ⇒
        **先占索引**; 当前层的投票索引从它们之后继续 (与「占位符出现顺序」一致)。
        """
        wanted = current.get("post_number")
        blocks: list[str] = []
        images: list[LinuxDoImage] = []
        polls: list[LinuxDoPoll] = []
        targets: list[dict[str, Any]] = []
        # ① 主楼: 当前就是主楼时不用带
        if wanted != 1:
            if op := next((p for p in posts if p.get("post_number") == 1), None):
                targets.append(op)
        # ② 被回复的楼层 (回复主楼时已在①里, 不重复)
        reply_to = current.get("reply_to_post_number")
        if reply_to and reply_to not in (1, wanted):
            if rp := next((p for p in posts if p.get("post_number") == reply_to), None):
                targets.append(rp)

        for post in targets:
            block, post_images, post_polls = cls._post_to_quote(
                post, poll_offset=len(polls), image_offset=image_offset + len(images)
            )
            if block:
                blocks.append(block)
            images.extend(post_images)
            polls.extend(post_polls)
        return blocks, images, polls

    @classmethod
    def _extract_quotes(cls, soup: BeautifulSoup) -> list[tuple[str, str]]:
        """把 Discourse 的引用回复 (``aside.quote``) 抽出来, 交给**公共**排版 helper 渲染。

        排版规则由 ``format_quote_block`` 决定 (整块斜体、作者行在块内、不写"引用/回复"字样),
        与 twitter / threads 完全一致 —— 这里不要再自己拼引用块的 markdown。
        """
        rendered: list[tuple[str, str]] = []
        for index, aside in enumerate(soup.find_all("aside", class_="quote")):
            username = str(aside.get("data-username") or "")
            blockquote = aside.find("blockquote")
            text = blockquote.get_text("\n", strip=True) if blockquote else ""
            placeholder = cls._QUOTE_PLACEHOLDER.format(index)
            aside.replace_with(placeholder)
            if not text:
                rendered.append((placeholder, ""))
                continue
            author = format_author_link("", username, profile_url(Platform.LINUXDO, username))
            rendered.append((placeholder, format_quote_block(text, author)))
        return rendered

    @staticmethod
    def _strip_lightbox(soup: BeautifulSoup, image_index: Mapping[str, int] | None = None) -> None:
        """去掉图片的 lightbox 包裹 —— 图会作为媒体单独发送, 正文里不再重复。

        清掉的是**整个包裹**: 除了 ``img``, 里面还有 ``div.meta`` 里的说明文字
        (``span.filename`` 是文件名, ``span.informations`` 是"分辨率 体积")。
        那些文字不是正文, 见 ``_from_payload`` 里为什么要在取纯文本前调用。

        :param image_index: 图片 URL → 它在 ``media`` 列表里的下标。给了就在
            **原位置**留 ``{{media:N}}``（渲染层换成媒体引用 ⇒ 图出现在文章里，
            而不是堆到消息末尾 —— 用户报「图片原位怎么还是坏的」）。
            ``None``、或该图不在表里时退回 ``decompose()``（老行为）。
        """
        for wrapper in soup.find_all("div", class_="lightbox-wrapper"):
            index = None
            if image_index is not None:
                img = wrapper.find("img")
                if img is not None:
                    link = img.find_parent("a")
                    src = str(img.get("src") or "")
                    url = str((link.get("href") if link else "") or src) or src
                    index = image_index.get(url)
            if index is None:
                wrapper.decompose()
            else:
                wrapper.replace_with(media_placeholder(index))
        # 没有 lightbox 包裹的裸图（`<p><img src=…></p>`）同样要换成标记，
        # 否则它留在正文里就是外链图片语法 —— 富文本不允许，渲染层只能整张剥掉。
        if image_index is not None:
            for img in soup.find_all("img"):
                classes = img.get("class") or []
                if "emoji" in classes or "avatar" in classes:
                    continue
                if img.find_parent("div", class_="lightbox-wrapper"):
                    continue
                link = img.find_parent("a")
                src = str(img.get("src") or "")
                url = str((link.get("href") if link else "") or src) or src
                index = image_index.get(url)
                if index is not None:
                    img.replace_with(media_placeholder(index))

    @staticmethod
    def _simplify(
        soup: BeautifulSoup, polls: list[dict[str, Any]] | None = None, offset: int = 0
    ) -> list[LinuxDoPoll]:
        """清理 Discourse 特有的包裹结构，避免转换出噪音。

        - ``div.lightbox-wrapper``：图片会作为媒体单独发送，正文里不再重复
        - ``details`` / ``summary``：富文本渲染不支持折叠，这里展开（保留内容、去掉外壳标题）
        - ``div.spoiler``：只保留内容
        - ``img.emoji`` / ``img.avatar``：直接去掉 —— emoji 的 alt 是 ``:name:`` 形式,
          留在正文里既不美观也不是原样文本; 头像则是引用回复头部的装饰
        - ``div.poll``：改写成**占位符行**（见 ``_convert_polls``）

        :param offset: poll 索引的起点 —— 上下文引用块 (排在正文最前) 里的投票
            已经占掉的索引个数; 本层的投票从 ``offset`` 起编。
        :return: 结构化投票列表 (DOM 顺序); 调用方把它带进 ``LinuxDoTopic.polls``。
        """
        for tag in soup.find_all("img", class_=["emoji", "avatar"]):
            tag.decompose()
        LinuxDoTopic._strip_lightbox(soup)
        for details in soup.find_all("details"):
            details.unwrap()
        for summary in soup.find_all("summary"):
            summary.decompose()
        for spoiler in soup.find_all("div", class_="spoiler"):
            spoiler.unwrap()
        return LinuxDoTopic._convert_polls(soup, polls or [], offset=offset)

    @staticmethod
    def _convert_polls(
        soup: BeautifulSoup, polls: list[dict[str, Any]], offset: int = 0
    ) -> list[LinuxDoPoll]:
        """把 Discourse 的 ``div.poll`` 原地改写成**占位符行** ``{{poll:N}}``, 返回结构化数据。

        N = 占位符在正文里的出现顺序 (``div.poll`` 的 DOM 顺序 + ``offset`` 起点) ——
        渲染层按 ``result.polls[N]`` 取数据、按用户语言拼「选项/票数/占比」表格。
        **lib 侧不再生成任何投票文案**。

        不处理的话，poll 就是一段 HTML，直接转 markdown 会把**选项与人数标签**
        糊成一串裸文字（如 ``选项A / 选项B / 0 / 投票人``）—— 看不出这是投票。

        **真实数据在同一次响应的 ``post["polls"]`` 结构化字段里**
        （``options[].votes`` / ``voters``）—— ``cooked`` 里那个 ``info-number``
        是恒为 0 的**占位壳**（服务端渲染与前端首次渲染都用它，真实数字由前端拿
        结构化数据填）。所以这里以 ``polls`` 为准，**不需要任何额外请求**。

        ⚠️ 教训: 判断"数据拿不到"之前要把**同一个响应里的其它字段**翻一遍 ——
        只看 ``cooked`` 会得出"票数要另请求"的错误结论（踩过）。
        没有结构化数据时（老版本站点）退化成只有选项文字 (``count=None``)、
        ``voters=None`` —— 渲染层渲染成单列表格, **不显示那个假 0**。
        """
        by_name = {str(p.get("name")): p for p in polls}
        structured: list[LinuxDoPoll] = []
        for index, poll in enumerate(soup.find_all("div", class_="poll")):
            name = str(poll.get("data-poll-name") or "")
            data = by_name.get(name) or (polls[index] if index < len(polls) else {})
            raw_options = data.get("options") or []
            options: list[LinuxDoPollOption] = []
            for option in raw_options:
                if isinstance(option, dict):
                    options.append(
                        LinuxDoPollOption(
                            text=LinuxDoTopic._option_text(option),
                            count=to_int(option.get("votes")),
                        )
                    )
            if not options:
                # 没有结构化数据: 退回 cooked 里的选项文字 (不带票数, 见 docstring)
                options = [
                    LinuxDoPollOption(text=li.get_text(" ", strip=True))
                    for li in poll.select("li[data-poll-option-id]")
                ]
            if not options:
                # 连选项都没有: 不要留下空壳 (占位符行也不放)
                poll.decompose()
                continue
            voters = to_int(data.get("voters")) or 0
            has_counts = all(option.count is not None for option in options)
            structured.append(
                LinuxDoPoll(
                    options=options,
                    total_votes=sum(option.count or 0 for option in options) if has_counts else None,
                    voters=voters if voters > 0 else None,
                    title=str(data.get("title") or "").strip(),
                )
            )
            # 占位符**独立成行** (包一个 ``p``): markdown 转换后就是正文里的一行
            placeholder = soup.new_tag("p")
            placeholder.string = f"{{{{poll:{offset + len(structured) - 1}}}}}"
            poll.replace_with(placeholder)
        return structured

    @staticmethod
    def _option_text(option: dict[str, Any]) -> str:
        """选项文字: ``html`` 字段可能带标签 (有的投票选项就是一张图)。"""
        raw = str(option.get("html") or "")
        if "<" not in raw:
            return raw.strip()
        return BeautifulSoup(raw, "lxml").get_text(" ", strip=True)

    @staticmethod
    def _to_markdown(html: str) -> str:
        """HTML → markdown。"""
        if not html:
            return ""
        converter = MarkdownConverter(heading_style="ATX")
        markdown_content = str(converter.convert(html))
        # 去掉转换后的空行堆积（删掉表情/头像后可能多出空段落）
        markdown_content = re.sub(r"\n{3,}", "\n\n", markdown_content)
        return markdown_content.strip()

    @staticmethod
    def _extract_images(soup: BeautifulSoup) -> list[LinuxDoImage]:
        """取帖子里的图片: lightbox 的 href 是原图, img 的宽高用来声明显示比例。"""
        images: list[LinuxDoImage] = []
        seen: set[str] = set()

        for img in soup.find_all("img"):
            classes = img.get("class") or []
            # 表情 (img.emoji) 与头像 (img.avatar, 引用回复里那个 24x24 小图) 都不是帖子媒体
            if "emoji" in classes or "avatar" in classes:
                continue
            src = str(img.get("src") or "")
            link = img.find_parent("a")
            original = str((link.get("href") if link else "") or src)
            url = original or src
            if not url or url in seen:
                continue
            seen.add(url)
            images.append(
                LinuxDoImage(
                    url=url,
                    width=to_int(img.get("width")) or 0,
                    height=to_int(img.get("height")) or 0,
                )
            )
        return images


__all__ = ["LinuxDoError", "LinuxDoImage", "LinuxDoPoll", "LinuxDoPollOption", "LinuxDoTopic"]
