"""用户表仓储（含白名单角色）。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models.user import User


class UserRepo:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_by_tg_user_id(self, telegram_user_id: int) -> User | None:
        user = await self._session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
        return user

    async def add(self, telegram_user_id: int, *, role: str | None = None) -> User:
        user = User(telegram_user_id=telegram_user_id, role=role)
        self._session.add(user)
        await self._session.flush()
        return user

    async def set_role(self, telegram_user_id: int, role: str | None) -> User | None:
        """改白名单角色（``None`` = 移除授权）；人不在表里时返回 ``None``。

        由调用方决定"不在表里也要授权"的语义（service 层会先建行再设角色）。
        """
        user = await self.get_by_tg_user_id(telegram_user_id)
        if user is None:
            return None
        user.role = role
        await self._session.flush()
        return user

    async def ensure_role(self, telegram_user_id: int, role: str | None) -> User:
        """确保该用户存在并处于指定角色（存在则**改角色**, 不存在则建行）。

        ``/add`` ``/promote`` 这类入口要求幂等, 不能用 ``add``（撞 unique 约束会炸）。
        """
        user = await self.get_by_tg_user_id(telegram_user_id)
        if user is None:
            user = User(telegram_user_id=telegram_user_id, role=role)
            self._session.add(user)
        else:
            user.role = role
        await self._session.flush()
        return user

    async def list_with_role(self) -> list[User]:
        """所有**有角色**的行（白名单成员），按建行时间排序（输出稳定可读）。

        没角色的普通用户行不列 —— 名单只关心被授权的人。
        """
        result = await self._session.scalars(
            select(User).where(User.role.is_not(None)).order_by(User.created_at, User.id)
        )
        return list(result)
