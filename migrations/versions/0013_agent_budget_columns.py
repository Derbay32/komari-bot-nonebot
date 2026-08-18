"""回复 Agent 执行预算列迁移：komari_chat_config 新增三项预算列

迁移 ID: 0013
父迁移: 0012

TSK-192 验收标准 1/2：回复 Agent 的执行预算从模块常量和入口参数收敛为
动态配置，本 revision 在 ``komari_chat_config`` 上新增三项预算列：

- ``agent_max_rounds``（默认 10，范围 2..20）：最大逻辑轮次；
- ``agent_max_tool_calls_per_round``（默认 4，范围 1..8）：单轮最大
  工具调用数；
- ``agent_max_total_tool_calls``（默认 20，范围 2..64）：整任务最大
  工具调用总数。

跨字段约束：``agent_max_tool_calls_per_round <= agent_max_total_tool_calls
<= agent_max_rounds * agent_max_tool_calls_per_round`` 以显式 CHECK
约束在数据库侧表达，与 ``KomariChatConfigSchema`` 的
``ck_komari_chat_config_agent_budget`` 元数据零漂移。

新列均不带 ``DEFAULT`` 之外的其他 server 行为；单行表按运行时
config_manager 的 upsert 路径显式赋值，历史行（如有）由默认值补齐。

本 revision 自包含（不导入 komari_bot），downgrade 对称回退：
先 DROP 跨字段 CHECK 约束，再逐列 DROP 新列（列级范围约束由
Pydantic 单字段 ``ge/le`` 表达，不在数据库冗余声明）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "komari_chat_config"
_CHECK_NAME = "ck_komari_chat_config_agent_budget"

#: 列名 -> 数据库默认值字面量（与 Schema 默认一致，供历史行补齐）。
_AGENT_BUDGET_COLUMNS: tuple[tuple[str, str], ...] = (
    ("agent_max_rounds", "10"),
    ("agent_max_tool_calls_per_round", "4"),
    ("agent_max_total_tool_calls", "20"),
)


def upgrade(name: str = "") -> None:
    if name:
        return

    for column, default in _AGENT_BUDGET_COLUMNS:
        op.execute(
            f"ALTER TABLE {_TABLE} "
            f"ADD COLUMN {column} INTEGER NOT NULL DEFAULT {default}"
        )

    # 跨字段 CHECK：单轮预算 <= 总预算 <= 轮次 x 单轮预算。
    # 列级范围约束由 Pydantic 单字段 ge/le 表达，数据库侧只冗余跨字段语义。
    op.execute(
        f"ALTER TABLE {_TABLE} "
        f"ADD CONSTRAINT {_CHECK_NAME} CHECK ("
        "agent_max_tool_calls_per_round <= agent_max_total_tool_calls "
        "AND agent_max_total_tool_calls <= agent_max_rounds * agent_max_tool_calls_per_round"
        ")"
    )


def downgrade(name: str = "") -> None:
    if name:
        return

    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT {_CHECK_NAME}")
    for column, _default in _AGENT_BUDGET_COLUMNS:
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN {column}")
