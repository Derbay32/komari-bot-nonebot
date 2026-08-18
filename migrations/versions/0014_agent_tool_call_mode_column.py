"""工具调用约束模式列迁移：komari_chat_config 新增 agent_tool_call_mode

迁移 ID: 0014
父迁移: 0013

TSK-193 验收标准：回复 Agent 的工具调用约束模式成为 ``komari_chat``
动态配置字段，本 revision 在 ``komari_chat_config`` 上新增
``agent_tool_call_mode`` 列：

- 类型 ``VARCHAR(32)``、``NOT NULL``，取值 ``required``（默认）或
  ``prompt_guided``，与 ``KomariChatConfigSchema`` 的强类型 Literal
  元数据零漂移；
- 非空 server default ``'required'``：升级时以默认值补齐存量行，
  全新行（未显式赋值）同样得到默认 required；
- 无兼容值 / 宽松规范化：非法值由 Pydantic Literal 拒绝。

本 revision 自包含（不导入 komari_bot），downgrade 对称回退：显式
DROP COLUMN 该列，存量行保留。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "komari_chat_config"


def upgrade(name: str = "") -> None:
    if name:
        return

    op.execute(
        "ALTER TABLE komari_chat_config "
        "ADD COLUMN agent_tool_call_mode VARCHAR(32) NOT NULL DEFAULT 'required'"
    )


def downgrade(name: str = "") -> None:
    if name:
        return

    op.execute(
        "ALTER TABLE komari_chat_config DROP COLUMN agent_tool_call_mode"
    )
