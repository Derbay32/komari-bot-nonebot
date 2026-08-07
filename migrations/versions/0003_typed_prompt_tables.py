"""强类型 Prompt 表：3 个 Prompt 资源各自一张 SQLModel 单行表

迁移 ID: 0003
父迁移: 0002
创建时间: 2026-08-06 17:30:00

本 revision 为 3 个 Prompt 资源建立强类型存储（结构真源为
``komari_bot/config/typed_config.TypedPromptModel`` 与各插件
``prompt_schema`` 的 SQLModel 元数据）：

- ``komari_chat`` → ``komari_prompt_komari_chat``
- ``komari_memory_summary`` → ``komari_prompt_memory_summary``
- ``group_history_summary`` → ``komari_prompt_group_history_summary``

每张表单行使用，主键 ``id`` 恒为 1；``revision`` 为跨进程 CAS 修订号，
写操作原子自增；``updated_at`` 为最后写入时间（带时区），由存储层显式
赋值。Prompt 正文统一为 TEXT 列；``display_name`` 不落库，管理 API 的
展示名由代码常量提供。

旧版通用 JSONB KV 表 ``komari_prompt_configs`` 在本迁移中保留，不做数据
回灌或删表：存量值由后续离线迁移脚本一次性搬运（ticket 05），旧表的
``DROP`` 由后续版本的 autogenerate revision 执行。

跨进程 Prompt 变更不再使用 asyncpg LISTEN/NOTIFY：运行时
``PromptTemplateLoader`` 缓存自带 1 秒陈限上限，本进程写入立即失效本地
缓存；因此本迁移不创建触发器或通知函数。

本文件自包含，不导入任何 ``komari_bot`` 运行时代码；DDL 与
``migrations/env.py`` 合并的 SQLModel 元数据逐列一致。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE komari_prompt_komari_chat (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        system_prompt TEXT NOT NULL,
        memory_ack TEXT NOT NULL,
        memory_ack_role TEXT NOT NULL,
        output_instruction TEXT NOT NULL,
        cot_prefix TEXT NOT NULL,
        cot_prefix_role TEXT NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE komari_prompt_memory_summary (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        memory_summary_common_system TEXT NOT NULL,
        profile_agent_workflow_system TEXT NOT NULL,
        summary_workflow_system TEXT NOT NULL,
        json_response_example TEXT NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE komari_prompt_group_history_summary (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        system_prompt TEXT NOT NULL,
        planning_system_prompt TEXT NOT NULL,
        memory_ack TEXT NOT NULL,
        memory_ack_role TEXT NOT NULL,
        output_instruction TEXT NOT NULL,
        cot_prefix TEXT NOT NULL,
        cot_prefix_role TEXT NOT NULL,
        PRIMARY KEY (id)
    )
    """,
)

_TABLE_NAMES: tuple[str, ...] = (
    "komari_prompt_komari_chat",
    "komari_prompt_memory_summary",
    "komari_prompt_group_history_summary",
)


def upgrade(name: str = "") -> None:
    if name:
        return

    for statement in _TABLE_STATEMENTS:
        op.execute(statement)


def downgrade(name: str = "") -> None:
    if name:
        return

    for table_name in reversed(_TABLE_NAMES):
        op.execute(f"DROP TABLE IF EXISTS {table_name}")
