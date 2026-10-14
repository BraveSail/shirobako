"""缓存格式演进: 页脚元数据版本不足的旧条目要重新解析。

背景（2026-10-14）: 小黑盒的发布时间/点赞/回复接上了，但**光改代码不作废老条目**，
用户重发同一条链接命中的还是老缓存，页脚那几段永远看不到（症状与"没修"一样）。

为什么不能用字段名判（``model_fields_set``）：``reply_count`` 这类字段名早就在 schema 里，
老条目写的是 ``None``，而**平台真没有**（小黑盒确实无浏览量）也是 ``None`` —— 两者在缓存里
长得一模一样。所以用显式版本号 ``CacheParseResult.meta_fields``：写入端写当前版本，
读取端低于当前版本即重新解析。**给新平台接页脚字段时把 ``FOOTER_META_FIELDS_VERSION`` +1。**
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

from services.cache import FOOTER_META_FIELDS_VERSION, PersistentCache

URL = "https://www.xiaoheihe.cn/app/bbs/link/970772a04ea8"


def _payload(*, meta_fields: int | None) -> dict:
    parse_result = {
        "title": "T",
        "author_name": "爱猪人士o",
        "published_at": None,
        "view_count": None,
        "like_count": None,
        "tags": [],
        "markdown_content": "",
        "quoted_media_count": 0,
        "reply_media_count": 0,
    }
    if meta_fields is not None:
        parse_result["meta_fields"] = meta_fields
    return {"parse_result": parse_result, "author_metadata_version": 1}


def _get(payload: dict):
    with patch("services.cache.get_optional", AsyncMock(return_value=json.dumps(payload))):
        return asyncio.run(PersistentCache().get(URL))


def test_entry_without_footer_meta_version_is_refreshed():
    """老条目（没有 meta_fields）→ 重新解析, 否则新接的页脚字段永远看不到"""
    assert _get(_payload(meta_fields=None)) is None


def test_entry_with_older_footer_meta_version_is_refreshed():
    """版本低于当前值也要重解析（下次给别的平台接字段时 +1 即生效）"""
    assert _get(_payload(meta_fields=FOOTER_META_FIELDS_VERSION - 1)) is None


def test_current_footer_meta_version_hits():
    """当前版本的条目照常命中（作废只针对老条目）"""
    assert _get(_payload(meta_fields=FOOTER_META_FIELDS_VERSION)) is not None


def test_new_entries_carry_the_current_version():
    """写入端必须带上当前版本 —— 否则下一次读又会被判旧, 无限重解析。"""
    import types as _types

    from plugins.parse.inline_rich import rich_cache_entry

    fake = _types.SimpleNamespace(
        title="T",
        content="Body",
        platform=None,
        raw_url=URL,
        is_sensitive=False,
        author_name="爱猪人士o",
    )
    entry = rich_cache_entry(fake, media=[])  # type: ignore[arg-type]
    assert entry.parse_result.meta_fields == FOOTER_META_FIELDS_VERSION
    assert "meta_fields" in entry.parse_result.model_fields_set


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
