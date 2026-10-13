"""分片阈值: **2MB 起分片**（2026-10-13 用户决定, 全局生效）。

背景: 阈值原为 10MB，导致 <10MB 的文件走单连接 —— 而 B 站/微博等 CDN 对单连接限速，
中小文件实测被卡在 0.1-0.7 MB/s（161 历史日志：8.1MB 文件 87 秒）。

分片数公式（未变）: ``min(connections, ceil(大小 / min_split_size))``，connections 默认 4。
新阈值下的实际行为（注意 ceil 的边界）:
- <2MB 完全不分片（1 连接）
- 2–4MB → 2 片；4–6MB → 3 片；6–8MB → 4 片；更多 → 4 片（封顶）

⚠️ 这些断言钉的是**默认值**与**分片公式**——改默认值（如回 10MB）必须同步改这里的
预期数字，否则测试会红（这是故意的：阈值是用户可见的性能契约）。
"""

from pathlib import Path

from parsehub.utils.downloader import SegmentDownloader

MB = 1024 * 1024


def _parts(total: int, *, connections: int = 4, min_split: int | None = None) -> int:
    """按分段器真实逻辑算分片数（用 _build_parts, 与生产同一条路径）。"""
    kwargs = {} if min_split is None else {"min_split_size": min_split}
    dl = SegmentDownloader("https://host/a.mp4", "/tmp/x.mp4", connections=connections, **kwargs)
    return len(dl._build_parts(total, Path("/tmp")))


def test_the_default_threshold_is_2mb():
    dl = SegmentDownloader("https://host/a.mp4", "/tmp/x.mp4")
    assert dl.min_split_size == 2 * MB


def test_the_multipart_gate_starts_at_2mb():
    """**核心**: ≥2MB 的文件会进入分片路径（<2MB 仍单连接）"""

    class _Probe:
        supports_range = True
        total_size = 0
        content_encoding = None

    dl = SegmentDownloader("https://host/a.mp4", "/tmp/x.mp4")
    probe = _Probe()

    probe.total_size = 2 * MB
    assert dl._should_use_multipart(probe) is True  # 恰好 2MB 就走分片

    probe.total_size = 2 * MB - 1
    assert dl._should_use_multipart(probe) is False  # 差一字节仍单连接


def test_the_split_count_formula():
    """分片数 = min(connections, ceil(size / min_split))，逐档核对"""
    assert _parts(MB) == 1  # < 2MB 单连接
    assert _parts(2 * MB - 1) == 1  # 差一字节
    assert _parts(2 * MB) == 1  # ceil(2/2)=1 → 仍是 1 片（进分片路径但恰好整一块）
    assert _parts(3 * MB) == 2  # ceil(3/2)=2
    assert _parts(5 * MB) == 3  # ceil(5/2)=3
    assert _parts(8 * MB) == 4  # ceil(8/2)=4
    assert _parts(100 * MB) == 4  # 封顶 connections=4


def test_connections_still_caps_the_split_count():
    assert _parts(100 * MB, connections=2) == 2
    assert _parts(100 * MB, connections=1) == 1  # connections=1 = 禁用分片


def test_an_explicit_override_keeps_working():
    """显式传参仍然生效（下游可能仍有人传 10MB）"""
    assert _parts(MB, min_split=512) == 4
    assert _parts(10 * MB, min_split=10 * MB) == 1
    assert _parts(11 * MB, min_split=10 * MB) == 2


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
