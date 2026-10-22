"""解析结果对象的**无损往返**（给缓存用）。

为什么不直接拿 ``to_dict()`` 往返:

1. **媒体类型会丢**。``to_dict()`` 用 ``asdict`` 序列化媒体, 而 ``MediaRef`` 子类
   **没有类型判别字段** —— ``VideoRef`` 与 ``AniRef`` 的字段集几乎相同 (都带 ``duration``),
   反推不出类型。天真往返会把动图当视频发出去, **静默出错**。这里给媒体项加 ``kind``。
2. ``to_dict()`` 是**公开输出格式** (CLI 输出、结果落盘 JSON), 且被既有测试逐字段冻住,
   不该为了缓存改它。
3. ``RichTextParseResult.content`` 是**派生属性** (由 ``markdown_content`` 算出来),
   重建时不能回填 ``content`` —— ``__init__`` 会覆盖掉它 (不报错, 静默不一致)。
4. ``raw_url`` 不在构造参数里, 必须重建后单独赋值。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from typing import Any

from loguru import logger

from ..utils.helpers import to_int
from .media_ref import AniRef, ImageRef, LivePhotoRef, MediaRef, VideoRef
from .platform import Platform
from .post import PostType
from .result import (
    AnyParseResult,
    ImageParseResult,
    MultimediaParseResult,
    ParseResult,
    Poll,
    PollOption,
    RichTextParseResult,
    VideoParseResult,
)


class ResultRebuildUnavailable(RuntimeError):
    """缓存里的**具体结果类无法重建**（通常因为它需要运行期句柄, 如 yt-dlp 系的 ``dl``）。

    调用方应当**重新解析**, 而不是拿一个降级的对象继续用 —— 降级会静默换掉下载逻辑,
    实测症状见 ``result_from_cache_dict`` 里那段注释。
    """


#: 结果层缓存的**正文格式版本**（与 bot 侧 ``CacheParseResult.content_version`` 同一语义）。
#:
#: - 1 = 正文可能含 lib 层标记（twitter 长文 richtext 粗体 ``**``, 2026-10-13 引入）。
#: - 2 = **粗体的区间换算修正**（2026-10-15）：``from_index``/``to_index`` 是
#:   **UTF-16 code unit** 偏移，而取值时按 code point 切 —— 区间前有 emoji（非 BMP）时
#:   标记整体左移（实测 ``[42:54]`` 切出 ``'27年1月7日(木)毎週'``，应为 ``'2027年1月7日(木)'``）。
#:   虽然**格式没变**（还是 ``**``），但 v1 缓存里存的是**位置错了的标记** ——
#:   不 bump 的话用户重发同一条链接会命中旧条目、看到一模一样的错误（观感等同“没修”）。
#:
#: - 3 = **正文图改为原位标记** ``{{media:N}}``（douyin 2026-10-16 / bgm 2026-10-19 /
#:   linux.do 2026-10-21）：旧条目里没有标记 ⇒ 命中就是"图还堆在消息末尾"，
#:   与没修一样（用户会报「图片原位怎么还是坏的」）。v1/v2 的 twitter 条目也一并作废。
#: - 4 = bilibili opus 改用详情页全文 + 图片原位标记（2026-10-22）：旧条目是
#:   ``detail`` 接口的截断摘要（``has_more=true``），正文短、图堆末尾。
#:
#: 读缓存时**缺版本/版本低**（老条目）→ 抛 ``ResultRebuildUnavailable`` 让调用方重解析;
#: **只对正文格式变过的平台**强制（其余平台格式未变, 强制会让它们的缓存白失效）。
_CACHE_CONTENT_VERSION = 4
_CONTENT_VERSION_PLATFORMS = frozenset({"twitter", "douyin", "bangumi", "linuxdo", "bilibili"})


#: 媒体类型标签 —— **序列化必须显式带上**, 否则重建时区分不了 (见模块 docstring)
_KIND_TO_CLASS: dict[str, type[MediaRef]] = {
    "video": VideoRef,
    "image": ImageRef,
    "animation": AniRef,
    "live_photo": LivePhotoRef,
}
_CLASS_TO_KIND = {cls: kind for kind, cls in _KIND_TO_CLASS.items()}

_TYPE_TO_CLASS: dict[str, type] = {
    PostType.VIDEO.value: VideoParseResult,
    PostType.IMAGE.value: ImageParseResult,
    PostType.MULTIMEDIA.value: MultimediaParseResult,
    PostType.RICHTEXT.value: RichTextParseResult,
}

#: 各"具体结果类"对应的**构造参数名**。平台子类都没有自己的 ``__init__``
#: (只覆盖 ``_do_download`` 或声明类属性), 所以按 MRO 里第一个命中的具体类构造即可。
_KIND_TO_PARAM = {
    VideoParseResult: "video",
    ImageParseResult: "photo",
    RichTextParseResult: "richtext",
    MultimediaParseResult: "media",
}


def _all_result_classes() -> dict[str, type]:
    """所有已注册的结果类 (含平台子类), 名字 → 类。

    ⚠️ **必须能恢复平台子类**: 平台专属的下载/渲染行为挂在子类上 —— 例如
    ``PixivParseResult._do_download`` 注入 ``Referer``(i.pximg.net 按它判, 没有就 403)。
    只按 ``PostType`` 重建会把对象降级成通用类, 那些覆盖就不再执行 ——
    症状是"缓存命中时行为变了"(2026-10-05 的 pixiv 403 就是这么来的)。
    """
    # 平台 parser 的 import 会注册子类; 延迟 import 避免循环依赖
    from ..parsers import parser as _parsers  # noqa: F401

    classes: dict[str, type] = {}
    stack: list[type] = [ParseResult]
    seen: set[type] = set()
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        classes[cls.__name__] = cls
        stack.extend(cls.__subclasses__())
    return classes


def _constructor_param(cls: type) -> str | None:
    """按 MRO 找这个类该用哪个构造参数名 (video / photo / richtext / media)。"""
    for base in cls.__mro__:
        if (param := _KIND_TO_PARAM.get(base)) is not None:
            return param
    return None


#: ``build()`` 能提供的构造参数名（与它实际传的 kwargs 一致）
_BUILDABLE_PARAMS = frozenset({
    "title", "author_name", "is_sensitive", "published_at", "view_count", "like_count",
    "author_handle", "author_url", "tags", "hashtags", "quoted_media_count", "reply_media_count",
    "position_label", "topic_label", "quote_roles", "origin_line", "reply_count", "polls", "content",
    "video", "photo", "media",
    "markdown_content",
})


def _required_init_params(cls: type) -> set[str]:
    """这个类 ``__init__`` 里**没有默认值**的参数名（不含 self）。"""
    import inspect

    try:
        sig = inspect.signature(cls.__init__)
    except (TypeError, ValueError):
        return set()
    return {
        name
        for name, param in sig.parameters.items()
        if name != "self"
        and param.default is inspect.Parameter.empty
        and param.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }


def can_rebuild_from_cache(result: AnyParseResult) -> bool:
    """这条结果能不能从缓存里**原样**读回来（同一个类、同一套行为）。

    不能的情况: 类要求**运行期句柄** —— 构造它必须传入一个只有解析现场才有的对象
    （历史上 yt-dlp 系的 ``YtVideoParseResult`` 必填 ``dl``，即一份 info JSON；
    那套类已于 2026-10-08 随 yt-dlp 一并移除，但**这个判据对将来的同类情况依然有效**）。

    这类结果**不要写进结果层缓存**: 写了也永远读不出来 (``result_from_cache_dict`` 会拒绝),
    只会让每次读都触发一次"删除 + 重新解析 + 再写", 并在日志里刷 warning。
    """
    cls = type(result)
    if _constructor_param(cls) is None:
        return False
    return _required_init_params(cls) <= _BUILDABLE_PARAMS


def _platform_from_id(platform_id: str) -> Platform | None:
    """按**短名**找平台枚举。

    ⚠️ 不能用 ``Platform(platform_id)``: 这个枚举每个成员的 value 是
    ``(短名, 显示名)`` 这样的 **tuple** (自定义 ``__init__`` 把短名存进 ``.id``),
    所以按 value 构造永远失败 —— 而失败是静默的 (platform 变 None, 页脚来源消失)。
    """
    return next((platform for platform in Platform if platform.id == platform_id), None)


def _media_to_dict(ref: MediaRef) -> dict[str, Any]:
    if (kind := _CLASS_TO_KIND.get(type(ref))) is None:
        raise TypeError(f"未知媒体类型, 无法序列化: {type(ref).__name__}")
    return {**asdict(ref), "kind": kind}


def _media_from_dict(data: dict[str, Any]) -> MediaRef:
    payload = dict(data)
    kind = payload.pop("kind", None)
    if kind is None:
        # 宁可响亮地失败, 也不猜 —— 猜错会把动图/实况照片当视频静默发错
        raise ValueError(f"缓存里的媒体缺少类型标记 (kind): {sorted(data)}")
    try:
        cls = _KIND_TO_CLASS[kind]
    except KeyError as e:
        raise ValueError(f"缓存里的媒体类型不认识: {kind}") from e
    return cls(**payload)


def _media_payload(media: Any) -> Any:
    """把 ``to_dict`` 风格的 media 字段还原成 ref 对象 (保持单个/列表的形态)。"""
    if media is None:
        return None
    if isinstance(media, list):
        return [_media_from_dict(item) for item in media]
    return _media_from_dict(media)


def _media_to_payload(media: Any) -> Any:
    """序列化 media 字段 (保持单个/列表的形态 —— 形态影响渲染)。"""
    if media is None:
        return None
    if isinstance(media, MediaRef):
        return _media_to_dict(media)
    if isinstance(media, Sequence) and not isinstance(media, str):
        return [_media_to_dict(item) for item in media]
    # 既不是 ref 也不是序列: 说明有平台塞了别的东西。这里不猜 (猜错就是静默发错媒体)
    raise TypeError(f"media 不是媒体对象也不是序列, 无法序列化: {type(media).__name__}")


def _as_list(payload: Any) -> list[Any]:
    """``ImageParseResult(photo=...)`` 内部要迭代, 单值必须包成列表。"""
    if payload is None:
        return []
    return payload if isinstance(payload, list) else [payload]


def _polls_to_payload(polls: Sequence[Poll]) -> list[dict[str, Any]]:
    """投票 → 可 JSON 化的字典列表 (``to_dict()`` 已有同一形态, 这里保持同一份算法)。"""
    return [asdict(poll) for poll in polls]


def _polls_from_payload(payload: Any) -> list[Poll]:
    """字典列表 → ``Poll`` 对象列表; 老缓存没有这个键、或结构异常时退化成空列表。"""
    polls: list[Poll] = []
    for item in payload or []:
        if not isinstance(item, dict):
            continue
        options = [
            PollOption(text=str(option.get("text") or ""), count=to_int(option.get("count")))
            for option in item.get("options") or []
            if isinstance(option, dict)
        ]
        polls.append(
            Poll(
                options=options,
                total_votes=to_int(item.get("total_votes")),
                voters=to_int(item.get("voters")),
                title=str(item.get("title") or ""),
                is_finished=bool(item.get("is_finished")),
            )
        )
    return polls


def result_to_cache_dict(result: AnyParseResult) -> dict[str, Any]:
    """结果对象 → 可 JSON 化的字典 (媒体带 ``kind``, 且记录**具体类名**)。

    ``impl`` 是这套缓存格式自己的字段 (``to_dict()`` 是公开格式, 不动它):
    少了它就只能按 ``type`` 重建, 平台子类会被降级 (见 ``_all_result_classes``)。
    """
    return {
        **result.to_dict(),
        "media": _media_to_payload(result.media),
        # ``to_dict()`` 是**公开输出格式**（被测试逐字段冻住），不往里加字段。
        # 标签是渲染层要用的（精确链接化），只走缓存这套格式。
        "hashtags": list(result.hashtags),
        # 位置标记同理: 渲染层要往作者行上加, 漏了它**缓存命中时楼层号就消失**
        "position_label": result.position_label,
        # 话题片段同理: 渲染层要往作者行上加, 漏了它缓存命中时话题消失
        "topic_label": result.topic_label,
        # 引用块的角色 (按出现顺序) —— 渲染层靠它归位媒体; 漏了它缓存命中时
        # 引用块里的媒体会退化成"按位置猜", 又是一次静默的降级
        "quote_roles": list(result.quote_roles),
        # 归属行（标题与作者之间的那一行）—— 漏了它缓存命中时归属行消失
        "origin_line": result.origin_line,
        # 回复数同理（页脚那一段）
        "reply_count": result.reply_count,
        # 投票 (结构化) —— 漏了它缓存命中时正文里的 ``{{poll:N}}`` 占位符
        # 会找不到数据 (渲染层只能整行删掉, 投票静默消失)
        "polls": _polls_to_payload(result.polls),
        "impl": type(result).__name__,
        # 正文格式版本 —— 缺它的老条目在读回时被判为旧格式重新解析 (见常量定义处)
        "content_version": _CACHE_CONTENT_VERSION,
    }


def result_from_cache_dict(data: dict[str, Any]) -> AnyParseResult:
    """字典 → 结果对象。``result_to_cache_dict`` 的逆运算。"""
    # 正文格式演进 (twitter richtext 粗体): 老条目里存的是没有 ``**`` 的正文,
    # 复用会让用户以为修复没生效 —— 抛给调用方重解析 (ResultCache.get 会删 key 按未命中处理)。
    platform_id = data.get("platform")
    if (
        platform_id in _CONTENT_VERSION_PLATFORMS
        and int(data.get("content_version") or 0) < _CACHE_CONTENT_VERSION
    ):
        raise ResultRebuildUnavailable(
            f"缓存的正文是旧格式 (content_version={data.get('content_version')!r}), "
            f"应为 {_CACHE_CONTENT_VERSION} —— 重新解析"
        )

    # 优先按**具体类名**重建 (平台子类带着平台专属的下载/渲染行为);
    # 老缓存没有 impl, 才退回按 PostType 的通用类
    cls: type | None = None
    if impl := data.get("impl"):
        cls = _all_result_classes().get(impl)
        if cls is None:
            logger.warning(f"缓存里的结果类已不存在, 退回通用类: impl={impl!r}")
    if cls is None:
        try:
            cls = _TYPE_TO_CLASS[data["type"]]
        except KeyError as e:
            raise ValueError(f"缓存里的结果类型不认识: {data.get('type')!r}") from e

    media = _media_payload(data.get("media"))
    published_at = data.get("published_at")
    common: dict[str, Any] = {
        "title": data.get("title", ""),
        "author_name": data.get("author_name", ""),
        "is_sensitive": bool(data.get("is_sensitive", False)),
        "published_at": datetime.fromisoformat(published_at) if published_at else None,
        "view_count": data.get("view_count"),
        "like_count": data.get("like_count"),
        "author_handle": data.get("author_handle", ""),
        "author_url": data.get("author_url", ""),
        "tags": data.get("tags") or [],
        "hashtags": data.get("hashtags") or [],
        "quoted_media_count": data.get("quoted_media_count", 0),
        "reply_media_count": data.get("reply_media_count", 0),
        "position_label": data.get("position_label", ""),
        "topic_label": data.get("topic_label", ""),
        "quote_roles": data.get("quote_roles") or [],
        "origin_line": data.get("origin_line", ""),
        "reply_count": data.get("reply_count"),
        # 老缓存没有这个键 -> 空列表 (渲染层拿不到数据时删掉占位符行)
        "polls": _polls_from_payload(data.get("polls")),
    }

    # content 只在**非 RichText** 时回填 —— 那个类的 content 是派生属性
    # (由 markdown_content 算), 喂回去会被 __init__ 静默覆盖
    content = data.get("content", "")

    def build(target: type) -> AnyParseResult:
        param = _constructor_param(target)
        if param == "video":
            # 视频是唯一可能是**单个** ref 的类型
            return target(video=media, content=content, **common)
        if param == "photo":
            return target(photo=_as_list(media), content=content, **common)
        if param == "richtext":
            # content 是派生属性: 只喂 markdown_content, 由它算 content
            return target(media=media, markdown_content=data.get("markdown_content", ""), **common)
        if param == "media":
            return target(media=media, content=content, **common)
        raise ValueError(f"不认识的结果类型, 无法重建: {target.__name__}")

    try:
        result = build(cls)
    except TypeError as exc:
        # 类**找得到但构造不了** —— 平台子类要求运行期句柄 (历史上是 yt-dlp 系的
        # `YtVideoParseResult` 必填 `dl`, 缓存里不可能有)。**不要退回通用类**:
        #
        # 退通用类 = 静默换上另一套下载逻辑。2026-10-06 实测的故障: YouTube 缓存命中后退成
        # ``VideoParseResult``, 它没有 yt-dlp 的下载实现, 于是走基类的分片下载器去下
        # ``VideoRef.url`` —— 那是 ``www.youtube.com/shorts/...`` 的**页面 URL**,
        # 下回来 1.2MB HTML, 产物不是媒体, 媒体处理阶段直接
        # ``ffprobe failed to get container: {}`` 失败 (用户报的就是这个)。
        # （触发它的那套 yt-dlp 类已于 2026-10-08 移除，但"宁可重新解析也不要换错类"这条
        # 教训与机制都保留 —— 它防的是**任何**构造不出原类的场景。）
        #
        # 抛出去让调用方**重新解析**: ``services/cache.py`` 会把这条缓存删掉并按未命中处理,
        # 于是行为与现场解析完全一致。代价是这些平台不再享受结果层缓存 (它们本来也几乎命中不了
        # 正确行为), 换来的是**不会拿错对象去下载**。
        generic = _TYPE_TO_CLASS[data["type"]]
        if cls is generic:
            raise
        raise ResultRebuildUnavailable(
            f"结果类 {cls.__name__} 需要运行期句柄, 无法从缓存重建 —— 应重新解析"
        ) from exc

    # raw_url 不在构造参数里 (基类里初始为空串), 重建后单独赋回
    result.raw_url = data.get("raw_url", "")
    if (platform_id := data.get("platform")) is not None:
        # 平台枚举改名/移除时 (老缓存) 留 None 而不是抛异常 —— 渲染退化成"拿不到来源"
        result.platform = _platform_from_id(platform_id)
    return result


__all__ = [
    "ResultRebuildUnavailable",
    "can_rebuild_from_cache",
    "result_from_cache_dict",
    "result_to_cache_dict",
]
