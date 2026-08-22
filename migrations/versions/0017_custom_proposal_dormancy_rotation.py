"""komari_custom 提案休眠换届轮换列（TSK-226）

迁移 ID: 0017
父迁移: 0016

AC5 二次回炉要求提案能感知「休眠过」并在恢复获准后换届轮换：受限期间业务处
理置休眠标记，恢复后首次业务效果轮换 vote_epoch，并把休眠期平台累积票整批记
为旧轮 baseline，使新轮达标只看「当前票 - baseline」。本轮 workflow 在当前链
上追加 revision：

1. ``komari_custom_proposals`` 新增 ``vote_baseline_voters ARRAY(Text) NOT
   NULL DEFAULT '{}'``：换届轮换时记录旧轮投票者快照。
2. 新增 ``dormant_seen BOOLEAN NOT NULL DEFAULT false``：受限（休眠）期业务
   处理置真，恢复换届识别后清除。

两列均带非空 server 默认值补齐存量行；列名/类型/默认与 ``ProposalRow`` 元数据
逐列一致（``server_default='{}'`` 与 ``'false'``），保证 ``orm_bootstrap
check`` 零 drift。``downgrade`` 反向删除两列。迁移链重排由 TSK-232 负责。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "komari_custom_proposals"

_COLUMNS: tuple[tuple[str, str, str], ...] = (
    (
        "vote_baseline_voters",
        "TEXT[]",
        "'{}'",
    ),
    ("dormant_seen", "BOOLEAN", "false"),
)


def upgrade(name: str = "") -> None:
    if name:
        return

    for column, column_type, default in _COLUMNS:
        op.execute(
            f"ALTER TABLE {_TABLE} "
            f"ADD COLUMN {column} {column_type} NOT NULL DEFAULT {default}"
        )


def downgrade(name: str = "") -> None:
    if name:
        return

    for column, _column_type, _default in _COLUMNS:
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN {column}")
