"""统一的 HTTP 客户端。

底层用 **curl_cffi**（真实浏览器 TLS 指纹），对外保持既有的构造风格与异常命名，
这样各个 provider 不必知道底层换成了什么：

    from ...utils import http
    async with http.AsyncClient(proxy=self.proxy, cookies=self.cookie, timeout=30) as cli:
        resp = await cli.get(url, headers=headers)
        resp.raise_for_status()

为什么要浏览器指纹：部分站点（如 linux.do 走 Cloudflare）会按 TLS 指纹拒绝普通客户端，
实测同一份 cookie 下 `httpx → 403`、`curl_cffi(chrome150) → 200`。
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from curl_cffi.const import CurlOpt
from curl_cffi.requests import AsyncSession, Cookies, Headers
from curl_cffi.requests import exceptions as _curl_errors

# ── 原始响应落盘（排查用）────────────────────────────────────────────────
#: 为什么要有: 每次排查平台问题都要重新请求一遍（还可能触发限流），用户明确要求
#: 「原始响应落盘」。这里在**唯一的 HTTP 入口**统一 dump，改一处覆盖所有平台。
#:
#: 开关: env ``PARSEHUB_DUMP_RAW=0`` 关闭；目录 env ``PARSEHUB_RAW_DIR``
#:       （默认 ``data/debug/raw``，容器里就是 bind mount 的 ``/app/data/debug/raw``）。
#: 只落**文本类**响应（json/html/text/xml），图片/视频等二进制一律跳过。
_RAW_DUMP_ENABLED = os.environ.get("PARSEHUB_DUMP_RAW", "1").strip().lower() not in ("0", "false", "no", "off")
_RAW_DUMP_DIR = Path(os.environ.get("PARSEHUB_RAW_DIR") or "data/debug/raw")
_RAW_DUMP_MAX_BYTES = 4 * 1024 * 1024
_RAW_DUMP_KEEP = 500
_TEXTUAL_CTYPES = ("json", "html", "text", "xml", "javascript")


def _prune_raw_dumps(directory: Path, keep: int = _RAW_DUMP_KEEP) -> None:
    """只保留最近 ``keep`` 个原始响应（按 mtime），避免长期运行把磁盘吃满。"""
    try:
        files = sorted(
            (p for p in directory.rglob("*") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in files[keep:]:
            stale.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - 清理失败绝不能影响主流程
        pass


def dump_raw_response(method: str, url: str, response: Any) -> None:
    """把一次 HTTP 响应体落盘（文本类限定）。

    **失败静默** —— 排查设施绝不能改变请求本身的成败。
    """
    if not _RAW_DUMP_ENABLED:
        return
    try:
        content_type = str((getattr(response, "headers", {}) or {}).get("content-type") or "").lower()
        if not any(kind in content_type for kind in _TEXTUAL_CTYPES):
            return
        body = getattr(response, "content", b"") or b""
        if not body or len(body) > _RAW_DUMP_MAX_BYTES:
            return
        host = urlparse(str(url)).netloc or "unknown"
        digest = hashlib.sha1(str(url).encode("utf-8")).hexdigest()[:10]
        ext = "json" if "json" in content_type else ("html" if "html" in content_type else "txt")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = _RAW_DUMP_DIR / host / f"{stamp}_{method.lower()}_{digest}.{ext}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        _prune_raw_dumps(_RAW_DUMP_DIR)
    except Exception:  # noqa: BLE001
        pass

#: 浏览器指纹 —— 集中一处，升级只改这里（curl_cffi 0.16 支持到 chrome150）
IMPERSONATE = "chrome150"

#: 强制**只用 IPv4** 解析（``CurlOpt.IPRESOLVE``: 1=V4, 2=V6, 0=默认）。
#:
#: 为什么默认只用 IPv4：本机 DNS 是 **IPv6 优先**，而有些 CDN/视频资源在 **IPv6 路径上
#: 会被拒**、IPv4 正常。实测推特一条视频：IPv4 → 200、IPv6 → 403
#: （同一条视频、同一客户端、同一 IP 的两种地址族）。
#: 站点普遍仍同时提供 A 与 AAAA；纯 IPv6 的站点极罕见，真遇到再单独放开。
FORCE_IPV4: dict[Any, Any] = {CurlOpt.IPRESOLVE: 1}

# ── 异常别名 ─────────────────────────────────────────────────────────────
# curl_cffi 的异常粒度比 httpx 粗（NetworkError/RemoteProtocolError/ReadError 都归到少数几类），
# 这里映射成调用点已经在用的名字，语义都是"网络/协议失败，可重试或报错"。
HTTPError = _curl_errors.RequestException
"""请求失败的基类（对应原来的 httpx.HTTPError）。"""
HTTPStatusError = _curl_errors.HTTPError
"""raise_for_status() 抛出的状态码错误，带 .response。"""
IncompleteRead = getattr(_curl_errors, "IncompleteRead", _curl_errors.HTTPError)
"""服务端在响应中途断流（curl 18 ``end of response with N bytes missing``）。

