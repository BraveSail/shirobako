"""管理命令的权限服务（两级：白名单普通用户 / 管理员；超管只认配置）。

- ``core/config.py::admin_users``（``.env`` 的 ``ADMIN_USERS``）—— **超管**。
  改 .env 即生效；也解决"第一个人怎么进来"（``/promote`` 仅超管可用，
  没有这层就没人能把任何人提升成管理员）。
- ``admin_users`` 表 —— 运行时加的人，两档角色：
  - ``role="user"``：白名单普通用户（``/add`` 加的）。
  - ``role="admin"``：管理员（``/promote`` 提升的），可调管理命令。

**「与 bot 同群」不是这张表的事**：私聊/群解析无身份 gate；inline/guest 走
``plugins/parse/access.py`` 的运行时同群判定（不写库）。本模块只管管理命令权限。
"""

from __future__ import annotations

from core import bs
from db import get_session
from db.models.admin_user import ROLE_ADMIN, ROLE_USER
from log import logger
from repo.admin_user import AdminUserRepo

logger = logger.bind(name="AdminUser")


class AdminUserService:
    def __init__(self, session):
        self._session = session
        self._repo = AdminUserRepo(session)

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
        """**管理员判定**（管理命令用）: 配置超管 ∪ DB 里 role=admin。

        白名单普通用户（role=user）**不是**管理员 —— 这是两级权限的核心边界。
        """
        if not user_id:
            return False
        if self.is_configured(user_id):
            return True
        row = await self._repo.get_by_tg_user_id(user_id)
        return row is not None and row.role == ROLE_ADMIN

    async def is_whitelisted(self, user_id: int | None) -> bool:
        """**白名单判定**: 配置 ∪ DB（任意角色）—— 名单语义，不用于管理命令。"""
        if not user_id:
            return False
        if self.is_configured(user_id):
            return True
        return await self._repo.get_by_tg_user_id(user_id) is not None

    async def add(self, user_id: int, *, added_by: int | None = None) -> bool:
        """``/add``: 加入白名单（**普通用户**, role=user）; 返回**是否新增**。

        配置里已有的用户不写 DB —— 否则 DB 会多出一行"其实是配置来的"记录,
        以后从配置删掉时会以为还在白名单里。
        """
        if self.is_configured(user_id):
            return False
        row = await self._repo.get_by_tg_user_id(user_id)
        if row is not None:
            return False
        await self._repo.add(user_id, added_by=added_by, role=ROLE_USER)
        logger.info(f"白名单新增(普通用户): user_id={user_id} added_by={added_by}")
        return True

    async def promote(self, user_id: int, *, promoted_by: int | None = None) -> bool:
        """``/promote``: 提升为管理员; 返回**是否发生了变化**。

        - 已在表里 role=admin → False（幂等）。
        - 在表里 role=user → 改 role 为 admin → True。
        - **不在表里 → 直接插入 role=admin**（``/promote`` 一个从没加白的人 =
          加白并提升，无需先 /add）。
        - 配置超管 → False（本来就是最高级，不写库）。
        """
        if self.is_configured(user_id):
            return False
        row = await self._repo.get_by_tg_user_id(user_id)
        if row is None:
            await self._repo.add(user_id, added_by=promoted_by, role=ROLE_ADMIN)
            logger.info(f"提升为管理员(新建): user_id={user_id} by={promoted_by}")
            return True
        if row.role == ROLE_ADMIN:
            return False
        await self._repo.set_role(user_id, ROLE_ADMIN)
        logger.info(f"提升为管理员: user_id={user_id} by={promoted_by}")
        return True

    async def demote(self, user_id: int) -> bool:
        """``/demote``: 管理员降回普通用户; 返回**是否发生了变化**。

        - 表里 role=admin → 改回 user → True；role=user → False（幂等）。
        - 不在表里 → False。
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
        """把用户移出白名单, 返回**是否真的从 DB 删掉了**。

        ⚠️ 配置那层删不掉 (它在 ``.env`` 里) —— 调用方**必须**区分"删了"与
        "他在配置里", 否则会对用户谎称"已移除"而其实还在白名单。
        用 ``is_configured()`` 判断。
        """
        removed = await self._repo.remove(user_id)
        if removed:
            logger.info(f"白名单移除: user_id={user_id}")
        return removed

    async def list_roles(self) -> list[tuple[int, str]]:
        """全部名单 (uid, 角色) —— 超管 / 管理员 / 普通用户三段都由此拼。

        角色取值: ``"super"``（配置超管）/ ``"admin"`` / ``"user"``；
        **与加入顺序无关, 排序输出**（列表稳定可读）。
        """
        rows = await self._repo.list_all()
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


__all__ = ["AdminUserService", "is_admin_user", "is_super_admin"]
