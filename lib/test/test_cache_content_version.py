"""结果层缓存的正文格式版本（lib serialize 层）。

twitter 的正文格式变过两次，老条目必须被判旧、重新解析 —— 两层缓存
（结果层 5 分钟 + 持久层 7 天）都要处理, 这里钉结果层。

- 1 = richtext 粗体标记 ``**`` 引入（2026-10-13）；
- 2 = 粗体区间换算修正（UTF-16 → code point，2026-10-15）——
  **格式没变但位置错了**, 所以同样必须让老条目重解析。

- **twitter 老条目**（缺 content_version / 版本低于当前）→ 抛 ``ResultRebuildUnavailable``;
- **twitter 新条目** → 正常往返;
- **其它平台**（无论有没有版本）→ 不受影响, 正常往返。
"""

import pytest

from parsehub.types import MultimediaParseResult, Platform
from parsehub.types.serialize import (
    _CACHE_CONTENT_VERSION,
    ResultRebuildUnavailable,
    result_from_cache_dict,
    result_to_cache_dict,
)


def _result(platform: Platform) -> MultimediaParseResult:
    result = MultimediaParseResult(content="正文")
    result.platform = platform
    result.raw_url = "https://x.com/u/status/1"
    return result


def test_roundtrip_carries_the_version():
    """写入的版本号必须**引用常量**（手抄字面量会让 bump 时漏掉这里）。"""
    data = result_to_cache_dict(_result(Platform.TWITTER))
    assert data["content_version"] == _CACHE_CONTENT_VERSION
    rebuilt = result_from_cache_dict(data)
    assert rebuilt.content == "正文"


def test_a_twitter_entry_without_the_version_is_refused():
    """老条目 (没有 content_version) → 抛 → 调用方重解析"""
    data = result_to_cache_dict(_result(Platform.TWITTER))
    del data["content_version"]
    with pytest.raises(ResultRebuildUnavailable):
        result_from_cache_dict(data)


def test_a_twitter_entry_with_an_old_version_is_refused():
    data = result_to_cache_dict(_result(Platform.TWITTER))
    data["content_version"] = _CACHE_CONTENT_VERSION - 1
    with pytest.raises(ResultRebuildUnavailable):
        result_from_cache_dict(data)


def test_a_twitter_entry_from_the_richtext_bold_era_is_refused():
    """v1（有 `**` 标记但**区间算错**）也必须重解析 —— 观感与"没修"一样。"""
    data = result_to_cache_dict(_result(Platform.TWITTER))
    data["content_version"] = 1
    data["content"] = "**27年1月7日(木)毎週**"
    with pytest.raises(ResultRebuildUnavailable):
        result_from_cache_dict(data)


def test_other_platforms_without_the_version_still_roundtrip():
    """其它平台不受这个判据影响 (正文格式没变, 不该白失效)"""
    data = result_to_cache_dict(_result(Platform.THREADS))
    del data["content_version"]
    rebuilt = result_from_cache_dict(data)
    assert rebuilt.content == "正文"
