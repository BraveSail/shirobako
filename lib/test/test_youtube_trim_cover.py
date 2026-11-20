"""``trim_cover``: yt3.ggpht.com 的图必须换成 ``=s0`` 才是原图。

## 用户报的问题

> 这条抓的不是高清图 — https://youtube.com/post/UgkxBr0QEfmt2I4saF2hpjKvAuhr-8RjMPEn

当时拿到的是 **512×288 / 241 KB**，而原图是 **1280×720 / 1.13 MB**。

## 根因

旧实现 `url.split("=", 1)[0]` 把尺寸修饰**整个删掉**。实测同一条配图：

| 请求 | 结果 |
| --- | --- |
| 无后缀 | 512×288（服务端默认小图） |
| `=s0` | **1280×720（原图）** |
| `=s288-rw-nd-v1`（页面给的 thumb） | 288×162 |

「去掉修饰」不等于「拿原图」——删掉后服务器给的是默认档，比原图小得多。

## 契约

1. ggpht 域的 URL → 统一改成 `=s0`；
2. 不用 `-c`（方形裁剪）：`=s1280-c` 会裁成 1280×1280，`=s0` 保留原比例；
3. 非 ggpht 域**原样返回**（别的图床 `=` 未必是尺寸语义）；
4. 已经是 `=s0` 的保持幂等。
"""

from __future__ import annotations

import pytest

from parsehub.provider_api.youtube import trim_cover

BASE = "https://yt3.ggpht.com/-52U2BpVK653zUJ15RwSQQENiAPuV2n-4yJDIOrFrZkzIfWY9vG2x98QnP4ojb8Ssbwv95bmNt5H"


class TestGgpht:
    def test_adds_s0_to_bare_url(self) -> None:
        """★ 核心回归：裸 URL 必须变成 =s0，否则只有 512×288。"""
        assert trim_cover(BASE) == f"{BASE}=s0"

    def test_replaces_existing_size(self) -> None:
        assert trim_cover(f"{BASE}=s288-rw-nd-v1") == f"{BASE}=s0"

    def test_replaces_square_crop(self) -> None:
        """``-c`` 是方形裁剪，换成 s0 后拿到原比例。"""
        assert trim_cover(f"{BASE}=s1280-c") == f"{BASE}=s0"

    def test_strips_fcrop_combo(self) -> None:
        """复合修饰（含第二个 ``=``）也要整段换掉，不能只截一半。"""
        url = f"{BASE}=s1080-c-fcrop64=1,00001999ffffe666-rw-nd-v1"
        assert trim_cover(url) == f"{BASE}=s0"

    def test_idempotent(self) -> None:
        once = trim_cover(f"{BASE}=s288-rw-nd-v1")
        assert trim_cover(once) == once

    def test_googleusercontent_host(self) -> None:
        alt = "https://yt3.googleusercontent.com/abc123"
        assert trim_cover(f"{alt}=s176") == f"{alt}=s0"


class TestOtherHosts:
    """别的图床不做处理 —— 它们的 ``=`` 不是尺寸语义。"""

    @pytest.mark.parametrize(
        "url",
        [
            "https://i.imgur.com/abc.jpg",
            "https://pbs.twimg.com/media/xyz.jpg?name=orig",
            "https://i.meee.com.tw/abc.jpg",
            "https://example.com/a.png?size=large",
        ],
    )
    def test_untouched(self, url: str) -> None:
        assert trim_cover(url) == url

    def test_empty(self) -> None:
        assert trim_cover("") == ""
