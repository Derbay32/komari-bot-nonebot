"""Dynamic PostgreSQL schema bootstrap helpers for embedding-backed plugins."""

from __future__ import annotations

from typing import Any

PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS = 2000


def build_memory_schema_statements(embedding_dimension: int) -> tuple[str, ...]:
    """Build Komari Memory storage schema statements for a specific dimension."""
    dimension = _normalize_dimension(embedding_dimension)
    return (
        "CREATE EXTENSION IF NOT EXISTS vector",
        """
        CREATE TABLE IF NOT EXISTS komari_memory_conversations (
            id SERIAL PRIMARY KEY,
            group_id VARCHAR(64) NOT NULL,
            summary TEXT NOT NULL,
            participants TEXT[],
            dedup_key VARCHAR(64),
            start_time TIMESTAMP NOT NULL,
            end_time TIMESTAMP NOT NULL,
            importance INT DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
            importance_initial INT DEFAULT 3 CHECK (importance_initial BETWEEN 1 AND 5),
            importance_current INT DEFAULT 3 CHECK (importance_current BETWEEN 0 AND 5),
            last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_fuzzy BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS komari_memory_conversation_embeddings (
            id BIGSERIAL PRIMARY KEY,
            conversation_id INT NOT NULL REFERENCES komari_memory_conversations(id) ON DELETE CASCADE,
            content_hash TEXT NOT NULL,
            embedding VECTOR({dimension}) NOT NULL,
            embedding_dim INT NOT NULL,
            embedded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (conversation_id)
        )
        """,
        """
        ALTER TABLE komari_memory_conversations
        ADD COLUMN IF NOT EXISTS dedup_key VARCHAR(64)
        """,
        """
        ALTER TABLE komari_memory_conversations
        DROP CONSTRAINT IF EXISTS komari_memory_conversations_importance_current_check
        """,
        """
        ALTER TABLE komari_memory_conversations
        ALTER COLUMN importance_current TYPE INTEGER
        USING LEAST(
            COALESCE(importance_initial, 5),
            GREATEST(
                0,
                LEAST(
                    5,
                    FLOOR(COALESCE(importance_current, importance_initial, 0))
                )::INTEGER
            )
        )
        """,
        """
        ALTER TABLE komari_memory_conversations
        ALTER COLUMN importance_current SET DEFAULT 3
        """,
        """
        ALTER TABLE komari_memory_conversations
        ADD CONSTRAINT komari_memory_conversations_importance_current_check
        CHECK (importance_current BETWEEN 0 AND 5)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_komari_memory_conv_group
        ON komari_memory_conversations(group_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_komari_memory_conv_time
        ON komari_memory_conversations(start_time DESC)
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_komari_memory_conv_dedup_key
        ON komari_memory_conversations (dedup_key)
        WHERE dedup_key IS NOT NULL
        """,
        """
        CREATE TABLE IF NOT EXISTS komari_memory_user_profile (
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
        CREATE INDEX IF NOT EXISTS idx_komari_memory_user_profile_group
        ON komari_memory_user_profile(group_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_komari_memory_user_profile_importance
        ON komari_memory_user_profile(importance DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_komari_memory_user_profile_display_name
        ON komari_memory_user_profile(display_name)
        """,
        _build_legacy_interaction_history_migration_statement(),
        """
        CREATE TABLE IF NOT EXISTS komari_memory_interaction_history (
            id SERIAL PRIMARY KEY,
            user_id VARCHAR(64) NOT NULL,
            display_name TEXT NOT NULL,
            event_summary TEXT NOT NULL,
            source_message_count INT NOT NULL DEFAULT 0,
            first_seen_at TIMESTAMPTZ NOT NULL,
            last_seen_at TIMESTAMPTZ NOT NULL,
            importance INT DEFAULT 4 CHECK (importance BETWEEN 1 AND 5),
            importance_initial INT DEFAULT 4 CHECK (importance_initial BETWEEN 1 AND 5),
            importance_current INT DEFAULT 4 CHECK (importance_current BETWEEN 0 AND 5),
            last_accessed TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            is_fuzzy BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        ALTER TABLE komari_memory_interaction_history
        ADD COLUMN IF NOT EXISTS display_name TEXT NOT NULL DEFAULT ''
        """,
        """
        ALTER TABLE komari_memory_interaction_history
        ADD COLUMN IF NOT EXISTS event_summary TEXT NOT NULL DEFAULT ''
        """,
        """
        ALTER TABLE komari_memory_interaction_history
        ADD COLUMN IF NOT EXISTS source_message_count INT NOT NULL DEFAULT 0
        """,
        """
        ALTER TABLE komari_memory_interaction_history
        ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        """,
        """
        ALTER TABLE komari_memory_interaction_history
        ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        """,
        """
        ALTER TABLE komari_memory_interaction_history
        ADD COLUMN IF NOT EXISTS importance INT DEFAULT 4 CHECK (importance BETWEEN 1 AND 5)
        """,
        """
        ALTER TABLE komari_memory_interaction_history
        ADD COLUMN IF NOT EXISTS importance_initial INT DEFAULT 4 CHECK (importance_initial BETWEEN 1 AND 5)
        """,
        """
        ALTER TABLE komari_memory_interaction_history
        ADD COLUMN IF NOT EXISTS importance_current INT DEFAULT 4 CHECK (importance_current BETWEEN 0 AND 5)
        """,
        """
        ALTER TABLE komari_memory_interaction_history
        ADD COLUMN IF NOT EXISTS last_accessed TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        """,
        """
        ALTER TABLE komari_memory_interaction_history
        ADD COLUMN IF NOT EXISTS is_fuzzy BOOLEAN DEFAULT FALSE
        """,
        """
        ALTER TABLE komari_memory_interaction_history
        ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_komari_memory_interaction_user
        ON komari_memory_interaction_history(user_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_komari_memory_interaction_user_time
        ON komari_memory_interaction_history(user_id, last_seen_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_komari_memory_interaction_importance
        ON komari_memory_interaction_history(importance_current DESC)
        """,
        f"""
        CREATE TABLE IF NOT EXISTS komari_memory_interaction_embeddings (
            id BIGSERIAL PRIMARY KEY,
            interaction_id INT NOT NULL REFERENCES komari_memory_interaction_history(id) ON DELETE CASCADE,
            content_hash TEXT NOT NULL,
            embedding VECTOR({dimension}) NOT NULL,
            embedding_dim INT NOT NULL,
            embedded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (interaction_id)
        )
        """,
        """
        DROP INDEX IF EXISTS idx_komari_memory_interaction_embedding
        """,
        _build_memory_embedding_column_cleanup_statement(),
        *(_build_memory_conversation_embedding_index_statements(dimension)),
        *(_build_memory_interaction_embedding_index_statements(dimension)),
    )


