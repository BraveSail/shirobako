import asyncio
import math
import os
import re
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import aiofiles
from loguru import logger

from ..errors import DownloadError
from . import http

ProgressCallback = Callable[..., Awaitable[None]]

#: 图床/CDN 的**限流**状态码。403 也要算进来: pixiv 的 i.pximg.net 在被短时高频请求
#: 时返回 403(正常防盗链也是 403), 实测同一出口隔 23 秒就能恢复 —— 所以它值得等一等重试,
#: 而不是立刻放弃(用户报「还是 403」的那次, 三次重试都在 1~2 秒内做完, 窗口没过)。
_RATE_LIMIT_STATUS = frozenset({403, 429})

#: 限流退避序列 (秒)。**故意比普通故障长**: 限流窗口通常几十秒, 2**attempt 那种
#: 1s/2s 的节奏在结构上就等不到恢复。
#:
#: 403 与 429 分开: 429 是明确的限流信号, 值得多等; 403 也可能是**稳定**的拒绝
#: (比如 Referer 不对), 等太久会让真正的错误延迟一分钟才报出来。
_BACKOFF_429 = (5.0, 15.0, 45.0)
_BACKOFF_403 = (3.0, 10.0)


def _is_rate_limited(status_code: int) -> bool:
    return status_code in _RATE_LIMIT_STATUS


def _request_headers_of(response: Any) -> dict:
    """从 curl_cffi 的响应里取**真实发出的请求头**。

    curl_cffi 的异常**不带** `.request`（只有 `.response`），但 `Response.request`
    是有的 —— 所以必须从 response 上取。全部用 getattr 兜底: **日志自己抛异常会掩盖
    原始错误**, 那就比没有日志更糟。
    """
    request = getattr(response, "request", None)
    return dict(getattr(request, "headers", None) or {})


def _response_headers_of(response: Any) -> dict:
    return dict(getattr(response, "headers", None) or {})


def _retry_delay(attempt: int, *, status_code: int | None = None) -> float:
    """重试前等多久。

    403/429 走**专用退避** (限流窗口通常几十秒), 其余可重试故障保持指数退避
    (2**attempt) —— 不给普通抖动引入几十秒的等待。
    """
    if status_code == 403:
        return _BACKOFF_403[min(attempt, len(_BACKOFF_403) - 1)]
    if status_code == 429:
        return _BACKOFF_429[min(attempt, len(_BACKOFF_429) - 1)]
    return float(2**attempt)


def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code < 600


def _describe_transport_error(error: BaseException) -> str:
    """把传输类异常讲成**可排障**的一句话。

    重点是**不要**退回成状态码 —— `IncompleteRead` 是 `HTTPStatusError` 的子类，
    历史上就是因为它，缓冲区里那个成功的 206 被拼进了错误信息
    （「分片下载失败: HTTP 206」），把排查方向带偏。
    """
    text = str(error).strip()
    if isinstance(error, http.IncompleteRead):
        # curl 的原话已经很具体：``curl: (18) end of response with N bytes missing``
        return f"响应中途断开（{text}）" if text else "响应中途断开"
    return text or type(error).__name__


@dataclass(frozen=True, slots=True)
class RangeProbe:
    supports_range: bool
    total_size: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    content_encoding: str | None = None


@dataclass(frozen=True, slots=True)
class RangePart:
    index: int
    start: int
    end: int
    path: Path

    @property
    def size(self) -> int:
        return self.end - self.start + 1


class FallbackToSingle(Exception):
    """服务端忽略 Range 时回退到普通单连接下载。"""


