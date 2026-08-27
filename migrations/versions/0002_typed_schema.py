"""全量基线：一次性建出当前运行时全部 PostgreSQL 表结构

迁移 ID: 0001
父迁移: None
创建时间: 2026-08-06 00:21:00

本 revision 是版本链的唯一基线，结构真源为当前运行时代码：

- ``komari_bot/plugins/config_manager/storage.py``
- ``komari_bot/config/prompt_storage.py``
- ``komari_bot/plugins/user_ban/init_db.sql``
- ``komari_bot/plugins/character_binding/database.py``
- ``komari_bot/plugins/user_data/database.py``
- ``komari_bot/plugins/komari_custom/init_db.sql``
- ``komari_bot/plugins/komari_management/announcement_repository.py``
- ``komari_bot/plugins/komari_decision/repositories/scene_schema.py``
- ``komari_bot/db/vector_storage_schema.py``
- ``komari_bot/plugins/komari_chat/repositories/reply_commit_repository.py``
- ``komari_bot/plugins/agent_run_logger/repository.py``

约定：

- 面向空库 ``upgrade head``：直接建出最终形态，不复刻运行时历次
  ``IF NOT EXISTS`` / ``ADD COLUMN IF NOT EXISTS`` 等兼容性迁移分支；
 既有生产库由运维执行 ``alembic stamp``，不走本迁移重建。
- 向量维度从环境变量 ``EMBEDDING_DIMENSION`` 读取（默认 512），
  必须为正整数；维度大于 2000 时与运行时行为一致跳过 HNSW 索引。
- 本文件自包含，不导入任何 ``komari_bot`` 运行时代码。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: pgvector HNSW 索引支持的最大维度，与运行时
#: ``komari_bot.db.vector_storage_schema.PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS``
#: 保持一致。
_PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS = 2000


def _read_embedding_dimension() -> int:
    """读取并校验环境变量中的向量维度（默认 512）。"""
    raw = os.environ.get("EMBEDDING_DIMENSION", "512")
    try:
        dimension = int(raw)
    except ValueError as exc:
        msg = f"EMBEDDING_DIMENSION 必须是正整数，实际值: {raw!r}"
        raise ValueError(msg) from exc
    if dimension <= 0:
        msg = f"非法 embedding 维度: {raw!r}"
        raise ValueError(msg)
    return dimension


def upgrade(name: str = "") -> None:
    if name:
        return

    dimension = _read_embedding_dimension()
    create_hnsw = dimension <= _PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS
    statements = _build_upgrade_statements(dimension, create_hnsw=create_hnsw)
    for statement in statements:
        if statement.strip():
            op.execute(statement)


def downgrade(name: str = "") -> None:
    if name:
        return

    # 依赖逆序清理：先删引用方表，再删被引用表，最后删函数与扩展。
    # 触发器随所属表一并删除，这里只需显式删除独立函数。
    statements = (
        # Agent Run 日志轻索引（UNLOGGED）
        "DROP TABLE komari_agent_run_log_index",
        # 聊天回复副作用 outbox
        "DROP TABLE komari_chat_reply_commit_outbox",
        # 帮助扫描租约与帮助/知识库（含搜索索引版本触发器）
        "DROP TABLE komari_help_scan_leases",
        "DROP TABLE komari_help",
        "DROP TABLE komari_knowledge",
        # 关键词索引版本戳表
        "DROP TABLE komari_search_index_versions",
        # 四层记忆：向量表先于主表，避免外键阻塞
        "DROP TABLE komari_memory_interaction_embeddings",
        "DROP TABLE komari_memory_interaction_history",
        "DROP TABLE komari_memory_user_profile",
        "DROP TABLE komari_memory_jobs",
        "DROP TABLE komari_memory_conversation_embeddings",
        "DROP TABLE komari_memory_conversations",
        # 场景系统：item/runtime 引用 scene_set 与 decision_scenes
        "DROP TABLE komari_memory_scene_item",
        "DROP TABLE komari_memory_scene_runtime",
        "DROP TABLE komari_decision_scenes",
        "DROP TABLE komari_memory_scene_set",
        # 知识库提案
        "DROP TABLE komari_custom_proposals",
        # 公告幂等账本
        "DROP TABLE komari_announcement_dispatches",
        # 角色名绑定
        "DROP TABLE komari_character_bindings",
        # 好感度当前值与幂等账本
        "DROP TABLE user_favorability_adjustment_ledger",
        "DROP TABLE user_favorability",
        # 用户封禁三表
        "DROP TABLE komari_user_ban_notification_outbox",
        "DROP TABLE komari_user_ban_cache_state",
        "DROP TABLE komari_user_bans",
        # Prompt 与插件动态配置
        # 独立函数（触发器已随表删除）
        "DROP FUNCTION bump_komari_search_index_version()",
        "DROP FUNCTION update_updated_at_column()",
        # pgvector 扩展（所有 vector 列已随表删除）
        "DROP EXTENSION vector",
    )
    for statement in statements:
        op.execute(statement)


def _build_upgrade_statements(
    dimension: int,
    *,
    create_hnsw: bool,
) -> tuple[str, ...]:
    """组装 upgrade 全部 DDL/DML，按依赖顺序排列。"""
    return (
        *_pgvector_statements(),
        *_user_ban_statements(),
        *_character_binding_statements(),
        *_user_data_statements(),
        *_custom_proposal_statements(),
        *_announcement_statements(),
        *_scene_statements(),
        *_memory_statements(dimension, create_hnsw=create_hnsw),
        *_knowledge_statements(dimension, create_hnsw=create_hnsw),
        *_help_statements(dimension, create_hnsw=create_hnsw),
        *_reply_commit_statements(),
        *_agent_run_log_statements(),
        *_typed_and_prompt_statements(),
    )


def _pgvector_statements() -> tuple[str, ...]:
    return ("CREATE EXTENSION IF NOT EXISTS vector",)


def _user_ban_statements() -> tuple[str, ...]:
    """用户封禁：主表、缓存 revision 单例行、通知 outbox。"""
    return (
        """
        CREATE TABLE komari_user_bans (
            user_id TEXT NOT NULL,
            ban_scope TEXT NOT NULL CHECK (ban_scope IN ('chat', 'command')),
            operator_id TEXT NOT NULL,
            reason TEXT,
            expires_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, ban_scope)
        )
        """,
        """
        CREATE INDEX idx_komari_user_bans_updated_at
        ON komari_user_bans (updated_at DESC)
        """,
        # 部分索引：只索引有到期时间的临时封禁，供自然解封扫描
        """
        CREATE INDEX idx_komari_user_bans_expires_at
        ON komari_user_bans (expires_at)
        WHERE expires_at IS NOT NULL
        """,
        """
        CREATE TABLE komari_user_ban_cache_state (
            singleton_id SMALLINT PRIMARY KEY CHECK (singleton_id = 1),
            revision BIGINT NOT NULL DEFAULT 1,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        # 缓存 revision 单例种子行
        """
        INSERT INTO komari_user_ban_cache_state (singleton_id, revision)
        VALUES (1, 1)
        """,
        """
        CREATE TABLE komari_user_ban_notification_outbox (
            notification_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            notification_kind TEXT NOT NULL
                CHECK (notification_kind = 'natural_expiry'),
            records JSONB,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'processing', 'sent')),
            owner_token TEXT,
            lease_expires_at TIMESTAMPTZ,
            available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error_code TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            sent_at TIMESTAMPTZ
        )
        """,
        """
        CREATE INDEX idx_komari_user_ban_notification_outbox_claim
        ON komari_user_ban_notification_outbox (available_at, created_at)
        WHERE status IN ('pending', 'processing')
        """,
    )


