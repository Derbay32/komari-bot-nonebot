"""fresh marker：全新数据库的身份标记（非全量基线）

迁移 ID: 0001
父迁移: None
创建时间: 2026-08-22 00:00:00

ADR-0012「定期执行基线 0001 fresh marker」把旧式一次性全量基线折为
chain 的空迁移 ``0001_fresh_marker``：

- 0001 只登记为版本链唯一基线身份，不创建任何 legacy 大表、不建
  pgvector 扩展、不产生任何结构变更；编号仅用于 Alembic 判定
  “无更早 revision 可依赖”。
- 全新空库经 0002 起逐 revision 建出全部表（含强类型配置表）；既有
  legacy 生产库由运维按上线窗口继续执行后续 upgrade，不依赖 0001 建表。

本 revision 自包含，不导入任何 ``komari_bot`` 运行时代码。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return
    # fresh marker：空迁移，不建表、不建扩展、不创建触发器。
    op.execute("SELECT 1")


def downgrade(name: str = "") -> None:
    if name:
        return
    # 空迁移的降级为 no-op；没有建任何对象可回收。
    op.execute("SELECT 1")