"""drop users.language_code

界面语言改为**每次按 Telegram 上报的语言码实时计算**，不再持久化（用户指令：
"那就删掉入库，每次根据用户的 tg 语言适配"）。

原来这一列是「首次出现时归一后写死」，所以只在第一次读 Telegram 语言 ——
``locale_for`` 早期只认 ISO 639-1 短码（``en``），Telegram 在某些客户端报 BCP-47
全码（``en-us``），全码查不到就退回 bot 全局语言（``zh-hans``），于是那批英语用户
被永久存成简中，之后怎么改客户端语言都不生效。

改为实时计算后这类「陈旧缓存」在结构上就不存在了：Telegram 里改语言，下一条消息
立刻用新语言。手动 ``/lang`` 也一并去掉（没有要保存的偏好，也就不需要选择界面）。

Revision ID: f3a92c81d6b4
Revises: d8e4f2a1b3c5
Create Date: 2026-11-18 15:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a92c81d6b4"
down_revision: str | Sequence[str] | None = "d8e4f2a1b3c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("users"):
        # 新库由 create_all 直接建出**没有** language_code 的表，无需迁移。
        return

    columns = {c["name"] for c in inspector.get_columns("users")}
    if "language_code" not in columns:
        return
    with op.batch_alter_table("users") as batch:
        batch.drop_column("language_code")


def downgrade() -> None:
    """Downgrade schema.

    回滚时把列加回来并填 bot 默认语言 —— 原值已经丢了（升级时被删），
    回滚只恢复**结构**，不恢复数据。
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("users"):
        return
    columns = {c["name"] for c in inspector.get_columns("users")}
    if "language_code" not in columns:
        with op.batch_alter_table("users") as batch:
            batch.add_column(
                sa.Column("language_code", sa.String(length=16), nullable=False, server_default="zh-hans")
            )