class SegmentDownloader:
    def __init__(
        self,
        url: str,
        save_path: str | Path | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        proxy: str | http.Proxy | None = None,
        progress: ProgressCallback | None = None,
        progress_args: tuple = (),
        progress_kwargs: dict[str, Any] | None = None,
        max_retries: int = 3,
        chunk_size: int = 64 * 1024,
        connections: int = 4,
        min_split_size: int = 2 * 1024 * 1024,
        timeout: float | http.Timeout | None = None,
        backup_urls: tuple[str, ...] = (),
    ):
        self.save_path = save_path
        self.headers = dict(headers or {})
        self.proxy = proxy
        self.progress = progress
        self.progress_args = progress_args
        self.progress_kwargs = progress_kwargs or {}
        self.max_retries = max(0, max_retries)
        self.chunk_size = max(1, chunk_size)
        self.connections = max(1, connections)
        self.min_split_size = max(1, min_split_size)
        self.timeout = timeout

        #: 主地址 + 备用地址（同一份内容的多份 CDN 副本）。
        #: **同一个地址内部**先走 `max_retries` 重试；全用尽再换下一个地址**整体重下**
        #: （不逐分片混源 —— 一份文件必须来自同一副本，否则字节可能不一致）。
        self.url_candidates: tuple[str, ...] = (url, *(u for u in backup_urls if u and u != url))
        self._url_index = 0
        #: 备用地址是**换地址**才动，所以要单独记：用它才能判「是否真的回退过」
        self._attempts_on_current_url = 0

        self.resolved_path: Path | None = None
        self.temp_dir: Path | None = None
        self.complete_path: Path | None = None
        self._progress_lock = asyncio.Lock()
        self._downloaded = 0
        self._part_downloaded: dict[int, int] = {}
        #: 本次下载实际切了几个分片 (速度日志要打出来 —— 分片数直接决定并发度)
        self._part_count = 0

    @property
    def url(self) -> str:
        """当前正在用的地址（换备用地址后它跟着变，日志与请求都用这里）。"""
        return self.url_candidates[self._url_index]

    async def run(self) -> str:
        """下载：同一地址内先重试，用尽后换下一个地址（备用 CDN 副本）整体重下。"""
        last_error: Exception | None = None
        for url_index, url in enumerate(self.url_candidates):
            self._url_index = url_index
            if url_index:
                logger.warning(
                    f"主地址下载失败，改用备用地址 ({url_index}/{len(self.url_candidates) - 1}): "
                    f"host={urlparse(url).netloc} 上次错误={last_error}"
                )
            self._attempts_on_current_url = 0
            error = await self._download_current_url()
            if error is None:
                return str(self.resolved_path)
            last_error = error

        raise DownloadError(f"达到最大重试次数，下载失败: {last_error}") from last_error

    async def _download_current_url(self) -> Exception | None:
        """在当前地址上跑完重试预算。成功返回 ``None``，否则返回最后一次错误。"""
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._reset_progress()
            rate_limited = False
            status: int | None = None
            self._attempts_on_current_url = attempt + 1
            try:
                async with self._client() as client:
                    self.resolved_path = await self._resolve_path(client)
                    await self._download_once(client)
                    return None
            except DownloadError as e:
                last_error = e
                if attempt == self.max_retries:
                    return e
            except http.TRANSPORT_ERRORS as e:
                # 同 `_download_part`：必须排在 HTTPStatusError 之前（IncompleteRead 是它的子类）
                last_error = e
                logger.warning(
                    f"下载传输中断: url={self.url} 已收 {self._downloaded} 字节 "
                    f"attempt={attempt + 1}/{self.max_retries + 1} err={e}"
                )
                if attempt == self.max_retries:
                    return DownloadError(f"传输中断: {_describe_transport_error(e)}")
            except http.HTTPStatusError as e:
                status = e.response.status_code
                last_error = e
                rate_limited = _is_rate_limited(status)
                # 失败一定要留下 **URL + 状态 + 响应头**: 以前这里一行日志都没有, 出问题只能靠猜
                # (响应头是判断限流/CDN 节点的关键线索: server / via / cf-ray / retry-after)
                logger.warning(
                    f"下载请求被拒: url={getattr(e.response, 'url', self.url)} status={status} "
                    f"attempt={attempt + 1}/{self.max_retries + 1} rate_limited={rate_limited}\n"
                    f"    请求头 = {_request_headers_of(e.response)}\n"
                    f"    响应头 = {_response_headers_of(e.response)}"
                )
                if attempt == self.max_retries or not (_is_retryable_status(status) or rate_limited):
                    return DownloadError(f"HTTP错误: {status}")
            except (http.TimeoutException, http.NetworkError, http.RemoteProtocolError, http.ReadError) as e:
                last_error = e
                logger.warning(f"下载网络错误: url={self.url} attempt={attempt + 1}/{self.max_retries + 1} err={e}")
                if attempt == self.max_retries:
                    return DownloadError(f"网络连接错误: {e}")
            except Exception as e:
                last_error = e
                if attempt == self.max_retries:
                    return DownloadError(f"下载失败: {e}")

            await asyncio.sleep(_retry_delay(attempt, status_code=status))

        return last_error

    def _client(self) -> http.AsyncClient:
        limits = http.Limits(
            max_connections=max(self.connections + 2, 10),
            max_keepalive_connections=max(self.connections, 1),
        )
        kwargs: dict[str, Any] = {
            "headers": self.headers,
            "proxy": self.proxy,
            "limits": limits,
        }
        if self.timeout is not None:
            kwargs["timeout"] = self.timeout
        return http.AsyncClient(**kwargs)

    async def _resolve_path(self, client: http.AsyncClient) -> Path:
        save_dir, filename = _parse_save_path(self.save_path)
        if not filename:
            filename = await get_filename_by_url(self.url, client)
        if not filename:
            raise DownloadError("无法获取文件名")

        resolved_path = save_dir.joinpath(filename)
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        return resolved_path

    async def _download_once(self, client: http.AsyncClient) -> None:
        resolved_path = self._require_resolved_path()
        self._prepare_temp_dir(resolved_path)
        try:
            probe = await self._probe(client)
            # 只给**传输阶段**计时: probe/merge 不算进速度, 否则小文件会被探测开销拉低
            started = time.monotonic()
            if self._should_use_multipart(probe):
                try:
                    await self._download_multipart(client, probe.total_size or 0)
                except FallbackToSingle:
                    self._cleanup_temp_dir()
                    self._reset_progress()
                    self._prepare_temp_dir(resolved_path)
                    await self._download_single(client, probe.total_size)
            else:
                await self._download_single(client, probe.total_size)
            elapsed = time.monotonic() - started

            os.replace(self._require_complete_path(), resolved_path)
            self._log_speed(elapsed, resolved_path)
        except BaseException:
            self._cleanup_temp_dir()
            raise
        finally:
            self._cleanup_temp_dir()

    async def _probe(self, client: http.AsyncClient) -> RangeProbe:
        total_size: int | None = None
        etag: str | None = None
        last_modified: str | None = None
        content_encoding: str | None = None

        try:
            response = await client.head(
                self.url,
                headers=self._headers({"Accept-Encoding": "identity"}),
                follow_redirects=True,
            )
            if response.status_code < 400:
                total_size = _parse_int(response.headers.get("Content-Length"))
                etag = response.headers.get("ETag")
                last_modified = response.headers.get("Last-Modified")
                content_encoding = response.headers.get("Content-Encoding")
        except http.HTTPError:
            pass

        if self.connections <= 1 or (total_size is not None and total_size < self.min_split_size):
            return RangeProbe(False, total_size, etag, last_modified, content_encoding)
        if _has_non_identity_encoding(content_encoding):
            return RangeProbe(False, total_size, etag, last_modified, content_encoding)

        try:
            response = await client.get(
                self.url,
                headers=self._headers({"Accept-Encoding": "identity", "Range": "bytes=0-0"}),
                follow_redirects=True,
            )
        except http.HTTPError:
            return RangeProbe(False, total_size, etag, last_modified, content_encoding)

        response_encoding = response.headers.get("Content-Encoding")
        if _has_non_identity_encoding(response_encoding):
            return RangeProbe(False, total_size, etag, last_modified, response_encoding)

        if response.status_code == 206:
            parsed_range = _parse_content_range(response.headers.get("Content-Range", ""))
            if parsed_range:
                start, end, range_total = parsed_range
                if start == 0 and end == 0 and range_total and range_total > 0:
                    return RangeProbe(True, range_total, etag, last_modified, response_encoding)
        if response.status_code == 200:
            probed_size = _parse_int(response.headers.get("Content-Length"))
            return RangeProbe(False, probed_size or total_size, etag, last_modified, response_encoding)
        if response.status_code == 416:
            parsed_range = _parse_content_range(response.headers.get("Content-Range", ""))
            range_total = parsed_range[2] if parsed_range else total_size
            return RangeProbe(False, range_total, etag, last_modified, response_encoding)

        return RangeProbe(False, total_size, etag, last_modified, response_encoding or content_encoding)

    async def _download_single(self, client: http.AsyncClient, total_size: int | None) -> None:
        complete_path = self._require_complete_path()
        # ⚠️ **必须带 `Range: bytes=0-`**：有些 CDN 会拒绝"不带 Range 的整段请求"而直接断连。
        # 实测（2026-10-08）googlevideo 的音频流 URL 带 ``rqh=1``（要求 Range 头）：
        # 无 Range 时**稳定** `curl: (56) Connection closed abruptly`，带上就是 206 + 完整内容。
        # 对**支持** Range 的服务器 ``bytes=0-`` 等价于整段（Content-Length 仍是总长），
        # 对**不支持**的服务器会被忽略（200 整段）—— 两边都安全。
        # curl_cffi 用 stream=True + aiter_content, 没有上下文管理器形态
        response = await client.get(
            self.url,
            headers=self._headers({"Accept-Encoding": "identity", "Range": "bytes=0-"}),
            allow_redirects=True,
            stream=True,
        )
        try:
            response.raise_for_status()
            content_encoding = response.headers.get("Content-Encoding")
            response_total = _parse_int(response.headers.get("Content-Length"))
            expected_size = response_total if not _has_non_identity_encoding(content_encoding) else None
            total = expected_size or total_size or 0
            current = 0

            async with aiofiles.open(complete_path, "wb") as f:
                async for chunk in response.aiter_content(chunk_size=self.chunk_size):
                    if not chunk:
                        continue
                    await f.write(chunk)
                    current += len(chunk)
                    await self._report_single(current, total)
        finally:
            await response.aclose()

        if expected_size is not None and current != expected_size:
            raise DownloadError(f"下载不完整: 期望 {expected_size} 字节, 实际 {current} 字节")
        await self._report_finish(total)

    async def _download_multipart(self, client: http.AsyncClient, total_size: int) -> None:
        temp_dir = self._require_temp_dir()
        parts_dir = temp_dir.joinpath("parts")
        parts_dir.mkdir(parents=True, exist_ok=True)
        parts = self._build_parts(total_size, parts_dir)
        self._part_count = len(parts)
        tasks = [asyncio.create_task(self._download_part(client, part, total_size)) for part in parts]

        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        await self._merge_parts(parts, total_size)
        await self._report_finish(total_size)

    async def _download_part(self, client: http.AsyncClient, part: RangePart, total_size: int) -> None:
        for attempt in range(self.max_retries + 1):
            received = 0
            rate_limited = False
            status: int | None = None
            try:
                if part.path.exists():
                    part.path.unlink()
                response = await client.get(
                    self.url,
                    headers=self._headers({"Accept-Encoding": "identity", "Range": f"bytes={part.start}-{part.end}"}),
                    allow_redirects=True,
                    stream=True,
                )
                try:
                    if response.status_code == 200:
                        raise FallbackToSingle
                    response.raise_for_status()
                    if response.status_code != 206:
                        raise DownloadError(f"分片下载失败: HTTP {response.status_code}")
                    if _has_non_identity_encoding(response.headers.get("Content-Encoding")):
                        raise FallbackToSingle
                    self._validate_part_response(part, response.headers, total_size)

                    async with aiofiles.open(part.path, "wb") as f:
                        async for chunk in response.aiter_content(chunk_size=self.chunk_size):
                            if not chunk:
                                continue
                            await f.write(chunk)
                            received += len(chunk)
                            await self._report_part(part.index, received, total_size)
                finally:
                    await response.aclose()

                if received != part.size:
                    raise DownloadError(f"分片大小不匹配: 期望 {part.size} 字节, 实际 {received} 字节")
                return
            except FallbackToSingle:
                raise
            except http.TRANSPORT_ERRORS as e:
                # ⚠️ **必须在 HTTPStatusError 之前**：`IncompleteRead` 是它的子类，
                # 放后面会让「传输中断」被当成「状态码错误」，拿成功码拼错误信息
                # （线上原话：`分片下载失败: HTTP 206`）并且不重试。
                status = None
                logger.warning(
                    f"分片传输中断: url={self.url} Range=bytes={part.start}-{part.end} "
                    f"已收 {received}/{part.size} 字节 attempt={attempt + 1}/{self.max_retries + 1} err={e}"
                )
                if attempt == self.max_retries:
                    raise DownloadError(f"分片传输中断: {_describe_transport_error(e)}") from e
            except http.HTTPStatusError as e:
                status = e.response.status_code
                rate_limited = _is_rate_limited(status)
                logger.warning(
                    f"分片请求被拒: url={getattr(e.response, 'url', self.url)} "
                    f"Range=bytes={part.start}-{part.end} status={status} "
                    f"attempt={attempt + 1}/{self.max_retries + 1} rate_limited={rate_limited}\n"
                    f"    请求头 = {_request_headers_of(e.response)}\n"
                    f"    响应头 = {_response_headers_of(e.response)}"
                )
                if attempt == self.max_retries or not (_is_retryable_status(status) or rate_limited):
                    raise DownloadError(f"分片下载失败: HTTP {status}") from e
            except (http.TimeoutException, http.NetworkError, http.RemoteProtocolError, http.ReadError) as e:
                logger.warning(f"分片网络错误: url={self.url} attempt={attempt + 1}/{self.max_retries + 1} err={e}")
                if attempt == self.max_retries:
                    raise DownloadError(f"分片网络错误: {e}") from e
            except DownloadError:
                if attempt == self.max_retries:
                    raise
            await asyncio.sleep(_retry_delay(attempt, status_code=status))

    async def _merge_parts(self, parts: list[RangePart], total_size: int) -> None:
        complete_path = self._require_complete_path()
        async with aiofiles.open(complete_path, "wb") as target:
            for part in parts:
                if not part.path.exists() or part.path.stat().st_size != part.size:
                    actual = part.path.stat().st_size if part.path.exists() else 0
                    raise DownloadError(f"分片文件不完整: 期望 {part.size} 字节, 实际 {actual} 字节")
                async with aiofiles.open(part.path, "rb") as source:
                    while chunk := await source.read(self.chunk_size):
                        await target.write(chunk)

        actual_size = complete_path.stat().st_size
        if actual_size != total_size:
            raise DownloadError(f"合并后文件大小不匹配: 期望 {total_size} 字节, 实际 {actual_size} 字节")

    def _build_parts(self, total_size: int, parts_dir: Path) -> list[RangePart]:
        part_count = min(self.connections, math.ceil(total_size / self.min_split_size))
        part_count = max(1, part_count)
        part_size = math.ceil(total_size / part_count)
        parts = []
        for index in range(part_count):
            start = index * part_size
            end = min(start + part_size - 1, total_size - 1)
            parts.append(RangePart(index=index, start=start, end=end, path=parts_dir.joinpath(f"{index:06d}.part")))
        return parts

    def _validate_part_response(self, part: RangePart, headers: http.Headers, total_size: int) -> None:
        parsed_range = _parse_content_range(headers.get("Content-Range", ""))
        if not parsed_range:
            raise DownloadError("分片响应缺少 Content-Range")
        start, end, response_total = parsed_range
        if start != part.start or end != part.end:
            raise DownloadError(f"分片范围不匹配: 期望 {part.start}-{part.end}, 实际 {start}-{end}")
        if response_total != total_size:
            raise DownloadError(f"远端文件大小变化: 期望 {total_size}, 实际 {response_total}")

    def _should_use_multipart(self, probe: RangeProbe) -> bool:
        return (
            self.connections > 1
            and probe.supports_range
            and probe.total_size is not None
            and probe.total_size > 0
            and probe.total_size >= self.min_split_size
            and not _has_non_identity_encoding(probe.content_encoding)
        )

    def _prepare_temp_dir(self, resolved_path: Path) -> None:
        self.temp_dir = resolved_path.parent.joinpath(f".{resolved_path.name}.{uuid.uuid4().hex}.parsehub-tmp")
        self.temp_dir.mkdir(parents=True, exist_ok=False)
        self.complete_path = self.temp_dir.joinpath("complete.tmp")

    def _cleanup_temp_dir(self) -> None:
        if self.temp_dir:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir = None
        self.complete_path = None

    async def _report_part(self, index: int, downloaded: int, total: int) -> None:
        # **字节统计与进度回调解耦**: 没有 ``progress`` 回调时也要记账 ——
        # 下载速度日志用的就是这里的 ``_downloaded``, 早退会让它永远是 0。
        async with self._progress_lock:
            previous = self._part_downloaded.get(index, 0)
            if downloaded <= previous:
                return
            self._part_downloaded[index] = downloaded
            self._downloaded += downloaded - previous
            if self.progress:
                await self.progress(self._downloaded, total, *self.progress_args, **self.progress_kwargs)

    async def _report_single(self, downloaded: int, total: int) -> None:
        async with self._progress_lock:
            if downloaded <= self._downloaded:
                return
            self._downloaded = downloaded
            if self.progress:
                await self.progress(downloaded, total, *self.progress_args, **self.progress_kwargs)

    async def _report_finish(self, total: int) -> None:
        if total <= 0:
            return
        async with self._progress_lock:
            if self._downloaded >= total:
                return
            self._downloaded = total
            if self.progress:
                await self.progress(total, total, *self.progress_args, **self.progress_kwargs)

    def _log_speed(self, elapsed: float, path: Path) -> None:
        """打一行下载速度日志: **实际字节数 / 耗时 / 平均速度**。

        字节数取落地文件的大小, 而下载器对完整性是有校验的(单连接比 ``Content-Length``、
        多分片比每个 part 的 size), 所以这个数就是真实传输量 —— 不是预分配的假象
        (本下载器不 truncate 预分配)。

        分片数与 CDN host 一起打出来: 二者是下载速度的主要变量(域名不同能差几十倍)。
        """
        try:
            size = path.stat().st_size
        except OSError:
            return
        speed = size / elapsed / 1048576 if elapsed > 0 else 0.0
        logger.info(
            f"下载完成: {path.name} {size / 1048576:.2f}MB 用时 {elapsed:.2f}s "
            f"平均 {speed:.2f} MB/s | 分片 {self._part_count or 1} | host={urlparse(self.url).netloc}"
        )

    def _headers(self, extra: Mapping[str, str]) -> dict[str, str]:
        merged = dict(self.headers)
        merged.update(extra)
        return merged

    def _reset_progress(self) -> None:
        self._downloaded = 0
        self._part_downloaded.clear()
        self._part_count = 0

    def _require_resolved_path(self) -> Path:
        if self.resolved_path is None:
            raise DownloadError("下载路径尚未初始化")
        return self.resolved_path

    def _require_temp_dir(self) -> Path:
        if self.temp_dir is None:
            raise DownloadError("临时目录尚未初始化")
        return self.temp_dir

    def _require_complete_path(self) -> Path:
        if self.complete_path is None:
            raise DownloadError("临时文件尚未初始化")
        return self.complete_path