⚠️ **它继承 ``HTTPError``**，也就是本项目别名的 ``HTTPStatusError``。
所以捕获 ``HTTPStatusError`` 的地方必须**让它先命中**，否则传输中断会被当成
状态码错误处理 —— 历史表现就是拿成功码拼出「分片下载失败: HTTP 206」
并且因为 206 不在可重试集合里而完全不重试。

旧版 curl_cffi 没有这个类（用 getattr 兜底到 HTTPError，行为退回旧状，不会 ImportError）。
"""

#: 传输层错误 —— 请求过程本身断了，与「服务器回答了但状态码不对」是**两回事**。
#: 捕获 ``HTTPStatusError`` 之前必须先让这里命中（`IncompleteRead` 是它的子类）。
TRANSPORT_ERRORS: tuple[type[Exception], ...] = (
    IncompleteRead,
    _curl_errors.Timeout,
    _curl_errors.ConnectionError,
    _curl_errors.ContentDecodingError,
    _curl_errors.SessionClosed,
    _curl_errors.ProxyError,
)
TimeoutException = _curl_errors.Timeout
ReadTimeout = _curl_errors.ReadTimeout
NetworkError = _curl_errors.ConnectionError
ConnectTimeout = _curl_errors.ConnectionError
RemoteProtocolError = _curl_errors.RequestException
ReadError = _curl_errors.ReadTimeout
RequestError = _curl_errors.RequestException
TooManyRedirects = _curl_errors.TooManyRedirects

# ── 类型占位（原来写 httpx.Proxy / httpx.Timeout / httpx.Headers 的地方）────
Proxy = str | Any
Timeout = float


class Limits:
    """连接池上限的占位（curl_cffi 用 max_clients 表达）。"""

    def __init__(
        self,
        max_keepalive_connections: int | None = None,
        max_connections: int | None = None,
        keepalive_expiry: float | None = None,
    ) -> None:
        self.max_keepalive_connections = max_keepalive_connections
        self.max_connections = max_connections
        self.keepalive_expiry = keepalive_expiry


#: 默认超时 ``(连接, 读/低速容忍)`` 秒。
#:
#: **必须显式给**: curl_cffi 的 ``timeout`` 默认是 ``None``, 源码里直接翻成
#: ``0 = indefinitely``(永不超时); 而 httpx 的默认是 5 秒。迁移后如果沿用
#: "不传就不设"的写法, 任何一次网络抖动都会让请求**永久挂起** ——
#: 表现就是 bot 既不报错也不回消息 (连接还在, 但协程永不返回)。
#:
#: 对 ``stream=True`` 的下载, curl_cffi 会把它翻成低速检测
#: (``LOW_SPEED_LIMIT=1`` + ``LOW_SPEED_TIME``): 只要数据还在流动就不会中断,
#: 所以下载大视频不会被总时长误杀。
DEFAULT_TIMEOUT: tuple[float, float] = (15, 60)


class AsyncClient(AsyncSession):
    """``curl_cffi.AsyncSession`` 的外壳，构造参数与原来的 httpx 用法一一对应。"""

    def __init__(
        self,
        *,
        proxy: Any = None,
        proxies: Any = None,
        cookies: Any = None,
        headers: Any = None,
        timeout: Timeout | None = None,
        follow_redirects: bool | None = None,
        limits: Any = None,
        **kwargs: Any,
    ) -> None:
        params: dict[str, Any] = {"impersonate": IMPERSONATE}
        params.update(kwargs)
        # 调用方没显式指定时, 默认只用 IPv4 (见 FORCE_IPV4 的原因)
        opts = dict(params.get("curl_options") or {})
        for key, value in FORCE_IPV4.items():
            opts.setdefault(key, value)
        params["curl_options"] = opts

        target = proxy if proxy is not None else proxies
        if target:
            # httpx 收单个代理字符串；curl_cffi 要 {scheme: url}。也接受 httpx.Proxy 这类带 .url 的对象
            url = getattr(target, "url", target)
            params["proxies"] = url if isinstance(url, dict) else {"all": str(url)}
        if cookies is not None:
            params["cookies"] = cookies
        if headers is not None:
            params["headers"] = headers
        # 无条件设置: 见 DEFAULT_TIMEOUT 的说明, 让"忘了传"不再等于"永不超时"
        params["timeout"] = DEFAULT_TIMEOUT if timeout is None else timeout
        if follow_redirects is not None:
            # httpx 默认不跟随重定向，curl_cffi 默认跟随 —— 必须显式对齐，
            # 否则原本依赖"不跟随"的地方（如下载器的 Range 探测）会静默改变行为
            params["allow_redirects"] = follow_redirects
        if limits is not None:
            max_conn = getattr(limits, "max_connections", None)
            if max_conn:
                params.setdefault("max_clients", max_conn)

        super().__init__(**params)
        self._closed = False

    @property
    def is_closed(self) -> bool:
        """curl_cffi 没有这个属性，但调用点用它判断"要不要重建客户端"。"""
        return self._closed

    async def aclose(self) -> None:
        """补齐 httpx 风格的 ``aclose()``（调用点用的是它）。

        注意 curl_cffi 的 ``close()`` **本身是协程** —— 不 await 的话连接不会被关闭，
        只会留下 ``coroutine 'AsyncSession.close' was never awaited`` 警告和一条泄漏的连接。
        """
        if self._closed:
            return
        self._closed = True
        await self.close()

    async def __aexit__(self, *args: Any) -> None:
        self._closed = True
        await super().__aexit__(*args)

    async def request(self, method: str, url: str, **kwargs: Any):  # type: ignore[override]
        """请求级也接受 ``follow_redirects``（httpx 的写法），翻译成 curl_cffi 的 ``allow_redirects``。

        并顺手把**原始响应**落盘（排查用；见 :func:`dump_raw_response`）。
        只落文本类、写盘失败静默 —— 不影响请求本身。
        """
        if "follow_redirects" in kwargs:
            kwargs["allow_redirects"] = kwargs.pop("follow_redirects")
        response = await super().request(method=method, url=url, **kwargs)
        dump_raw_response(method, str(url), response)
        return response


def new_client(**kwargs: Any) -> AsyncClient:
    """构造客户端的简写（需要显式调用点时用）。"""
    return AsyncClient(**kwargs)


__all__ = [
    "DEFAULT_TIMEOUT",
    "FORCE_IPV4",
    "IMPERSONATE",
    "AsyncClient",
    "ConnectTimeout",
    "Cookies",
    "HTTPError",
    "HTTPStatusError",
    "Headers",
    "Limits",
    "NetworkError",
    "Proxy",
    "ReadError",
    "ReadTimeout",
    "RemoteProtocolError",
    "RequestError",
    "Timeout",
    "TimeoutException",
    "TooManyRedirects",
    "new_client",
]
