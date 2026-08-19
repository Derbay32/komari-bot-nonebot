"""图片理解模式与下载预算迁移：memory → chat（TSK-194 / ADR-0010）

迁移 ID: 0015
父迁移: 0014

TSK-194 验收标准：``komari_chat`` 拥有图片理解模式与 8 项图片输入预算，
``komari_memory`` 不再持有任何图片配置。本 revision 在同一事务内完成：

1. ``komari_chat_config`` 新增 ``image_understanding_mode``（默认
   ``delegated``）与 8 项 ``vision_image_download_*`` 预算列，全部带
   非空 server 默认值（升级以默认值补齐存量行）；
2. 按旧 bool 双分支精确迁移：memory ``vision_tool_enabled=true`` →
   ``delegated``、``false`` → ``native``；8 项预算值原样复制到 chat；
   memory 无单行时 chat 行保持默认 ``delegated``；
3. 随后删除 ``komari_memory_config`` 的 9 个旧图片列（开关 + 8 项预算），
   不保留 alias / 双读 / fallback；
4. 新增两个图片预算跨字段 CHECK 约束（总字节 >= 单图字节、总时限 >=
   连接超时），与 ``KomariChatConfigSchema`` 的
   ``ck_komari_chat_config_image_budget_*`` 元数据零漂移。

``downgrade`` 反向执行：重建 memory 9 列（携带与 schema 一致的临时默认
值）→ 从 chat 单行回填（模式 delegated→true / native→false，预算原样）→
移除临时默认值 → 删除 chat 的 2 个 CHECK 约束与 9 个新列。回填 UPDATE
带 EXISTS 守卫：chat 单行未初始化时跳过回填，保留临时默认值，避免 NOT
NULL 列被赋 NULL（模式为 NULL 时回退临时默认 true，对应
``vision_tool_enabled=True`` 旧行为）。

本文件自包含，不导入任何 ``komari_bot`` 运行时代码；DDL 与
``migrations/env.py`` 合并的 SQLModel 元数据逐列一致。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "komari_chat_config"
_MEMORY_TABLE = "komari_memory_config"

#: 从 komari_memory_config 迁入 komari_chat_config 的 8 项预算列
#:（值原样 COPY，列名与类型一致）。
_BUDGET_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("vision_image_download_max_count", "INTEGER", "4"),
    ("vision_image_download_max_bytes", "INTEGER", "8388608"),
    ("vision_image_download_total_max_bytes", "INTEGER", "20971520"),
    ("vision_image_download_max_pixels", "INTEGER", "40000000"),
    ("vision_image_download_concurrency", "INTEGER", "2"),
    ("vision_image_download_connect_timeout_seconds", "FLOAT", "5.0"),
    ("vision_image_download_read_timeout_seconds", "FLOAT", "30.0"),
    ("vision_image_download_total_timeout_seconds", "FLOAT", "45.0"),
)

#: 从 komari_memory_config 删除的全部旧图片列（开关 + 8 项预算）。
_DROPPED_MEMORY_COLUMNS: tuple[str, ...] = (
    "vision_tool_enabled",
    "vision_image_download_max_count",
    "vision_image_download_max_bytes",
    "vision_image_download_total_max_bytes",
    "vision_image_download_max_pixels",
    "vision_image_download_concurrency",
    "vision_image_download_connect_timeout_seconds",
    "vision_image_download_read_timeout_seconds",
    "vision_image_download_total_timeout_seconds",
)

#: 逐列 DROP 语句（字面量自包含，便于审阅与静态校验）。
_DROP_COLUMN_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE komari_memory_config DROP COLUMN vision_tool_enabled",
    "ALTER TABLE komari_memory_config DROP COLUMN vision_image_download_max_count",
    "ALTER TABLE komari_memory_config DROP COLUMN vision_image_download_max_bytes",
    "ALTER TABLE komari_memory_config DROP COLUMN vision_image_download_total_max_bytes",
    "ALTER TABLE komari_memory_config DROP COLUMN vision_image_download_max_pixels",
    "ALTER TABLE komari_memory_config DROP COLUMN vision_image_download_concurrency",
    "ALTER TABLE komari_memory_config DROP COLUMN vision_image_download_connect_timeout_seconds",
    "ALTER TABLE komari_memory_config DROP COLUMN vision_image_download_read_timeout_seconds",
    "ALTER TABLE komari_memory_config DROP COLUMN vision_image_download_total_timeout_seconds",
)

#: 列类型（downgrade 重建 memory 列时使用，与 0002 中 DDL 一致）。
_MEMORY_COLUMN_TYPES: dict[str, str] = {
    "vision_tool_enabled": "BOOLEAN",
    **{column: column_type for column, column_type, _default in _BUDGET_COLUMNS},
}

#: 列默认值（downgrade 重建 NOT NULL 列时临时携带，回填后移除；
#: 与 0002 时代 memory schema 默认值一致，保证空表回滚后结构完整）。
_MEMORY_COLUMN_DEFAULTS: dict[str, str] = {
    "vision_tool_enabled": "true",
    **{column: default for column, _type, default in _BUDGET_COLUMNS},
}

#: downgrade 重建 memory 列的逐列 ADD 语句（字面量自包含）。
_ADD_MEMORY_COLUMN_STATEMENTS: tuple[str, ...] = tuple(
    f"ALTER TABLE komari_memory_config ADD COLUMN {column} "
    f"{_MEMORY_COLUMN_TYPES[column]} "
    f"DEFAULT {_MEMORY_COLUMN_DEFAULTS[column]} NOT NULL"
    for column in _DROPPED_MEMORY_COLUMNS
)

#: downgrade 移除临时默认值的逐列语句（字面量自包含）。
_DROP_MEMORY_COLUMN_DEFAULT_STATEMENTS: tuple[str, ...] = tuple(
    f"ALTER TABLE komari_memory_config ALTER COLUMN {column} DROP DEFAULT"
    for column in _DROPPED_MEMORY_COLUMNS
)

_IMAGE_BUDGET_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "ck_komari_chat_config_image_budget_bytes",
        "vision_image_download_total_max_bytes >= vision_image_download_max_bytes",
    ),
    (
        "ck_komari_chat_config_image_budget_timeout",
        "vision_image_download_total_timeout_seconds "
        ">= vision_image_download_connect_timeout_seconds",
    ),
)


def upgrade(name: str = "") -> None:
    if name:
        return

    # 1. chat 表新增模式列（非空默认 delegated，升级补齐存量行）
    op.execute(
        "ALTER TABLE komari_chat_config ADD COLUMN image_understanding_mode "
        "VARCHAR(32) NOT NULL DEFAULT 'delegated'"
    )

    # 2. chat 表新增 8 项预算列（非空默认，升级补齐存量行）
    for column, column_type, default in _BUDGET_COLUMNS:
        op.execute(
            f"ALTER TABLE {_TABLE} "
            f"ADD COLUMN {column} {column_type} NOT NULL DEFAULT {default}"
        )

    # 3. 图片预算跨字段 CHECK（与 Schema __table_args__ 零漂移）
    for constraint_name, expression in _IMAGE_BUDGET_CHECKS:
        op.execute(
            f"ALTER TABLE {_TABLE} "
            f"ADD CONSTRAINT {constraint_name} CHECK ({expression})"
        )

    # 4. 数据迁移：旧 bool 双分支 → 模式；8 项预算原样复制。
    #    memory 无单行（或 chat 无单行）时跳过，chat 行保持默认 delegated。
    budget_assignments = ", ".join(
        f"{column} = m.{column}" for column, _type, _default in _BUDGET_COLUMNS
    )
    op.execute(
        f"UPDATE {_TABLE} t SET image_understanding_mode = "
        "CASE WHEN m.vision_tool_enabled THEN 'delegated' ELSE 'native' END, "
        f"{budget_assignments} "
        f"FROM {_MEMORY_TABLE} m WHERE t.id = 1 AND m.id = 1"
    )

    # 5. 删除 memory 旧图片列（开关 + 8 项预算），不留 alias / 双读
    for statement in _DROP_COLUMN_STATEMENTS:
        op.execute(statement)


def downgrade(name: str = "") -> None:
    if name:
        return

    # 1. 重建 memory 旧图片列（带临时默认值满足 NOT NULL，回填后移除）
    for statement in _ADD_MEMORY_COLUMN_STATEMENTS:
        op.execute(statement)

    # 2. 从 chat 单行回填（EXISTS 守卫：chat 单行未初始化时跳过，
    #    保留临时默认值，避免 NOT NULL 列被赋 NULL）
    budget_assignments = ", ".join(
        f"{column} = t.{column}" for column, _type, _default in _BUDGET_COLUMNS
    )
    op.execute(
        f"UPDATE {_MEMORY_TABLE} m SET "
        "vision_tool_enabled = "
        "CASE WHEN t.image_understanding_mode = 'delegated' THEN true ELSE false END, "
        f"{budget_assignments} "
        f"FROM {_TABLE} t WHERE m.id = 1 AND t.id = 1"
    )

    # 3. 移除临时默认值
    for statement in _DROP_MEMORY_COLUMN_DEFAULT_STATEMENTS:
        op.execute(statement)

    # 4. 删除 chat 图片预算 CHECK 约束，再删 chat 新列；存量行保留
    for constraint_name, _expression in _IMAGE_BUDGET_CHECKS:
        op.execute(
            f"ALTER TABLE {_TABLE} DROP CONSTRAINT {constraint_name}"
        )
    op.execute(
        "ALTER TABLE komari_chat_config DROP COLUMN image_understanding_mode"
    )
    for column, _type, _default in _BUDGET_COLUMNS:
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN {column}")