async def download(
    url: str,
    save_path: str | Path | None = None,
    *,
    headers: dict[str, str] | None = None,
    proxy: str | http.Proxy | None = None,
    progress: ProgressCallback | None = None,
    progress_args: tuple = (),
    progress_kwargs: dict[str, Any] | None = None,
    max_retries: int = 3,
    chunk_size: int = 64 * 1024,
    connections: int = 4,
    min_split_size: int = 2 * 1024 * 1024,
    timeout: float | http.Timeout | None = None,
    backup_urls: tuple[str, ...] = (),
) -> str:
    """
    下载单个文件。服务端支持 Range 时使用多连接分片下载；不支持时回退普通单连接下载。

    :param url: 下载链接
    :param save_path: 保存路径, 默认保存到 downloads 文件夹, 如果路径以 / 结尾，则自动获取文件名
    :param headers: 请求头
    :param proxy: 代理
    :param progress: 下载进度回调函数
    :param progress_args: 下载进度回调函数的参数
    :param progress_kwargs: 下载进度回调函数的关键字参数
    :param max_retries: 最大重试次数
    :param chunk_size: 分块大小
    :param connections: 单文件最大并发连接数，1 表示禁用分片
    :param min_split_size: 文件小于该值时不分片
    :param timeout: 超时配置
    :param backup_urls: 同一份内容的**备用地址**（同一平台给的多份 CDN 副本）。
        主地址重试耗尽后按序整体换用 —— 某个副本部分损坏时（实测 B 站 Akamai
        镜像在固定偏移断流，curl 18）换副本是唯一可行的规避。
    :return: 文件路径

    .. note::
        下载进度回调函数签名: async def progress(current: int, total: int, *args, **kwargs) -> None:
    """
    downloader = SegmentDownloader(
        url,
        save_path,
        headers=headers,
        proxy=proxy,
        progress=progress,
        progress_args=progress_args,
        progress_kwargs=progress_kwargs,
        max_retries=max_retries,
        chunk_size=chunk_size,
        connections=connections,
        min_split_size=min_split_size,
        timeout=timeout,
        backup_urls=backup_urls,
    )
    return await downloader.run()


