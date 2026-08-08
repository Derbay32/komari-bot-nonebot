"""komari_chat 强类型配置表：承接 komari_memory 迁出的 10 个活配置字段

迁移 ID: 0004
父迁移: 0003
创建时间: 2026-08-08 20:30:00

主动回复频控与回复送达副作用 outbox 的 10 个活字段从
``komari_memory_config`` 迁入 komari_chat 自有单行表
``komari_chat_config``（结构真源为 ``komari_chat/config_schema.py`` 的
``KomariChatConfigSchema``）；死字段 ``proactive_score_threshold`` 随批
删除，不迁移。本 revision 在同一事务内完成：

1. 建 ``komari_chat_config``（10 个迁出字段，存储专用列 id/revision/
   updated_at 与其余配置表约定一致）；
2. 从 ``komari_memory_config`` 单行（id=1）COPY 活字段值；无行则不插入，
   运行时 config_manager 会按默认值初始化；
3. ``komari_memory_config`` 仅 DROP 这 11 列，表本身保留（其余字段仍归
   komari_memory 所有），不得删表。

迁移后运行时行为零变化：字段默认值、范围校验与 apply_mode 元数据原样
保留（见 ``komari_chat/config_schema.py``）。

本文件自包含，不导入任何 ``komari_bot`` 运行时代码；DDL 与
``migrations/env.py`` 合并的 SQLModel 元数据逐列一致。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: 从 komari_memory_config 迁入 komari_chat_config 的活字段（COPY + 回填共用）。
_MIGRATED_COLUMNS: tuple[str, ...] = (
    "proactive_enabled",
    "proactive_cooldown",
    "proactive_max_per_hour",
    "proactive_reservation_ttl_seconds",
    "reply_commit_worker_interval_seconds",
    "reply_commit_batch_size",
    "reply_commit_lease_seconds",
    "reply_commit_max_attempts",
    "reply_commit_retry_base_seconds",
    "reply_commit_tombstone_retention_days",
)

#: 从 komari_memory_config 删除的全部列（10 活字段 + 死字段）。
_DROPPED_COLUMNS: tuple[str, ...] = (*_MIGRATED_COLUMNS, "proactive_score_threshold")

#: 逐列 DROP 语句（字面量自包含，便于审阅与静态校验）。
_DROP_COLUMN_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE komari_memory_config DROP COLUMN proactive_enabled",
    "ALTER TABLE komari_memory_config DROP COLUMN proactive_score_threshold",
    "ALTER TABLE komari_memory_config DROP COLUMN proactive_cooldown",
    "ALTER TABLE komari_memory_config DROP COLUMN proactive_max_per_hour",
    "ALTER TABLE komari_memory_config DROP COLUMN proactive_reservation_ttl_seconds",
    "ALTER TABLE komari_memory_config DROP COLUMN reply_commit_worker_interval_seconds",
    "ALTER TABLE komari_memory_config DROP COLUMN reply_commit_batch_size",
    "ALTER TABLE komari_memory_config DROP COLUMN reply_commit_lease_seconds",
    "ALTER TABLE komari_memory_config DROP COLUMN reply_commit_max_attempts",
    "ALTER TABLE komari_memory_config DROP COLUMN reply_commit_retry_base_seconds",
    "ALTER TABLE komari_memory_config DROP COLUMN reply_commit_tombstone_retention_days",
)

#: 列类型（downgrade 重建列时使用，与 0002 中 komari_memory_config 的 DDL 一致）。
_COLUMN_TYPES: dict[str, str] = {
    "proactive_enabled": "BOOLEAN",
    "proactive_score_threshold": "FLOAT",
    "proactive_cooldown": "INTEGER",
    "proactive_max_per_hour": "INTEGER",
    "proactive_reservation_ttl_seconds": "INTEGER",
    "reply_commit_worker_interval_seconds": "INTEGER",
    "reply_commit_batch_size": "INTEGER",
    "reply_commit_lease_seconds": "INTEGER",
    "reply_commit_max_attempts": "INTEGER",
    "reply_commit_retry_base_seconds": "INTEGER",
    "reply_commit_tombstone_retention_days": "INTEGER",
}

#: 列默认值（downgrade 重建 NOT NULL 列时临时携带，回填后移除；
#: 与 schema 默认值一致，保证空表回滚后结构与语义完整）。
_COLUMN_DEFAULTS: dict[str, str] = {
    "proactive_enabled": "false",
    "proactive_score_threshold": "0.0",
    "proactive_cooldown": "300",
    "proactive_max_per_hour": "400",
    "proactive_reservation_ttl_seconds": "360",
    "reply_commit_worker_interval_seconds": "5",
    "reply_commit_batch_size": "20",
    "reply_commit_lease_seconds": "120",
    "reply_commit_max_attempts": "20",
    "reply_commit_retry_base_seconds": "5",
    "reply_commit_tombstone_retention_days": "30",
}

_CREATE_TABLE_SQL = """
CREATE TABLE komari_chat_config (
    id INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    proactive_enabled BOOLEAN NOT NULL,
    proactive_cooldown INTEGER NOT NULL,
    proactive_max_per_hour INTEGER NOT NULL,
    proactive_reservation_ttl_seconds INTEGER NOT NULL,
    reply_commit_worker_interval_seconds INTEGER NOT NULL,
    reply_commit_batch_size INTEGER NOT NULL,
    reply_commit_lease_seconds INTEGER NOT NULL,
    reply_commit_max_attempts INTEGER NOT NULL,
    reply_commit_retry_base_seconds INTEGER NOT NULL,
    reply_commit_tombstone_retention_days INTEGER NOT NULL,
    PRIMARY KEY (id)
)
"""


def upgrade(name: str = "") -> None:
    if name:
        return

    op.execute(_CREATE_TABLE_SQL)

    # 数据搬运在迁移内完成，不留运行时搬运逻辑：
    # 无行则不插入，运行时 config_manager 会按默认值初始化。
    columns = ", ".join(_MIGRATED_COLUMNS)
    op.execute(
        f"INSERT INTO komari_chat_config (id, revision, updated_at, {columns}) "
        f"SELECT 1, 1, updated_at, {columns} FROM komari_memory_config "
        "WHERE id = 1"
    )

    for statement in _DROP_COLUMN_STATEMENTS:
        op.execute(statement)


def downgrade(name: str = "") -> None:
    if name:
        return

    # 重建列（带临时默认值满足 NOT NULL，回填后移除默认值）
    for column in _DROPPED_COLUMNS:
        op.execute(
            f"ALTER TABLE komari_memory_config ADD COLUMN {column} "
            f"{_COLUMN_TYPES[column]} DEFAULT {_COLUMN_DEFAULTS[column]} NOT NULL"
        )

    # 从 komari_chat_config 回填活字段值
    columns = ", ".join(_MIGRATED_COLUMNS)
    op.execute(
        f"UPDATE komari_memory_config SET ({columns}) = "
        f"(SELECT {columns} FROM komari_chat_config WHERE id = 1) "
        "WHERE id = 1"
    )

    for column in _DROPPED_COLUMNS:
        op.execute(
            f"ALTER TABLE komari_memory_config ALTER COLUMN {column} DROP DEFAULT"
        )

    op.execute("DROP TABLE komari_chat_config")
