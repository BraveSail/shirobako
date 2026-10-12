"""add role to admin_users

两级权限（2026-10-12）：``/add`` 加白名单普通用户、``/promote`` 提升管理员。
既有行全部落在 ``user``（现有白名单保持普通身份，不自动升级）。

Revision ID: c4f1a2b3d5e6
Revises: 765f26cfd84a
Create Date: 2026-10-12 20:15:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4f1a2b3d5e6"
down_revision: str | Sequence[str] | None = "765f26cfd84a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 新数据库由 create_all 直接建出带 role 的表（server_default 生效），无需再加列。
    if not inspector.has_table("admin_users"):
        return
    columns = {c["name"] for c in inspector.get_columns("admin_users")}
    if "role" in columns:
        return

    # server_default 同时兜住既有行（SQLite 的 ADD COLUMN 带常量默认值会回填）
    op.add_column(
        "admin_users",
        sa.Column("role", sa.String(length=16), nullable=False, server_default="user"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("admin_users"):
        return
    columns = {c["name"] for c in inspector.get_columns("admin_users")}
    if "role" not in columns:
        return
    with op.batch_alter_table("admin_users") as batch:
        batch.drop_column("role")
