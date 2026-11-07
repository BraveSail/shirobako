"""YouTube 取流用的 cookie 必须是**真值**，不能被脱敏。

## 为什么

``SecretCookie.__str__`` 故意返回脱敏形式（``SAPISID=**********``，防日志
泄漏），这是对的。但曾经有个 bug 把它当作真实 cookie 传给了 SABR 工具：

    cookie=str(SecretCookie(cookie_value))

后果是 SABR 子进程拿一串假 cookie 生成 PO token → 绑定假身份 →
``Stream protection attestation rejected``。

**症状极具误导性**：匿名视频照样能下（假 cookie 等价匿名），只有需要登录
态的视频失败，于是被误判成"cookie 过期"或"SABR 协议变了"。这份测试锁住
真值传递，防止再退回那种写法。
"""

from __future__ import annotations

from parsehub.utils.helpers import SecretCookie


def test_secret_cookie_str_is_masked() -> None:
    """前提：``str()`` 确实是脱敏的（如果哪天变了，这个测试要一起改）。"""
    cookie = SecretCookie({"SAPISID": "AbCdEfGhIjKlMnOpQrStUvWxYz0123456"})
    assert str(cookie) == "SAPISID=**********"
    assert "AbCdEf" not in str(cookie)


def test_secret_cookie_get_value_returns_real_values() -> None:
    """真值只能从 ``get_value()`` 拿 —— 这是构造 SABR cookie 串的唯一正确来源。"""
    real = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456"
    cookie = SecretCookie({"SAPISID": real})
    value = cookie.get_value()
    assert value is not None
    assert value["SAPISID"] == real


def test_sabr_cookie_string_carries_real_values() -> None:
    """``youtube.py`` 用的拼法必须产出真值串。

    这里直接复刻解析器里的那行，确保两个前提同时成立：
    ① 拼出来的是 ``k=v; k=v`` 形式；② 值是真值而不是星号。
    """
    real = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456"
    cookie_value = SecretCookie({"SAPISID": real, "HSID": "short"}).get_value()
    assert cookie_value is not None

    cookie_str = "; ".join(f"{k}={v}" for k, v in cookie_value.items())

    assert cookie_str == f"SAPISID={real}; HSID=short"
    assert "******" not in cookie_str
    assert real in cookie_str
