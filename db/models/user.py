"""用户表（含白名单角色）。

- ``role=None`` —— 无授权（普通路人；解析入口对非同群调用会拒他们）。
- ``role="user"`` —— 白名单普通用户（``/add`` 加）。
- ``role="admin"`` —— 管理员（``/promote`` 提升），能调管理命令。

**超管不在这张表里** —— 只认配置（``.env`` 的 ``ADMIN_USERS``），``/promote`` 仅超管可用。

历史：2026-10-12 前授权存在单独的 ``admin_users`` 表；经用户指示并入本表
（"users 下 admin flag"），迁移 ``d8e4f2a1b3c5`` 完成数据搬迁并删除旧表。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core import bs
from db.base import Base

if TYPE_CHECKING:
    from db.models.settings import Settings

#: 白名单普通用户（``/add`` 加的人）
ROLE_USER = "user"
#: 管理员（``/promote`` 提升），能调管理命令
ROLE_ADMIN = "admin"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)

    language_code: Mapped[str] = mapped_column(
        String(16), nullable=False, default=bs.language, server_default=bs.language
    )

    #: 白名单角色: ``None`` = 无授权（路人）; ``user`` = 白名单普通用户; ``admin`` = 管理员。
    #: 超管不在这里 —— 只认配置（``.env`` 的 ``ADMIN_USERS``）。
    role: Mapped[str | None] = mapped_column(String(16), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    settings: Mapped[list[Settings]] = relationship(
        "Settings",
        back_populates="user",
        passive_deletes=True,
    )
