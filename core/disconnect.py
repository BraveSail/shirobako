"""断线分类 —— 不同断法要用不同的救法。

## 为什么需要分类

「断了就重连」是错的。Telegram 的断线至少有四类，处置方式完全不同：

- **会话死亡**（``AuthKeyNotFound`` / ``AuthKeyUnregistered`` / ``SessionRevoked``）
  → **删 session 文件重新认证**。重连一万次也没用，密钥已作废。
- **网络抖动**（``OSError`` / ``ConnectionError`` / ``Timeout``）
  → 等一会儿重连。删 session 等于白丢一份好凭证。
- **限流**（``TransportFlood`` / ``FloodWait``）
  → 退避等待，**不要重启**。重启只会延长惩罚。
- **配置错误**（``ApiIdInvalid`` / ``AccessTokenInvalid``）
  → 直接退出报警。重启无用，只会掩盖真实问题。

## 2026-11-07 那次事故就是分类错了

``AuthKeyNotFound`` 属于**会话死亡**，但当时的 watchdog 只数「断了几次」：
要断 3 次才重启、重启 3 次才删 session。而 ``AuthKeyNotFound`` 抛在
pyrogram 内部的 ``recv_task`` 里，``_stop()`` 在 ``await self.recv_task``
那一步就炸了，**``disconnect_handler`` 根本不会被调用** —— 于是计数停在
1，日志里还留着「重连成功」，然后整整静默了 40 小时。

结论：这类错误不能靠「断开次数」发现，必须**主动探活**识别。
"""

from __future__ import annotations

from enum import Enum


class DisconnectKind(Enum):
    """断线类型 —— 决定 watchdog 怎么救。"""

    SESSION_DEAD = "session_dead"
    """会话/授权已失效。**必须**删 session 重新认证，重连无用。"""

    NETWORK = "network"
    """网络抖动、连接被重置。原样重连即可，别动 session。"""

    FLOOD = "flood"
    """被限流。要退避等待，重启会加重惩罚。"""

    CONFIG = "config"
    """配置错误（api_id/token）。重启和重登都救不了，必须人工介入。"""

    UNKNOWN = "unknown"
    """识别不出来 —— 按网络类保守处理（宁可多试，也别误删凭证）。"""


#: 会话已死：密钥被服务端作废 / 注销 / 重复使用。
#: 这些**只能**通过删 session 文件 + 用 bot token 重新认证解决。
SESSION_DEAD_NAMES = frozenset(
    {
        # RPC 层（pyrogram.errors）
        "AuthKeyUnregistered",
        "AuthKeyInvalid",
        "AuthKeyPermEmpty",
        "AuthKeyDuplicated",
        "SessionRevoked",
        "SessionExpired",
        "UserDeactivated",
        "UserDeactivatedBan",
        # 传输层（pyrogram.session.session，不走 RPCError）
        "AuthKeyNotFound",
    }
)

#: 限流：要等，不要重启。
FLOOD_NAMES = frozenset(
    {
        "TransportFlood",
        "Flood",
        "FloodWait",
        "SlowmodeWait",
    }
)

#: 配置问题：重启/重登都无解。
CONFIG_NAMES = frozenset(
    {
        "ApiIdInvalid",
        "ApiIdPublishedFlood",
        "AccessTokenInvalid",
        "AccessTokenExpired",
    }
)

#: 网络类：原样重连。
NETWORK_NAMES = frozenset(
    {
        "Timeout",
        "ConnectionError",
        "ConnectionResetError",
        "OSError",
        "InternalServerError",
        "ServiceUnavailable",
        "BadMsgNotification",
        "AuthKeyUnsynchronized",
        "NetworkMigrate",
        "InvalidDC",
    }
)

#: 文本兜底：pyrogram 2.0.x 收到 404 只打 warning、不抛异常，
#: 那条日志是**唯一**能看出会话已死的线索。
#:
#: 两种写法都要认：日志里是自然语言（``auth key not found``），
#: 而 RPC 错误串/部分版本是常量风格（``AUTH_KEY_UNREGISTERED``）。
#: 比较前统一小写并把下划线换成空格，于是两种都能命中。
SESSION_DEAD_MARKERS = (
    "auth key not found",
    "auth key unregistered",
    "auth key invalid",
    "auth key perm empty",
    "auth key duplicated",
    "session revoked",
    "session expired",
    "user deactivated",
    "authorization has been invalidated",
)

#: 文本兜底：限流。
FLOOD_MARKERS = (
    "transport flood",
    "flood wait",
    "too many requests",
)


def classify(exc: BaseException | None) -> DisconnectKind:
    """把异常归类成 :class:`DisconnectKind`。

    按**名字**匹配而不是 isinstance —— 会话死那类里 ``AuthKeyNotFound``
    定义在 ``pyrogram.session.session``（``TransportError`` 子类），其余在
    ``pyrogram.errors``（``RPCError`` 子类），没有共同基类可用；按名字匹配
    同时也能兼容不同 pyrogram 版本。

    还要看**错误文本**：pyrogram 2.0.x 收到 404 只打一条 warning
    （``Server sent transport error: 404 (auth key not found)``）然后无限
    重连，**根本不抛异常**。所以文本匹配是旧版本上唯一的线索。
    """
    if exc is None:
        return DisconnectKind.UNKNOWN

    # 沿异常链走一圈：pyrogram 常把真实原因包在 __cause__/__context__ 里
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__
        if name in SESSION_DEAD_NAMES:
            return DisconnectKind.SESSION_DEAD
        if name in FLOOD_NAMES:
            return DisconnectKind.FLOOD
        if name in CONFIG_NAMES:
            return DisconnectKind.CONFIG
        if name in NETWORK_NAMES:
            return DisconnectKind.NETWORK

        # 文本兜底（旧版 pyrogram 不抛专用异常，只有这句话）。
        # 统一小写 + 下划线转空格，这样 ``AUTH_KEY_UNREGISTERED`` 这种
        # 常量写法和 ``auth key not found`` 这种自然语言写法都能命中。
        text = str(current).lower().replace("_", " ")
        if any(marker in text for marker in SESSION_DEAD_MARKERS):
            return DisconnectKind.SESSION_DEAD
        if any(marker in text for marker in FLOOD_MARKERS):
            return DisconnectKind.FLOOD

        current = current.__cause__ or current.__context__

    return DisconnectKind.UNKNOWN


def needs_session_reset(kind: DisconnectKind) -> bool:
    """这个类型是否必须删掉 session 文件重新认证。"""
    return kind is DisconnectKind.SESSION_DEAD


def should_reconnect(kind: DisconnectKind) -> bool:
    """这个类型是否值得重连（False = 重连也没意义，直接退出报警）。"""
    return kind is not DisconnectKind.CONFIG
