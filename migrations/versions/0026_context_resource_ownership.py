"""上下文资源所有权迁移（TSK-192 phase A）

迁移 ID: 0026
父迁移: 0025

把 Redis 数据库编号从长期记忆配置迁移到独立的最近上下文资源：

1. 新建 ``komari_context_config`` 强类型单行表；
2. 当 memory 单行存在时，按原值复制 ``redis_db``（同时保留 revision/
   updated_at，避免无意义修订变化）；
3. 只删除 ``komari_memory_config.redis_db``，不触碰任何正式记忆内容表或值。

``downgrade`` 反向执行：为 memory 表临时重建 ``redis_db`` 默认列，从
context 单行回填后移除默认值，再删除 context 表。本文件自包含，不导入任何
``komari_bot`` 运行时代码。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0026"
down_revision: str | Sequence[str] | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONTEXT_TABLE = "komari_context_config"
_MEMORY_TABLE = "komari_memory_config"


def upgrade(name: str = "") -> None:
    if name:
        return

    op.create_table(
        _CONTEXT_TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redis_db", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    # Preserve the selected database and CAS identity when the source row exists.
    op.execute(
        f"INSERT INTO {_CONTEXT_TABLE} (id, revision, updated_at, redis_db) "
        f"SELECT id, revision, updated_at, redis_db FROM {_MEMORY_TABLE} "
        "WHERE id = 1"
    )

    with op.batch_alter_table(_MEMORY_TABLE, schema=None) as batch_op:
        batch_op.drop_column("redis_db")


def downgrade(name: str = "") -> None:
    if name:
        return

    with op.batch_alter_table(_MEMORY_TABLE, schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "redis_db",
                sa.INTEGER(),
                autoincrement=False,
                nullable=False,
                server_default=sa.text("1"),
            )
        )

    # Empty-context databases keep the temporary default; initialized contexts
    # restore the exact value selected by the new owner.
    op.execute(
        f"UPDATE {_MEMORY_TABLE} m SET redis_db = c.redis_db "
        f"FROM {_CONTEXT_TABLE} c WHERE m.id = 1 AND c.id = 1"
    )

    with op.batch_alter_table(_MEMORY_TABLE, schema=None) as batch_op:
        batch_op.alter_column("redis_db", server_default=None)

    op.drop_table(_CONTEXT_TABLE)
