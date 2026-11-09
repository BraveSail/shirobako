"""断线分类 —— 不同类型必须走不同处置。

## 背景

2026-11-07 那次 bot 静默 40 小时：``AuthKeyNotFound`` 让 pyrogram 的
``recv_task`` 死掉，但**不触发** disconnect 回调，watchdog 的计数停在 1
（日志里还写着"重连成功"）。恢复连接后才发现挂了快两天。

## 两个 pyrogram 版本行为不同（实测）

- 生产 2.2.26：抛 ``AuthKeyNotFound``（``pyrogram.session.session`` 里定义）
- 开发 2.0.106：**不抛**，只打 ``transport error: 404 (auth key not found)``
  然后无限重连

所以分类器既要认异常名，也要认错误文本 —— 后者是旧版本上唯一的线索。

这份测试锁住两件事：
① 分类正确（会话死亡 vs 网络抖动 vs 限流 vs 配置错）；
② 只有会话死亡才允许删 session —— 误删等于白丢一份好凭证。
"""

from __future__ import annotations

import pytest

from core.disconnect import (
    DisconnectKind,
    classify,
    needs_session_reset,
    should_reconnect,
)


def fake_exc(name: str, message: str = "") -> BaseException:
    """按**类名**造一个异常。

    不用 ``pyrogram.errors`` 里的真类 —— 它们在 2.0.x / 2.2.x 之间增删过，
    直接导入会让测试在其中一个版本上收集失败。分类器本来就是按名字匹配的，
    这里照同样的契约造样本即可。
    """
    return type(name, (Exception,), {})(message)


#: 会话已死：密钥被服务端作废 / 注销 / 重复使用
SESSION_DEAD_EXC = [
    ("AuthKeyNotFound", "Auth key not found in the system."),
    ("AuthKeyUnregistered", ""),
    ("AuthKeyInvalid", ""),
    ("AuthKeyPermEmpty", ""),
    ("AuthKeyDuplicated", ""),
    ("SessionRevoked", ""),
    ("SessionExpired", ""),
    ("UserDeactivated", ""),
    ("UserDeactivatedBan", ""),
]


class TestSessionDead:
    """密钥/授权已作废 —— 只能删 session 重登。"""

    @pytest.mark.parametrize(("name", "msg"), SESSION_DEAD_EXC)
    def test_classified_as_session_dead(self, name: str, msg: str) -> None:
        assert classify(fake_exc(name, msg)) is DisconnectKind.SESSION_DEAD

    def test_only_session_dead_triggers_reset(self) -> None:
        assert needs_session_reset(DisconnectKind.SESSION_DEAD) is True
        for kind in (
            DisconnectKind.NETWORK,
            DisconnectKind.FLOOD,
            DisconnectKind.CONFIG,
            DisconnectKind.UNKNOWN,
        ):
            assert needs_session_reset(kind) is False, f"{kind} 不该删 session"


class TestNetwork:
    """抖动 —— 原样重连，别动 session。"""

    @pytest.mark.parametrize(
        ("name", "msg"),
        [
            ("Timeout", ""),
            ("InternalServerError", ""),
            ("ConnectionResetError", "connection reset by peer"),
            ("OSError", "network unreachable"),
            ("InvalidDC", ""),
        ],
    )
    def test_classified_as_network(self, name: str, msg: str) -> None:
        assert classify(fake_exc(name, msg)) is DisconnectKind.NETWORK


class TestFlood:
    """限流 —— 要等，重启会加重惩罚。"""

    @pytest.mark.parametrize("name", ["TransportFlood", "Flood", "FloodWait"])
    def test_classified_as_flood(self, name: str) -> None:
        kind = classify(fake_exc(name, "slow down"))
        assert kind is DisconnectKind.FLOOD
        assert needs_session_reset(kind) is False


class TestConfig:
    """配置错 —— 重启和重登都救不了。"""

    @pytest.mark.parametrize("name", ["ApiIdInvalid", "AccessTokenInvalid"])
    def test_classified_as_config(self, name: str) -> None:
        kind = classify(fake_exc(name))
        assert kind is DisconnectKind.CONFIG
        # 不该重试：重启只会掩盖真实问题
        assert should_reconnect(kind) is False


class TestTextFallback:
    """pyrogram 2.0.x 不抛专用异常，只有日志文本 —— 文本必须能识别。"""

    @pytest.mark.parametrize(
        "msg",
        [
            "Server sent transport error: 404 (auth key not found)",
            "auth key not found in the system",
            "AUTH_KEY_UNREGISTERED",
            "the authorization has been invalidated",
        ],
    )
    def test_session_dead_from_text(self, msg: str) -> None:
        assert classify(RuntimeError(msg)) is DisconnectKind.SESSION_DEAD

    def test_flood_from_text(self) -> None:
        assert classify(RuntimeError("transport flood")) is DisconnectKind.FLOOD


class TestWrapping:
    """真实链路里异常常被包一层 —— 要能透过 __cause__ 找到真因。"""

    def test_finds_cause(self) -> None:
        inner = fake_exc("AuthKeyNotFound", "Auth key not found in the system.")
        outer = RuntimeError("while restarting session")
        outer.__cause__ = inner
        assert classify(outer) is DisconnectKind.SESSION_DEAD

    def test_finds_context(self) -> None:
        inner = fake_exc("SessionRevoked")
        outer = RuntimeError("cleanup failed")
        outer.__context__ = inner
        assert classify(outer) is DisconnectKind.SESSION_DEAD

    def test_no_exception_is_unknown(self) -> None:
        assert classify(None) is DisconnectKind.UNKNOWN

    def test_unrecognised_is_unknown_not_session_dead(self) -> None:
        """认不出来时绝不能猜成会话死亡 —— 那会误删凭证。"""
        kind = classify(ValueError("something odd"))
        assert kind is DisconnectKind.UNKNOWN
        assert needs_session_reset(kind) is False

    def test_cyclic_chain_does_not_hang(self) -> None:
        """异常链可能有环（自己指向自己），别死循环。"""
        exc = RuntimeError("loop")
        exc.__cause__ = exc
        assert classify(exc) is DisconnectKind.UNKNOWN
