"""解析结果的缓存 —— 两层都放在 Redis 里。

| 层 | 实例 | TTL | 存什么 |
| --- | --- | --- | --- |
| 结果层 | ``parse_cache`` | 5 分钟 | 解析结果对象 (JSON) |
| 持久层 | ``persistent_cache`` | 7 天 | 解析字段 + 媒体 **file_id** |

几个要点:

- **Redis 是共用实例** (161 上宝塔那个), 所有 key 带 ``bs.cache_key_prefix`` 前缀,
  清空只删前缀内的 key —— 见 ``services/redis_client.py``。
- **Redis 不可用时 bot 照常工作**: 读写失败退化成"没有缓存", 继续走完整解析。
- 持久层原来在 SQLite 表 ``cache`` 里; 换 Redis 后**序列化格式不变**
  (``CacheEntry.model_dump``), 连"旧缓存缺字段要重新解析"的判断也一起保留 ——
  那些检查靠 ``model_fields_set``, 与存储介质无关。
- 结果层原来在进程内 (重启即丢), 现在的值走 ``parsehub.types.serialize`` 往返,
  **媒体类型显式带 kind** (否则重建时区分不出动图/视频)。
"""

import asyncio
import hashlib
import json
import time
from datetime import datetime
from enum import StrEnum
from typing import Any

from parsehub.types.result import AnyParseResult
from parsehub.types.serialize import (
    can_rebuild_from_cache,
    result_from_cache_dict,
    result_to_cache_dict,
)
from pydantic import BaseModel

from core import bs
from log import logger
from services.redis_client import (
    cache_key,
    delete_by_prefix,
    delete_keys,
    get_optional,
    set_with_ttl,
)


