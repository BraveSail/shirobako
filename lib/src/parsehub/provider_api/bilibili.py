# mypy: disable-error-code=no-untyped-def
import asyncio
import json
import re
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from functools import reduce
from hashlib import md5
from typing import Any, Self, cast

from loguru import logger

from ..utils import http
from ..utils.helpers import get_author_name, media_placeholder, to_int

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
)
XOR_CODE = 23442827791579
MASK_CODE = 2251799813685247
MAX_AID = 1 << 51
ALPHABET = "FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf"
ENCODE_MAP = 8, 7, 0, 5, 1, 3, 2, 4, 6
DECODE_MAP = tuple(reversed(ENCODE_MAP))

BASE = len(ALPHABET)
PREFIX = "BV1"
PREFIX_LEN = len(PREFIX)
CODE_LEN = len(ENCODE_MAP)


class BiliAPI:
    def __init__(self, proxy: str | None = None):
        self.headers = {"User-Agent": USER_AGENT}
        self.proxy = proxy
        self._client: http.AsyncClient | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.aclose()

    async def get_dynamic_info(self, url: str, cookie: dict | None = None) -> "BiliDynamic":
        """获取动态信息

        opus（图文）走详情页: ``detail`` 接口对 opus **只回截断摘要**
        （``summary.has_more = true`` + ``fold_action: 全文``，实测只给 4 段 103 字），
        全文和图片顺序只在详情页的 ``__INITIAL_STATE__.detail.modules`` 里
        （同一内容 165 段 / 2854 字 / 35 张图）。抓页面**替换**掉接口调用，
        仍然是一次请求，不额外增加。
        """
        match = re.search(r"\b\d{18,19}\b", url)
        if not match:
            raise ValueError(f"Invalid dynamic url: {url}")
        dyn_id = match.group(0)
        if "/opus/" in url:
            if opus_item := await self._get_opus_page_item(dyn_id, cookie):
                return BiliDynamic.parse({"item": opus_item})
            logger.debug("opus 详情页不可用, 回退 detail 接口: %s", url)
        params = {
            "timezone_offset": "-480",
            "id": dyn_id,
            "features": "itemOpusStyle",
        }
        headers = self.headers.copy()
        headers |= {
            "referer": f"https://t.bilibili.com/{dyn_id}",
        }
        response = await self._get_client().get(
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail",
            headers=headers,
            params=params,
            cookies=cookie,
        )
        response.raise_for_status()
        mj = response.json()
        if not (data := mj.get("data")):
            match mj.get("code"):
                case -352:
                    raise Exception("获取动态信息失败: -352 风控限制")
                case 4101152:
                    raise Exception("动态不可见")
                case _:
                    raise Exception(f"获取动态信息失败: {mj}")
        return BiliDynamic.parse(cast(dict[str, Any], data))

    #: opus 详情页里的内联状态（含全文段落）
    _OPUS_PAGE = "https://www.bilibili.com/opus/{id}"
    _INITIAL_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});\s*\(function", re.S)

    async def _get_opus_page_item(self, opus_id: str, cookie: dict | None) -> dict | None:
        """opus 详情页 → 与 ``detail`` 接口 ``item`` 同构的 dict（拿全文 + 图片顺序）。

        失败（无内联状态 / 结构变了 / 风控）返回 ``None``，调用方回退接口，
        不会把一次正常解析变成硬失败。
        """
        headers = self.headers | {"referer": self._OPUS_PAGE.format(id=opus_id)}
        response = await self._get_client().get(
            self._OPUS_PAGE.format(id=opus_id),
            headers=headers,
            cookies=cookie,
        )
        response.raise_for_status()
        if not (match := self._INITIAL_STATE_RE.search(response.text)):
            return None
        try:
            # 页面内联的是 JS 字面量，含裸 undefined
            state = json.loads(re.sub(r"\bundefined\b", "null", match.group(1)))
        except ValueError:
            logger.debug("opus 页 __INITIAL_STATE__ 解析失败: %s", opus_id)
            return None
        return BiliDynamic.item_from_opus_page(cast(dict[str, Any], state.get("detail") or {}))

    async def get_video_info(self, url: str, cookie: dict | None = None):
        """获取视频详细信息

        bilibili 的 view/detail 端点对匿名请求会直接风控 (非 JSON 响应),
        必须带登录 cookie 才能拿到数据。
        """
        bvid = self.get_bvid(url)
        if not bvid:
            raise ValueError(f"Invalid url: {url}")
        response = await self._get_client().get(
            "https://api.bilibili.com/x/web-interface/view/detail",
            params={"bvid": bvid},
            cookies=cookie,
        )
        if response.status_code == 412:
            raise Exception("由于触发哔哩哔哩安全风控策略，该次访问请求被拒绝。")
        else:
            response.raise_for_status()
        return response.json()

    async def get_video_playurl(self, url, cid, b3, b4, is_high_quality=True) -> dict:
        bvid = self.get_bvid(url)
        params = {
            "bvid": bvid,
            "cid": cid,
            "qn": 64 if is_high_quality else 16,  # 高画质为720p, 低画质为360p
            "fnver": 0,
            "fnval": 1,
            "fourk": 1,
            "gaia_source": "",
            "from_client": "BROWSER",
            "is_main_page": "false",
            "need_fragment": "false",
            "isGaiaAvoided": "true",
            "web_location": 1315873,
            "voice_balance": 1,
        }
        cookies = {
            "SESSDATA": "",
            "buvid3": b3,
            "buvid4": b4,
            "bili_jct": "",
            "ac_time_value": "",
            "opus-goback": "1",
        }
        response = await self._get_client().get(
            "https://api.bilibili.com/x/player/playurl",
            params=params,
            cookies=cookies,
        )
        return cast(dict[str, Any], response.json())

    async def get_buvid(self):
        """获取 buvid"""
        response = await self._get_client().get(
            "https://api.bilibili.com/x/frontend/finger/spi",
        )
        data = response.json()
        return data["data"]["b_3"], data["data"]["b_4"]

    async def ai_summary(self, bvid: str) -> "AISummaryResult":
        bvid = self.av2bv(aid=bvid)
        info = await self.get_video_info(bvid)
        cid = info["data"]["View"]["cid"]
        up_mid = info["data"]["View"]["owner"]["mid"]
        wbi = await BiliWbiSigner().wbi(bvid=bvid, cid=cid, up_mid=up_mid)
        return await self.get_ai_summary(bvid, cid, up_mid, wbi["w_rid"], wbi["wts"])

    async def get_ai_summary(self, bvid: str, cid: int, up_mid: int, w_rid: str, wts: int) -> "AISummaryResult":
        url = "https://api.bilibili.com/x/web-interface/view/conclusion/get"
        result = await self._get_client().get(
            url,
            params={
                "bvid": bvid,
                "cid": cid,
                "up_mid": up_mid,
                "w_rid": w_rid,
                "wts": wts,
            },
        )
        return AISummaryResult.parse(result.json())

    def _get_client(self) -> http.AsyncClient:
        if self._client is None or getattr(self._client, "is_closed", False):
            self._client = http.AsyncClient(proxy=self.proxy, headers=self.headers)
        return self._client

    async def aclose(self):
        if self._client is not None and not getattr(self._client, "is_closed", False):
            await self._client.aclose()
            self._client = None

    @staticmethod
    def av2bv(aid: str) -> str:
        if aid.upper().startswith("BV"):
            return aid
        aid_num = int(aid.removeprefix("av"))
        bvid = [""] * 9
        tmp = (MAX_AID | aid_num) ^ XOR_CODE
        for i in range(CODE_LEN):
            bvid[ENCODE_MAP[i]] = ALPHABET[tmp % BASE]
            tmp //= BASE
        return PREFIX + "".join(bvid)

    @staticmethod
    def bv2av(bvid: str) -> str:
        assert bvid[:3] == PREFIX

        bvid = bvid[3:]
        tmp = 0
        for i in range(CODE_LEN):
            idx = ALPHABET.index(bvid[DECODE_MAP[i]])
            tmp = tmp * BASE + idx
        return f"av{(tmp & MASK_CODE) ^ XOR_CODE}"

    def get_bvid(self, url: str):
        m_bv = re.search(r"BV[0-9A-Za-z]{10,}", url)
        if m_bv:
            return m_bv.group(0)
        m_av = re.search(r"(?i)\bav(\d+)\b", url)
        if m_av:
            return self.av2bv(f"av{m_av.group(1)}")
        return None


class DynamicType(Enum):
    """动态类型"""

    DYNAMIC_TYPE_FORWARD = "DYNAMIC_TYPE_FORWARD"  # 动态转发
    DYNAMIC_TYPE_DRAW = "DYNAMIC_TYPE_DRAW"  # 带图动态
    DYNAMIC_TYPE_AV = "DYNAMIC_TYPE_AV"  # 投稿视频
    DYNAMIC_TYPE_PGC_UNION = "DYNAMIC_TYPE_PGC_UNION"  # 剧集 (番剧、电影、纪录片)
    DYNAMIC_TYPE_WORD = "DYNAMIC_TYPE_WORD"  # 纯文字动态
    DYNAMIC_TYPE_ARTICLE = "DYNAMIC_TYPE_ARTICLE"  # 投稿专栏
    DYNAMIC_TYPE_MUSIC = "DYNAMIC_TYPE_MUSIC"  # 音乐
    DYNAMIC_TYPE_COMMON_SQUARE = "DYNAMIC_TYPE_COMMON_SQUARE"  # 装扮 / 剧集点评 / 普通分享
    DYNAMIC_TYPE_LIVE = "DYNAMIC_TYPE_LIVE"  # 直播间分享
    DYNAMIC_TYPE_MEDIALIST = "DYNAMIC_TYPE_MEDIALIST"  # 收藏夹
    DYNAMIC_TYPE_COURSES_SEASON = "DYNAMIC_TYPE_COURSES_SEASON"  # 课程
    DYNAMIC_TYPE_UGC_SEASON = "DYNAMIC_TYPE_UGC_SEASON"  # 合集更新
    UNKNOWN = "UNKNOWN"

    @classmethod
    def _missing_(cls, value):
        return cls.UNKNOWN


#: 正文里的转发格式: ``//@原作者:被转发的原文`` (可能夹零宽字符)
_FORWARD_COMMENT_RE = re.compile(r"\s*\u200b?\s*//\s*@(?P<author>[^:：\n]+)[:：](?P<body>.+)", re.S)


class MajorType(Enum):
    """动态主体类型"""

    MAJOR_TYPE_OPUS = "MAJOR_TYPE_OPUS"  # 图文动态
    MAJOR_TYPE_ARCHIVE = "MAJOR_TYPE_ARCHIVE"  # 视频
    MAJOR_TYPE_PGC = "MAJOR_TYPE_PGC"  # 剧集更新
    MAJOR_TYPE_MUSIC = "MAJOR_TYPE_MUSIC"  # 音频更新
    MAJOR_TYPE_COMMON = "MAJOR_TYPE_COMMON"  # 一般类型
    MAJOR_TYPE_LIVE = "MAJOR_TYPE_LIVE"  # 直播间分享
    MAJOR_TYPE_MEDIALIST = "MAJOR_TYPE_MEDIALIST"  # 收藏夹
    MAJOR_TYPE_COURSES = "MAJOR_TYPE_COURSES"  # 课程
    MAJOR_TYPE_UGC_SEASON = "MAJOR_TYPE_UGC_SEASON"  # 合集更新
    MAJOR_TYPE_UPOWER_COMMON = "MAJOR_TYPE_UPOWER_COMMON"  # 充电相关
    UNKNOWN = "UNKNOWN"

    @classmethod
    def _missing_(cls, value):
        return cls.UNKNOWN


@dataclass(kw_only=True)
class BiliImage:
    url: str
    width: int = 0
    height: int = 0
    live_url: str | None = None


@dataclass(kw_only=True)
class BiliDynamic:
    title: str | None = ""
    content: str | None = ""
    images: list[BiliImage] | None = None
    author_name: str = ""
    #: 作者的 UID (B 站没有 @用户名, 主页靠它拼)
    author_mid: int | str | None = None
    #: 发布时间的 unix 时间戳 (module_author.pub_ts)
    published_at: int | None = None
    #: 点赞数 (module_stat.like.count)
    like_count: int | None = None
    #: 视频动态的 BV 号 (``archive.bvid``); 用来把标题链到视频。非视频动态为 None。
    bvid: str | None = None
    #: 转发的原动态 (``item["orig"]``); 不是转发时为 None。递归结构, 支持嵌套转发。
    forward: "BiliDynamic | None" = None
    #: 正文里的话题节点: ``[{"name": 话题名(不含#), "url": 话题页地址}, …]``。
    #: 取自 ``desc.rich_text_nodes`` 里 ``type == RICH_TEXT_NODE_TYPE_TOPIC`` 的节点 ——
    #: **类型与边界由 B 站判定**, 拿来链接化就不必用正则猜（正则在 ``C# 与 #tag#``
    #: 这种成对 ``#`` 的场景会匹配出 ``# 与 #``）。
    topics: list[dict[str, str]] | None = None

    @property
    def video_url(self) -> str:
        """视频播放页地址; 没有 BV 号时返回空串。"""
        return f"https://www.bilibili.com/video/{self.bvid}" if self.bvid else ""

    def has_content(self) -> bool:
        """是否拿到了可展示的内容 (标题/正文/媒体任一)。"""
        return bool((self.title or "").strip() or (self.content or "").strip() or self.images)

    @classmethod
    def parse(cls, data: dict) -> Self:
        return cls._from_item(data["item"])

    @classmethod
    def _from_item(cls, item: dict) -> Self:
        """把一条动态 item 转成 BiliDynamic。

        主动态与被转发的原动态 (``orig``) 结构相同, 所以两边走同一段逻辑,
        转发的原动态递归挂到 ``forward`` 上。
        """
        modules = item.get("modules") or {}
        module_dynamic: dict = modules.get("module_dynamic") or {}
        major: dict | None = module_dynamic.get("major", None)
        result = cls._parse_forward(module_dynamic) if not major else cls._parse_major(module_dynamic, major)

        author = modules.get("module_author") or {}
        result.author_name = get_author_name(author)
        result.author_mid = author.get("mid")
        result.published_at = to_int(author.get("pub_ts"))
        stat = (modules.get("module_stat") or {}).get("like") or {}
        result.like_count = to_int(stat.get("count"))

        result.topics = cls._get_desc_topics(module_dynamic)
        result.forward = cls._resolve_forward(item, module_dynamic)
        return result

    @staticmethod
    def _get_desc_topics(module_dynamic: dict) -> list[dict[str, str]]:
        """正文里的话题节点（权威来源，替代正则猜边界）。

        节点自带 ``jump_url``（话题页 / 搜索页），比自己按名字拼 URL 权威；
        节点 ``text`` 是 ``#话题#``（两侧都带 #，B 站的形态），这里存**不含 #** 的名字。
        """
        desc = module_dynamic.get("desc") or {}
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for node in desc.get("rich_text_nodes") or []:
            if node.get("type") != "RICH_TEXT_NODE_TYPE_TOPIC":
                continue
            name = str(node.get("text") or "").strip().strip("#").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append({"name": name, "url": str(node.get("jump_url") or "")})
        return out

    @classmethod
    def _resolve_forward(cls, item: dict, module_dynamic: dict) -> "BiliDynamic | None":
        """确定被转发的原动态。

        两条来源, 优先前者:
        1. ``item["orig"]`` —— 正常转发;
        2. 正文里的 ``//@原作者:原文`` —— **原动态被删/接口降级**时 ``orig`` 是空壳
           (desc 空、major 是 MAJOR_TYPE_NONE、author.mid=0), 但 B 站客户端仍靠正文这段
           显示引用卡片, 我们也照做, 否则用户看到的就是「引用没了」。
        """
        if orig := item.get("orig"):
            try:
                candidate = cls._from_item(orig)
            except Exception as e:  # 原动态结构不认识时不能拖垮主动态
                logger.warning(f"被转发的动态解析失败, 回退正文的 //@ 段: {type(e).__name__}: {e}")
            else:
                if candidate.has_content():
                    return candidate
                logger.debug("被转发的动态是空壳 (原动态可能已删), 回退正文的 //@ 段")
        return cls._from_forward_comment(module_dynamic)

    @classmethod
    def _parse_major(cls, module_dynamic: dict, major: dict) -> Self:
        major_type = major["type"]
        major_parsers: dict[MajorType, Callable[[dict, dict], Self]] = {
            MajorType.MAJOR_TYPE_MEDIALIST: cls._parse_medialist,
            MajorType.MAJOR_TYPE_UPOWER_COMMON: cls._parse_upower_common,
            MajorType.MAJOR_TYPE_COMMON: cls._parse_common,
            MajorType.MAJOR_TYPE_OPUS: cls._parse_opus,
            MajorType.MAJOR_TYPE_ARCHIVE: cls._parse_av,
            MajorType.MAJOR_TYPE_PGC: cls._parse_pgc_union,
            MajorType.MAJOR_TYPE_LIVE: cls._parse_live,
            MajorType.MAJOR_TYPE_COURSES: cls._parse_courses,
            MajorType.MAJOR_TYPE_UGC_SEASON: cls._parse_ugc_season,
            MajorType.MAJOR_TYPE_MUSIC: cls._parse_music,
        }
        major_parser = major_parsers.get(MajorType(major_type), None)
        if not major_parser:
            # 不抛异常: 接口会返回 MAJOR_TYPE_NONE (原动态被删/降级), 抛出去会让
            # 调用方**整个放弃**这条内容 —— 转发场景下就是引用块凭空消失。
            # 退回按 desc 文本解析, 至少保住文字。
            logger.debug(f"未知的 major 类型, 按纯文本处理: {major_type}")
            return cls._parse_forward(module_dynamic)
        return major_parser(module_dynamic, major)

    @classmethod
    def _from_forward_comment(cls, module_dynamic: dict) -> "BiliDynamic | None":
        """从正文的 ``//@原作者:被转发的原文`` 段还原被转发的动态。

        这是 B 站的原生转发格式, 客户端就是靠它渲染引用卡片。节点里紧随 ``//``
        之后的 ``RICH_TEXT_NODE_TYPE_AT`` 带 ``rid`` (= 对方 mid), 有了它作者名
        才能链到主页。
        """
        desc = module_dynamic.get("desc") or {}
        text = str(desc.get("text") or "")
        match = _FORWARD_COMMENT_RE.search(text)
        if not match:
            return None
        author = match.group("author").strip()
        body = match.group("body").strip()
        if not author and not body:
            return None
        return cls(
            content=body,
            author_name=author,
            author_mid=cls._forward_comment_mid(desc.get("rich_text_nodes") or []),
        )

    @staticmethod
    def _forward_comment_mid(nodes: list[dict]) -> int | str | None:
        """取 ``//@`` 里那个 AT 节点的 rid (被转发者的 mid)。"""
        for i, node in enumerate(nodes):
            if "//" not in str(node.get("text") or ""):
                continue
            for later in nodes[i + 1 :]:
                if later.get("type") == "RICH_TEXT_NODE_TYPE_AT":
                    return to_int(later.get("rid"))
            return None
        return None

    @classmethod
    def _parse_pgc_union(cls, _: dict, major: dict) -> Self:
        pgc = major["pgc"]
        return cls(title=pgc["title"], images=[BiliImage(url=pgc["cover"])])

    @classmethod
    def _parse_forward(cls, module_dynamic: dict) -> Self:
        return cls(content=cls._get_desc_text(module_dynamic))

    @classmethod
    def _parse_av(cls, module_dynamic: dict, major: dict) -> Self:
        archive = major.get("archive") or {}
        return cls(
            title=archive.get("title") or "",
            # 动态自己的正文优先 (分享时写的话术), 其次用视频简介;
            # 以前一有 desc 就**整条只返回 content**, 视频标题/封面/BV 号全丢
            content=cls._get_desc_text(module_dynamic) or archive.get("desc") or "",
            images=cls._get_major_cover(archive),
            bvid=archive.get("bvid") or None,
        )

    @classmethod
    def _parse_music(cls, module_dynamic: dict, major: dict) -> Self:
        if content := cls._get_desc_text(module_dynamic):
            return cls(content=content)
        music = major["music"]
        return cls(title=music["title"], images=cls._get_major_cover(music))

    @classmethod
    def item_from_opus_page(cls, detail: dict) -> dict | None:
        """把 opus 详情页的 ``detail`` 转成 ``detail`` 接口的 ``item`` 同构结构。

        页面给的是 **modules 列表**（按 ``module_type`` 区分），接口给的是 **modules 字典**；
        下游（作者/统计/转发/正文解析）都按接口形态取值，所以在这里统一。
        正文段落**原样**放进 ``major.opus.summary.paragraphs``，并把 ``has_more`` 置 False
        —— 页面给的就是全文（没有折叠）。
        """
        modules = {m.get("module_type"): m for m in (detail.get("modules") or []) if isinstance(m, dict)}
        content = (modules.get("MODULE_TYPE_CONTENT") or {}).get("module_content") or {}
        paragraphs = content.get("paragraphs") or []
        if not paragraphs:
            return None
        author = (modules.get("MODULE_TYPE_AUTHOR") or {}).get("module_author") or {}
        stat = (modules.get("MODULE_TYPE_STAT") or {}).get("module_stat") or {}
        title = ((modules.get("MODULE_TYPE_TITLE") or {}).get("module_title") or {}).get("text") or ""
        # 图片按**段落顺序**收集 —— 与 _render_opus_paragraphs 里生成的下标一一对应
        pics: list[dict[str, Any]] = []
        for para in paragraphs:
            for pic in ((para.get("pic") or {}).get("pics") or []):
                if pic.get("url"):
                    pics.append(
                        {
                            "url": pic["url"],
                            "width": to_int(pic.get("width")),
                            "height": to_int(pic.get("height")),
                            "live_url": pic.get("live_url") or None,
                        }
                    )
        opus = {
            "title": title,
            "pics": pics,
            "jump_url": f"//www.bilibili.com/opus/{detail.get('id_str') or ''}",
            "summary": {
                "text": "",
                "has_more": False,
                "rich_text_nodes": [],
                "paragraphs": paragraphs,
            },
        }
        return {
            "id_str": detail.get("id_str"),
            "type": "DYNAMIC_TYPE_DRAW",
            "modules": {
                "module_author": author,
                "module_stat": stat,
                "module_dynamic": {
                    "major": {"type": "MAJOR_TYPE_OPUS", "opus": opus},
                    "desc": None,
                },
            },
        }

    @staticmethod
    def _opus_nodes_text(nodes: list[dict] | None) -> str:
        """富文本节点 → 纯文本（word 节点取 ``words``，其它节点尽量取 ``text``）。"""
        out: list[str] = []
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            word = node.get("word") or {}
            text = word.get("words")
            if text is None:
                text = node.get("text")
            if text:
                out.append(str(text))
        return "".join(out)

    @classmethod
    def _render_opus_paragraphs(cls, paragraphs: list[dict], images: list["BiliImage"]) -> str:
        """opus 段落 → markdown。图片按段序追加到 ``images`` 并留 ``{{media:N}}`` 原位标记。

        段落类型（B 站 opus）：1=文本、2=图片、8=标题；其余（列表/代码/引用/链接卡片）
        按能取到的文本兜底，至少不丢内容。
        """
        blocks: list[str] = []
        for para in paragraphs:
            if not isinstance(para, dict):
                continue
            pic = para.get("pic") or {}
            if pics := pic.get("pics"):
                for item in pics:
                    if not item.get("url"):
                        continue
                    images.append(
                        BiliImage(
                            url=item["url"],
                            width=to_int(item.get("width")) or 0,
                            height=to_int(item.get("height")) or 0,
                            live_url=item.get("live_url") or None,
                        )
                    )
                    blocks.append(media_placeholder(len(images) - 1))
                continue
            if heading := para.get("heading"):
                text = cls._opus_nodes_text(heading.get("nodes"))
                if text.strip():
                    # 不能用 ``#`` 标题: 正文随后要过 hashtag_handler，行首的 ``#``
                    # 会被当成 B 站话题边界（``#话题#``）链接化，把标题连同后面的正文
                    # 一起吞进 <a> 里。加粗在正文和引用块里都安全。
                    blocks.append(f"**{text.strip()}**")
                continue
            # 文本段落；列表/代码/引用等也尽量取 nodes 文本
            text = cls._opus_nodes_text((para.get("text") or {}).get("nodes"))
            if not text.strip():
                for holder in ("list", "code", "blockquote", "link_card"):
                    holder_data = para.get(holder) or {}
                    if isinstance(holder_data, dict):
                        text = cls._opus_nodes_text(holder_data.get("nodes"))
                        if text.strip():
                            break
            if text.strip():
                blocks.append(text.strip())
        return "\n\n".join(blocks)

    @classmethod
    def _parse_opus(cls, _: dict, major: dict) -> Self:
        opus = major.get("opus") or {}
        summary = opus.get("summary") or {}
        paragraphs = summary.get("paragraphs") or []
        images: list[BiliImage] = []
        # 有完整段落（详情页路径 / 未折叠的短动态）→ 按段渲染，图片原位；
        # 否则退回接口的截断摘要（has_more=true，只有前几段纯文本 + 顶部图）
        if paragraphs and not summary.get("has_more"):
            if content := cls._render_opus_paragraphs(paragraphs, images):
                return cls(title=opus.get("title") or "", content=content, images=images or None)
        images = []
        if pics := opus.get("pics"):
            images = [
                BiliImage(url=p["url"], live_url=p.get("live_url"), width=p.get("width"), height=p.get("height"))
                for p in pics
            ]
        return cls(
            title=opus.get("title") or "",
            content=summary.get("text") or "",
            images=images or None,
        )

    @classmethod
    def _parse_common(cls, module_dynamic: dict, major: dict) -> Self:
        if content := cls._get_desc_text(module_dynamic):
            return cls(content=content)
        common = major["common"]
        return cls(title=common["title"], content=common["desc"], images=cls._get_major_cover(common))

    @classmethod
    def _parse_live(cls, module_dynamic: dict, major: dict) -> Self:
        if content := cls._get_desc_text(module_dynamic):
            return cls(content=content)
        live = major["live"]
        content = f"{live['desc_first']} · {live['desc_second']}" if live["desc_second"] else live["desc_first"]
        return cls(title=live["title"], content=content, images=cls._get_major_cover(live))

    @classmethod
    def _parse_medialist(cls, module_dynamic: dict, major: dict) -> Self:
        if content := cls._get_desc_text(module_dynamic):
            return cls(content=content)
        medialist = major["medialist"]
        return cls(title=medialist["title"], content=medialist["sub_title"], images=cls._get_major_cover(medialist))

    @classmethod
    def _parse_courses(cls, module_dynamic: dict, major: dict) -> Self:
        if content := cls._get_desc_text(module_dynamic):
            return cls(content=content)
        courses = major["courses"]
        content = f"{courses['sub_title']}\n\n{courses['desc']}" if courses["sub_title"] else courses["desc"]
        return cls(title=courses["title"], content=content, images=cls._get_major_cover(courses))

    @classmethod
    def _parse_ugc_season(cls, module_dynamic: dict, major: dict) -> Self:
        if content := cls._get_desc_text(module_dynamic):
            return cls(content=content)
        ugc_season = major["ugc_season"]
        return cls(title=ugc_season["title"], content=ugc_season["desc"], images=cls._get_major_cover(ugc_season))

    @classmethod
    def _parse_upower_common(cls, module_dynamic: dict, major: dict) -> Self:
        if content := cls._get_desc_text(module_dynamic):
            return cls(content=content)
        upower_common = major["upower_common"]
        title = (
            f"{upower_common['title_prefix']}: {upower_common['title']}"
            if upower_common["title_prefix"]
            else upower_common["title"]
        )
        return cls(title=title)

    @staticmethod
    def _get_desc_text(module_dynamic: dict) -> str:
        # 用 .get: 被转发的原动态可能缺 desc 键 (以前是硬索引, 缺失就 KeyError)
        if desc := module_dynamic.get("desc"):
            return str(desc.get("text") or "").strip()
        return ""

    @staticmethod
    def _get_major_cover(major_content: dict) -> list[BiliImage] | None:
        if cover := major_content.get("cover"):
            return [BiliImage(url=cover)]
        return None


@dataclass
class PartOutline:
    timestamp: int
    content: str

    @staticmethod
    def parse(data: dict[str, Any]) -> "PartOutline":
        return PartOutline(timestamp=data["timestamp"], content=data["content"])


@dataclass
class Outline:
    title: str
    part_outline: list[PartOutline]
    timestamp: int

    @staticmethod
    def parse(data: dict[str, Any]) -> "Outline":
        part_outline = [PartOutline.parse(item) for item in data["part_outline"]]
        return Outline(title=data["title"], part_outline=part_outline, timestamp=data["timestamp"])


@dataclass
class ModelResult:
    result_type: int
    summary: str
    outline: list[Outline]

    @staticmethod
    def parse(data: dict[str, Any]) -> "ModelResult":
        outline = [Outline.parse(item) for item in data.get("outline") or []]
        return ModelResult(result_type=data["result_type"], summary=data["summary"], outline=outline)


@dataclass
class Data:
    code: int
    model_result: ModelResult
    stid: str
    status: int
    like_num: int
    dislike_num: int

    @staticmethod
    def parse(data: dict[str, Any]) -> "Data":
        model_result = ModelResult.parse(data["model_result"])
        return Data(
            code=data["code"],
            model_result=model_result,
            stid=data["stid"],
            status=data["status"],
            like_num=data["like_num"],
            dislike_num=data["dislike_num"],
        )


@dataclass
class AISummaryResult:
    code: int
    message: str
    ttl: int
    data: Data | None

    @staticmethod
    def parse(json_dict: dict) -> "AISummaryResult":
        if data := json_dict.get("data"):
            data = Data.parse(data)
        else:
            data = None
        return AISummaryResult(
            code=json_dict["code"],
            message=json_dict["message"],
            ttl=json_dict["ttl"],
            data=data,
        )


class BiliWbiSigner:
    MIXIN_KEY_ENC_TAB = [
        46,
        47,
        18,
        2,
        53,
        8,
        23,
        32,
        15,
        50,
        10,
        31,
        58,
        3,
        45,
        35,
        27,
        43,
        5,
        49,
        33,
        9,
        42,
        19,
        29,
        28,
        14,
        39,
        12,
        38,
        41,
        13,
        37,
        48,
        7,
        16,
        24,
        55,
        40,
        61,
        26,
        17,
        0,
        1,
        60,
        51,
        30,
        4,
        22,
        25,
        54,
        21,
        56,
        59,
        6,
        63,
        57,
        62,
        11,
        36,
        20,
        34,
        44,
        52,
    ]

    def get_mixin_key(self, orig: str) -> str:
        """对 img_key 和 sub_key 进行字符顺序打乱编码"""
        return reduce(lambda s, i: s + orig[i], self.MIXIN_KEY_ENC_TAB, "")[:32]

    def sign_request_params(self, params: dict, img_key: str, sub_key: str) -> dict:
        """为请求参数进行 wbi 签名"""
        mixin_key = self.get_mixin_key(img_key + sub_key)
        params["wts"] = round(time.time())  # 添加 wts 字段
        params = {k: str(v) for k, v in sorted(params.items())}  # 按 key 排序并转为 str
        query = urllib.parse.urlencode(params, safe="!'()*")  # 序列化参数并指定不编码字符
        wbi_sign = md5((query + mixin_key).encode()).hexdigest()  # 计算 w_rid
        params["w_rid"] = wbi_sign
        return params

    @staticmethod
    async def fetch_wbi_keys() -> tuple[str, str]:
        """获取最新的 img_key 和 sub_key"""
        async with http.AsyncClient() as client:
            try:
                resp = await client.get(
                    "https://api.bilibili.com/x/web-interface/nav",
                    headers={"User-Agent": USER_AGENT},
                )
                resp.raise_for_status()
                json_data = resp.json()
                img_url: str = json_data["data"]["wbi_img"]["img_url"]
                sub_url: str = json_data["data"]["wbi_img"]["sub_url"]
            except http.HTTPError as e:
                raise Exception(f"请求 wbi_img 失败: {e}") from e
            except (KeyError, TypeError, ValueError) as e:
                raise Exception(f"解析 wbi_img 失败: {e}") from e

            img_key = img_url.rsplit("/", 1)[1].split(".")[0]
            sub_key = sub_url.rsplit("/", 1)[1].split(".")[0]
            return img_key, sub_key

    async def wbi(self, **kwargs) -> dict:
        img_key, sub_key = await self.fetch_wbi_keys()
        signed_params = self.sign_request_params(
            params={**kwargs},
            img_key=img_key,
            sub_key=sub_key,
        )
        return signed_params


if __name__ == "__main__":
    r = asyncio.run(BiliAPI().get_dynamic_info("https://t.bilibili.com/1169207844562534435"))
    print(r)
