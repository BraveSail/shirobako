"""B 站视频地址：**优先 bilivideo 副本，从响应给的副本里挑，不改写域名**。

决策历史:
- 2026-10-06 定案「用 B 站给的 ``durl.url`` 首选」—— 当时的对照里 ``durl.url``
  就是 bilivideo，而旧代码的 ``change_source()`` 会取 ``backup_url[0]``（akamai）再
  **改写域名**，两半都错（首选搞反 + 伪造签名，实测 403/超时）。
- 2026-10-13 改为「**优先 bilivideo 副本**」——161 上 18 条历史下载统计:
  bilivideo 平均 8.99 MB/s、0 次中断；akamai 平均 3.42 MB/s、6 次中断
  （有过主地址连断 4 次才切备用）。而 B 站会把 akamai 当首选发给某些视频。
  挑选范围仍**只限响应给的副本**（``durl.url`` + ``backup_url[]``）—— 不改写域名。
"""

import asyncio
from unittest.mock import AsyncMock, patch

from parsehub.parsers.parser.bilibili import BiliParse
from parsehub.provider_api.bilibili import BiliAPI

INFO = {
    "data": {
        "View": {
            "cid": 111,
            "duration": 198,
            "dimension": {"width": 1914, "height": 1080},
            "desc": "简介",
            "title": "标题",
            "pic": "https://i2.hdslb.com/x.jpg",
            "pubdate": 1791208244,
            "owner": {"mid": 224267770, "name": "作者"},
            "stat": {"view": 100},
        }
    }
}

#: 两种 CDN 副本（各自带签名，指向同一份内容的不同镜像）
BILIVIDEO = "https://upos-sz-mirrorcosov.bilivideo.com/upgcxcode/1/path.mp4?e=sig"
AKAMAI = "https://upos-hz-mirrorakam.akamaized.net/upgcxcode/1/path.mp4?e=sig"


def _parse(playurl_payload: dict):
    async def fake_video_info(*args, **kwargs):  # noqa: ANN002, ANN003
        return INFO

    with (
        patch.object(BiliAPI, "get_video_info", new=AsyncMock(side_effect=fake_video_info), create=True),
        patch.object(BiliAPI, "get_buvid", new=AsyncMock(return_value=("b3", "b4"))),
        patch.object(BiliAPI, "get_video_playurl", new=AsyncMock(return_value=playurl_payload)),
    ):
        return asyncio.run(BiliParse()._do_parse("https://www.bilibili.com/video/BV1q4ab6FECm"))


# ── 主场景: bilivideo 在响应里（无论它是首选还是备用）都该被选中 ──────


def test_bilivideo_is_preferred_when_it_is_the_backup():
    """**核心（2026-10-13）**: B 站把 akamai 当首选、bilivideo 作备用时,

    选 bilivideo（161 实测它快 2.6 倍、0 中断）。这是本次改动的主场景。
    """
    result = _parse({"data": {"durl": [{"url": AKAMAI, "backup_url": [BILIVIDEO]}]}})
    assert result.media.url == BILIVIDEO, result.media.url
    # 被降级的原首选继续作备用
    assert AKAMAI in result.media.backup_urls


def test_bilivideo_stays_when_it_is_already_the_preferred():
    result = _parse({"data": {"durl": [{"url": BILIVIDEO, "backup_url": [AKAMAI]}]}})
    assert result.media.url == BILIVIDEO
    assert AKAMAI in result.media.backup_urls


def test_falls_back_to_the_response_when_no_bilivideo_copy_exists():
    """响应里没有 bilivideo 副本时**保持原样** —— 绝不能凭空造地址（伪造签名）"""
    result = _parse({"data": {"durl": [{"url": AKAMAI}]}})
    assert result.media.url == AKAMAI


def test_the_host_is_never_rewritten():
    """任何情况下都不改写域名/路径 —— B 站签名与 host 绑定, 改写 = 伪造"""
    for payload in (
        {"data": {"durl": [{"url": AKAMAI, "backup_url": [BILIVIDEO]}]}},
        {"data": {"durl": [{"url": BILIVIDEO, "backup_url": [AKAMAI]}]}},
        {"data": {"durl": [{"url": AKAMAI}]}},
    ):
        result = _parse(payload)
        assert "upcdnbda2" not in result.media.url
        # 选中的地址必须**逐字**来自响应
        assert result.media.url in (BILIVIDEO, AKAMAI)


def test_all_remaining_copies_become_backups():
    """选中 bilivideo 后, 其余副本全部进备用（含原首选 akamai）"""
    result = _parse({"data": {"durl": [{"url": AKAMAI, "backup_url": [BILIVIDEO]}]}})
    assert result.media.url == BILIVIDEO
    assert result.media.backup_urls == (AKAMAI,)


def test_backup_urls_keep_the_host_untouched():
    """备用地址必须**原样**保留（它同样带签名）"""
    result = _parse({"data": {"durl": [{"url": AKAMAI, "backup_url": [BILIVIDEO]}]}})
    assert AKAMAI in result.media.backup_urls


def test_backup_urls_is_empty_when_absent():
    """响应里没有 backup_url 且首选就是 bilivideo 时, 备用为空"""
    result = _parse({"data": {"durl": [{"url": BILIVIDEO}]}})
    assert result.media.url == BILIVIDEO
    assert result.media.backup_urls == ()


def test_duplicates_are_dropped():
    """同一地址重复出现（首选与备用相同）时去重, 不重复放进备用"""
    result = _parse({"data": {"durl": [{"url": BILIVIDEO, "backup_url": [BILIVIDEO]}]}})
    assert result.media.url == BILIVIDEO
    assert result.media.backup_urls == ()


def test_change_source_is_gone():
    """那个改域名的函数已删除（防止有人把它加回来）"""
    assert not hasattr(BiliParse, "change_source")


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
