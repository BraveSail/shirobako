"""管理命令白名单（两级权限）。

**运行时加的人存这里**（超管在 ``core/config.py::admin_users``，两处取并集）。

为什么单独一张表、而不是给 ``users`` 加一列: ``users`` 表是"用户偏好"(语言等),
授权是另一回事 —— 混在一起以后清用户数据会误伤授权。

**两级**（2026-10-12 起）：
- ``role="user"`` —— 白名单普通用户（``/add`` 加的人）。**与 bot 同群的人天然可用**
  （那是运行时判断、不写库），这一层是"显式持久化的普通用户"。
- ``role="admin"`` —— 管理员（``/promote`` 提升），能调管理命令（``/purge`` ``/list`` ``/del``）。
- **超管**不在这张表里 —— 只认配置（``.env`` 的 ``ADMIN_USERS``），``/promote`` 仅超管可用。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

#: 普通白名单用户（默认角色）
ROLE_USER = "user"
#: 管理员（可调管理命令）
ROLE_ADMIN = "admin"


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)

    #: 谁加的 (审计用; 配置里那批没有这个值)
    added_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    #: 角色: ``user`` = 白名单普通用户 / ``admin`` = 管理员。
    #: 迁移时既有行全部落在 ``user``（需求：现有白名单保持普通身份）。
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default=ROLE_USER)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
