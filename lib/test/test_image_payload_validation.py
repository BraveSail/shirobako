"""下载结果不是图片时必须立刻报错，而不是留给 Pillow 抛 UnidentifiedImageError。

## 用户报的问题

    ▎下载错误:
    cannot identify image file '/app/downloads/新聞-久留美-…/001_….gif'

## 根因

媒体下载走 ``https://imgur.com/WLAHkWh.gif``（**网页地址，缺 ``i.`` 前缀**）。

- 该地址先 **302** 到 ``i.imgur.com/WLAHkWh.gif``
- 跳转后拿到 **429**（imgur 对服务器 IP 限流），响应体是 HTML 错误页
- 下载器只要 HTTP 200 就把 body 落盘 ⇒ 8 KB 的 ``<!doctype html>`` 存成 ``.gif``
- Pillow 打开时抛 ``cannot identify image file``，看起来像「文件坏了」

## 契约

1. 落盘内容头部不是已知图片魔数 ⇒ 立刻抛 ``DownloadError``，**信息里点明 URL 和"不是图片"**；
2. HTML 错误页要额外提示「通常是 429/403 错误页或需要跟随跳转」；
3. 正常图片（GIF/JPEG/PNG/WEBP/BMP/TIFF）一律放行，不因后缀不符而拒绝
   —— 后缀撒谎是另一个已知问题（`_TG_PHOTO_SUFFIX`），这里只看内容。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from parsehub.errors import DownloadError
from parsehub.types.result import _ensure_image_payload, _sniff_image

GIF = b"GIF89a" + b"\x00" * 40
GIF87 = b"GIF87a" + b"\x00" * 40
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 40
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
WEBP = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 40
BMP = b"BM" + b"\x00" * 40
HTML_PAGE = b"<!doctype html>\n<html><head><title>429</title></head></html>"


class TestSniff:
    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            (GIF, "GIF"),
            (GIF87, "GIF"),
            (JPEG, "JPEG"),
            (PNG, "PNG"),
            (WEBP, "WEBP"),
            (BMP, "BMP"),
            (HTML_PAGE, ""),
            (b"", ""),
        ],
    )
    def test_sniff(self, tmp_path: Path, payload: bytes, expected: str) -> None:
        p = tmp_path / "f.bin"
        p.write_bytes(payload)
        assert _sniff_image(p) == expected


class TestEnsurePayload:
    @pytest.mark.parametrize("payload", [GIF, JPEG, PNG, WEBP, BMP])
    def test_real_image_passes(self, tmp_path: Path, payload: bytes) -> None:
        p = tmp_path / "ok.gif"
        p.write_bytes(payload)
        _ensure_image_payload(p, "https://i.imgur.com/x.gif")  # 不抛

    def test_html_error_page_raises(self, tmp_path: Path) -> None:
        """★ 核心回归：HTML 错误页被当图片落盘时必须立刻报错。"""
        p = tmp_path / "bad.gif"
        p.write_bytes(HTML_PAGE)
        with pytest.raises(DownloadError) as exc:
            _ensure_image_payload(p, "https://imgur.com/WLAHkWh.gif")
        msg = str(exc.value)
        assert "不是图片" in msg
        assert "imgur.com/WLAHkWh.gif" in msg, "错误信息必须带上是哪个 URL"
        assert "HTML" in msg, "要提示这是 HTML 错误页"

    def test_plain_garbage_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "junk.gif"
        p.write_bytes(b"\x00\x01\x02\x03" * 100)
        with pytest.raises(DownloadError) as exc:
            _ensure_image_payload(p, "https://example.com/x.gif")
        assert "不是图片" in str(exc.value)

    def test_zero_byte_file_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.gif"
        p.write_bytes(b"")
        with pytest.raises(DownloadError):
            _ensure_image_payload(p, "https://example.com/x.gif")

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(DownloadError):
            _ensure_image_payload(tmp_path / "nope.gif", "https://example.com/x.gif")


class TestRealCase:
    """复现用户那条：imgur 网页地址拿到的 HTML 错误页。"""

    def test_imgur_webpage_html(self, tmp_path: Path) -> None:
        p = tmp_path / "001_新聞-久留美-開播以來台股慘摔近萬點.gif"
        # 实测 8313 字节的响应体，头是 doctype
        p.write_bytes(HTML_PAGE + b"\x00" * 8100)
        with pytest.raises(DownloadError) as exc:
            _ensure_image_payload(p, "https://imgur.com/WLAHkWh.gif")
        assert "不是图片" in str(exc.value)
