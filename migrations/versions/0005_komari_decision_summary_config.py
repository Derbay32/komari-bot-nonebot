"""群总结归类配置列：加入既有 komari_decision_config 单行表

迁移 ID: 0005
父迁移: 0004
创建时间: 2026-08-10 03:20:00

群总结场景归类（KOMARIBOT-22）的 9 个运维字段加入判定插件的既有强类型
单行表 ``komari_decision_config``（结构真源为
``komari_decision/config_schema.py`` 的 ``KomariDecisionConfigSchema``）。
本 revision 只做列级 DDL，不触碰表结构与数据搬运：

1. ``summary_embedding_instruction_query`` / ``summary_rerank_instruction``
   （VARCHAR NOT NULL）、``summary_scene_top_k``（INTEGER NOT NULL）、
   ``summary_rerank_enabled``（BOOLEAN NOT NULL）、``summary_rerank_threshold``
   （FLOAT NOT NULL）、``summary_rerank_fallback_enabled``（BOOLEAN NOT
   NULL）、``summary_rerank_failure_threshold``（INTEGER NOT NULL）、
   ``summary_rerank_failure_window_seconds``（INTEGER NOT NULL）为 NOT NULL
   列，ADD COLUMN 时携带与 schema 默认值一致的临时 server default，保证
   既有单行（id=1）安全获得默认值；随后 DROP DEFAULT，最终不留无意的
   server default；
2. ``summary_similarity_threshold``（FLOAT NULL）为可空列，直接 ADD，
   无默认值。

``downgrade`` 反向执行：逐列 DROP COLUMN，不删表。字段默认值与范围校验
的运行时语义由 Pydantic Schema 承载（见 ``config_schema.py``），迁移内
不复制业务逻辑。

本文件自包含，不导入任何 ``komari_bot`` 运行时代码；DDL 与
``migrations/env.py`` 合并的 SQLModel 元数据逐列一致。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 逐列 ADD COLUMN 语句（升级方向；NOT NULL 列携带与 schema 默认值一致的
#: 临时 server default，字面量自包含，便于审阅与静态校验）。
_ADD_COLUMN_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE komari_decision_config ADD COLUMN "
    "summary_embedding_instruction_query VARCHAR DEFAULT "
    "'任务：将群聊历史消息编码为群总结场景归类检索向量。重点保留消息的对话意图、话题归属、事件类型与信息价值；忽略口头禅、语气词、无意义重复字符。' "
    "NOT NULL",
    "ALTER TABLE komari_decision_config ADD COLUMN "
    "summary_rerank_instruction VARCHAR DEFAULT "
    "'你在做群聊总结候选场景归类精排。按语义匹配强度给候选打分：1) 消息与场景的归属度；2) 场景区分度；3) 信息价值。优先语义，不因礼貌措辞或语气强弱偏置。' "
    "NOT NULL",
    "ALTER TABLE komari_decision_config ADD COLUMN "
    "summary_scene_top_k INTEGER DEFAULT 4 NOT NULL",
    "ALTER TABLE komari_decision_config ADD COLUMN "
    "summary_rerank_enabled BOOLEAN DEFAULT true NOT NULL",
    "ALTER TABLE komari_decision_config ADD COLUMN "
    "summary_rerank_threshold FLOAT DEFAULT 0.6 NOT NULL",
    "ALTER TABLE komari_decision_config ADD COLUMN "
    "summary_similarity_threshold FLOAT",
    "ALTER TABLE komari_decision_config ADD COLUMN "
    "summary_rerank_fallback_enabled BOOLEAN DEFAULT false NOT NULL",
    "ALTER TABLE komari_decision_config ADD COLUMN "
    "summary_rerank_failure_threshold INTEGER DEFAULT 3 NOT NULL",
    "ALTER TABLE komari_decision_config ADD COLUMN "
    "summary_rerank_failure_window_seconds INTEGER DEFAULT 3600 NOT NULL",
)

#: 移除临时 server default 的语句（只针对 NOT NULL 列，可空列无默认值）。
_DROP_DEFAULT_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE komari_decision_config ALTER COLUMN "
    "summary_embedding_instruction_query DROP DEFAULT",
    "ALTER TABLE komari_decision_config ALTER COLUMN "
    "summary_rerank_instruction DROP DEFAULT",
    "ALTER TABLE komari_decision_config ALTER COLUMN "
    "summary_scene_top_k DROP DEFAULT",
    "ALTER TABLE komari_decision_config ALTER COLUMN "
    "summary_rerank_enabled DROP DEFAULT",
    "ALTER TABLE komari_decision_config ALTER COLUMN "
    "summary_rerank_threshold DROP DEFAULT",
    "ALTER TABLE komari_decision_config ALTER COLUMN "
    "summary_rerank_fallback_enabled DROP DEFAULT",
    "ALTER TABLE komari_decision_config ALTER COLUMN "
    "summary_rerank_failure_threshold DROP DEFAULT",
    "ALTER TABLE komari_decision_config ALTER COLUMN "
    "summary_rerank_failure_window_seconds DROP DEFAULT",
)

#: 逐列 DROP COLUMN 语句（降级方向；字面量自包含）。
_DROP_COLUMN_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE komari_decision_config DROP COLUMN summary_embedding_instruction_query",
    "ALTER TABLE komari_decision_config DROP COLUMN summary_rerank_instruction",
    "ALTER TABLE komari_decision_config DROP COLUMN summary_scene_top_k",
    "ALTER TABLE komari_decision_config DROP COLUMN summary_rerank_enabled",
    "ALTER TABLE komari_decision_config DROP COLUMN summary_rerank_threshold",
    "ALTER TABLE komari_decision_config DROP COLUMN summary_similarity_threshold",
    "ALTER TABLE komari_decision_config DROP COLUMN summary_rerank_fallback_enabled",
    "ALTER TABLE komari_decision_config DROP COLUMN summary_rerank_failure_threshold",
    "ALTER TABLE komari_decision_config DROP COLUMN summary_rerank_failure_window_seconds",
)


def upgrade(name: str = "") -> None:
    if name:
        return

    for statement in _ADD_COLUMN_STATEMENTS:
        op.execute(statement)

    # 既有单行已由 ADD COLUMN 的临时默认值安全填充；移除默认值后与
    # SQLModel 元数据一致（默认值由 Pydantic Schema 承载，不留 server default）
    for statement in _DROP_DEFAULT_STATEMENTS:
        op.execute(statement)


def downgrade(name: str = "") -> None:
    if name:
        return

    for statement in _DROP_COLUMN_STATEMENTS:
        op.execute(statement)
