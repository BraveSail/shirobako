"""告警 —— 出事了必须让人知道。

## 背景

2026-11-07 那次 bot 挂了 40 小时无人知：所有失败路径都是「写一行日志 + exit()」。
日志没人翻、容器没人盯，「日志里写清楚」不等于「有人知道」。

这份测试锁住三件事：
① 用 HTTP 直连 Bot API（连接本身坏掉时也能发，这是最关键的场景）；
② 发给配置里的超管（ADMIN_USERS）；
③ **发送失败绝不能反过来搞崩进程** —— 告警是尽力而为。
"""

from __future__ import annotations

import json
import urllib.parse
from unittest.mock import MagicMock, patch

import pytest

from core import alert


@pytest.fixture
def admins(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """假装配了两个超管。

    不能 monkeypatch ``bs.admin_user_ids`` —— 它是 pydantic 的只读 property，
    没有 setter。替换 ``_recipients`` 这个函数更直接，也更贴近
    「告警发给谁」这个真实关注点。
    """
    ids = [111, 222]
    monkeypatch.setattr(alert, "_recipients", lambda: ids)
    return ids


def _ok_response() -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps({"ok": True, "result": {}}).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda *a: None
    return resp


class TestSendAlert:
    def test_sends_to_every_admin(self, admins: list[int]) -> None:
        with patch.object(alert.urllib.request, "urlopen", return_value=_ok_response()) as m:
            ok = alert.send_alert("标题", "详情")

        assert ok is True
        assert m.call_count == len(admins)

    def test_payload_carries_title_and_detail(self, admins: list[int]) -> None:
        with patch.object(alert.urllib.request, "urlopen", return_value=_ok_response()) as m:
            alert.send_alert("会话失效", "已自动重登")

        # payload 是 URL 编码的表单，先解码再断言（中文会被百分号转义）
        raw = m.call_args_list[0][0][0].data.decode()
        sent = urllib.parse.unquote_plus(raw)
        assert "会话失效" in sent
        assert "已自动重登" in sent
        # chat_id 必须是真实数字，不能是占位符
        assert "chat_id=111" in sent

    def test_no_admins_configured_is_not_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """没配 ADMIN_USERS 时返回 False，但**不能抛异常**。"""
        monkeypatch.setattr(alert, "_recipients", lambda: [])
        with patch.object(alert.urllib.request, "urlopen") as m:
            ok = alert.send_alert("标题")
        assert ok is False
        m.assert_not_called()

    def test_network_failure_does_not_raise(self, admins: list[int]) -> None:
        """★ 告警通道自己坏了，也不能把进程搞崩 —— 否则等于二次事故。"""
        with patch.object(
            alert.urllib.request,
            "urlopen",
            side_effect=alert.urllib.error.URLError("no route to host"),
        ):
            ok = alert.send_alert("标题")  # 不该抛
        assert ok is False

    def test_api_rejection_is_reported_but_survivable(self, admins: list[int]) -> None:
        """Telegram 回 ok=false（比如 token 错）时不能崩。"""
        bad = MagicMock()
        bad.read.return_value = json.dumps({"ok": False, "description": "Unauthorized"}).encode()
        bad.__enter__ = lambda s: s
        bad.__exit__ = lambda *a: None
        with patch.object(alert.urllib.request, "urlopen", return_value=bad):
            ok = alert.send_alert("标题")
        assert ok is False

    def test_partial_success_counts(self, admins: list[int]) -> None:
        """一个超管发成功、另一个失败 → 至少发出去了一条，算成功。"""
        calls = {"n": 0}

        def flaky(*a, **kw):  # noqa: ANN002, ANN003, ANN202
            calls["n"] += 1
            if calls["n"] == 1:
                raise alert.urllib.error.URLError("first one fails")
            return _ok_response()

        with patch.object(alert.urllib.request, "urlopen", side_effect=flaky):
            ok = alert.send_alert("标题")
        assert ok is True
