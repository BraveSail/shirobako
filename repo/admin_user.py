"""管理命令白名单的仓储。"""

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.admin_user import ROLE_USER, AdminUser


class AdminUserRepo:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_by_tg_user_id(self, telegram_user_id: int) -> AdminUser | None:
        return await self._session.scalar(
            select(AdminUser).where(AdminUser.telegram_user_id == telegram_user_id)
        )

    async def add(
        self, telegram_user_id: int, *, added_by: int | None = None, role: str = ROLE_USER
    ) -> AdminUser:
        """加入白名单。**已存在时直接返回既有行** (幂等) ——

        调用方靠返回值判断"新加了"还是"本来就有", 所以这里不能重复插入
        (unique 约束会抛, 而且 /add 重复调用不该报错)。
        """
        if existing := await self.get_by_tg_user_id(telegram_user_id):
            return existing
        row = AdminUser(telegram_user_id=telegram_user_id, added_by=added_by, role=role)
        self._session.add(row)
        await self._session.flush()
        return row

    async def set_role(self, telegram_user_id: int, role: str) -> AdminUser | None:
        """改角色（``/promote`` ``/demote``）；人不在表里时返回 ``None``。

        由调用方决定"不在表里也要提升"的语义（service 层会先 ``add`` 再 ``set_role``）。
        """
        row = await self.get_by_tg_user_id(telegram_user_id)
        if row is None:
            return None
        row.role = role
        await self._session.flush()
        return row

    async def remove(self, telegram_user_id: int) -> bool:
        """从白名单移除, 返回**是否真删掉了** (本来就不在则为 False)。"""
        result = await self._session.execute(
            delete(AdminUser).where(AdminUser.telegram_user_id == telegram_user_id)
        )
        return bool(result.rowcount)

    async def list_all(self) -> list[AdminUser]:
        """全部白名单行, 按加入时间排序 (列表输出稳定可读)。"""
        result = await self._session.scalars(select(AdminUser).order_by(AdminUser.created_at, AdminUser.id))
        return list(result)