def _character_binding_statements() -> tuple[str, ...]:
    return (
        """
        CREATE TABLE komari_character_bindings (
            user_id TEXT PRIMARY KEY,
            character_name TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
    )


def _user_data_statements() -> tuple[str, ...]:
    """好感度当前值与幂等调整账本。"""
    return (
        """
        CREATE TABLE user_favorability (
            user_id TEXT PRIMARY KEY,
            favorability INTEGER NOT NULL DEFAULT 0
                CHECK (favorability >= 0 AND favorability <= 400),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE user_favorability_adjustment_ledger (
            operation_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            requested_delta INTEGER NOT NULL,
            before_value INTEGER,
            after_value INTEGER,
            result_updated_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (
                (before_value IS NULL AND after_value IS NULL
                    AND result_updated_at IS NULL)
                OR
                (before_value IS NOT NULL AND after_value IS NOT NULL
                    AND result_updated_at IS NOT NULL)
            )
        )
        """,
    )


def _custom_proposal_statements() -> tuple[str, ...]:
    """.custom 知识库提案表（含发布/采纳状态机字段）。"""
    return (
        """
        CREATE TABLE komari_custom_proposals (
            id              SERIAL PRIMARY KEY,
            group_id        BIGINT NOT NULL,
            proposer_id     BIGINT NOT NULL,
            proposer_name   TEXT,
            title           TEXT NOT NULL,
            content         TEXT NOT NULL,
            status          VARCHAR(20) DEFAULT 'publishing' NOT NULL,
            publication_key TEXT NOT NULL,
            publication_token TEXT,
            publication_started_at TIMESTAMPTZ,
            publication_attempts INT DEFAULT 0 NOT NULL,
            publication_error_code TEXT,
            approval_token  TEXT,
            approval_started_at TIMESTAMPTZ,
            vote_message_id BIGINT,
            vote_count      INT DEFAULT 0 NOT NULL,
            required_votes  INT NOT NULL,
            voted_users     TEXT[] DEFAULT '{}' NOT NULL,
            created_at      TIMESTAMPTZ DEFAULT NOW() NOT NULL,
            updated_at      TIMESTAMPTZ DEFAULT NOW() NOT NULL,
            approved_at     TIMESTAMPTZ,
            knowledge_id    INT,
            expired_at      TIMESTAMPTZ
        )
        """,
        """
        CREATE INDEX idx_custom_proposals_status
            ON komari_custom_proposals(status)
        """,
        """
        CREATE INDEX idx_custom_proposals_group_id
            ON komari_custom_proposals(group_id)
        """,
        """
        CREATE INDEX idx_custom_proposals_proposer_status
            ON komari_custom_proposals(proposer_id, status)
        """,
        """
        CREATE INDEX idx_custom_proposals_vote_message_id
            ON komari_custom_proposals(vote_message_id)
            WHERE vote_message_id IS NOT NULL
        """,
        """
        CREATE UNIQUE INDEX idx_custom_proposals_publication_key
            ON komari_custom_proposals(publication_key)
        """,
    )


def _announcement_statements() -> tuple[str, ...]:
    """公告请求跨 worker 幂等与冷却账本。"""
    return (
        """
        CREATE TABLE komari_announcement_dispatches (
            request_id TEXT PRIMARY KEY,
            payload_hash TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK (status IN (
                    'processing', 'completed', 'reconciliation_required', 'done'
                )),
            owner_token TEXT,
            lease_expires_at TIMESTAMPTZ,
            response_payload JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMPTZ
        )
        """,
        """
        CREATE INDEX idx_komari_announcement_dispatches_created_at
        ON komari_announcement_dispatches (created_at DESC)
        """,
    )


def _scene_statements() -> tuple[str, ...]:
    """场景系统：构建集、场景定义、场景条目快照、运行时单例。"""
    return (
        """
        CREATE TABLE komari_memory_scene_set (
            id BIGSERIAL PRIMARY KEY,
            source_path TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            embedding_model TEXT NOT NULL,
            embedding_instruction_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('BUILDING', 'READY', 'FAILED')),
            item_total INT NOT NULL DEFAULT 0 CHECK (item_total >= 0),
            item_ready INT NOT NULL DEFAULT 0 CHECK (item_ready >= 0),
            item_failed INT NOT NULL DEFAULT 0 CHECK (item_failed >= 0),
            error_message TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ready_at TIMESTAMPTZ
        )
        """,
        """
        CREATE INDEX idx_komari_memory_scene_set_status
        ON komari_memory_scene_set(status, created_at DESC)
        """,
        """
        CREATE INDEX idx_komari_memory_scene_set_source_hash
        ON komari_memory_scene_set(source_hash)
        """,
        """
        CREATE TABLE komari_decision_scenes (
            id BIGSERIAL PRIMARY KEY,
            scene_key TEXT NOT NULL UNIQUE,
            scene_type TEXT NOT NULL CHECK (scene_type IN ('fixed', 'general')),
            content_text TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            order_index INT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE INDEX idx_komari_decision_scenes_type_order
        ON komari_decision_scenes(scene_type, enabled, order_index)
        """,
        """
        CREATE INDEX idx_komari_decision_scenes_content_hash
        ON komari_decision_scenes(content_hash)
        """,
        # 场景条目保存构建时刻的场景快照，外键级联删除
        """
        CREATE TABLE komari_memory_scene_item (
            id BIGSERIAL PRIMARY KEY,
            set_id BIGINT NOT NULL
                REFERENCES komari_memory_scene_set(id) ON DELETE CASCADE,
            scene_id BIGINT NOT NULL
                REFERENCES komari_decision_scenes(id) ON DELETE CASCADE,
            scene_key_snapshot TEXT NOT NULL,
            scene_type_snapshot TEXT NOT NULL,
            content_text_snapshot TEXT NOT NULL,
            enabled_snapshot BOOLEAN NOT NULL,
            order_index_snapshot INT NOT NULL,
            content_hash TEXT NOT NULL,
            embedding REAL[],
            embedding_dim INT,
            status TEXT NOT NULL,
            error_message TEXT,
            last_error_code TEXT,
            attempt_count INT NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            next_retry_at TIMESTAMPTZ,
            lease_owner TEXT,
            lease_expires_at TIMESTAMPTZ,
            embedded_at TIMESTAMPTZ,
            CONSTRAINT ck_komari_memory_scene_item_status
                CHECK (status IN ('PENDING', 'PROCESSING', 'READY', 'FAILED')),
            UNIQUE (set_id, scene_id)
        )
        """,
        """
        CREATE INDEX idx_komari_memory_scene_item_scene_id
        ON komari_memory_scene_item(scene_id)
        """,
        """
        CREATE INDEX idx_komari_memory_scene_item_set_status
        ON komari_memory_scene_item(set_id, status)
        """,
        """
        CREATE INDEX idx_komari_memory_scene_item_claim
        ON komari_memory_scene_item(set_id, status, next_retry_at, lease_expires_at)
        """,
        """
        CREATE INDEX idx_komari_memory_scene_item_reuse
        ON komari_memory_scene_item(scene_id, content_hash)
        """,
        """
        CREATE TABLE komari_memory_scene_runtime (
            id INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            active_set_id BIGINT REFERENCES komari_memory_scene_set(id),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        # 运行时单例种子行：初始无激活构建集
        """
        INSERT INTO komari_memory_scene_runtime (id, active_set_id)
        VALUES (1, NULL)
        """,
        # 构建集指纹唯一，防止同源同模型重复构建
        """
        CREATE UNIQUE INDEX idx_komari_memory_scene_set_fingerprint
        ON komari_memory_scene_set(
            source_hash,
            embedding_model,
            embedding_instruction_hash
        )
        """,
    )


def _memory_statements(dimension: int, *, create_hnsw: bool) -> tuple[str, ...]:
    """四层记忆：对话摘要、向量、任务租约、用户画像、互动历史。"""
    statements: list[str] = [
        """
        CREATE TABLE komari_memory_conversations (
            id SERIAL PRIMARY KEY,
            group_id VARCHAR(64) NOT NULL,
            summary TEXT NOT NULL,
            participants TEXT[],
            dedup_key VARCHAR(64),
            start_time TIMESTAMP NOT NULL,
            end_time TIMESTAMP NOT NULL,
            importance INT DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
            importance_initial INT DEFAULT 3
                CHECK (importance_initial BETWEEN 1 AND 5),
            importance_current INT DEFAULT 3
                CHECK (importance_current BETWEEN 0 AND 5),
            last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_fuzzy BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        f"""
        CREATE TABLE komari_memory_conversation_embeddings (
            id BIGSERIAL PRIMARY KEY,
            conversation_id INT NOT NULL
                REFERENCES komari_memory_conversations(id) ON DELETE CASCADE,
            content_hash TEXT NOT NULL,
            embedding VECTOR({dimension}) NOT NULL,
            embedding_dim INT NOT NULL,
            embedded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (conversation_id)
        )
        """,
        """
        CREATE INDEX idx_komari_memory_conv_group
        ON komari_memory_conversations(group_id)
        """,
        """
        CREATE INDEX idx_komari_memory_conv_time
        ON komari_memory_conversations(start_time DESC)
        """,
        """
        CREATE UNIQUE INDEX idx_komari_memory_conv_dedup_key
        ON komari_memory_conversations (dedup_key)
        WHERE dedup_key IS NOT NULL
        """,
        """
        CREATE TABLE komari_memory_jobs (
            job_name TEXT NOT NULL,
            run_date DATE NOT NULL,
            owner_token TEXT NOT NULL,
            lease_until TIMESTAMPTZ NOT NULL,
            stage TEXT NOT NULL,
            attempt INT NOT NULL DEFAULT 0 CHECK (attempt >= 0),
            last_error_code TEXT,
            started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMPTZ,
            PRIMARY KEY (job_name, run_date)
        )
        """,
        """
        CREATE INDEX idx_komari_memory_jobs_lease
        ON komari_memory_jobs (job_name, lease_until)
        WHERE stage <> 'completed'
        """,
        """
        CREATE TABLE komari_memory_user_profile (
            user_id VARCHAR(64) NOT NULL,
            group_id VARCHAR(64) NOT NULL,
            version INT NOT NULL DEFAULT 1 CHECK (version >= 1),
            display_name TEXT NOT NULL,
            traits JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            importance INT DEFAULT 4 CHECK (importance BETWEEN 1 AND 5),
            access_count INT DEFAULT 0 CHECK (access_count >= 0),
            last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_komari_memory_user_profile_traits_object
                CHECK (jsonb_typeof(traits) = 'object'),
            PRIMARY KEY (user_id, group_id)
        )
        """,
        """
        CREATE INDEX idx_komari_memory_user_profile_group
        ON komari_memory_user_profile(group_id)
        """,
        """
        CREATE INDEX idx_komari_memory_user_profile_importance
        ON komari_memory_user_profile(importance DESC)
        """,
        """
        CREATE INDEX idx_komari_memory_user_profile_display_name
        ON komari_memory_user_profile(display_name)
        """,
        """
        CREATE TABLE komari_memory_interaction_history (
            id SERIAL PRIMARY KEY,
            user_id VARCHAR(64) NOT NULL,
            display_name TEXT NOT NULL,
            event_summary TEXT NOT NULL,
            source_dedup_key VARCHAR(64),
            source_message_count INT NOT NULL DEFAULT 0,
            first_seen_at TIMESTAMPTZ NOT NULL,
            last_seen_at TIMESTAMPTZ NOT NULL,
            importance INT DEFAULT 4 CHECK (importance BETWEEN 1 AND 5),
            importance_initial INT DEFAULT 4
                CHECK (importance_initial BETWEEN 1 AND 5),
            importance_current INT DEFAULT 4
                CHECK (importance_current BETWEEN 0 AND 5),
            last_accessed TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            is_fuzzy BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE INDEX idx_komari_memory_interaction_user
        ON komari_memory_interaction_history(user_id)
        """,
        """
        CREATE INDEX idx_komari_memory_interaction_user_time
        ON komari_memory_interaction_history(user_id, last_seen_at DESC)
        """,
        """
        CREATE INDEX idx_komari_memory_interaction_importance
        ON komari_memory_interaction_history(importance_current DESC)
        """,
        """
        CREATE UNIQUE INDEX idx_komari_memory_interaction_source_dedup
        ON komari_memory_interaction_history(source_dedup_key)
        WHERE source_dedup_key IS NOT NULL
        """,
        f"""
        CREATE TABLE komari_memory_interaction_embeddings (
            id BIGSERIAL PRIMARY KEY,
            interaction_id INT NOT NULL
                REFERENCES komari_memory_interaction_history(id) ON DELETE CASCADE,
            content_hash TEXT NOT NULL,
            embedding VECTOR({dimension}) NOT NULL,
            embedding_dim INT NOT NULL,
            embedded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (interaction_id)
        )
        """,
        """
        CREATE INDEX idx_komari_memory_conv_embedding_conversation_id
        ON komari_memory_conversation_embeddings(conversation_id)
        """,
        """
        CREATE INDEX idx_komari_memory_conv_embedding_content_hash
        ON komari_memory_conversation_embeddings(content_hash)
        """,
        """
        CREATE INDEX idx_komari_memory_interaction_embedding_interaction_id
        ON komari_memory_interaction_embeddings(interaction_id)
        """,
        """
        CREATE INDEX idx_komari_memory_interaction_embedding_content_hash
        ON komari_memory_interaction_embeddings(content_hash)
        """,
    ]
    if create_hnsw:
        statements.extend(
            (
                """
                CREATE INDEX idx_komari_memory_conv_embedding_vector
                ON komari_memory_conversation_embeddings
                USING hnsw (embedding vector_cosine_ops)
                WITH (m = 16, ef_construction = 64)
                """,
                """
                CREATE INDEX idx_komari_memory_interaction_embedding_vector
                ON komari_memory_interaction_embeddings
                USING hnsw (embedding vector_cosine_ops)
                WITH (m = 16, ef_construction = 64)
                """,
            )
        )
    return tuple(statements)


def _knowledge_statements(dimension: int, *, create_hnsw: bool) -> tuple[str, ...]:
    """RAG 知识库表、更新时间触发器与关键词索引版本戳。"""
    statements: list[str] = [
        f"""
        CREATE TABLE komari_knowledge (
            id SERIAL PRIMARY KEY,
            category VARCHAR(50) DEFAULT 'general',
            keywords TEXT[],
            content TEXT NOT NULL,
            embedding VECTOR({dimension}),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT,
            source_key TEXT
        )
        """,
    ]
    if create_hnsw:
        statements.append(
            """
            CREATE INDEX idx_komari_knowledge_embedding
            ON komari_knowledge
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            """
        )
    statements.extend(
        (
            """
            CREATE UNIQUE INDEX idx_komari_knowledge_source_key
            ON komari_knowledge(source_key)
            WHERE source_key IS NOT NULL
            """,
            # GIN 关键词索引
            """
            CREATE INDEX idx_komari_knowledge_keywords
            ON komari_knowledge
            USING gin (keywords)
            """,
            """
            CREATE INDEX idx_komari_knowledge_category
            ON komari_knowledge(category)
            """,
            """
            CREATE INDEX idx_komari_knowledge_created_at
            ON komari_knowledge(created_at DESC)
            """,
            *_search_index_version_statements(
                table_name="komari_knowledge",
                index_name="komari_knowledge",
            ),
        )
    )
    return tuple(statements)


def _help_statements(dimension: int, *, create_hnsw: bool) -> tuple[str, ...]:
    """智能帮助表、扫描租约表与关键词索引版本戳。"""
    statements: list[str] = [
        f"""
        CREATE TABLE komari_help (
            id SERIAL PRIMARY KEY,
            category TEXT NOT NULL DEFAULT 'other',
            plugin_name TEXT,
            keywords TEXT[] NOT NULL DEFAULT '{{}}',
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            notes TEXT,
            is_auto_generated BOOLEAN NOT NULL DEFAULT FALSE,
            embedding VECTOR({dimension}),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE komari_help_scan_leases (
            lease_name TEXT PRIMARY KEY,
            owner_token TEXT NOT NULL,
            lease_expires_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        # 每个插件仅保留一条自动生成帮助条目
        """
        CREATE UNIQUE INDEX uq_komari_help_auto_plugin
        ON komari_help(plugin_name)
        WHERE is_auto_generated = TRUE
          AND plugin_name IS NOT NULL
        """,
    ]
    if create_hnsw:
        statements.append(
            """
            CREATE INDEX idx_komari_help_embedding
            ON komari_help
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            """
        )
    statements.extend(
        (
            """
            CREATE INDEX idx_komari_help_keywords
            ON komari_help
            USING gin (keywords)
            """,
            """
            CREATE INDEX idx_komari_help_category
            ON komari_help(category)
            """,
            """
            CREATE INDEX idx_komari_help_plugin_name
            ON komari_help(plugin_name)
            """,
            """
            CREATE INDEX idx_komari_help_created_at
            ON komari_help(created_at DESC)
            """,
            *_search_index_version_statements(
                table_name="komari_help",
                index_name="komari_help",
            ),
            # 更新时间自动维护函数（knowledge 与 help 共用）
            """
            CREATE FUNCTION update_updated_at_column()
            RETURNS TRIGGER AS $$
            BEGIN
                NEW.updated_at = CURRENT_TIMESTAMP;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """,
            """
            CREATE TRIGGER trigger_komari_knowledge_updated_at
            BEFORE UPDATE ON komari_knowledge
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column()
            """,
            """
            CREATE TRIGGER trigger_komari_help_updated_at
            BEFORE UPDATE ON komari_help
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column()
            """,
        )
    )
    return tuple(statements)


def _search_index_version_statements(
    *,
    table_name: str,
    index_name: str,
) -> tuple[str, ...]:
    """关键词索引版本戳表、种子行、语句级版本递增触发器。

    版本戳表与递增函数由 knowledge 首次调用创建，help 调用只追加
    自己的种子行与语句级触发器。
    """
    statements: list[str] = []
    if table_name == "komari_knowledge":
        statements.append(
            """
            CREATE TABLE komari_search_index_versions (
                index_name TEXT PRIMARY KEY,
                version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    # 版本戳种子行：初始版本为 0，worker 轮询到变化后重建关键词快照
    statements.append(
        f"""
        INSERT INTO komari_search_index_versions (index_name, version)
        VALUES ('{index_name}', 0)
        """
    )
    if table_name == "komari_knowledge":
        # 业务写入事务内递增版本号，供其他 worker 轮询重建快照
        statements.append(
            """
            CREATE FUNCTION bump_komari_search_index_version()
            RETURNS TRIGGER AS $$
            BEGIN
                INSERT INTO komari_search_index_versions (
                    index_name,
                    version,
                    updated_at
                )
                VALUES (TG_ARGV[0], 1, CURRENT_TIMESTAMP)
                ON CONFLICT (index_name) DO UPDATE
                SET version = komari_search_index_versions.version + 1,
                    updated_at = CURRENT_TIMESTAMP;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    statements.append(
        f"""
        CREATE TRIGGER trigger_{table_name}_index_version
        AFTER INSERT OR UPDATE OR DELETE ON {table_name}
        FOR EACH STATEMENT
        EXECUTE FUNCTION bump_komari_search_index_version('{index_name}')
        """
    )
    return tuple(statements)


def _reply_commit_statements() -> tuple[str, ...]:
    """聊天回复送达后副作用 outbox。"""
    return (
        """
        CREATE TABLE komari_chat_reply_commit_outbox (
            operation_id TEXT PRIMARY KEY,
            payload_hash TEXT NOT NULL,
            request_trace_id TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            platform_message_id TEXT,
            group_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            user_nickname TEXT,
            bot_nickname TEXT,
            reply_content TEXT,
            reply_timestamp DOUBLE PRECISION NOT NULL,
            favorability_delta INT NOT NULL,
            favorability_reason TEXT,
            interaction_history JSONB,
            proactive_reservation_id TEXT,
            proactive_cooldown_seconds INT CHECK (
                proactive_cooldown_seconds >= 0
            ),
            global_interaction_enabled BOOLEAN NOT NULL,
            global_interaction_trigger_size INT NOT NULL CHECK (
                global_interaction_trigger_size > 0
            ),
            status TEXT NOT NULL CHECK (
                status IN (
                    'PREPARED', 'DELIVERED', 'PROCESSING',
                    'COMPLETED', 'CANCELLED', 'FAILED'
                )
            ),
            proactive_confirmed_at TIMESTAMPTZ,
            favorability_applied_at TIMESTAMPTZ,
            ai_history_stored_at TIMESTAMPTZ,
            interaction_stored_at TIMESTAMPTZ,
            attempt_count INT NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            next_retry_at TIMESTAMPTZ,
            lease_owner TEXT,
            lease_expires_at TIMESTAMPTZ,
            last_error_code TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            delivered_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        """
        CREATE INDEX idx_komari_chat_reply_commit_claim
        ON komari_chat_reply_commit_outbox(
            status, next_retry_at, lease_expires_at, created_at
        )
        """,
        """
        CREATE INDEX idx_komari_chat_reply_commit_cleanup
        ON komari_chat_reply_commit_outbox(status, completed_at)
        """,
    )


def _agent_run_log_statements() -> tuple[str, ...]:
    """Agent Run 日志轻索引：UNLOGGED 表 + GIN 数组索引。"""
    return (
        """
        CREATE UNLOGGED TABLE komari_agent_run_log_index (
            run_id TEXT PRIMARY KEY,
            trace_id TEXT NOT NULL,
            run_type TEXT NOT NULL,
            task_kind TEXT NOT NULL,
            origin TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TIMESTAMPTZ NOT NULL,
            finished_at TIMESTAMPTZ NOT NULL,
            log_date DATE NOT NULL,
            file_name TEXT NOT NULL,
            byte_offset BIGINT NOT NULL CHECK (byte_offset >= 0),
            byte_length BIGINT NOT NULL CHECK (byte_length > 0),
            models TEXT[] NOT NULL DEFAULT '{}',
            methods TEXT[] NOT NULL DEFAULT '{}',
            round_count INTEGER NOT NULL DEFAULT 0,
            tool_count INTEGER NOT NULL DEFAULT 0,
            input_tokens BIGINT NOT NULL DEFAULT 0,
            cached_input_tokens BIGINT NOT NULL DEFAULT 0,
            cache_miss_input_tokens BIGINT NOT NULL DEFAULT 0,
            output_tokens BIGINT NOT NULL DEFAULT 0,
            reasoning_output_tokens BIGINT NOT NULL DEFAULT 0,
            total_tokens BIGINT NOT NULL DEFAULT 0,
            usage_complete BOOLEAN NOT NULL DEFAULT FALSE
        )
        """,
        """
        CREATE INDEX idx_agent_run_log_started_at
        ON komari_agent_run_log_index (started_at DESC, run_id DESC)
        """,
        """
        CREATE INDEX idx_agent_run_log_date
        ON komari_agent_run_log_index (log_date DESC, started_at DESC)
        """,
        """
        CREATE INDEX idx_agent_run_log_trace_id
        ON komari_agent_run_log_index (trace_id)
        """,
        """
        CREATE INDEX idx_agent_run_log_run_type
        ON komari_agent_run_log_index (run_type, started_at DESC)
        """,
        """
        CREATE INDEX idx_agent_run_log_task_kind
        ON komari_agent_run_log_index (task_kind, started_at DESC)
        """,
        """
        CREATE INDEX idx_agent_run_log_status
        ON komari_agent_run_log_index (status, started_at DESC)
        """,
        """
        CREATE INDEX idx_agent_run_log_origin
        ON komari_agent_run_log_index (origin, started_at DESC)
        """,
        """
        CREATE INDEX idx_agent_run_log_models
        ON komari_agent_run_log_index USING GIN (models)
        """,
        """
        CREATE INDEX idx_agent_run_log_methods
        ON komari_agent_run_log_index USING GIN (methods)
        """,
    )


# 强类型配置表（14 张；komari_chat_config 由 0003 单独建表）。
_TABLE_STATEMENTS: tuple[str, ...] = (
    """

    CREATE TABLE komari_agent_run_logger_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        log_enabled BOOLEAN NOT NULL,
        retention_days INTEGER NOT NULL,
        PRIMARY KEY (id)
    )

    """,
    """

    CREATE TABLE komari_embedding_provider_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        embedding_model VARCHAR NOT NULL,
        embedding_api_url VARCHAR NOT NULL,
        embedding_api_key VARCHAR NOT NULL,
        embedding_dimension INTEGER NOT NULL,
        request_connect_timeout_seconds FLOAT NOT NULL,
        request_read_timeout_seconds FLOAT NOT NULL,
        request_total_timeout_seconds FLOAT NOT NULL,
        request_retry_attempts INTEGER NOT NULL,
        request_retry_backoff_seconds FLOAT NOT NULL,
        response_max_bytes INTEGER NOT NULL,
        rerank_enabled BOOLEAN NOT NULL,
        rerank_model VARCHAR NOT NULL,
        rerank_api_url VARCHAR NOT NULL,
        rerank_api_key VARCHAR NOT NULL,
        rerank_top_n INTEGER NOT NULL,
        PRIMARY KEY (id)
    )

    """,
    """

    CREATE TABLE komari_group_history_summary_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        plugin_enable BOOLEAN NOT NULL,
        redis_db INTEGER NOT NULL,
        summary_lock_ttl_seconds INTEGER NOT NULL,
        history_min_coverage_ratio FLOAT NOT NULL,
        min_summary_count INTEGER NOT NULL,
        max_summary_count INTEGER NOT NULL,
        fetch_batch_size INTEGER NOT NULL,
        summary_default_count INTEGER NOT NULL,
        summary_planning_model VARCHAR NOT NULL,
        summary_planning_max_tokens INTEGER NOT NULL,
        summary_planning_round_limit INTEGER NOT NULL,
        summary_planning_request_api VARCHAR(32) NOT NULL,
        summary_planning_stream_enabled BOOLEAN NOT NULL,
        summary_planning_thinking_mode BOOLEAN NOT NULL,
        summary_planning_reasoning_effort VARCHAR NOT NULL,
        summary_tool_scan_limit INTEGER NOT NULL,
        summary_model VARCHAR NOT NULL,
        summary_temperature FLOAT NOT NULL,
        summary_max_tokens INTEGER NOT NULL,
        summary_request_api VARCHAR(32) NOT NULL,
        summary_stream_enabled BOOLEAN NOT NULL,
        summary_thinking_mode BOOLEAN NOT NULL,
        summary_reasoning_effort VARCHAR NOT NULL,
        assistant_prefill_enabled BOOLEAN NOT NULL,
        dsv4_roleplay_instruct_mode VARCHAR NOT NULL,
        layout_params JSONB NOT NULL,
        PRIMARY KEY (id)
    )

    """,
    """

    CREATE TABLE komari_custom_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        plugin_enable BOOLEAN NOT NULL,
        required_votes INTEGER NOT NULL,
        vote_emoji_id VARCHAR NOT NULL,
        proposal_expire_hours INTEGER NOT NULL,
        list_chunk_size INTEGER NOT NULL,
        max_proposals_per_user INTEGER NOT NULL,
        redis_db INTEGER NOT NULL,
        PRIMARY KEY (id)
    )

    """,
    """

    CREATE TABLE komari_decision_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        plugin_enable BOOLEAN NOT NULL,
        filter_min_length INTEGER NOT NULL,
        filter_history_check_size INTEGER NOT NULL,
        message_buffer_size INTEGER NOT NULL,
        bot_aliases JSONB NOT NULL,
        scene_top_k INTEGER NOT NULL,
        reply_threshold FLOAT NOT NULL,
        timing_weight FLOAT NOT NULL,
        noise_conf_threshold FLOAT NOT NULL,
        noise_margin_threshold FLOAT NOT NULL,
        call_margin_threshold FLOAT NOT NULL,
        social_window_activity_seconds INTEGER NOT NULL,
        social_window_dialogue_seconds INTEGER NOT NULL,
        social_silence_seconds INTEGER NOT NULL,
        social_bot_cooldown_seconds INTEGER NOT NULL,
        social_timing_mention_bonus FLOAT NOT NULL,
        social_timing_silence_bonus FLOAT NOT NULL,
        social_timing_activity_max_penalty FLOAT NOT NULL,
        social_timing_dialogue_penalty FLOAT NOT NULL,
        social_timing_cooldown_max_penalty FLOAT NOT NULL,
        social_timing_activity_threshold INTEGER NOT NULL,
        social_timing_activity_slope_denominator INTEGER NOT NULL,
        embedding_instruction_query VARCHAR NOT NULL,
        embedding_instruction_scene VARCHAR NOT NULL,
        rerank_instruction VARCHAR NOT NULL,
        scene_persist_enabled BOOLEAN NOT NULL,
        scene_sync_poll_seconds INTEGER NOT NULL,
        scene_embedding_lease_seconds INTEGER NOT NULL,
        scene_embedding_max_attempts INTEGER NOT NULL,
        scene_embedding_retry_base_seconds INTEGER NOT NULL,
        scene_keep_versions INTEGER NOT NULL,
        PRIMARY KEY (id)
    )

    """,
    """

    CREATE TABLE komari_help_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        plugin_enable BOOLEAN NOT NULL,
        similarity_threshold FLOAT NOT NULL,
        layer1_limit INTEGER NOT NULL,
        layer2_limit INTEGER NOT NULL,
        total_limit INTEGER NOT NULL,
        default_result_limit INTEGER NOT NULL,
        max_reply_result_count INTEGER NOT NULL,
        query_rewrite_rules JSONB NOT NULL,
        auto_scan_on_startup BOOLEAN NOT NULL,
        disabled_auto_help_plugins JSONB NOT NULL,
        show_category_emoji BOOLEAN NOT NULL,
        max_content_preview_length INTEGER NOT NULL,
        PRIMARY KEY (id)
    )

    """,
    """

    CREATE TABLE komari_knowledge_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        plugin_enable BOOLEAN NOT NULL,
        similarity_threshold FLOAT NOT NULL,
        query_rewrite_rules JSONB NOT NULL,
        layer1_limit INTEGER NOT NULL,
        layer2_limit INTEGER NOT NULL,
        total_limit INTEGER NOT NULL,
        PRIMARY KEY (id)
    )

    """,
    """

    CREATE TABLE komari_management_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        plugin_enable BOOLEAN NOT NULL,
        api_credentials JSONB NOT NULL,
        api_allowed_origins JSONB NOT NULL,
        announce_status_page_url VARCHAR NOT NULL,
        announce_max_group_count INTEGER NOT NULL,
        announce_send_interval_seconds FLOAT NOT NULL,
        announce_request_cooldown_seconds FLOAT NOT NULL,
        PRIMARY KEY (id)
    )

    """,
    """

    CREATE TABLE komari_memory_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        plugin_enable BOOLEAN NOT NULL,
        redis_db INTEGER NOT NULL,
        llm_model_chat VARCHAR NOT NULL,
        llm_temperature_chat FLOAT NOT NULL,
        llm_max_tokens_chat INTEGER NOT NULL,
        llm_request_api_chat VARCHAR(32) NOT NULL,
        llm_stream_enabled_chat BOOLEAN NOT NULL,
        llm_thinking_mode_chat BOOLEAN NOT NULL,
        llm_reasoning_effort_chat VARCHAR NOT NULL,
        assistant_prefill_enabled BOOLEAN NOT NULL,
        dsv4_roleplay_instruct_mode VARCHAR NOT NULL,
        vision_tool_enabled BOOLEAN NOT NULL,
        vision_image_download_max_count INTEGER NOT NULL,
        vision_image_download_max_bytes INTEGER NOT NULL,
        vision_image_download_total_max_bytes INTEGER NOT NULL,
        vision_image_download_max_pixels INTEGER NOT NULL,
        vision_image_download_concurrency INTEGER NOT NULL,
        vision_image_download_connect_timeout_seconds FLOAT NOT NULL,
        vision_image_download_read_timeout_seconds FLOAT NOT NULL,
        vision_image_download_total_timeout_seconds FLOAT NOT NULL,
        llm_model_summary VARCHAR NOT NULL,
        llm_temperature_summary FLOAT NOT NULL,
        llm_max_tokens_summary INTEGER NOT NULL,
        llm_request_api_summary VARCHAR(32) NOT NULL,
        llm_stream_enabled_summary BOOLEAN NOT NULL,
        llm_thinking_mode_summary BOOLEAN NOT NULL,
        llm_reasoning_effort_summary VARCHAR NOT NULL,
        knowledge_enabled BOOLEAN NOT NULL,
        knowledge_limit INTEGER NOT NULL,
        summary_idle_timeout INTEGER NOT NULL,
        summary_min_messages INTEGER NOT NULL,
        summary_max_buffer_size INTEGER NOT NULL,
        conversation_snapshot_ttl_seconds INTEGER NOT NULL,
        conversation_processing_lease_seconds INTEGER NOT NULL,
        profile_snapshot_ttl_seconds INTEGER NOT NULL,
        profile_snapshot_enable BOOLEAN NOT NULL,
        profile_trait_limit INTEGER NOT NULL,
        memory_agent_max_rounds INTEGER NOT NULL,
        memory_agent_staging_ttl_seconds INTEGER NOT NULL,
        memory_agent_max_tool_calls INTEGER NOT NULL,
        memory_agent_max_read_profiles INTEGER NOT NULL,
        memory_agent_max_write_operations INTEGER NOT NULL,
        memory_agent_lock_timeout_seconds INTEGER,
        memory_search_limit INTEGER NOT NULL,
        context_messages_limit INTEGER NOT NULL,
        context_max_utf8_bytes INTEGER NOT NULL,
        context_max_estimated_tokens INTEGER NOT NULL,
        global_interaction_enabled BOOLEAN NOT NULL,
        global_interaction_trigger_size INTEGER NOT NULL,
        global_interaction_summary_interval_minutes INTEGER NOT NULL,
        global_interaction_processing_lease_seconds INTEGER NOT NULL,
        proactive_enabled BOOLEAN NOT NULL,
        proactive_score_threshold FLOAT NOT NULL,
        proactive_cooldown INTEGER NOT NULL,
        proactive_max_per_hour INTEGER NOT NULL,
        proactive_reservation_ttl_seconds INTEGER NOT NULL,
        reply_commit_worker_interval_seconds INTEGER NOT NULL,
        reply_commit_batch_size INTEGER NOT NULL,
        reply_commit_lease_seconds INTEGER NOT NULL,
        reply_commit_max_attempts INTEGER NOT NULL,
        reply_commit_retry_base_seconds INTEGER NOT NULL,
        reply_commit_tombstone_retention_days INTEGER NOT NULL,
        bot_nickname VARCHAR NOT NULL,
        response_tag VARCHAR NOT NULL,
        forgetting_enabled BOOLEAN NOT NULL,
        forgetting_importance_threshold INTEGER NOT NULL,
        forgetting_decay_factor FLOAT NOT NULL,
        forgetting_access_boost FLOAT NOT NULL,
        forgetting_min_age_days INTEGER NOT NULL,
        forgetting_fuzzify_concurrency INTEGER NOT NULL,
        forgetting_job_lease_seconds INTEGER NOT NULL,
        query_rewrite_history_limit INTEGER NOT NULL,
        bot_aliases JSONB NOT NULL,
        face_reaction_enabled BOOLEAN NOT NULL,
        face_reaction_id VARCHAR NOT NULL,
        error_notify_enabled BOOLEAN NOT NULL,
        PRIMARY KEY (id)
    )

    """,
    """

    CREATE TABLE komari_search_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        plugin_enable BOOLEAN NOT NULL,
        search_provider VARCHAR(16) NOT NULL,
        search_api_key VARCHAR NOT NULL,
        search_enabled BOOLEAN NOT NULL,
        max_results INTEGER NOT NULL,
        result_content_limit INTEGER NOT NULL,
        search_timeout_seconds FLOAT NOT NULL,
        fetch_enabled BOOLEAN NOT NULL,
        fetch_max_urls INTEGER NOT NULL,
        fetch_content_limit INTEGER NOT NULL,
        fetch_timeout_seconds FLOAT NOT NULL,
        tavily_search_depth VARCHAR(16) NOT NULL,
        tavily_include_answer BOOLEAN NOT NULL,
        exa_search_type VARCHAR(16) NOT NULL,
        exa_fetch_format VARCHAR(16) NOT NULL,
        circuit_breaker_failure_threshold INTEGER NOT NULL,
        circuit_breaker_recovery_seconds FLOAT NOT NULL,
        PRIMARY KEY (id)
    )

    """,
    """

    CREATE TABLE komari_sentry_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        plugin_enable BOOLEAN NOT NULL,
        dsn VARCHAR NOT NULL,
        environment VARCHAR NOT NULL,
        release VARCHAR NOT NULL,
        debug BOOLEAN NOT NULL,
        error_sample_rate FLOAT NOT NULL,
        traces_sample_rate FLOAT NOT NULL,
        profiles_sample_rate FLOAT NOT NULL,
        attach_stacktrace BOOLEAN NOT NULL,
        send_default_pii BOOLEAN NOT NULL,
        max_breadcrumbs INTEGER NOT NULL,
        shutdown_timeout FLOAT NOT NULL,
        breadcrumb_level VARCHAR NOT NULL,
        sentry_logs_level VARCHAR NOT NULL,
        event_level VARCHAR NOT NULL,
        PRIMARY KEY (id)
    )

    """,
    """

    CREATE TABLE komari_llm_provider_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        api_token VARCHAR NOT NULL,
        api_base VARCHAR NOT NULL,
        model VARCHAR NOT NULL,
        request_api VARCHAR(32) NOT NULL,
        stream_enabled BOOLEAN NOT NULL,
        temperature FLOAT NOT NULL,
        max_tokens INTEGER NOT NULL,
        timeout_seconds FLOAT NOT NULL,
        summary_task_rpm_limit INTEGER NOT NULL,
        chat_rpm_limit INTEGER NOT NULL,
        frequency_penalty FLOAT NOT NULL,
        extra_params JSONB NOT NULL,
        vision_model VARCHAR NOT NULL,
        vision_request_api VARCHAR(32) NOT NULL,
        vision_stream_enabled BOOLEAN NOT NULL,
        vision_temperature FLOAT NOT NULL,
        vision_max_tokens INTEGER NOT NULL,
        vision_thinking_mode BOOLEAN NOT NULL,
        vision_reasoning_effort VARCHAR NOT NULL,
        PRIMARY KEY (id)
    )

    """,
    """

    CREATE TABLE komari_sr_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        plugin_enable BOOLEAN NOT NULL,
        sr_list JSONB NOT NULL,
        list_chunk_size INTEGER NOT NULL,
        redis_db INTEGER NOT NULL,
        PRIMARY KEY (id)
    )

    """,
    """

    CREATE TABLE komari_user_data_config (
        id INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
        plugin_enable BOOLEAN NOT NULL,
        initial_favorability INTEGER NOT NULL,
        max_favorability_delta_per_reply INTEGER NOT NULL,
        PRIMARY KEY (id)
    )

    """,
)

# Prompt 强类型表（3 张）。
_PROMPT_TABLE_STATEMENTS: tuple[str, ...] = (
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


def _typed_and_prompt_statements() -> tuple[str, ...]:
    return (*_TABLE_STATEMENTS, *_PROMPT_TABLE_STATEMENTS)

