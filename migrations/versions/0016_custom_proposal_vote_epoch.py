"""komari_custom 提案表新增 vote_epoch 投票轮次列（TSK-226）

迁移 ID: 0016
父迁移: 0015

TSK-226（AC5）要求恢复准入后按换届式轮换投票轮次，上一 epoch 沉眠期累计的
``voted_users``/``vote_count`` 不得作为新 epoch 达标依据触发自动采纳。本轮
workflow 在当前迁移链上追加一个普通 revision：

1. ``komari_custom_proposals`` 新增 ``vote_epoch INTEGER NOT NULL
   DEFAULT 1``：既有行以默认值补齐，新行由发布认领时承载。

关键设计（同一事务内）：
- 列名/类型/默认与 ``ProposalRow`` 元数据（``server_default=text("1")``）
  逐列一致，保证 ``orm_bootstrap check`` 零 drift；
- 采用非空 server 默认的加法迁移，不触碰业务层现有列，为回滚提供确定降级
  （``downgrade`` 删除该列）。

迁移链重排由 TSK-232 负责，本 revision 按当前链正常追加，不重排、不 alias、
不提供双读/缓存兼容。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "komari_custom_proposals"


def upgrade(name: str = "") -> None:
    if name:
        return

    op.execute(
        f"ALTER TABLE {_TABLE} "
        "ADD COLUMN vote_epoch INTEGER NOT NULL DEFAULT 1"
    )


def downgrade(name: str = "") -> None:
    if name:
        return

    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN vote_epoch")
