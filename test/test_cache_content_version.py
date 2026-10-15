"""缓存格式演进: twitter 旧正文 (无 richtext 标记) 要重新解析。

背景（2026-10-13）: lib 开始把长推文的 richtext 粗体织进正文 (``**…**``)。
**只改代码不作废老条目 = 用户重发链接还是看到旧正文**（缓存命中），症状与"没修"一样。
判据用 ``CacheParseResult.content_version``: 缺(=0) 且平台是 twitter → 判旧, 重解析。

其它平台**不受影响** —— 它们的正文格式没变, 让条目重解析只会白白重下媒体。
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

from services.cache import CONTENT_VERSION, PersistentCache

BASE = {
    "title": "T",
    "author_name": "Author",
    "published_at": None,
    "view_count": None,
    "like_count": None,
    "meta_fields": 1,
    "tags": [],
    "markdown_content": "",
    "quoted_media_count": 0,
    "reply_media_count": 0,
}


def _entry_payload(*, platform: str, content_version: int | None) -> dict:
    parse_result = {**BASE, "platform": platform}
    if content_version is not None:
        parse_result["content_version"] = content_version
    return {"parse_result": parse_result, "author_metadata_version": 1}


def _get(payload: dict):
    with patch("services.cache.get_optional", AsyncMock(return_value=json.dumps(payload))):
        return asyncio.run(PersistentCache().get("https://x.com/u/status/1"))


def test_a_twitter_entry_without_content_version_is_refreshed():
    """旧 twitter 条目 (没有 content_version) → 重新解析"""
    assert _get(_entry_payload(platform="twitter", content_version=None)) is None


def test_a_twitter_entry_with_the_current_version_hits():
    """写了**当前**版本的 twitter 条目 → 正常命中（引用常量, 别手抄字面量）"""
    assert _get(_entry_payload(platform="twitter", content_version=CONTENT_VERSION)) is not None


def test_a_twitter_entry_from_the_previous_version_is_refreshed():
    """上一版（v1: 有 `**` 但区间算错）也要重解析 —— 观感与"没修"一样。"""
    assert _get(_entry_payload(platform="twitter", content_version=CONTENT_VERSION - 1)) is None


def test_other_platforms_are_not_refreshed():
    """其它平台不受这个版本判据影响 (不白白重下媒体)"""
    assert _get(_entry_payload(platform="threads", content_version=None)) is not None


def test_new_entries_carry_the_version():
    """写入端必须带上**当前**版本 —— 否则下一次读又会被判旧, 无限重解析"""
    import types as _types

    from plugins.parse.inline_rich import rich_cache_entry

    parse_result = _types.SimpleNamespace(
        title="T",
        content="正文",
        markdown_content="",
        position_label="",
        quote_roles=[],
        origin_line="",
        reply_count=None,
        hashtags=[],
        polls=[],
        author_name="Author",
        author_handle="",
        author_url="",
        is_sensitive=False,
        published_at=None,
        view_count=None,
        like_count=None,
        tags=[],
        platform=_types.SimpleNamespace(id="twitter"),
    )
    entry = rich_cache_entry(parse_result, [])
    assert entry.parse_result.content_version == CONTENT_VERSION

    # 直接往返一次: 该条目必须能命中 (不被判旧)
    payload = json.loads(entry.model_dump_json())
    payload["author_metadata_version"] = 1
    assert _get(payload) is not None
