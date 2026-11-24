import abc
import asyncio
import re
from abc import abstractmethod
from dataclasses import dataclass
from enum import Enum
from functools import cached_property
from inspect import signature
from typing import Any, Self, Union
from urllib.parse import parse_qs, quote, urlparse

from bs4 import BeautifulSoup, Tag

from ..utils import http
from ..utils.helpers import get_author_name, to_datetime, to_int

#: 详情 API 的 ``text`` 字段里, 话题**已经是锚点**（服务端给好的）::
#:
#:     <a href="//s.weibo.com/weibo?q=%23话题%23" target="_blank">#话题#</a>
#:
#: 所以话题名（与边界）直接从 ``q=`` 参数解出来即可 —— 不必用正则去正文里猜:
#: 正文里出现**单个** ``#`` 时正则会误配（``C# 与 Python# 都`` 会被当成一个话题）。
#: 播放量/统计数的**中文缩写**形态（TV 接口的 ``play_count``: ``"8.1万"`` / ``"8,340"`` /
#: ``"1.2亿"``）。youtube 的 ``_compact_count`` 只认 K/M/B，中文站点用这个。
_WEIBO_COUNT_RE = re.compile(r"\s*([\d,]+(?:\.\d+)?)\s*([万亿]?)")


def parse_weibo_count(value: object) -> int | None:
    """``"8.1万"`` / ``"8,340"`` / ``"1.2亿"`` / ``81818`` → int；识别不了返回 None。

    微博在不同接口用不同形态给同一个数: 详情 API 给**精确整数**（``81818``）、
    TV 接口给**中文缩写**（``"8.1万"``）。缺失（``None`` / 空串）不是错误, 返回 None
    让调用点跳过那一段（与其它平台同一条原则）。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    match = _WEIBO_COUNT_RE.match(str(value).strip())
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return int(number * {"": 1, "万": 10_000, "亿": 100_000_000}[match.group(2)])


class WeiboAPI:
    def __init__(self, proxy: str | None = None):
        self.proxy = proxy
        self._cookies = {
            "SUB": "_2AkMR47Mlf8NxqwFRmfocxG_lbox2wg7EieKnv0L-JRMxHRl-yT9yqhFdtRB6OmOdyoia9pKPkqoHRRmSBA_WNPaHuybH",
        }

    @staticmethod
    def is_tv(url: str) -> bool:
        if "/tv/show" in url:
            return True
        return False

    async def resolve_url(self, url: str) -> str:
        parsed = urlparse(url)

        async def fn() -> str:
            async with http.AsyncClient(proxy=self.proxy, follow_redirects=False, timeout=30) as client:
                response = await client.get(url)
                if not response.ok:
                    # curl_cffi 的响应没有 httpx 的 is_error, 用 ok
                    response.raise_for_status()
            return response.headers.get("location") or url

        if parsed.hostname == "mapp.api.weibo.cn" and parsed.path.startswith("/fx/"):
            return await fn()
        if parsed.hostname == "video.weibo.com" and parsed.path.startswith("/show"):
            return await fn()
        return url

    async def get_id_by_url(self, url: str) -> str | None:
        parsed = urlparse(url)
        if match := re.compile(r"^/status/([^/?#]+)").match(parsed.path):
            return match[1]

        id_ = parsed.path.split("/")[-1]

        if self.is_tv(url) and len(id_) == 21:
            return id_

        if id_.isdigit() or len(id_) == 9:
            return id_
        return None

    async def statuses_show(self, bid: str) -> dict:
        headers = {
            "referer": "https://weibo.com",
        }
        api = f"https://weibo.com/ajax/statuses/show?id={bid}&isGetLongText=true"
        async with http.AsyncClient(proxy=self.proxy) as client:
            response = await client.get(api, cookies=self._cookies, headers=headers)
            response.raise_for_status()
            result: dict = response.json()
            return result

    async def tv_show(self, oid: str) -> dict:
        headers = {
            "content-type": "application/x-www-form-urlencoded",
            "referer": "https://weibo.com/tv/home",
        }
        params = {
            "page": f"/tv/show/{oid}",
        }
        data = {"data": f'{{"Component_Play_Playinfo":{{"oid":"{oid}"}}}}'}
        async with http.AsyncClient(proxy=self.proxy) as client:
            response = await client.post(
                "https://weibo.com/tv/api/component", cookies=self._cookies, headers=headers, data=data, params=params
            )
            response.raise_for_status()
            result: dict = response.json()
            return result

    async def parse(self, url: str) -> Union["WeiboContent", "WeiboTVContent"]:
        resolve_url = await self.resolve_url(url)
        id_ = await self.get_id_by_url(resolve_url)
        if not id_:
            raise ValueError("Invalid URL")
        if self.is_tv(resolve_url):
            result = await self.tv_show(id_)
            return WeiboTVContent.parse(result)
        result = await self.statuses_show(id_)
        return WeiboContent.parse(result)


class MediaType(Enum):
    VIDEO = "video"
    PHOTO = "pic"
    LIVE_PHOTO = "livephoto"
    GIF = "gif"
    ARTICLE = "article"
    #: 没见过的 ``object_type`` 都归这里（如微博智搜的 ``ai_summary``）。
    #: **不能抛异常**: 枚举里没有的值以前会让**整条微博解析失败** ——
    #: 实测 ``ValueError: 'ai_summary' is not a valid MediaType``。
    #: 卡片类型不认识，顶多是"这个卡片不特殊处理"，不该整条挂掉。
    UNKNOWN = "unknown"

    @classmethod
    def _missing_(cls, value: object) -> "MediaType":
        return cls.UNKNOWN


class Info(abc.ABC):
    @property
    @abstractmethod
    def media_url(self) -> str | None:
        raise NotImplementedError()

    @property
    @abstractmethod
    def thumb_url(self) -> str | None:
        raise NotImplementedError()


@dataclass
class Playback:
    url: str
    width: int = 0
    height: int = 0
    duration: float = 0
    bitrate: int = 0
    size: int = 0

    @classmethod
    def parse(cls, playback: dict) -> Self:
        pi = playback["play_info"]
        url = pi["url"]
        width = pi["width"]
        height = pi["height"]
        duration = pi.get("duration", 0)
        bitrate = pi.get("bitrate", 0)
        size = pi.get("size", 0)
        return cls(url, width, height, duration, bitrate, size)


@dataclass
class MediaInfo:
    format: str | None = None
    mp4_hd_url: str | None = None
    mp4_sd_url: str | None = None
    duration: int = 0
    prefetch_size: int | None = None
    playback: Playback | None = None
    #: **视频播放量**（累计）。字段名是微博的历史遗留（``online_users_number`` 听起来像
    #: "当前在线人数"），实测它就是播放量：与 TV 接口的 ``play_count`` 对照，6 个样本
    #: 全部吻合（2026-10-10；如 81818 vs "8.1万"、8338 vs "8,340"）。
    #: 详情 API 给**精确整数**，TV 接口给**中文缩写** —— 两处形态不同但同一个数。
    online_users_number: int | None = None

    @classmethod
    def parse(cls, media_dict: dict) -> Self:
        format_ = media_dict["format"]
        mp4_hd_url = media_dict.get("mp4_hd_url")
        mp4_sd_url = media_dict.get("mp4_sd_url")
        duration = media_dict["duration"]
        prefetch_size = media_dict["prefetch_size"]
        playback_list = media_dict.get("playback_list", [])
        playback = Playback.parse(playback_list[0]) if playback_list else None
        online = to_int(media_dict.get("online_users_number"))
        return cls(format_, mp4_hd_url, mp4_sd_url, duration, prefetch_size, playback, online)

    @property
    def play_count(self) -> int | None:
        """播放量。字段名见 ``online_users_number`` 的说明。"""
        return self.online_users_number


@dataclass
class PageInfo(Info):
    object_type: MediaType | None = None
    media_info: MediaInfo | None = None
    page_pic: str | None = None
    short_url: str | None = None

    @classmethod
    def parse(cls, page_info_dict: dict) -> Self:
        """解析卡片信息。

        ``object_type`` 与 ``media_info`` 都**用 .get**：不同卡片带不同的键，
        不认识的卡片（如微博智搜的 ``ai_summary``）整个没有 ``media_info`` ——
        硬索引会让**整条微博解析失败**（实测 ``KeyError: 'media_info'``）。
        缺了就是"这个卡片没有播放信息"，不是解析错误。
        """
        object_type = MediaType(page_info_dict.get("object_type") or "")
        raw_media_info = page_info_dict.get("media_info")
        media_info = (
            MediaInfo.parse(raw_media_info)
            if raw_media_info and object_type != MediaType.ARTICLE
            else None
        )
        page_pic = page_info_dict.get("page_pic")
        short_url = page_info_dict.get("short_url")
        return cls(object_type, media_info, page_pic, short_url)

    @property
    def media_url(self) -> str | None:
        if self.media_info and self.media_info.playback:
            return self.media_info.playback.url
        if self.media_info:
            return self.media_info.mp4_hd_url or self.media_info.mp4_sd_url
        return None

    @property
    def thumb_url(self) -> str | None:
        return self.page_pic

    @property
    def height(self) -> int:
        if self.media_info and self.media_info.playback:
            return self.media_info.playback.height
        return 0

    @property
    def width(self) -> int:
        if self.media_info and self.media_info.playback:
            return self.media_info.playback.width
        return 0

    @property
    def duration(self) -> int:
        return self.media_info.duration if self.media_info else 0


@dataclass
class Pic:
    url: str | None = None
    hdr_url: str | None = None
    width: int | None = None
    height: int | None = None
    cut_type: int | None = None
    type: str | None = None


@dataclass
class PicInfo(Info):
    """photo, livephoto, gif.
    video为livephoto和gif视频
    """

    pic_id: str | None = None
    type: MediaType | None = None
    thumbnail: Pic | None = None
    largest: Pic | None = None
    video: str | None = None

    @classmethod
    def parse(cls, pic_dict: dict) -> Self:
        return cls(
            pic_id=pic_dict["pic_id"],
            type=MediaType(pic_dict["type"]),
            thumbnail=Pic(**pic_dict["thumbnail"]),
            largest=Pic(**pic_dict["largest"]),
            video=pic_dict.get("video"),
        )

    @property
    def media_url(self) -> str | None:
        return self.largest.url if self.type == MediaType.PHOTO and self.largest else self.video

    @property
    def thumb_url(self) -> str | None:
        return self.thumbnail.url if self.thumbnail else None

    @property
    def height(self) -> int:
        return self.largest.height if self.largest and self.largest.height is not None else 0

    @property
    def width(self) -> int:
        return self.largest.width if self.largest and self.largest.width is not None else 0

    @property
    def duration(self) -> int:
        return 0


@dataclass
class MixMediaInfoItem(Info):
    type: MediaType | None = None
    data: PageInfo | PicInfo | None = None

    @property
    def media_url(self) -> str | None:
        return self.data.media_url if self.data else None

    @property
    def thumb_url(self) -> str | None:
        return self.data.thumb_url if self.data else None

    @property
    def height(self) -> int:
        return self.data.height if self.data else 0

    @property
    def width(self) -> int:
        return self.data.width if self.data else 0

    @property
    def duration(self) -> int:
        return self.data.duration if self.data else 0


@dataclass
class MixMediaInfo:
    items: list[MixMediaInfoItem] | None = None

    @classmethod
    def parse(cls, mix_media_info_dict: dict) -> Self:
        items: list[MixMediaInfoItem] = []
        for item_dict in mix_media_info_dict["items"]:
            type_ = MediaType(item_dict["type"])
            data: PageInfo | PicInfo | None
            if type_ == MediaType.PHOTO:
                data = PicInfo.parse(item_dict["data"])
            elif type_ == MediaType.VIDEO:
                data = PageInfo.parse(item_dict["data"])
            else:
                data = None
            items.append(MixMediaInfoItem(type_, data))
        return cls(items)


@dataclass
class Data:
    id: str | None = None
    mid: str | None = None
    text: str | None = None  # 带html标签
    text_raw: str | None = None  # 纯文本
    pic_infos: list[PicInfo] | None = None
    page_info: PageInfo | None = None
    mix_media_info: MixMediaInfo | None = None
    retweeted_status: "Data | None" = None
    author_name: str = ""
    #: 发布时间, 微博形态 ``"Sat Oct 10 15:17:21 +0800 2026"``（``published_at`` 属性归一化）
    created_at: str | None = None
    #: 点赞数（微博 UI 叫「赞」）。整数或字符串（不同接口形态不同, ``to_int`` 都认）
    attitudes_count: int | str | None = None
    #: 评论数（``reply_count`` 属性转 int）
    comments_count: int | str | None = None
    topic_struct: list[dict[str, Any]] | None = None

    @property
    def published_at(self):
        """发布时间归一化（无时区/or 解析不了 → None, 渲染层跳过那一段）。"""
        return to_datetime(self.created_at)

    @property
    def like_count(self) -> int | None:
        return to_int(self.attitudes_count)

    @property
    def reply_count(self) -> int | None:
        return to_int(self.comments_count)

    @cached_property
    def _anchors(self) -> list[Tag]:
        return BeautifulSoup(self.text or "", "lxml").find_all("a", href=True)

    @property
    def topics(self) -> list[dict[str, str]]:
        """正文里的话题: ``[{"name": 名字(不含#), "url": 话题页地址}, …]``。

        ``url`` 直接取自服务端锚点的 ``href``（``//s.weibo.com/weibo?q=%23…%23``）——
        比按名字自己拼更权威。拿不到（老接口 / 字段缺失）时为空列表, 调用方退回正则。
        """
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for anchor in self._anchors:
            url = str(anchor["href"])
            if not url.startswith(("//s.weibo.com/weibo?", "https://s.weibo.com/weibo?", "http://s.weibo.com/weibo?")):
                continue
            query = parse_qs(urlparse(url).query).get("q", [""])[0]
            if not (query.startswith("#") and query.endswith("#")):
                continue
            name = query.strip("#").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            if url.startswith("//"):
                # 协议相对地址直接塞进 ``<a href>`` 会被当成站内相对路径 —— 补 https:
                url = f"https:{url}"
            out.append({"name": name, "url": url})
        # Mobile deep links carry the search query, not a browser destination.
        for item in self.topic_struct or ():
            if not isinstance(item, dict) or not isinstance(name := item.get("topic_title"), str):
                continue
            name = name.strip().strip("#").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            url = str(item.get("topic_url") or "")
            if url.startswith("//"):
                url = f"https:{url}"
            try:
                parsed = urlparse(url)
            except ValueError:
                parsed = urlparse("")
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                query = parse_qs(parsed.query).get("q", [f"#{name}#"])[0]
                url = f"https://s.weibo.com/weibo?q={quote(query, safe='')}"
            out.append({"name": name, "url": url})
        return out

    @classmethod
    def parse(cls, data_dict: dict) -> Self:
        data_dict = dict(data_dict)
        data_dict["author_name"] = get_author_name(data_dict.get("user"))
        if page_info := data_dict.get("page_info"):
            data_dict["page_info"] = PageInfo.parse(page_info)
        if pic_infos := data_dict.get("pic_infos"):
            data_dict["pic_infos"] = [PicInfo.parse(pic_info) for pic_info in pic_infos.values()]
        if mix_media_info := data_dict.get("mix_media_info"):
            data_dict["mix_media_info"] = MixMediaInfo.parse(mix_media_info)
        if retweeted_status := data_dict.get("retweeted_status"):
            data_dict["retweeted_status"] = Data.parse(retweeted_status)
        return cls.from_kwargs(**data_dict)

    @classmethod
    def from_kwargs(cls, **kwargs: Any) -> Self:
        cls_fields = set(signature(cls).parameters)

        native_args, new_args = {}, {}
        for name, val in kwargs.items():
            if name in cls_fields:
                native_args[name] = val
            else:
                new_args[name] = val

        ret = cls(**native_args)

        for new_name, new_val in new_args.items():
            setattr(ret, new_name, new_val)
        return ret

    @property
    def content(self) -> str:
        """干净的正文"""
        text = self.text_raw or ""
        if short_url := (self.page_info and self.page_info.short_url):
            text = text.replace(short_url, "")
        if self.page_info and self.page_info.object_type == MediaType.VIDEO:
            for anchor in self._anchors:
                if not str(anchor["href"]).startswith(("https://video.weibo.com/show?", "//video.weibo.com/show?")):
                    continue
                # Status responses without page_info.short_url put the injected token on the video-card icon.
                icon = anchor.find("img", class_="icon-link")
                if icon and re.fullmatch(r"https?://t\.cn/[A-Za-z0-9]+", token := str(icon.get("title") or "")):
                    text = re.sub(re.escape(token) + r"(?![A-Za-z0-9])", "", text)
        return text.strip()


@dataclass
class WeiboContent:
    data: Data

    @classmethod
    def parse(cls, json_dict: dict) -> Self:
        data = Data.parse(json_dict)
        return cls(data=data)


@dataclass
class WeiboTVContent:
    text: str
    video_url: str
    video_duration: float
    cover_image: str
    author_name: str = ""
    #: 发布时间: TV 接口给 unix 秒（``real_date``）
    published_at: Any = None
    #: 播放量（TV 接口 ``play_count`` 是**中文缩写** ``"8.1万"`` → parse_weibo_count）
    play_count: int | None = None
    #: 点赞数 / 评论数（TV 接口是整数；to_int 两种都认）
    like_count: int | None = None
    reply_count: int | None = None

    @classmethod
    def parse(cls, json_dict: dict) -> Self:
        data = json_dict["data"]
        cpp = data["Component_Play_Playinfo"]

        cover_image = f"https:{cpp['cover_image']}"
        duration_time = cpp["duration_time"]
        text = cpp["text"]
        urls: dict[str, str] = cpp["urls"]
        video_url = f"https:{list(urls.values())[0]}"
        return cls(
            text=text,
            video_url=video_url,
            video_duration=duration_time,
            cover_image=cover_image,
            author_name=get_author_name(cpp.get("user")) or get_author_name(cpp, "author", "screen_name"),
            published_at=to_datetime(cpp.get("real_date")),
            play_count=parse_weibo_count(cpp.get("play_count")),
            like_count=to_int(cpp.get("attitudes_count")),
            reply_count=to_int(cpp.get("comments_count")),
        )


if __name__ == "__main__":
    print(asyncio.run(WeiboAPI().parse("https://weibo.com/ttarticle/p/show?id=2309405312350592041114")))