class TTLCache:
    """轻量的**进程内** TTL 缓存 (key → 任意值)。

    用途: 门禁那种"能不能用 inline/guest"的**布尔**结果 (见 ``plugins/parse/access.py``) ——
    短 TTL、量小、重启丢了也无所谓, 不值得绕一趟 Redis。

    ⚠️ **解析结果缓存不再用它** —— 那个已经换成 ``ResultCache`` (Redis), 因为要跨进程/跨重启。
    """
    def __init__(self, ttl: float = 300, cleanup_interval: float = 60, maxsize: int = 0):
        self._ttl = ttl
        self._store: dict[str, tuple[Any, float]] = {}
        self._lock = asyncio.Lock()
        self.logger = logger.bind(name="TTLCache")
        self._cleanup_interval = cleanup_interval
        self._cleanup_task: asyncio.Task | None = None
        self._maxsize = maxsize

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.logger.debug(f"缓存未命中: key={key}")
                return None
            value, expire_at = entry
            if time.monotonic() > expire_at:
                self.logger.debug(f"缓存已过期: key={key}")
                del self._store[key]
                return None
            self.logger.debug(f"缓存命中: key={key}")
            return value

    async def set(self, key: str, value: Any, ttl: float | None = None) -> None:
        async with self._lock:
            effective_ttl = ttl or self._ttl
            self.logger.debug(f"缓存写入: key={key}, ttl={effective_ttl}s")
            if key in self._store:
                del self._store[key]
            self._store[key] = (value, time.monotonic() + effective_ttl)
            await self._evict_overflow_locked()

    async def _evict_overflow_locked(self) -> None:
        if self._maxsize <= 0:
            return
        overflow = len(self._store) - self._maxsize
        if overflow <= 0:
            return
        for key in list(self._store)[:overflow]:
            del self._store[key]
        self.logger.debug(f"缓存数量超限, 淘汰最旧缓存: {overflow} 条")

    async def pop(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._store.pop(key, None)
            if entry is None:
                self.logger.debug(f"缓存 pop 未命中: key={key}")
                return None
            value, expire_at = entry
            if time.monotonic() > expire_at:
                self.logger.debug(f"缓存 pop 已过期: key={key}")
                return None
            self.logger.debug(f"缓存 pop 命中: key={key}")
            return value

    async def clear(self) -> int:
        """清空全部条目, 返回清掉的条数。"""
        async with self._lock:
            count = len(self._store)
            self._store.clear()
        self.logger.warning(f"清空全部内存缓存: {count} 条")
        return count

    def start_cleanup(self) -> None:
        """启动后台清理任务（需在事件循环运行后调用）"""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
            self.logger.debug(f"后台清理任务已启动, interval={self._cleanup_interval}s")

    async def _periodic_cleanup(self) -> None:
        while True:
            await asyncio.sleep(self._cleanup_interval)
            async with self._lock:
                now = time.monotonic()
                expired_keys = [k for k, (_, exp) in self._store.items() if now > exp]
                for k in expired_keys:
                    del self._store[k]
                if expired_keys:
                    self.logger.debug(f"定时清理过期缓存: {len(expired_keys)} 条")




class ResultCache:
    """解析结果缓存 (短 TTL)。

    接口保持老的 ``TTLCache`` 那样 (``get`` / ``set`` / ``pop`` / ``clear``),
    调用方不用改。区别是值要能跨进程 —— 所以存 JSON, 而不是 Python 对象。
    """

    def __init__(self, ttl: int = 5 * 60):
        self._ttl = ttl
        self.logger = logger.bind(name="ResultCache")

    @staticmethod
    def _key(url: str) -> str:
        return cache_key("result", hashlib.sha256(url.encode("utf-8")).hexdigest())

    async def get(self, url: str) -> AnyParseResult | None:
        raw = await get_optional(self._key(url))
        if raw is None:
            self.logger.debug(f"结果缓存未命中: url={url}")
            return None
        try:
            return result_from_cache_dict(json.loads(raw))
        except Exception as e:  # noqa: BLE001 - 坏条目按未命中处理, 但别留着反复失败
            self.logger.warning(f"结果缓存内容无效, 已删除: url={url} err={type(e).__name__}: {e}")
            await delete_keys([self._key(url)])
            return None

    async def set(self, url: str, result: AnyParseResult, ttl: int | None = None) -> None:
        # **读不回来的结果不要写**: 例如 yt-dlp 系 (必填运行期句柄 `dl`), 写进去也永远
        # 重建不了 —— 每次读都要"删除 + 重新解析 + 再写", 白占 Redis 还刷 warning 日志。
        if not can_rebuild_from_cache(result):
            self.logger.debug(
                f"结果无法从缓存原样重建, 跳过写入: url={url} impl={type(result).__name__}"
            )
            return
        try:
            payload = json.dumps(result_to_cache_dict(result), ensure_ascii=False)
        except Exception as e:  # noqa: BLE001 - 序列化失败只是没缓存, 不该打断解析
            self.logger.warning(f"结果缓存序列化失败, 跳过写入: url={url} err={type(e).__name__}: {e}")
            return
        await set_with_ttl(self._key(url), payload, ttl or self._ttl)

    async def pop(self, url: str) -> AnyParseResult | None:
        result = await self.get(url)
        if result is not None:
            await delete_keys([self._key(url)])
        return result

    async def clear(self) -> int:
        """清空全部结果缓存, 返回条数。"""
        removed = await delete_by_prefix(cache_key("result", "*"))
        self.logger.warning(f"清空全部结果缓存: {removed} 条")
        return removed


class CacheMediaType(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"
    ANIMATION = "animation"
    DOCUMENT = "document"


class CacheParseResult(BaseModel):
    title: str = ""
    content: str = ""
    #: **富文本正文的源**（``RichTextParseResult.markdown_content``）。
    #:
    #: ⚠️ **必须存它**：``content`` 对 RichText 平台是**派生属性**（``plaintext_content``）
    #: —— 由 markdown 转出来的**纯文本**。只存 content 的话，缓存命中时富文本渲染拿到的是
    #: 纯文本，**引用块、链接、楼层号全部塌掉**（实测：现场 24 行 → 缓存 18 行，
    #: ``> <i><a …>作者</a> …</i>`` 变成一行裸文字）。
    #: 非 RichText 平台没有这个属性，存空串。
    markdown_content: str = ""
    author_name: str = ""
    author_handle: str = ""
    author_url: str = ""
    is_sensitive: bool = False
    published_at: datetime | None = None
    view_count: int | None = None
    like_count: int | None = None
    tags: list[str] = []
    #: 平台短名 (``Platform.id``)。**渲染要用**: 标签页链接等依赖它。
    #: 老缓存没有这个字段 ⇒ 空串 ⇒ 由 ``raw_url`` 兜底推断（见 build_cached_rich_content）。
    platform: str = ""
    #: 末尾有多少个媒体项属于被引用内容, 紧接其前的多少个属于被回复内容
    #: (两者都在各自的引用块内部渲染)
    quoted_media_count: int = 0
    reply_media_count: int = 0
    #: 内容在源站的位置标记（如 linux.do 的楼层号 ``#16``）—— 渲染层接在作者行后
    position_label: str = ""
    #: 正文里每个**引用块的角色**（按出现顺序，``reply`` / ``quoted``）—— 渲染层靠它
    #: 把媒体归到正确的卡片里。
    #:
    #: ⚠️ 老缓存没有这个字段时**不需要重新解析**：渲染层对空 roles 回退到按位置推断，
    #: 行为与声明之前一致。所以这里不加"缺了就叫停"的判据（与上述几个字段不同）。
    quote_roles: list[str] = []
    #: **归属行**（"这条内容属于哪里"，如 bgm 的「小组/条目名 » 讨论」）——
    #: 渲染层放在**标题与作者行之间**。
    #:
    #: 老缓存没有这个字段**不需要重新解析**：最多少显示那一行，不会让别的格式塌掉。
    origin_line: str = ""
    #: **回复数**（页脚那一段）。同上：老缓存缺它只是少显示，不必重新解析。
    reply_count: int | None = None
    #: 页脚元数据的**写入标记**（见下 ``FOOTER_META_FIELDS_VERSION``）。
    #:
    #: 光有 ``reply_count`` 这类字段不够: 它们默认 None, 平台**真的没有**（小黑盒无浏览量）
    #: 与**我们的代码没取**在缓存里长得一模一样, 而 ``model_fields_set`` 区分不出来
    #: —— 老条目（代码还没接这些字段时写的）会被当成"平台就是没有", 用户重发同一条链接
    #: 永远看不到新接的那几段。写入时带上这个键, 读取时缺它（或低于当前版本）就重新解析。
    meta_fields: int = 0
    #: 正文标签名的**平台实体**（twitter ``entities.hashtags[].text`` 等）。
    #: 渲染层用它做精确链接化；缺了会退回正则，日文标点/全角符号处会切错边界。
    hashtags: list[str] = []
    #: **投票数据**（正文里 ``{{poll:N}}`` 占位符的渲染源）—— lib 只出数据，
    #: 表格由渲染层按用户语言拼。每项字段名与 lib ``asdict(Poll)`` 一致：
    #: ``options[{text, count}]`` / ``total_votes`` / ``voters`` / ``title`` / ``is_finished``。
    #:
    #: 老缓存没有这个字段**不需要重新解析**：默认空 ⇒ 占位符不替换（老缓存里本来
    #: 也没有占位符——那是 lib 新版才出的形态），行为与改动前一致。
    polls: list[dict] = []

    #: 正文格式版本（cache 格式演进用）。当前值见 ``CONTENT_VERSION``：
    #: 1 = 正文可能含 lib 层标记（twitter 长文的 richtext 粗体 ``**``，2026-10-13）；
    #: 2 = 粗体区间换算修正（UTF-16 → code point，2026-10-15）——
    #:     格式没变但**位置是错的**，不重解析用户重发会看到一模一样的偏移。
    #:
    #: 缺失/低版本的 **twitter** 条目会被判为旧格式重新解析 —— 否则用户重发链接
    #: 命中的还是旧正文，症状与"没修"一模一样。
    #: 其它平台不检查（它们的正文格式没变，让条目重解析只会白白重下媒体）。
    content_version: int = 0


#: 页脚元数据（发布时间 / 点赞 / 回复 / 作者标识）的**当前写入版本**。
#:
#: **每给一个新平台接上页脚字段就 +1** —— 老条目里那些字段是 None, 与"平台确实没有"
#: 在缓存里没法区分（``model_fields_set`` 也不行: 字段名早就在 schema 里）。不升版本的话
#: 用户重发同一条链接命中的还是老条目, 看不到新接的那几段（症状与"没修"一样）。
FOOTER_META_FIELDS_VERSION = 1


#: ``CacheParseResult.content_version`` 的**当前写入版本**（仅 twitter 检查）。
#:
#: - 1 = 正文可能含 lib 层标记（twitter 长文 richtext 粗体 ``**``，2026-10-13）。
#: - 2 = **粗体区间换算修正**（2026-10-15）：服务端的 ``from_index``/``to_index`` 是
#:   UTF-16 code unit 偏移，取值时按 code point 切 ⇒ 区间前有 emoji 时标记整体左移。
#:   注意这一版**正文格式没变**（仍是 ``**``），但 v1 缓存里存的是**位置错了的标记** ——
#:   不 bump 的话用户重发同一条链接会命中旧条目、看到一模一样的偏移（观感等同"没修"）。
#:
#: ⚠️ 与 lib 侧 ``parsehub.types.serialize._CACHE_CONTENT_VERSION`` 是**同一语义的两个副本**，
#: 改动必须同步（lib 决定结果层缓存，这里决定持久层缓存）。
CONTENT_VERSION = 2


class CacheMedia(BaseModel):
    type: CacheMediaType
    file_id: str
    cover_file_id: str | None = None


class CacheEntry(BaseModel):
    parse_result: CacheParseResult
    media: list[CacheMedia] | None = None
    rich: bool = False
    author_metadata_version: int = 1


class PersistentCache:
    """媒体 file_id + 解析字段的缓存 (长 TTL, 跨重启)。

    key: ``{prefix}parse:{sha256(raw_url)}`` —— 与写入时用的是**同一个 url**
    (``ParseService.get_raw_url`` 的结果), 否则清不掉。
    """

    def __init__(self, ttl: int = 7 * 24 * 60 * 60, disable: bool = False):
        self._ttl = ttl
        self._disable = disable
        self.logger = logger.bind(name="PersistentCache")

    @staticmethod
    def _key(url: str) -> str:
        return cache_key("parse", hashlib.sha256(url.encode("utf-8")).hexdigest())

    async def get(self, url: str) -> CacheEntry | None:
        if self._disable:
            return None

        raw = await get_optional(self._key(url))
        if raw is None:
            self.logger.debug(f"缓存未命中: key={url}")
            return None

        try:
            entry: CacheEntry = CacheEntry.model_validate(json.loads(raw))
        except Exception as e:  # noqa: BLE001 - 坏条目删掉重解析
            self.logger.warning(f"缓存内容无效, 已删除: key={url}, error={e}")
            await delete_keys([self._key(url)])
            return None

        if not entry.parse_result.author_name and "author_metadata_version" not in entry.model_fields_set:
            self.logger.debug(f"旧缓存缺少作者信息, 重新解析: key={url}")
            return None

        if "published_at" not in entry.parse_result.model_fields_set:
            # 旧缓存没有统计字段: 直接复用会让同一条链接第二次发送时缺统计行
            self.logger.debug(f"旧缓存缺少发布时间/浏览量, 重新解析: key={url}")
            return None

        if "like_count" not in entry.parse_result.model_fields_set:
            # 旧缓存没有点赞数: 复用会让页脚缺那一段
            self.logger.debug(f"旧缓存缺少点赞数, 重新解析: key={url}")
            return None

        if entry.parse_result.meta_fields < FOOTER_META_FIELDS_VERSION:
            # 页脚元数据（发布时间/点赞/回复/作者标识）是**逐平台**陆续接的:
            # 老条目里那些字段是 None, 与"平台确实没有"没法区分（``model_fields_set`` 也
            # 不行 —— 字段名早就在 schema 里了）。版本不足说明条目写于某平台接入之前,
            # 复用会让用户重发同一条链接永远看不到新接的那几段。
            self.logger.debug(f"旧缓存页脚元数据版本不足, 重新解析: key={url}")
            return None

        if "tags" not in entry.parse_result.model_fields_set:
            # 旧缓存没有标签: 复用会让 inline 结果缺 tag 行
            self.logger.debug(f"旧缓存缺少标签, 重新解析: key={url}")
            return None

        if "markdown_content" not in entry.parse_result.model_fields_set:
            # 旧缓存**只存了纯文本**（RichText 的 content 是 markdown 转出来的派生值），
            # 复用会让第二次发送时引用块/链接全塌成裸文字 —— 必须重新解析。
            self.logger.debug(f"旧缓存缺少富文本正文源, 重新解析: key={url}")
            return None

        if {"quoted_media_count", "reply_media_count"} - entry.parse_result.model_fields_set:
            # 旧缓存不知道引用块里有没有媒体: 复用会让被引用/被回复内容的图/视频丢掉
            self.logger.debug(f"旧缓存缺少引用媒体信息, 重新解析: key={url}")
            return None

        if entry.parse_result.platform == "twitter" and entry.parse_result.content_version < CONTENT_VERSION:
            # twitter 长文正文的格式/取值变过（richtext 粗体支持 2026-10-13；
            # 粗体区间 UTF-16 换算修正 2026-10-15）:
            # 旧条目里存的是没有 ``**``、或 **位置偏掉** 的正文，复用会让用户以为修复没生效。
            # 只查 twitter —— 其它平台正文格式未变，让它们重解析只会白白重下媒体。
            self.logger.debug(
                f"twitter 旧格式正文 (content_version={entry.parse_result.content_version} "
                f"< {CONTENT_VERSION}), 重新解析: key={url}"
            )
            return None

        self.logger.debug(f"缓存命中: key={url}")
        return entry

    async def set(self, url: str, entry: CacheEntry) -> None:
        if self._disable:
            return

        try:
            payload = json.dumps(entry.model_dump(mode="json"), ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"缓存序列化失败, 跳过写入: key={url}, err={type(e).__name__}: {e}")
            return
        await set_with_ttl(self._key(url), payload, self._ttl)

    async def remove(self, url: str) -> None:
        if self._disable:
            return
        await delete_keys([self._key(url)])

    async def clear(self) -> int:
        """清空全部解析缓存 (含媒体 file_id), 返回清掉的条数。

        这是**重活**: 清掉之后所有人的下一次解析都要重新走一遍完整流程
        (重新下载/重新上传), 所以只给白名单的 ``/purge all`` 用, 且日志记 warning。
        """
        if self._disable:
            return 0

        removed = await delete_by_prefix(cache_key("parse", "*"))
        self.logger.warning(f"清空全部解析缓存: {removed} 条")
        return removed


# 解析结果缓存 5 分钟; 持久缓存 7 天 (TTL 由 Redis 管, 不再需要进程内清理任务)
parse_cache = ResultCache(ttl=5 * 60)
persistent_cache = PersistentCache(disable=bs.cache_disabled)
