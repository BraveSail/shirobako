"""UA 单一真源一致性测试。

三处必须逐字相同（同一账号会话的三个环节，UA 不一致会露馅）：
  1. 无头刷新: scripts/yt-cookie/refresh_cookie.py 的 UA
  2. 取流:     lib/src/parsehub/provider_api/youtube_video.py 的 _WEB_USER_AGENT
  3. SABR:     tools/yt-sabr/sabr.mjs 的 DEFAULT_UA

这是**跨文件**的一致性（Python ↔ JS），所以测试直接读文件比对 ——
任何一处被改而另两处没跟上，这个测试立刻红。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
REFRESH_PY = REPO / "scripts" / "yt-cookie" / "refresh_cookie.py"
VIDEO_PY = REPO / "lib" / "src" / "parsehub" / "provider_api" / "youtube_video.py"
SABR_MJS = REPO / "tools" / "yt-sabr" / "sabr.mjs"


def _extract(path: Path, pattern: str, *, flags: int = re.S) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(pattern, text, flags)
    assert m, f"{path.name} 里找不到 UA 定义（正则没匹配上，可能被重构了）"
    return re.sub(r"\s+", " ", m.group(1)).strip()


@pytest.mark.skipif(not REFRESH_PY.exists(), reason="refresh_cookie.py 不在本仓库布局里")
def test_ua_single_source_of_truth() -> None:
    """核心回归: 三处 UA 必须逐字一致。

    背景: 无头刷新曾用 Playwright 默认 ``HeadlessChrome/156``（带 Headless 标记）、
    SABR 工具用 bun 默认 ``Bun/1.4.3``（根本不是浏览器）—— 同一个 cookie 配三种 UA，
    用户点名要求统一（2026-10-16）。
    """
    # 1) refresh_cookie.py: UA = ("..."  "..."）（相邻字符串拼接，可能跨行）
    refresh_raw = _extract(REFRESH_PY, r'^UA = \(\s*(.*?)\s*\)\s*$', flags=re.S | re.M)
    refresh_ua = "".join(re.findall(r'"([^"]*)"', refresh_raw))

    # 2) youtube_video.py: _WEB_USER_AGENT = ("..." "...")
    video_ua_raw = _extract(VIDEO_PY, r'_WEB_USER_AGENT = \(\s*(.*?)\s*\)\n"""', flags=re.S)
    video_ua = "".join(re.findall(r'"([^"]*)"', video_ua_raw))

    # 3) sabr.mjs: DEFAULT_UA = '...'
    sabr_ua = _extract_sabr_ua()

    assert refresh_ua, "refresh_cookie.py 的 UA 解析为空"
    assert video_ua, "youtube_video.py 的 UA 解析为空"
    assert sabr_ua, "sabr.mjs 的 UA 解析为空"

    assert refresh_ua == video_ua, (
        f"无头刷新 UA 与取流 UA 不一致:\n  refresh={refresh_ua!r}\n  video={video_ua!r}"
    )
    assert video_ua == sabr_ua, (
        f"取流 UA 与 SABR 工具 UA 不一致:\n  video={video_ua!r}\n  sabr={sabr_ua!r}"
    )
    # 兜底: 都必须是真浏览器 UA，不能带 Headless / Bun 这类暴露标记
    for label, ua in (("refresh", refresh_ua), ("video", video_ua), ("sabr", sabr_ua)):
        assert "Headless" not in ua, f"{label} 的 UA 带 Headless 标记: {ua!r}"
        assert "Bun" not in ua, f"{label} 的 UA 是 bun 默认: {ua!r}"
        assert ua.startswith("Mozilla/5.0"), f"{label} 的 UA 不像浏览器: {ua!r}"


def _extract_sabr_ua() -> str:
    """从 sabr.mjs 取 ``DEFAULT_UA`` 的值（跨行字符串拼接已在上游压平）。"""
    if not SABR_MJS.exists():
        pytest.skip("sabr.mjs 不在本仓库布局里")
    text = SABR_MJS.read_text(encoding="utf-8")
    m = re.search(r"const DEFAULT_UA\s*=\s*\n?\s*'([^']+)'", text)
    if not m:
        m = re.search(r"const DEFAULT_UA\s*=\s*'([^']+)'", text)
    assert m, "sabr.mjs 里找不到 DEFAULT_UA"
    return m.group(1).strip()
