"""告警 —— 出事了要让人知道，而不是静默退出。

## 为什么需要它

2026-11-07 那次 bot 挂着 40 小时没人发现，因为所有失败路径都只做两件事：
写一行日志、``exit()``。而容器退出后没人盯着 ``docker ps``，日志也没人翻。

「日志里写清楚」不等于「有人知道」。所以凡是**需要人工介入**的失败，
必须主动推到超管的 Telegram —— 这是最后一道防线。

## 注意

- 这里刻意**不依赖 bot 客户端**：连接本身坏掉时（正是最需要告警的时候），
  用 HTTP 直连 Bot API 才发得出去。
- 发送失败不能反过来把进程搞崩 —— 告警是尽力而为。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from core.config import bs
from log import logger

logger = logger.bind(name="Alert")

#: Bot API 基址（不走 bot 客户端，连接坏了也能发）
_API = "https://api.telegram.org/bot{token}/sendMessage"

#: 超时（秒）。告警不该把关闭流程拖住。
_TIMEOUT = 15


def _recipients() -> list[int]:
    """告警收件人：配置里的超管。"""
    return list(bs.admin_user_ids or [])


def send_alert(title: str, detail: str = "", *, token: str | None = None) -> bool:
    """把告警推给所有超管。返回是否**至少发出了一条**。

    :param title: 一句话说明出了什么事
    :param detail: 补充信息（错误类型、原因、需要做什么）
    :param token: bot token；不给就用配置里的
    """
    recipients = _recipients()
    if not recipients:
        logger.warning(f"没有配置超管 (ADMIN_USERS)，告警发不出去: {title}")
        return False

    text = f"🚨 **{title}**"
    if detail:
        text += f"\n\n{detail}"

    ok = False
    for chat_id in recipients:
        try:
            payload = urllib.parse.urlencode(
                {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
            ).encode()
            req = urllib.request.Request(
                _API.format(token=token or bs.bot_token),
                data=payload,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 - 固定 https 域名
                body = json.loads(resp.read().decode())
            if body.get("ok"):
                ok = True
                logger.info(f"告警已发送给 {chat_id}: {title}")
            else:
                logger.error(f"告警发送被拒 ({chat_id}): {body}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            logger.error(f"告警发送失败 ({chat_id}): {type(e).__name__}: {e}")

    return ok


def send_alert_sync(title: str, detail: str = "") -> bool:
    """同 :func:`send_alert`，名字表明它是阻塞调用（用于 ``exit()`` 前）。"""
    return send_alert(title, detail)
