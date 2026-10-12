"""管理命令: 权限判定 / ``/add`` 取人 / ``/promote`` ``/demote`` / ``/purge`` 清缓存。

这些命令是**破坏性**的 (清掉别人解析用的 file_id 缓存) 且是**权限**相关, 所以
把几件事都钉死:
- 两级权限: 白名单普通用户 (role=user) **不是**管理员; 管理员 (role=admin) 是;
  超管只认配置;
- ``/add`` 加的是普通用户; ``/promote`` 提升管理员 (**仅超管可调**);
- ``/add`` 回复转发消息时取的是**原转发者** (私聊里最容易加错人的地方);
- ``/purge`` 用的是 ``get_raw_url`` 那把 key (拿原始输入去清会 hash 对不上、清不掉)。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from db.models.admin_user import ROLE_ADMIN, ROLE_USER
from services.admin_user import AdminUserService

CONFIGURED = 1879026273
FROM_DB = 111222333
STRANGER = 999888777


def _repo(*, existing: int | None = None, rows: list | None = None, role: str = ROLE_USER) -> MagicMock:
    repo = MagicMock()
    repo.get_by_tg_user_id = AsyncMock(
        return_value=SimpleNamespace(telegram_user_id=existing, role=role) if existing else None
    )
    repo.add = AsyncMock(return_value=SimpleNamespace(telegram_user_id=0, role=role))
    repo.set_role = AsyncMock(return_value=SimpleNamespace(telegram_user_id=existing, role=role))
    repo.list_all = AsyncMock(
        return_value=[SimpleNamespace(telegram_user_id=uid, role=r) for uid, r in (rows or [])]
    )
    return repo


def _service(repo: MagicMock) -> AdminUserService:
    """绕过 __init__ 的 session 依赖, 直接把 mock 仓储塞进去。"""
    svc = AdminUserService.__new__(AdminUserService)
    svc._session = None
    svc._repo = repo
    return svc


# ---------------------------------------------------------------- 权限判定（两级）


def test_a_configured_id_is_admin():
    repo = _repo()
    svc = _service(repo)
    with patch("services.admin_user.bs", SimpleNamespace(admin_user_ids=[CONFIGURED])):
        assert asyncio.run(svc.is_admin(CONFIGURED)) is True
    repo.get_by_tg_user_id.assert_not_awaited()  # 配置命中就不必查库


def test_a_db_admin_is_admin():
    repo = _repo(existing=FROM_DB, role=ROLE_ADMIN)
    svc = _service(repo)
    with patch("services.admin_user.bs", SimpleNamespace(admin_user_ids=[CONFIGURED])):
        assert asyncio.run(svc.is_admin(FROM_DB)) is True


def test_a_db_plain_user_is_not_an_admin():
    """**两级权限的核心边界**: 白名单普通用户 (role=user) 不是管理员"""
    repo = _repo(existing=FROM_DB, role=ROLE_USER)
    svc = _service(repo)
    with patch("services.admin_user.bs", SimpleNamespace(admin_user_ids=[CONFIGURED])):
        assert asyncio.run(svc.is_admin(FROM_DB)) is False


def test_a_db_plain_user_is_still_whitelisted():
    """但他是白名单成员（名单语义）"""
    repo = _repo(existing=FROM_DB, role=ROLE_USER)
    svc = _service(repo)
    with patch("services.admin_user.bs", SimpleNamespace(admin_user_ids=[CONFIGURED])):
        assert asyncio.run(svc.is_whitelisted(FROM_DB)) is True


def test_a_stranger_is_rejected():
    repo = _repo()
    svc = _service(repo)
    with patch("services.admin_user.bs", SimpleNamespace(admin_user_ids=[CONFIGURED])):
        assert asyncio.run(svc.is_admin(STRANGER)) is False


def test_no_user_is_rejected():
    """频道消息没有 from_user —— 不能因此放行"""
    repo = _repo()
    svc = _service(repo)
    with patch("services.admin_user.bs", SimpleNamespace(admin_user_ids=[CONFIGURED])):
        assert asyncio.run(svc.is_admin(None)) is False


def test_the_configured_list_is_parsed_leniently():
    """``.env`` 里手写的东西: 逗号或空白分隔, 认不出来的片段跳过而不是让 bot 起不来"""
    from core.config import BotSettings

    assert BotSettings(admin_users="1, 2  3").admin_user_ids == [1, 2, 3]
    assert BotSettings(admin_users="").admin_user_ids == []
    assert BotSettings(admin_users="me,@name,").admin_user_ids == []


# ---------------------------------------------------------------- /add（普通用户）


def test_adding_a_new_user_writes_to_the_db():
    repo = _repo(existing=None)
    svc = _service(repo)
    with patch("services.admin_user.bs", SimpleNamespace(admin_user_ids=[])):
        assert asyncio.run(svc.add(STRANGER, added_by=CONFIGURED)) is True
    repo.add.assert_awaited_once()
    # **/add 写的必须是 role=user**（普通用户），不是 admin
    assert repo.add.await_args.kwargs.get("role") == ROLE_USER


def test_adding_twice_does_not_insert_again():
    repo = _repo(existing=STRANGER)
    svc = _service(repo)
    with patch("services.admin_user.bs", SimpleNamespace(admin_user_ids=[])):
        assert asyncio.run(svc.add(STRANGER, added_by=CONFIGURED)) is False
    repo.add.assert_not_awaited()


def test_a_configured_user_is_not_copied_into_the_db():
    """配置里已有的不写库 —— 否则从配置删掉时会以为还在白名单里"""
    repo = _repo(existing=None)
    svc = _service(repo)
    with patch("services.admin_user.bs", SimpleNamespace(admin_user_ids=[CONFIGURED])):
        assert asyncio.run(svc.add(CONFIGURED, added_by=CONFIGURED)) is False
    repo.add.assert_not_awaited()


# ---------------------------------------------------------------- /promote /demote


def test_promoting_an_existing_plain_user_upgrades_the_role():
    """把已加白的普通用户提升为管理员: 改 role, 不新增行"""
    repo = _repo(existing=FROM_DB, role=ROLE_USER)
    svc = _service(repo)
    with patch("services.admin_user.bs", SimpleNamespace(admin_user_ids=[])):
        assert asyncio.run(svc.promote(FROM_DB, promoted_by=CONFIGURED)) is True
    repo.set_role.assert_awaited_once_with(FROM_DB, ROLE_ADMIN)
    repo.add.assert_not_awaited()


def test_promoting_someone_not_listed_creates_an_admin_row():
    """``/promote`` 一个从没加白的人 = 加白并提升（无需先 /add）"""
    repo = _repo(existing=None)
    svc = _service(repo)
    with patch("services.admin_user.bs", SimpleNamespace(admin_user_ids=[])):
        assert asyncio.run(svc.promote(STRANGER, promoted_by=CONFIGURED)) is True
    repo.add.assert_awaited_once()
    assert repo.add.await_args.kwargs.get("role") == ROLE_ADMIN


def test_promoting_an_admin_is_idempotent():
    repo = _repo(existing=FROM_DB, role=ROLE_ADMIN)
    svc = _service(repo)
    with patch("services.admin_user.bs", SimpleNamespace(admin_user_ids=[])):
        assert asyncio.run(svc.promote(FROM_DB)) is False
    repo.set_role.assert_not_awaited()


def test_a_configured_superadmin_is_never_written_to_the_db():
    repo = _repo(existing=None)
    svc = _service(repo)
    with patch("services.admin_user.bs", SimpleNamespace(admin_user_ids=[CONFIGURED])):
        assert asyncio.run(svc.promote(CONFIGURED)) is False
    repo.add.assert_not_awaited()


def test_demoting_an_admin_downgrades_the_role():
    repo = _repo(existing=FROM_DB, role=ROLE_ADMIN)
    svc = _service(repo)
    with patch("services.admin_user.bs", SimpleNamespace(admin_user_ids=[])):
        assert asyncio.run(svc.demote(FROM_DB)) is True
    repo.set_role.assert_awaited_once_with(FROM_DB, ROLE_USER)


def test_demoting_a_plain_user_is_idempotent():
    repo = _repo(existing=FROM_DB, role=ROLE_USER)
    svc = _service(repo)
    with patch("services.admin_user.bs", SimpleNamespace(admin_user_ids=[])):
        assert asyncio.run(svc.demote(FROM_DB)) is False
    repo.set_role.assert_not_awaited()


def test_list_roles_marks_configured_as_super_and_merges_sorted():
    repo = _repo(rows=[(FROM_DB, ROLE_ADMIN), (999888777, ROLE_USER), (CONFIGURED, ROLE_USER)])
    svc = _service(repo)
    with patch("services.admin_user.bs", SimpleNamespace(admin_user_ids=[CONFIGURED])):
        assert asyncio.run(svc.list_roles()) == sorted(
            [(CONFIGURED, "super"), (FROM_DB, ROLE_ADMIN), (999888777, ROLE_USER)]
        )



# ---------------------------------------------------------------- /add 取谁


def test_reply_to_a_forwarded_message_takes_the_original_sender():
    """私聊里 /add 回复的其实是转发过来的内容 —— 要加的是**原发送者**,

    看 ``from_user`` 会加白转发的中间人 (甚至 bot 自己)。
    """
    from pyrogram.types import MessageOriginUser

    from plugins.admin import _target_user_id_from_reply

    reply = SimpleNamespace(
        forward_origin=MessageOriginUser(sender_user=SimpleNamespace(id=FROM_DB)),
        from_user=SimpleNamespace(id=CONFIGURED, is_bot=False),
    )
    assert _target_user_id_from_reply(reply) == FROM_DB


def test_reply_to_a_plain_message_takes_its_sender():
    from plugins.admin import _target_user_id_from_reply

    reply = SimpleNamespace(forward_origin=None, from_user=SimpleNamespace(id=FROM_DB, is_bot=False))
    assert _target_user_id_from_reply(reply) == FROM_DB


def test_reply_to_a_bot_message_is_not_a_target():
    from plugins.admin import _target_user_id_from_reply

    reply = SimpleNamespace(
        forward_origin=None,
        from_user=SimpleNamespace(id=555000111, is_bot=True),
    )
    assert _target_user_id_from_reply(reply) is None


def test_a_numeric_argument_is_the_target():
    from plugins.admin import _resolve_target

    msg = SimpleNamespace(command=["add", "123456789"], reply_to_message=None)
    assert asyncio.run(_resolve_target(MagicMock(), msg)) == 123456789


def test_a_username_argument_is_resolved_through_the_api():
    from plugins.admin import _resolve_target

    cli = MagicMock()
    cli.get_users = AsyncMock(return_value=SimpleNamespace(id=FROM_DB))
    msg = SimpleNamespace(command=["add", "@someone"], reply_to_message=None)
    assert asyncio.run(_resolve_target(cli, msg)) == FROM_DB


def test_an_unresolvable_argument_gives_no_target():
    from pyrogram.errors import UsernameNotOccupied

    from plugins.admin import _resolve_target

    cli = MagicMock()
    cli.get_users = AsyncMock(side_effect=UsernameNotOccupied())
    msg = SimpleNamespace(command=["add", "@nobody"], reply_to_message=None)
    assert asyncio.run(_resolve_target(cli, msg)) is None


# ---------------------------------------------------------------- 权限守卫


def _sender_recorder():
    sender = MagicMock()
    sender.return_value.text = AsyncMock()
    return sender


def _session_cm():
    """一个 get_session 的替身 (只 yield None, 不做提交/回滚)。"""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_session():
        yield None

    return fake_session


def test_a_non_whitelisted_user_gets_a_refusal_and_nothing_else():
    from plugins import admin

    cli, sender = MagicMock(), _sender_recorder()
    msg = SimpleNamespace(from_user=SimpleNamespace(id=STRANGER), command=["purge", "https://x.com/a/status/1"])

    with (
        patch.object(admin, "is_admin_user", AsyncMock(return_value=False)),
        patch.object(admin, "_context", AsyncMock(return_value=("zh-hans", SimpleNamespace()))),
        patch.object(admin, "MessageSender", sender),
        patch.object(admin, "parse_cache") as cache,
    ):
        asyncio.run(admin.purge_cache(cli, msg))

    said = sender.return_value.text.await_args.args[0]
    assert said == "无权使用该命令"
    cache.pop.assert_not_called()  # 没执行任何清理


# ---------------------------------------------------------------- /promote /demote（仅超管）


def test_a_non_superadmin_cannot_promote():
    """**核心**: ``/promote`` 只有超管能调 —— 普通管理员也被拒。

    （这是需求原话「/promote 只能由我使用」的守门测试。）
    """
    from plugins import admin

    cli, sender = MagicMock(), _sender_recorder()
    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=FROM_DB), command=["promote", "123456789"], reply_to_message=None
    )

    service = MagicMock()
    service.return_value.promote = AsyncMock(return_value=True)

    with (
        # 他是"管理员"（能调 /purge），但不是超管
        patch.object(admin, "is_super_admin", AsyncMock(return_value=False)),
        patch.object(admin, "_context", AsyncMock(return_value=("zh-hans", SimpleNamespace()))),
        patch.object(admin, "MessageSender", sender),
        patch.object(admin, "AdminUserService", service),
        patch.object(admin, "get_session", _session_cm()),
    ):
        asyncio.run(admin.promote_user(cli, msg))

    assert sender.return_value.text.await_args.args[0] == "该命令仅限超管使用"
    service.return_value.promote.assert_not_awaited()  # 没有任何提升动作


def test_a_non_superadmin_cannot_demote():
    from plugins import admin

    cli, sender = MagicMock(), _sender_recorder()
    msg = SimpleNamespace(from_user=SimpleNamespace(id=FROM_DB), command=["demote", "123456789"], reply_to_message=None)

    service = MagicMock()
    service.return_value.demote = AsyncMock(return_value=True)

    with (
        patch.object(admin, "is_super_admin", AsyncMock(return_value=False)),
        patch.object(admin, "_context", AsyncMock(return_value=("zh-hans", SimpleNamespace()))),
        patch.object(admin, "MessageSender", sender),
        patch.object(admin, "AdminUserService", service),
        patch.object(admin, "get_session", _session_cm()),
    ):
        asyncio.run(admin.demote_user(cli, msg))

    assert sender.return_value.text.await_args.args[0] == "该命令仅限超管使用"
    service.return_value.demote.assert_not_awaited()


def test_the_superadmin_can_promote():
    from plugins import admin

    cli, sender = MagicMock(), _sender_recorder()
    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=CONFIGURED), command=["promote", "123456789"], reply_to_message=None
    )

    service = MagicMock()
    service.is_configured = MagicMock(return_value=False)
    service.return_value.promote = AsyncMock(return_value=True)

    with (
        patch.object(admin, "is_super_admin", AsyncMock(return_value=True)),
        patch.object(admin, "_context", AsyncMock(return_value=("zh-hans", SimpleNamespace()))),
        patch.object(admin, "MessageSender", sender),
        patch.object(admin, "AdminUserService", service),
        patch.object(admin, "get_session", _session_cm()),
    ):
        asyncio.run(admin.promote_user(cli, msg))

    service.return_value.promote.assert_awaited_once()
    assert "已提升为管理员" in sender.return_value.text.await_args.args[0]


def test_demoting_a_configured_superadmin_says_to_edit_the_env():
    """配置超管降不了 —— 必须说「需改 .env」, 不能谎称已降级"""
    from plugins import admin

    cli, sender = MagicMock(), _sender_recorder()
    target = 1879026273
    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=CONFIGURED), command=["demote", str(target)], reply_to_message=None
    )

    service = MagicMock()
    service.is_configured = MagicMock(return_value=True)
    service.return_value.demote = AsyncMock(return_value=True)

    with (
        patch.object(admin, "is_super_admin", AsyncMock(return_value=True)),
        patch.object(admin, "_context", AsyncMock(return_value=("zh-hans", SimpleNamespace()))),
        patch.object(admin, "MessageSender", sender),
        patch.object(admin, "AdminUserService", service),
        patch.object(admin, "get_session", _session_cm()),
    ):
        asyncio.run(admin.demote_user(cli, msg))

    said = sender.return_value.text.await_args.args[0]
    assert "ADMIN_USERS" in said and ".env" in said
    service.return_value.demote.assert_not_awaited()


def test_list_shows_the_three_tiers():
    """``/list`` 分三段列出: 超管 / 管理员 / 普通用户"""
    from plugins import admin

    cli, sender = MagicMock(), _sender_recorder()
    msg = SimpleNamespace(from_user=SimpleNamespace(id=CONFIGURED), command=["list"], reply_to_message=None)

    service = MagicMock()
    service.return_value.list_roles = AsyncMock(
        return_value=[(CONFIGURED, "super"), (FROM_DB, "admin"), (STRANGER, "user")]
    )

    with (
        patch.object(admin, "is_admin_user", AsyncMock(return_value=True)),
        patch.object(admin, "_context", AsyncMock(return_value=("zh-hans", SimpleNamespace()))),
        patch.object(admin, "MessageSender", sender),
        patch.object(admin, "AdminUserService", service),
        patch.object(admin, "get_session", _session_cm()),
    ):
        asyncio.run(admin.list_admin_users(cli, msg))

    said = sender.return_value.text.await_args.args[0]
    assert "超管" in said and "管理员" in said and "普通用户" in said
    # 三段各含自己的 ID
    for uid in (CONFIGURED, FROM_DB, STRANGER):
        assert str(uid) in said



# ---------------------------------------------------------------- /purge


def _purge(url_arg: str = "https://x.com/a/status/1", *, raw: str = "https://x.com/a/status/1", had=True):
    """跑一次 /purge, 返回 (回复文本, 两层缓存 mock, ParseService mock)。"""
    from plugins import admin

    cli, sender = MagicMock(), _sender_recorder()
    msg = SimpleNamespace(from_user=SimpleNamespace(id=CONFIGURED), command=["purge", url_arg], reply_to_message=None)

    parser = MagicMock()
    parser.get_platform = MagicMock(return_value=SimpleNamespace(id="twitter"))
    svc = MagicMock()
    svc.get_raw_url = AsyncMock(return_value=raw)
    svc.parser = parser

    persistent, memory = MagicMock(), MagicMock()
    persistent.get = AsyncMock(return_value=SimpleNamespace() if had else None)
    persistent.remove = AsyncMock()
    memory.get = AsyncMock(return_value=None)
    memory.pop = AsyncMock()

    with (
        patch.object(admin, "is_admin_user", AsyncMock(return_value=True)),
        patch.object(admin, "_context", AsyncMock(return_value=("zh-hans", SimpleNamespace()))),
        patch.object(admin, "MessageSender", sender),
        patch.object(admin, "ParseService", MagicMock(return_value=svc)),
        patch.object(admin, "persistent_cache", persistent),
        patch.object(admin, "parse_cache", memory),
    ):
        asyncio.run(admin.purge_cache(cli, msg))

    said = sender.return_value.text.await_args.args[0]
    return said, persistent, memory


def test_purge_clears_both_cache_layers_with_the_raw_url_key():
    """key 必须是 ``get_raw_url`` 的结果 —— 写入时用的就是它, 拿用户输入去清会清不掉"""
    said, persistent, memory = _purge("https://x.com/a/status/1?s=20", raw="https://x.com/a/status/1")
    assert persistent.remove.await_args.args[0] == "https://x.com/a/status/1"
    assert memory.pop.await_args.args[0] == "https://x.com/a/status/1"
    assert "已清除缓存" in said


def test_purge_says_so_when_there_was_no_cache():
    said, persistent, memory = _purge(had=False)
    assert "没有缓存" in said
    persistent.remove.assert_awaited()  # 仍然清一次 (幂等, 不做判断分支)


def test_purge_asks_for_a_link_when_given_none():
    from plugins import admin

    cli, sender = MagicMock(), _sender_recorder()
    msg = SimpleNamespace(from_user=SimpleNamespace(id=CONFIGURED), command=["purge"], reply_to_message=None)

    with (
        patch.object(admin, "is_admin_user", AsyncMock(return_value=True)),
        patch.object(admin, "_context", AsyncMock(return_value=("zh-hans", SimpleNamespace()))),
        patch.object(admin, "MessageSender", sender),
    ):
        asyncio.run(admin.purge_cache(cli, msg))

    assert sender.return_value.text.await_args.args[0] == "请加上链接或回复一条消息"


# ---------------------------------------------------------------- /purge all


def _purge_all(arg: str = "all"):
    """跑一次 ``/purge all``。"""
    from plugins import admin

    cli, sender = MagicMock(), _sender_recorder()
    msg = SimpleNamespace(from_user=SimpleNamespace(id=CONFIGURED), command=["purge", arg], reply_to_message=None)

    parser = MagicMock()
    parser.get_platform = MagicMock(return_value=SimpleNamespace(id="twitter"))
    svc = MagicMock()
    svc.get_raw_url = AsyncMock()
    svc.parser = parser
    persistent, memory = MagicMock(), MagicMock()
    persistent.clear = AsyncMock(return_value=36)
    memory.clear = AsyncMock(return_value=5)

    with (
        patch.object(admin, "is_admin_user", AsyncMock(return_value=True)),
        patch.object(admin, "_context", AsyncMock(return_value=("zh-hans", SimpleNamespace()))),
        patch.object(admin, "MessageSender", sender),
        patch.object(admin, "ParseService", MagicMock(return_value=svc)),
        patch.object(admin, "persistent_cache", persistent),
        patch.object(admin, "parse_cache", memory),
    ):
        asyncio.run(admin.purge_cache(cli, msg))

    return sender.return_value.text.await_args.args[0], persistent, memory, svc


def test_purge_all_clears_both_layers_and_reports_the_counts():
    """清空全部: 两层都清, 且**报出各清了多少** —— "清空全部"必须能自证做了什么"""
    said, persistent, memory, _ = _purge_all()
    persistent.clear.assert_awaited_once()
    memory.clear.assert_awaited_once()
    assert "36" in said and "5" in said


def test_purge_all_does_not_touch_any_link():
    """``all`` 分支不该去解析链接 (它跟链接无关)"""
    _, _, _, svc = _purge_all()
    svc.get_raw_url.assert_not_awaited()


def test_purge_all_is_case_insensitive():
    _, persistent, _, _ = _purge_all("ALL")
    persistent.clear.assert_awaited_once()


def test_purge_all_is_not_a_url_argument():
    """只有**单独一个** ``all`` 才算清空; 跟别的词混在一起时不能当清空处理"""
    from plugins import admin

    cli, sender = MagicMock(), _sender_recorder()
    msg = SimpleNamespace(
        from_user=SimpleNamespace(id=CONFIGURED), command=["purge", "all", "extra"], reply_to_message=None
    )
    parser = MagicMock()
    parser.get_platform = MagicMock(return_value=None)
    svc = MagicMock()
    svc.parser = parser
    persistent, memory = MagicMock(), MagicMock()
    persistent.clear, memory.clear = AsyncMock(), AsyncMock()

    with (
        patch.object(admin, "is_admin_user", AsyncMock(return_value=True)),
        patch.object(admin, "_context", AsyncMock(return_value=("zh-hans", SimpleNamespace()))),
        patch.object(admin, "MessageSender", sender),
        patch.object(admin, "ParseService", MagicMock(return_value=svc)),
        patch.object(admin, "persistent_cache", persistent),
        patch.object(admin, "parse_cache", memory),
    ):
        asyncio.run(admin.purge_cache(cli, msg))

    persistent.clear.assert_not_called()


# ---------------------------------------------------------------- /del


def _del(target_arg: str | None = "123456789", *, in_config: bool = False, removed: bool = True):
    from plugins import admin

    cli, sender = MagicMock(), _sender_recorder()
    command = ["del"] if target_arg is None else ["del", target_arg]
    msg = SimpleNamespace(from_user=SimpleNamespace(id=CONFIGURED), command=command, reply_to_message=None)

    service = MagicMock()
    service.is_configured = MagicMock(return_value=in_config)
    service.return_value.remove = AsyncMock(return_value=removed)

    with (
        patch.object(admin, "is_admin_user", AsyncMock(return_value=True)),
        patch.object(admin, "_context", AsyncMock(return_value=("zh-hans", SimpleNamespace()))),
        patch.object(admin, "MessageSender", sender),
        patch.object(admin, "AdminUserService", service),
        patch.object(admin, "get_session", _session_cm()),
    ):
        asyncio.run(admin.remove_admin_user(cli, msg))

    return sender.return_value.text.await_args.args[0], service


def test_del_removes_a_user_added_at_runtime():
    said, service = _del()
    service.return_value.remove.assert_awaited_once_with(123456789)
    assert "已移出白名单" in said


def test_del_says_so_when_the_user_was_not_listed():
    said, service = _del(removed=False)
    assert "不在白名单中" in said


def test_del_does_not_pretend_to_remove_a_configured_user():
    """配置里的用户删不掉 —— 必须说"要改 .env", 不能回一句"已移出"骗人

    (配置那层在 .env 里, 代码删不掉; 谎称删了的话用户会发现他还在白名单)
    """
    said, service = _del("1879026273", in_config=True)
    service.return_value.remove.assert_not_awaited()
    assert "ADMIN_USERS" in said and ".env" in said


def test_del_asks_for_a_target_when_given_none():
    said, _ = _del(None)
    assert said == "请回复某人的消息，或给出用户 ID"


# ---------------------------------------------------------------- 清空接口本身


def test_result_cache_clear_only_touches_our_prefix():
    """清空结果缓存**只能删自己前缀内的 key** ——

    Redis 是共用实例 (161 上宝塔那个), 用 FLUSHDB 会删掉别的项目的数据。
    """
    from services.cache import ResultCache

    with patch("services.cache.delete_by_prefix", AsyncMock(return_value=2)) as dbp:
        assert asyncio.run(ResultCache().clear()) == 2
    assert dbp.await_args.args[0] == "shirobako:result:*"


def test_persistent_clear_only_touches_our_prefix():
    from services.cache import PersistentCache

    with patch("services.cache.delete_by_prefix", AsyncMock(return_value=42)) as dbp:
        assert asyncio.run(PersistentCache().clear()) == 42
    assert dbp.await_args.args[0] == "shirobako:parse:*"


def test_purge_reads_the_link_from_the_replied_message():
    from plugins import admin

    cli, sender = MagicMock(), _sender_recorder()
    reply = SimpleNamespace(text="", caption="看这个 https://x.com/a/status/7")
    msg = SimpleNamespace(from_user=SimpleNamespace(id=CONFIGURED), command=["purge"], reply_to_message=reply)

    parser = MagicMock()
    parser.get_platform = MagicMock(return_value=SimpleNamespace(id="twitter"))
    svc = MagicMock()
    svc.get_raw_url = AsyncMock(return_value="https://x.com/a/status/7")
    svc.parser = parser
    persistent, memory = MagicMock(), MagicMock()
    persistent.get = AsyncMock(return_value=None)
    persistent.remove = AsyncMock()
    memory.get, memory.pop = AsyncMock(return_value=None), AsyncMock()

    with (
        patch.object(admin, "is_admin_user", AsyncMock(return_value=True)),
        patch.object(admin, "_context", AsyncMock(return_value=("zh-hans", SimpleNamespace()))),
        patch.object(admin, "MessageSender", sender),
        patch.object(admin, "ParseService", MagicMock(return_value=svc)),
        patch.object(admin, "persistent_cache", persistent),
        patch.object(admin, "parse_cache", memory),
    ):
        asyncio.run(admin.purge_cache(cli, msg))

    assert svc.get_raw_url.await_args.args[0] == "https://x.com/a/status/7"
    assert memory.pop.await_args.args[0] == "https://x.com/a/status/7"


def test_the_purge_all_message_goes_through_the_catalog():
    """`/purge all` 的文案必须**模板交给 `_t` 再 format**。

    写成 f-string 先求值的话, `_t` 拿到的是"…30 条 · …1 条", 而词条的 key 是**模板**的
    md5 —— 匹配不上, 于是别的语言看到中文原文 (以前就是这样)。这条测试同时保证
    16 个语言文件里都有这条词条。
    """
    import hashlib
    from pathlib import Path

    from i18n import t_

    for lang in ("en-us", "ja-jp", "zh-hant"):
        # **字面量**调用点 —— `_t` 靠调用点源码的 AST 取模板; 把模板存进变量再传
        # 会解析失败并回落原文, 测出来的就是假故障。
        got = t_[lang]("已清空全部缓存（Redis）: 持久层 {persistent} 条 · 结果层 {result} 条")
        assert "{" in got, got
        assert got != "已清空全部缓存（Redis）: 持久层 {persistent} 条 · 结果层 {result} 条", (
            f"{lang} 没命中词条 (回落成了源语言原文)"
        )
        assert got.format(persistent=30, result=1).count("30") == 1

    key = hashlib.md5("已清空全部缓存（Redis）: 持久层 {persistent} 条 · 结果层 {result} 条".encode()).hexdigest()[:12]
    files = sorted(Path(__file__).resolve().parent.parent.joinpath("i18n").glob("*.yaml"))
    assert files, "找不到 i18n 文件"
    for f in files:
        assert f"{key}:" in f.read_text(encoding="utf-8"), f"{f.name} 缺这条词条"