def _build_legacy_interaction_history_migration_statement() -> str:
    return """
        DO $$
        DECLARE
            backup_name text;
            has_old_records boolean;
            has_new_event_summary boolean;
            has_old_group_id boolean;
        BEGIN
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'komari_memory_interaction_history'
                  AND column_name = 'records'
            ) INTO has_old_records;

            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'komari_memory_interaction_history'
                  AND column_name = 'event_summary'
            ) INTO has_new_event_summary;

            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'komari_memory_interaction_history'
                  AND column_name = 'group_id'
            ) INTO has_old_group_id;

            IF to_regclass('komari_memory_interaction_history') IS NOT NULL
               AND has_old_records
               AND has_old_group_id
               AND NOT has_new_event_summary THEN
                backup_name := 'komari_memory_interaction_history_legacy_'
                    || to_char(CURRENT_TIMESTAMP, 'YYYYMMDD_HH24MISS');
                EXECUTE format(
                    'ALTER TABLE komari_memory_interaction_history RENAME TO %I',
                    backup_name
                );
                RAISE NOTICE '旧互动历史表已重命名为 %', backup_name;
            END IF;
        END
        $$;
        """


def _build_memory_embedding_column_cleanup_statement() -> str:
    return """
        DO $$
        BEGIN
            IF to_regclass('komari_memory_conversation_embeddings') IS NOT NULL
               AND to_regclass('komari_memory_interaction_embeddings') IS NOT NULL THEN
                ALTER TABLE komari_memory_conversations DROP COLUMN IF EXISTS embedding;
                ALTER TABLE komari_memory_interaction_history DROP COLUMN IF EXISTS embedding;
            END IF;
        END
        $$;
        """


def _build_memory_conversation_embedding_index_statements(dimension: int) -> tuple[str, ...]:
    statements = [
        """
        CREATE INDEX IF NOT EXISTS idx_komari_memory_conv_embedding_conversation_id
        ON komari_memory_conversation_embeddings(conversation_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_komari_memory_conv_embedding_content_hash
        ON komari_memory_conversation_embeddings(content_hash)
        """,
    ]
    if dimension <= PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS:
        statements.append(
            """
        CREATE INDEX IF NOT EXISTS idx_komari_memory_conv_embedding_vector
        ON komari_memory_conversation_embeddings
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """
        )
    return tuple(statements)


def _build_memory_interaction_embedding_index_statements(dimension: int) -> tuple[str, ...]:
    statements = [
        """
        CREATE INDEX IF NOT EXISTS idx_komari_memory_interaction_embedding_interaction_id
        ON komari_memory_interaction_embeddings(interaction_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_komari_memory_interaction_embedding_content_hash
        ON komari_memory_interaction_embeddings(content_hash)
        """,
    ]
    if dimension <= PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS:
        statements.append(
            """
        CREATE INDEX IF NOT EXISTS idx_komari_memory_interaction_embedding_vector
        ON komari_memory_interaction_embeddings
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """
        )
    return tuple(statements)


