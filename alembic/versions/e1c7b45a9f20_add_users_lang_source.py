"""add users.lang_source

给 ``users`` 表加 ``lang_source`` 列，区分「语言是怎么来的」：

- ``tg``     —— 首次出现时由 Telegram 上报的语言码自动归一出（可被重新同步）
- ``manual`` —— 用户在 ``/lang`` 里手动选的（任何自动同步都不能覆盖）

## 为什么需要

``ensure_lang`` 对已有用户不读 Telegram 语言，否则会冲掉手动选择。但这样就修不了
早期的一批错数据：``locale_for`` 只认 ISO 639-1 短码时，TG 报 ``en-us`` 的用户
被归一成 ``zh-hans``（只认短码 → 查不到 → 退回 bot 全局语言），之后永远显示简中。
有了这列就能只更新 ``tg`` 的行，手动选过的一律不动。

## 存量行的处理

**全部置为 ``tg``**。2026-11-18 之前 ``set_lang`` 只有 ``/lang`` 一个外部入口
（``ensure_lang`` 初始化也走它），库里无法区分来源 —— 把存量当 ``tg`` 意味着
这些用户下次发消息时会按当前 Telegram 语言重新同步一次。这是用户明确要的行为
（"加个source批量修"），代价是那些**手动选过但 TG 语言不同**的人会被拉回 TG 语言；
真遇上再手动 ``/lang`` 一次即可。

Revision ID: e1c7b45a9f20
Revises: d8e4f2a1b3c5
Create Date: 2026-11-18 13:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1c7b45a9f20"
down_revision: str | Sequence[str] | None = "d8e4f2a1b3c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("users"):
        # 新库由 create_all 直接建出带 lang_source 的表，无需迁移。
        return

    columns = {c["name"] for c in inspector.get_columns("users")}
    if "lang_source" not in columns:
        op.add_column(
            "users",
            sa.Column("lang_source", sa.String(length=8), nullable=False, server_default="tg"),
        )
        # 存量行统一标成 tg（见模块 docstring 的取舍说明）。
        op.execute("UPDATE users SET lang_source = 'tg'")


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("users"):
        return
    columns = {c["name"] for c in inspector.get_columns("users")}
    if "lang_source" in columns:
        with op.batch_alter_table("users") as batch:
            batch.drop_column("lang_source")
