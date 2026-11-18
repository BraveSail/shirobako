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

#: ``lang_source`` 取值：语言由 Telegram 上报的语言码自动归一出（可被重新同步）
LANG_SOURCE_TG = "tg"
#: ``lang_source`` 取值：用户在 ``/lang`` 里手动选的（**任何自动同步都不能覆盖**）
LANG_SOURCE_MANUAL = "manual"


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

    #: 这个语言是怎么来的：``tg`` = 首次出现时由 Telegram 上报的语言自动归一出;
    #: ``manual`` = 用户自己用 ``/lang`` 选的。
    #:
    #: **为什么要区分**：``ensure_lang`` 对已有用户不读 Telegram 语言（否则会冲掉
    #: 手动选择）。但这样一来，早期因为「只认短码、不认 BCP-47」被误存成简中的用户
    #: （TG 是 ``en-us`` 却被归一成 ``zh-hans``）就永远纠不回来。
    #: 有了这个字段就能只更新 ``tg`` 那些行 —— 手动选过的坚决不动。
    #:
    #: 存量行一律视为 ``tg``：2026-11-18 之前的 ``set_lang`` 只有 ``/lang``
    #: 一个入口（``ensure_lang`` 初始化也走它），没法区分，但把存量当 ``tg``
    #: 重新同步一次 Telegram 语言是**用户当时要求的行为**（"加个source批量修"）。
    lang_source: Mapped[str] = mapped_column(
        String(8), nullable=False, default=LANG_SOURCE_TG, server_default=LANG_SOURCE_TG
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
