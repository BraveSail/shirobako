"""非同群调用鉴权: 只认本地库白名单（2026-10-12 权限模型）。

**权限模型**（用户定义）:
- 群内自动匹配（bot 在群）—— **不鉴权**（那是群内触发机制）。
- 私聊 / inline / guest —— **鉴权**, 且**只查本地数据库**（``users.role`` 非空 ∪ 配置超管）,
  不再检查"与 bot 同群"（旧 ``AccessGate`` 已删除）。

本文件测:
1. 便捷函数 ``is_whitelisted_user`` 的语义;
2. 私聊入口（``handlers.parse``）拦截/放行;
3. inline 入口（``inline._call_inline_parse``）拦截/放行 —— 含"私聊唤起不再豁免"。
guest 入口在 test_guest_mode.py 里覆盖。
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pyrogram.enums import ChatType

USER = 1879026273


# ---------------------------------------------------------------- 便捷函数 is_whitelisted_user


def _run_with_db(result: bool, configured: list[int] | None = None):
    """跑 ``is_whitelisted_user``; 返回 (结果, service mock)。"""
    from services import admin_user as mod

    @asynccontextmanager
    async def fake_session():
        yield None

    service = MagicMock()
    service.is_whitelisted = AsyncMock(return_value=result)
    # ``is_configured`` 是**静态方法**, mock 类时要按 configured 正确短路
    # （否则属性访问得到 truthy MagicMock, 一切用户都会被当成超管放行;
    #   或恒 False, 超管短路失效）
    service_cls = MagicMock(return_value=service)
    service_cls.is_configured = MagicMock(
        side_effect=lambda uid: bool(uid) and uid in (configured or [])
    )
    with (
        patch.object(mod, "bs", SimpleNamespace(admin_user_ids=configured or [])),
        patch.object(mod, "get_session", fake_session),
        patch.object(mod, "AdminUserService", service_cls),
    ):
        return asyncio.run(mod.is_whitelisted_user(USER)), service


def test_a_configured_superadmin_passes_without_touching_the_db():
    allowed, service = _run_with_db(False, configured=[USER])
    assert allowed is True
    service.is_whitelisted.assert_not_awaited()


def test_a_db_whitelisted_user_passes():
    allowed, _ = _run_with_db(True)
    assert allowed is True


def test_a_plain_user_is_rejected():
    allowed, _ = _run_with_db(False)
    assert allowed is False


def test_no_user_is_rejected_without_touching_the_db():
    from services import admin_user as mod

    service = MagicMock()
    service.is_whitelisted = AsyncMock(return_value=True)
    service_cls = MagicMock(return_value=service)
    service_cls.is_configured = MagicMock(return_value=False)
    with (
        patch.object(mod, "bs", SimpleNamespace(admin_user_ids=[])),
        patch.object(mod, "AdminUserService", service_cls),
    ):
        assert asyncio.run(mod.is_whitelisted_user(None)) is False
    service.is_whitelisted.assert_not_awaited()


# ---------------------------------------------------------------- 私聊入口


def _run_private_parse(allowed: bool, chat_type=ChatType.PRIVATE, *, return_service: bool = False):
    """跑一次 parse（默认私聊）, 返回 (MessageSender mock, _handle_parse_request mock)。

    ``return_service=True`` 时第二个返回值换成 service mock（供断言白名单查询本身）。
    """
    from plugins.parse import handlers as mod

    @asynccontextmanager
    async def fake_session():
        yield None

    cli = MagicMock()
    msg = SimpleNamespace(
        chat=SimpleNamespace(type=chat_type, id=-100123 if chat_type != ChatType.PRIVATE else 1, is_forum=False),
        from_user=SimpleNamespace(id=USER, language_code="zh"),
        text="https://x.com/a/status/1",
        caption=None,
        command=None,
        entities=None,
        message_thread_id=None,
        id=42,
    )
    sender = MagicMock()
    sender.return_value.text = AsyncMock()
    service = MagicMock()
    service.is_whitelisted = AsyncMock(return_value=allowed)
    user_service = MagicMock()
    user_service.ensure_lang = AsyncMock(return_value="zh-hans")
    settings_service = MagicMock()
    settings_service.get_config = AsyncMock(
        return_value=SimpleNamespace(default_mode="preview", custom_content=False, auto_delete_url=False)
    )
    parser = MagicMock()
    parser.get_platform = MagicMock(return_value=SimpleNamespace(id="twitter"))
    parse_service = MagicMock()
    parse_service.parser = parser
    handle = AsyncMock()

    with (
        patch.object(mod, "get_session", fake_session),
        patch.object(mod, "AdminUserService", MagicMock(return_value=service)),
        patch.object(mod, "UserService", MagicMock(return_value=user_service)),
        patch.object(mod, "SettingsService", MagicMock(return_value=settings_service)),
        patch.object(mod, "MessageSender", sender),
        patch.object(mod, "ParseService", MagicMock(return_value=parse_service)),
        patch.object(mod, "_handle_parse_request", handle),
    ):
        asyncio.run(mod.parse(cli, msg))
    if return_service:
        return sender, service
    return sender, handle


def test_a_private_message_from_a_stranger_is_refused():
    """**核心**: 私聊发链接要鉴权（以前无门禁）"""
    sender, handle = _run_private_parse(allowed=False)
    assert sender.return_value.text.await_args.args[0] == "仅限白名单用户使用"
    handle.assert_not_awaited()  # 没进解析流程


def test_a_private_message_from_a_whitelisted_user_goes_on():
    """白名单用户私聊正常解析: 进了解析流程, 且不该收到拒绝文案"""
    sender, handle = _run_private_parse(allowed=True)
    handle.assert_awaited()
    for call in sender.return_value.text.await_args_list:
        assert "仅限白名单用户使用" not in str(call)


def test_a_group_message_is_never_gated():
    """**核心（另一半）**: 群内自动匹配**不鉴权** —— 与 bot 同群的人发链接是

    bot 的群内触发机制; 即使这个人不在白名单（allowed=False）也必须照常解析。
    """
    sender, handle = _run_private_parse(allowed=False, chat_type=ChatType.SUPERGROUP)
    handle.assert_awaited()  # 照样进解析流程
    for call in sender.return_value.text.await_args_list:
        assert "仅限白名单用户使用" not in str(call)


def test_a_group_message_does_not_even_consult_the_whitelist():
    """群消息连白名单查询都不该发生（省一次 DB 查询; 语义上也无关）"""
    _, service = _run_private_parse(allowed=False, chat_type=ChatType.SUPERGROUP, return_service=True)
    service.is_whitelisted.assert_not_awaited()


# ---------------------------------------------------------------- inline 入口


def _run_inline(allowed: bool, chat_type=ChatType.GROUP):
    """跑一次 ``_call_inline_parse``; 返回 (answer_inline mock, service mock)。"""
    from plugins.parse import inline as mod

    @asynccontextmanager
    async def fake_session():
        yield None

    inline_query = SimpleNamespace(
        from_user=SimpleNamespace(id=USER, language_code="zh"),
        query="https://x.com/a/status/1",
        chat_type=chat_type,
    )
    answer = AsyncMock()
    service = MagicMock()
    service.is_whitelisted = AsyncMock(return_value=allowed)
    user_service = MagicMock()
    user_service.ensure_lang = AsyncMock(return_value="zh-hans")
    settings_service = MagicMock()
    settings_service.get_config_by_user = AsyncMock(return_value=SimpleNamespace())
    parse_service = MagicMock()
    parse_service.get_raw_url = AsyncMock(return_value="https://x.com/a/status/1")
    persistent = MagicMock()
    persistent.get = AsyncMock(return_value=None)
    result_cache = MagicMock()
    result_cache.get = AsyncMock(return_value=SimpleNamespace())
    build_results = AsyncMock(return_value=[])

    with (
        patch.object(mod, "get_session", fake_session),
        patch.object(mod, "AdminUserService", MagicMock(return_value=service)),
        patch.object(mod, "UserService", MagicMock(return_value=user_service)),
        patch.object(mod, "SettingsService", MagicMock(return_value=settings_service)),
        patch.object(mod, "ParseService", MagicMock(return_value=parse_service)),
        patch.object(mod, "persistent_cache", persistent),
        patch.object(mod, "parse_cache", result_cache),
        patch.object(mod, "build_inline_results", build_results),
        patch.object(mod, "answer_inline", answer),
    ):
        asyncio.run(mod._call_inline_parse(MagicMock(), inline_query))
    return answer, service


def test_inline_from_a_stranger_is_refused():
    answer, service = _run_inline(allowed=False)
    service.is_whitelisted.assert_awaited_once()
    # 拒绝: answer_inline 只回一条"无权限"结果项
    results = answer.await_args.args[1]
    assert len(results) == 1
    assert "无权限" in str(results[0].title)


def test_inline_is_gated_even_in_private_chat():
    """**行为变化**: 以前私聊唤起 inline 豁免, 现在同样要白名单"""
    _, service = _run_inline(allowed=False, chat_type=ChatType.PRIVATE)
    service.is_whitelisted.assert_awaited_once()


def test_inline_from_a_whitelisted_user_parses():
    answer, _ = _run_inline(allowed=True)
    # 放行: answer_inline 被调用（不是被拒的那条单结果）
    assert answer.await_count >= 1
