"""下载器：服务器发 curl 不认识的 ``content-encoding`` 时要能下下来。

## 真实故障（2026-11-12，用户报 PTT 链接下载失败）

```
下载错误: 达到最大重试次数，下载失败: 下载错误: HTTP错误: 206
```

URL 是 ``https://i.meee.com.tw/4aXaM1d.jpg``，响应头写着
``content-encoding: base64``（**服务器乱标**，body 其实是原始 JPEG ``ff d8 ff``）。
libcurl 见到不认识的编码直接失败，抛 ``RequestException``（本项目别名
``HTTPStatusError``），而 ``e.response.status_code`` 是 **206** —— 于是错误信息里
出现一个「成功码」，且 206 不在可重试集合里，第一次就放弃、一次都不重试。

## 修法契约

- 识别 curl 错误码 61（``Unrecognized content encoding``）；
- 置 ``_disable_content_decoding``，下一次重试时用
  ``CurlOpt.HTTP_CONTENT_DECODING=0`` 建 client；
- **不能全局关**（gzip 响应会拿到压缩字节），所以是「撞上才降级」。
"""

from __future__ import annotations

import asyncio

import pytest

from parsehub.utils import http
from parsehub.utils.downloader import SegmentDownloader, _is_unknown_encoding_error


class TestUnknownEncodingDetection:
    def test_detects_curl_61(self) -> None:
        assert _is_unknown_encoding_error(Exception("Failed to perform, curl: (61) Unrecognized content encoding type."))

    def test_detects_without_number(self) -> None:
        assert _is_unknown_encoding_error(Exception("Unrecognized content encoding"))

    def test_ignores_other_errors(self) -> None:
        assert not _is_unknown_encoding_error(Exception("HTTP错误: 206"))
        assert not _is_unknown_encoding_error(Exception("传输中断: IncompleteRead"))
        assert not _is_unknown_encoding_error(Exception("curl: (18) end of response"))

    def test_ignores_empty(self) -> None:
        assert not _is_unknown_encoding_error(Exception(""))


def _make_downloader() -> SegmentDownloader:
    return SegmentDownloader(
        url="https://i.meee.com.tw/4aXaM1d.jpg",
        save_path="/tmp/x.jpg",
        max_retries=0,
    )


class TestDecodingFlag:
    def test_flag_defaults_off(self) -> None:
        dl = _make_downloader()
        assert dl._disable_content_decoding is False

    def test_client_has_no_curl_options_when_flag_off(self) -> None:
        dl = _make_downloader()
        client = dl._client()
        assert dl._disable_content_decoding is False
        # 不关解码时不该注入这个选项（保留项目默认的 IPv4 curl_options）
        opts = getattr(client, "curl_options", None) or {}
        from curl_cffi import CurlOpt

        assert CurlOpt.HTTP_CONTENT_DECODING not in opts


class TestRetryOnUnknownEncoding:
    """撞上未知编码 → 置位并重试，第二次用关掉解码的 client。"""

    def test_flag_set_and_retries_once(self, monkeypatch) -> None:
        dl = _make_downloader()
        dl.max_retries = 2
        calls: list[bool] = []

        def fake_client():
            calls.append(dl._disable_content_decoding)
            raise _FakeStatusError("Failed to perform, curl: (61) Unrecognized content encoding type.")

        monkeypatch.setattr(dl, "_client", fake_client)
        monkeypatch.setattr(dl, "_resolve_path", _async_none)
        error = asyncio.run(dl._download_current_url())

        # 第一次 flag=False，之后降级
        assert calls[0] is False
        assert dl._disable_content_decoding is True
        assert error is not None
        assert "206" not in str(error), f"错误信息不该再出现成功码: {error}"


class _FakeResponse:
    status_code = 206
    headers: dict = {}
    url = "https://i.meee.com.tw/4aXaM1d.jpg"
    request = None


class _FakeStatusError(http.HTTPStatusError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.response = _FakeResponse()


async def _async_none(*args, **kwargs):
    return None
