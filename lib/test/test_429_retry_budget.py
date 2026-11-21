"""429 不跟普通故障共用重试次数。

## 为什么

imgur 这类图床按**出口 IP** 封（实测 152、161 都 429，同一条链接换 WARP
出口立刻 200 / 694829 字节）。原来的退避是 ``(5, 15, 45)`` 共 **65 秒**，
重试不会改变结果，只是把整条 PTT 解析拖到一分多钟 —— 用户报「卡在下载中了」。

现在的语义：

- **429**：最多再试 1 次（5 秒窗口），之后立刻失败，由上层把这一项降级成链接；
- **403**：保持原样（pixiv 的 i.pximg.net 会 403，隔 23 秒能恢复，值得等）；
- 其它 5xx / 网络抖动：照旧用 ``max_retries``。
"""

from __future__ import annotations

import pytest

from parsehub.utils import downloader as dl


class TestBackoffTables:
    def test_429_backoff_is_one_short_wait(self) -> None:
        assert dl._BACKOFF_429 == (5.0,), "429 只应有一档短等待"

    def test_429_backoff_total_is_not_a_minute(self) -> None:
        total = sum(dl._BACKOFF_429)
        assert total <= 10.0, f"429 总退避 {total}s 太久，会拖住整条解析"

    def test_403_keeps_its_two_step_window(self) -> None:
        """403 是 pixiv 那种可恢复限流，不能一起砍短。"""
        assert dl._BACKOFF_403 == (3.0, 10.0)

    def test_429_retry_cap_is_small(self) -> None:
        assert dl._MAX_RETRIES_429 <= 1


class TestRetryDelay:
    def test_429_first_delay(self) -> None:
        assert dl._retry_delay(0, status_code=429) == 5.0

    def test_429_later_attempts_clamp(self) -> None:
        """超出序列长度时取最后一档，不能 IndexError。"""
        assert dl._retry_delay(5, status_code=429) == 5.0

    def test_403_uses_its_own_table(self) -> None:
        assert dl._retry_delay(0, status_code=403) == 3.0
        assert dl._retry_delay(1, status_code=403) == 10.0

    def test_other_status_keeps_exponential(self) -> None:
        assert dl._retry_delay(0) == 1.0
        assert dl._retry_delay(3) == 8.0

    @pytest.mark.parametrize("status", [429, 403])
    def test_rate_limited_is_recognised(self, status: int) -> None:
        assert dl._is_rate_limited(status)

    @pytest.mark.parametrize("status", [404, 410, 200])
    def test_plain_status_is_not_rate_limited(self, status: int) -> None:
        assert not dl._is_rate_limited(status)


class Test429AttemptBudget:
    """模拟 `_download_current_url` 的循环判定：429 应在第 2 次尝试后放弃。"""

    @staticmethod
    def _would_stop(attempt: int, status: int) -> bool:
        """复刻下载器里那条 429 提前返回的判定。"""
        return status == 429 and attempt >= dl._MAX_RETRIES_429

    def test_first_429_retries_once(self) -> None:
        assert not self._would_stop(0, 429), "第一次 429 应允许再试一次"

    def test_second_429_stops(self) -> None:
        assert self._would_stop(1, 429), "第二次 429 必须放弃，不能再拖"

    def test_403_is_not_affected(self) -> None:
        """403 走原路径（由 max_retries 决定），这条判定不该管它。"""
        assert not self._would_stop(1, 403)

    def test_total_wait_for_two_429s(self) -> None:
        """2 次尝试 = 等待一次 = 5 秒（不是原来的 65 秒）。"""
        waits = [dl._retry_delay(a, status_code=429) for a in range(dl._MAX_RETRIES_429)]
        assert sum(waits) == 5.0