def build_knowledge_schema_statements(embedding_dimension: int) -> tuple[str, ...]:
    """Build Komari Knowledge storage schema statements for a specific dimension."""
    dimension = _normalize_dimension(embedding_dimension)
    statements = [
        "CREATE EXTENSION IF NOT EXISTS vector",
        f"""
        CREATE TABLE IF NOT EXISTS komari_knowledge (
            id SERIAL PRIMARY KEY,
            category VARCHAR(50) DEFAULT 'general',
            keywords TEXT[],
            content TEXT NOT NULL,
            embedding VECTOR({dimension}),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT
        )
        """,
    ]
    embedding_index_statement = build_knowledge_embedding_index_statement(dimension)
    if embedding_index_statement is not None:
        statements.append(embedding_index_statement)
    statements.extend(
        [
            """
        CREATE INDEX IF NOT EXISTS idx_komari_knowledge_keywords
        ON komari_knowledge
        USING gin (keywords)
        """,
            """
        CREATE INDEX IF NOT EXISTS idx_komari_knowledge_category
        ON komari_knowledge(category)
        """,
            """
        CREATE INDEX IF NOT EXISTS idx_komari_knowledge_created_at
        ON komari_knowledge(created_at DESC)
        """,
            """
        CREATE OR REPLACE FUNCTION update_updated_at_column()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = CURRENT_TIMESTAMP;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
            """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_trigger
                WHERE tgname = 'trigger_komari_knowledge_updated_at'
                  AND tgrelid = 'komari_knowledge'::regclass
            ) THEN
                CREATE TRIGGER trigger_komari_knowledge_updated_at
                BEFORE UPDATE ON komari_knowledge
                FOR EACH ROW
                EXECUTE FUNCTION update_updated_at_column();
            END IF;
        END
        $$;
        """,
        ]
    )
    return tuple(statements)


def build_knowledge_embedding_index_statement(
    embedding_dimension: int,
) -> str | None:
    """Return the knowledge embedding index DDL when pgvector supports it."""
    dimension = _normalize_dimension(embedding_dimension)
    if dimension > PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS:
        return None
    return """
        CREATE INDEX IF NOT EXISTS idx_komari_knowledge_embedding
        ON komari_knowledge
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """


def build_help_schema_statements(embedding_dimension: int) -> tuple[str, ...]:
    """Build Komari Help storage schema statements for a specific dimension."""
    dimension = _normalize_dimension(embedding_dimension)
    statements = [
        "CREATE EXTENSION IF NOT EXISTS vector",
        f"""
        CREATE TABLE IF NOT EXISTS komari_help (
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
    ]
    embedding_index_statement = build_help_embedding_index_statement(dimension)
    if embedding_index_statement is not None:
        statements.append(embedding_index_statement)
    statements.extend(
        [
            """
        CREATE INDEX IF NOT EXISTS idx_komari_help_keywords
        ON komari_help
        USING gin (keywords)
        """,
            """
        CREATE INDEX IF NOT EXISTS idx_komari_help_category
        ON komari_help(category)
        """,
            """
        CREATE INDEX IF NOT EXISTS idx_komari_help_plugin_name
        ON komari_help(plugin_name)
        """,
            """
        CREATE INDEX IF NOT EXISTS idx_komari_help_created_at
        ON komari_help(created_at DESC)
        """,
            """
        CREATE OR REPLACE FUNCTION update_updated_at_column()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = CURRENT_TIMESTAMP;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
            """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_trigger
                WHERE tgname = 'trigger_komari_help_updated_at'
                  AND tgrelid = 'komari_help'::regclass
            ) THEN
                CREATE TRIGGER trigger_komari_help_updated_at
                BEFORE UPDATE ON komari_help
                FOR EACH ROW
                EXECUTE FUNCTION update_updated_at_column();
            END IF;
        END
        $$;
        """,
        ]
    )
    return tuple(statements)


def build_help_embedding_index_statement(
    embedding_dimension: int,
) -> str | None:
    """Return the help embedding index DDL when pgvector supports it."""
    dimension = _normalize_dimension(embedding_dimension)
    if dimension > PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS:
        return None
    return """
        CREATE INDEX IF NOT EXISTS idx_komari_help_embedding
        ON komari_help
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """


async def apply_schema_statements(
    pg_pool: Any,
    *,
    statements: tuple[str, ...],
) -> None:
    """Execute schema bootstrap statements sequentially."""
    async with pg_pool.acquire() as conn:
        for statement in statements:
            await conn.execute(statement)


def _normalize_dimension(embedding_dimension: int) -> int:
    dimension = int(embedding_dimension)
    if dimension <= 0:
        msg = f"非法 embedding 维度: {embedding_dimension}"
        raise ValueError(msg)
    return dimension
