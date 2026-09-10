"""俄罗斯轮盘强类型配置表：道具权重与闭集文案池（TSK-279）

迁移 ID: 0021
父迁移: 0020

TSK-279 Stage-A：``komari_roulette`` 获得唯一一个强类型单行配置资源
``komari_roulette_config``（``id=1``、CAS ``revision``、``updated_at``）。

1. ``plugin_enable`` 默认 ``false``（fail closed，与 QQ matcher 惰性装配一致）；
2. 四项道具权重与领域 ``_normalize_weights`` 对齐：非负整数、每局
   ``waiting -> active`` 只读取一次并随对局快照冻结；
3. ``action_copy_pool`` / ``final_copy_pool`` 使用 ``JSONB``，保存闭集键 →
   非空模板列表；键集合与模板规则的唯一真源是运行时
   ``komari_roulette.copy_pool``（本迁移不复制校验逻辑）。

与全部既有强类型配置表一致：列无 server 默认值，缺失行由
``config_manager`` 在首次读取时按 Pydantic 默认值初始化，因此本迁移只需
建表。本文件不导入任何 ``komari_bot`` 运行时代码，DDL 与
``migrations/env.py`` 合并的 SQLModel 元数据逐列一致。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return
    op.create_table(
        "komari_roulette_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("plugin_enable", sa.Boolean(), nullable=False),
        sa.Column("item_weight_magnifier", sa.Integer(), nullable=False),
        sa.Column("item_weight_beer", sa.Integer(), nullable=False),
        sa.Column("item_weight_burst", sa.Integer(), nullable=False),
        sa.Column("item_weight_lock", sa.Integer(), nullable=False),
        sa.Column(
            "action_copy_pool",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "final_copy_pool",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade(name: str = "") -> None:
    if name:
        return
    op.drop_table("komari_roulette_config")
