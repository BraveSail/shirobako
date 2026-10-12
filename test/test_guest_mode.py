"""guest 查询的入口判定: 鉴权 + 纯链接 + 有 query id 才回。

guest 是"任何群都能召唤"的入口, 三条前置条件缺一不可。
**鉴权只查本地库白名单**（2026-10-12 权限模型; 旧"与 bot 同群"的 AccessGate 已删除）。
实际的解析/发送在 `_answer` 里 (需要真实网络), 这里把它换掉, 只验证入口逻辑。
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from plugins.parse import guest as guest_mod


def _msg(text=None, caption=None, user_id=1879026273, guest_query_id="q1", language_code="ja"):
    return SimpleNamespace(
        text=text,
        caption=caption,
        guest_query_id=guest_query_id,
        from_user=SimpleNamespace(id=user_id, language_code=language_code) if user_id else None,
        chat=SimpleNamespace(id=-1001234567890),
    )


def _run(msg, allowed=True, locale="zh-hans") -> tuple[AsyncMock, AsyncMock]:
    """跑一次 guest_parse; 返回 (解析路径 _answer, 直发 answer_guest_query)。"""

    @asynccontextmanager
    async def fake_session():
        yield None

    answer = AsyncMock(return_value=True)
    direct = AsyncMock(return_value=SimpleNamespace(inline_message_id="x"))
    cli = SimpleNamespace(answer_guest_query=direct, me=SimpleNamespace(username="shirobakobot"))
    service = MagicMock()
    service.is_whitelisted = AsyncMock(return_value=allowed)
    with (
        patch.object(guest_mod, "AdminUserService", MagicMock(return_value=service)),
        patch.object(guest_mod, "_answer", answer),
        patch.object(guest_mod, "get_session", fake_session),
        patch.object(
            guest_mod,
            "UserService",
            MagicMock(return_value=SimpleNamespace(ensure_lang=AsyncMock(return_value=locale))),
        ),
    ):
        asyncio.run(guest_mod.guest_parse(cli, msg))
    return answer, direct


LINK = "https://x.com/a/status/123"


def test_bare_link_is_answered():
    answer, direct = _run(_msg(text=LINK))
    answer.assert_awaited_once()
    assert answer.await_args.args[1] == "q1"
    assert answer.await_args.args[2] == LINK
    direct.assert_not_awaited()


def test_bare_link_in_caption_is_answered():
    """带说明的媒体消息 text 是 None"""
    answer, _ = _run(_msg(caption=LINK))
    answer.assert_awaited_once()


def test_bot_mention_is_stripped_before_the_link_check():
    """guest 靠提到 bot 触发, 所以 "@bot <链接>" 必须能过纯链接判定"""
    answer, _ = _run(_msg(text=f"@shirobakobot {LINK}"))
    answer.assert_awaited_once()
    assert answer.await_args.args[2] == LINK


def test_link_before_the_mention_is_answered():
    """链接在前、@ 在后才是能正常发出的写法 (@ 在开头 Telegram 会进 inline 面板)"""
    answer, _ = _run(_msg(text=f"{LINK} @shirobakobot"))
    answer.assert_awaited_once()
    assert answer.await_args.args[2] == LINK


def test_chatter_around_the_link_is_ignored():
    """群里随口提到链接不该触发解析"""
    for text in ("看看这个 " + LINK, LINK + " 很好笑", "普通聊天内容"):
        answer, direct = _run(_msg(text=text))
        assert answer.await_count == 0 and direct.await_count == 0, text


def test_denied_user_gets_a_permission_reply():
    """鉴权不通过: 不解析, 但要回一条"无权限"说明 (静默会让用户以为 bot 坏了)"""
    answer, direct = _run(_msg(text=LINK), allowed=False)
    answer.assert_not_awaited()          # 没走解析
    direct.assert_awaited_once()         # 直接回提示
    assert direct.await_args.args[0] == "q1"


def test_missing_guest_query_id_is_ignored():
    """没有 query id 就无从回复, 不该白跑一遍解析"""
    answer, direct = _run(_msg(text=LINK, guest_query_id=None))
    assert answer.await_count == 0 and direct.await_count == 0


def test_missing_user_is_ignored():
    """拿不到发起者就无法做鉴权判定 → 拒绝 (fail-closed)"""
    answer, direct = _run(_msg(text=LINK, user_id=None), allowed=False)
    assert answer.await_count == 0
    direct.assert_awaited_once()


def test_empty_message_is_ignored():
    answer, direct = _run(_msg())
    assert answer.await_count == 0 and direct.await_count == 0
