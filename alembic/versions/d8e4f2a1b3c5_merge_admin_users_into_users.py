"""merge admin_users into users

调用鉴权改为**只查本地数据库**（2026-10-12 用户指令）：
白名单角色从独立的 ``admin_users`` 表并入 ``users`` 表（"users 下 admin flag"）。

- 加列 ``users.role``（NULL = 无授权; ``user`` = 白名单普通用户; ``admin`` = 管理员）
- 把 ``admin_users`` 的既有行搬到 ``users.role``（既有白名单保持原角色）
- 删除 ``admin_users`` 表

Revision ID: d8e4f2a1b3c5
Revises: c4f1a2b3d5e6
Create Date: 2026-10-12 22:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8e4f2a1b3c5"
down_revision: str | Sequence[str] | None = "c4f1a2b3d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("users"):
        # 新数据库由 create_all 直接建出带 role 的 users 表, 无需迁移。
        return

    columns = {c["name"] for c in inspector.get_columns("users")}
    if "role" not in columns:
        op.add_column("users", sa.Column("role", sa.String(length=16), nullable=True))

    # 搬运 admin_users -> users.role。按 telegram_user_id 匹配;
    # users 里没有的人 (理论上不该有) 直接建行, 别丢授权。
    if inspector.has_table("admin_users"):
        metadata = sa.MetaData()
        users = sa.Table("users", metadata, autoload_with=bind)
        admin_users = sa.Table("admin_users", metadata, autoload_with=bind)

        rows = bind.execute(sa.select(admin_users.c.telegram_user_id, admin_users.c.role)).all()
        for telegram_user_id, role in rows:
            role = role or "user"
            existing = bind.execute(
                sa.select(users.c.id).where(users.c.telegram_user_id == telegram_user_id)
            ).first()
            if existing:
                bind.execute(
                    users.update().where(users.c.id == existing.id).values(role=role)
                )
            else:
                bind.execute(
                    users.insert().values(telegram_user_id=telegram_user_id, role=role)
                )

        # 校验: 搬运后每个旧行的角色都能在 users 里查到 (不一致宁可报错, 不静默丢)
        for telegram_user_id, role in rows:
            actual = bind.execute(
                sa.select(users.c.role).where(users.c.telegram_user_id == telegram_user_id)
            ).scalar()
            if actual != (role or "user"):
                raise RuntimeError(
                    f"admin_users 并入 users 校验失败: telegram_user_id={telegram_user_id} "
                    f"期望 role={role or 'user'!r}, 实际 {actual!r}"
                )

        op.drop_table("admin_users")


def downgrade() -> None:
    """Downgrade schema: 重建 admin_users 表, 从 users.role 搬回。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("users"):
        return

    if not inspector.has_table("admin_users"):
        op.create_table(
            "admin_users",
            sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
            sa.Column("telegram_user_id", sa.BigInteger, nullable=False, unique=True),
            sa.Column("added_by", sa.BigInteger, nullable=True),
            sa.Column("role", sa.String(length=16), nullable=False, server_default="user"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    metadata = sa.MetaData()
    users = sa.Table("users", metadata, autoload_with=bind)
    admin_users = sa.Table("admin_users", metadata, autoload_with=bind)

    rows = bind.execute(
        sa.select(users.c.telegram_user_id, users.c.role).where(users.c.role.is_not(None))
    ).all()
    for telegram_user_id, role in rows:
        exists = bind.execute(
            sa.select(admin_users.c.id).where(admin_users.c.telegram_user_id == telegram_user_id)
        ).first()
        if not exists:
            bind.execute(admin_users.insert().values(telegram_user_id=telegram_user_id, role=role))

    if "role" in {c["name"] for c in inspector.get_columns("users")}:
        with op.batch_alter_table("users") as batch:
            batch.drop_column("role")
