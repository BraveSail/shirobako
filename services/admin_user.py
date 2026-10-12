"""调用鉴权与管理命令权限（白名单 = ``users`` 表, 不单独建表）。

**鉴权用途**（2026-10-12 用户定义）:

- **群内自动匹配不鉴权** —— bot 在群里收到链接属于"群内触发机制"。
- **非同群调用（私聊 / inline / guest）查本地数据库白名单** —— 不再检查
  "与 bot 同群"（旧 ``AccessGate`` 的 ``get_chat_member`` 已废弃）。

**权限层级**:

- ``users.role`` 非空 = 白名单成员: ``"user"``（``/add`` 加）/ ``"admin"``（``/promote`` 提升）。
- **超管只认配置**（``.env`` 的 ``ADMIN_USERS``）—— ``/promote`` ``/demote`` 仅它可用;
  它在鉴权里也永远通过。
- 管理命令（``/purge`` ``/list`` ``/del``）: 配置超管 ∪ ``role="admin"``。
  白名单普通用户（``role="user"``）**不是**管理员。
"""

from __future__ import annotations

from core import bs
from db import get_session
from db.models.user import ROLE_ADMIN, ROLE_USER
from log import logger
from repo.user import UserRepo

logger = logger.bind(name="AdminUser")


class AdminUserService:
    def __init__(self, session):
        self._session = session
        self._repo = UserRepo(session)

    @staticmethod
    def configured_ids() -> set[int]:
        """配置里的超管 (``.env`` 的 ``ADMIN_USERS``, 逗号分隔)。"""
        return set(bs.admin_user_ids)

    @staticmethod
    def is_configured(user_id: int | None) -> bool:
        """该用户是不是**配置里的超管**（最高级；``/promote`` 只认它）。

        配置层不可被 ``/demote``（删不掉，只能改 .env）。
        """
        return bool(user_id) and user_id in AdminUserService.configured_ids()

    async def is_admin(self, user_id: int | None) -> bool:
        """**管理员判定**（管理命令用）: 配置超管 ∪ ``role="admin"``。

        白名单普通用户（``role="user"``）**不是**管理员 —— 这是两级权限的核心边界。
        """
        if not user_id:
            return False
        if self.is_configured(user_id):
            return True
        row = await self._repo.get_by_tg_user_id(user_id)
        return row is not None and row.role == ROLE_ADMIN

    async def is_whitelisted(self, user_id: int | None) -> bool:
        """**白名单判定**（非同群调用鉴权用）: 配置超管 ∪ ``role`` 非空。

        每次**直接查库**（不缓存）—— SQLite 本地查询远比解析本身轻,
        换来的是 ``/add`` ``/del`` 立即生效, 没有缓存窗口的不一致。
        """
        if not user_id:
            return False
        if self.is_configured(user_id):
            return True
        row = await self._repo.get_by_tg_user_id(user_id)
        return row is not None and row.role is not None

    async def add(self, user_id: int, *, added_by: int | None = None) -> bool:
        """``/add``: 加入白名单（**普通用户**, role=user）; 返回**是否新增**。

        - 已在名单（无论 user/admin）→ False（``/add`` 不降级、不重复）。
        - 配置超管 → False（不写库）。
        - 用户在 ``users`` 表已有行但没角色（普通用户）→ 设 role=user → True。
        """
        if self.is_configured(user_id):
            return False
        row = await self._repo.get_by_tg_user_id(user_id)
        if row is not None and row.role is not None:
            return False
        await self._repo.ensure_role(user_id, ROLE_USER)
        logger.info(f"白名单新增(普通用户): user_id={user_id} added_by={added_by}")
        return True

    async def promote(self, user_id: int, *, promoted_by: int | None = None) -> bool:
        """``/promote``: 提升为管理员; 返回**是否发生了变化**。

        - 已是 admin → False（幂等）。
        - role=user 或没有角色（含不在表里）→ 设 role=admin → True。
        - 配置超管 → False（本来就是最高级，不写库）。
        """
        if self.is_configured(user_id):
            return False
        row = await self._repo.get_by_tg_user_id(user_id)
        if row is not None and row.role == ROLE_ADMIN:
            return False
        await self._repo.ensure_role(user_id, ROLE_ADMIN)
        logger.info(f"提升为管理员: user_id={user_id} by={promoted_by}")
        return True

    async def demote(self, user_id: int) -> bool:
        """``/demote``: 管理员降回普通用户; 返回**是否发生了变化**。

        - role=admin → 改回 user → True；role=user / 无角色 / 不在表 → False（幂等）。
        - **配置超管降不了**（在 .env 里）—— 调用方必须先查 ``is_configured``
          并明确告知用户改 .env，绝不谎称"已降级"。
        """
        row = await self._repo.get_by_tg_user_id(user_id)
        if row is None or row.role != ROLE_ADMIN:
            return False
        await self._repo.set_role(user_id, ROLE_USER)
        logger.info(f"管理员降为普通用户: user_id={user_id}")
        return True

    async def remove(self, user_id: int) -> bool:
        """``/del``: 移出白名单（role → ``None``）; 返回**是否真的移除了**。

        **不删 ``users`` 行** —— 行里还有语言设置等用户数据, 删了会一并丢失。
        旧模型（单独表）时期这里是删行; 并入 users 表后改为清角色。

        ⚠️ 配置那层删不掉 (它在 ``.env`` 里) —— 调用方**必须**区分"移除了"与
        "他在配置里", 否则会对用户谎称"已移除"而其实还在白名单。
        """
        row = await self._repo.get_by_tg_user_id(user_id)
        if row is None or row.role is None:
            return False
        await self._repo.set_role(user_id, None)
        logger.info(f"白名单移除: user_id={user_id}")
        return True

    async def list_roles(self) -> list[tuple[int, str]]:
        """全部名单 (uid, 角色) —— 超管 / 管理员 / 普通用户三段都由此拼。

        角色取值: ``"super"``（配置超管）/ ``"admin"`` / ``"user"``；
        **与加入顺序无关, 排序输出**（列表稳定可读）。
        """
        rows = await self._repo.list_with_role()
        entries: dict[int, str] = {r.telegram_user_id: r.role for r in rows}
        for uid in self.configured_ids():
            entries[uid] = "super"
        return sorted(entries.items())


async def is_admin_user(user_id: int | None) -> bool:
    """便捷判定 (自开会话): 该用户是**管理员或超管**吗 —— 管理命令入口用。"""
    if not user_id:
        return False
    if AdminUserService.is_configured(user_id):
        return True
    async with get_session() as session:
        return await AdminUserService(session).is_admin(user_id)


async def is_super_admin(user_id: int | None) -> bool:
    """便捷判定 (自开会话): 该用户是**配置超管**吗 —— ``/promote`` ``/demote`` 入口用。"""
    return AdminUserService.is_configured(user_id)


async def is_whitelisted_user(user_id: int | None) -> bool:
    """便捷判定 (自开会话): 该用户**可做非同群调用**吗（私聊/inline/guest 鉴权）。

    群内自动匹配不经过这里 —— 那条路径不鉴权。
    """
    if not user_id:
        return False
    if AdminUserService.is_configured(user_id):
        return True
    async with get_session() as session:
        return await AdminUserService(session).is_whitelisted(user_id)


__all__ = ["AdminUserService", "is_admin_user", "is_super_admin", "is_whitelisted_user"]
