"""结果层缓存的正文格式版本（lib serialize 层）。

twitter 的正文格式变了（richtext 粗体）, 老条目必须被判旧、重新解析 —— 两层缓存
（结果层 5 分钟 + 持久层 7 天）都要处理, 这里钉结果层。

- **twitter 老条目**（缺 content_version）→ 抛 ``ResultRebuildUnavailable``;
- **twitter 新条目**（content_version=1）→ 正常往返;
- **其它平台**（无论有没有版本）→ 不受影响, 正常往返。
"""

import pytest

from parsehub.types import MultimediaParseResult, Platform
from parsehub.types.serialize import (
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
    data = result_to_cache_dict(_result(Platform.TWITTER))
    assert data["content_version"] == 1
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
    data["content_version"] = 0
    with pytest.raises(ResultRebuildUnavailable):
        result_from_cache_dict(data)


def test_other_platforms_without_the_version_still_roundtrip():
    """其它平台不受这个判据影响 (正文格式没变, 不该白失效)"""
    data = result_to_cache_dict(_result(Platform.THREADS))
    del data["content_version"]
    rebuilt = result_from_cache_dict(data)
    assert rebuilt.content == "正文"
