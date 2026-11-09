"""限流退避 —— 被限流要**等**，不要重启。

## 为什么不重启

Telegram 的 FloodWait 是惩罚期。重启进程只会把登录/请求再打一遍，
惩罚期越拖越长。正确做法是照着 ``value`` 秒数睡过去。

这也是「断线要分类」最实际的收益：以前一律重启，等于把限流越修越重。
"""

from __future__ import annotations

import pytest

from core.watchdog import DEFAULT_FLOOD_WAIT, _flood_seconds


class TestFloodSeconds:
    def test_reads_value_attribute(self) -> None:
        """pyrogram.FloodWait 用 ``value`` 存秒数。"""

        class FloodWait(Exception):
            def __init__(self, value: int) -> None:
                self.value = value
                super().__init__(f"wait of {value} seconds")

        assert _flood_seconds(FloodWait(30)) == 30

    def test_reads_seconds_attribute(self) -> None:
        class Flood(Exception):
            def __init__(self, seconds: int) -> None:
                self.seconds = seconds
                super().__init__("flood")

        assert _flood_seconds(Flood(45)) == 45

    def test_falls_back_to_text(self) -> None:
        assert _flood_seconds(RuntimeError("Flood wait of 120 seconds")) == 120
        assert _flood_seconds(RuntimeError("请等待 90 秒")) == 90

    def test_unknown_uses_default(self) -> None:
        assert _flood_seconds(RuntimeError("something else")) == DEFAULT_FLOOD_WAIT
        assert _flood_seconds(None) == DEFAULT_FLOOD_WAIT

    def test_capped_at_one_hour(self) -> None:
        """异常里给了天文数字也不能睡死。"""

        class Huge(Exception):
            def __init__(self) -> None:
                self.value = 999_999
                super().__init__("flood")

        assert _flood_seconds(Huge()) == 3600

    @pytest.mark.parametrize("bad", [0, -5, None, "abc"])
    def test_ignores_invalid_values(self, bad: object) -> None:
        class Weird(Exception):
            def __init__(self) -> None:
                self.value = bad
                super().__init__("flood")

        assert _flood_seconds(Weird()) == DEFAULT_FLOOD_WAIT