async def get_filename_by_url(url: str, client: http.AsyncClient) -> str | None:
    """从 URL 或 HTTP 响应头中获取文件名"""
    try:
        response = await client.head(url, follow_redirects=True)
        response.raise_for_status()
    except http.HTTPError:
        pass
    else:
        if content_disposition := response.headers.get("content-disposition"):
            if filename := _parse_content_disposition(content_disposition):
                return _sanitize_filename(filename)

    parsed = urlparse(url)
    path = unquote(parsed.path).removesuffix("/")
    filename = path.split("/")[-1] if path else None
    return _sanitize_filename(filename) if filename else None


def _parse_save_path(save_path: str | Path | None) -> tuple[Path, str | None]:
    """解析保存路径，返回 (目录, 文件名或 None)"""
    if not save_path:
        return Path.cwd().joinpath("downloads"), None

    save_path_str = str(save_path)
    save_dir_str, filename = os.path.split(save_path_str)
    save_dir = Path(os.path.abspath(save_dir_str)) if save_dir_str else Path.cwd().joinpath("downloads")
    return save_dir, filename if filename else None


def _parse_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _parse_content_range(header: str) -> tuple[int | None, int | None, int | None] | None:
    if match := re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", header.strip(), flags=re.IGNORECASE):
        start = int(match.group(1))
        end = int(match.group(2))
        total = None if match.group(3) == "*" else int(match.group(3))
        return start, end, total
    if match := re.fullmatch(r"bytes\s+\*/(\d+|\*)", header.strip(), flags=re.IGNORECASE):
        total = None if match.group(1) == "*" else int(match.group(1))
        return None, None, total
    return None


def _has_non_identity_encoding(content_encoding: str | None) -> bool:
    if not content_encoding:
        return False
    return content_encoding.lower().strip() not in {"identity", ""}


def _parse_content_disposition(header: str) -> str | None:
    """解析 Content-Disposition 头中的文件名，支持 filename*= 和带引号的 filename="""
    if match := re.search(r"filename\*\s*=\s*[\w-]+'[^']*'(.+?)(?:;|$)", header):
        return unquote(match.group(1).strip())

    if match := re.search(r'filename\s*=\s*"([^"]+)"', header):
        return match.group(1).strip()

    if match := re.search(r"filename\s*=\s*([^\s;]+)", header):
        return match.group(1).strip()

    return None


def _sanitize_filename(filename: str) -> str:
    """清理文件名中不安全的字符"""
    filename = re.sub(r'[<>:"/\\|?*]', "_", filename)
    return filename[:255] if filename else filename
