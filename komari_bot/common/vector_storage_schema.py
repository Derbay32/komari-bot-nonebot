"""Dynamic PostgreSQL schema bootstrap helpers for embedding-backed plugins."""

from __future__ import annotations

from typing import Any


def build_memory_schema_statements(embedding_dimension: int) -> tuple[str, ...]:
    """Build Komari Memory storage schema statements for a specific dimension."""
    dimension = _normalize_dimension(embedding_dimension)
    return (
        "CREATE EXTENSION IF NOT EXISTS vector",
        f"""
        CREATE TABLE IF NOT EXISTS komari_memory_conversations (
            id SERIAL PRIMARY KEY,
            group_id VARCHAR(64) NOT NULL,
            summary TEXT NOT NULL,
            embedding VECTOR({dimension}),
            participants TEXT[],
            start_time TIMESTAMP NOT NULL,
            end_time TIMESTAMP NOT NULL,
            importance INT DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
            importance_initial INT DEFAULT 3 CHECK (importance_initial BETWEEN 1 AND 5),
            importance_current DOUBLE PRECISION DEFAULT 3 CHECK (importance_current BETWEEN 0 AND 5),
            last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_fuzzy BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        ALTER TABLE komari_memory_conversations
        ALTER COLUMN importance_current TYPE DOUBLE PRECISION
        USING importance_current::DOUBLE PRECISION
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
        CREATE TABLE IF NOT EXISTS komari_memory_entity (
            user_id VARCHAR(64) NOT NULL,
            group_id VARCHAR(64) NOT NULL,
            key VARCHAR(100) NOT NULL CHECK (key IN ('user_profile', 'interaction_history')),
            value TEXT NOT NULL,
            category VARCHAR(50) NOT NULL CHECK (category IN ('profile_json', 'interaction_history')),
            importance INT DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
            access_count INT DEFAULT 0,
            last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT ck_komari_memory_entity_two_row_model CHECK (
                (key = 'user_profile' AND category = 'profile_json')
                OR
                (key = 'interaction_history' AND category = 'interaction_history')
            ),
            PRIMARY KEY (user_id, group_id, key)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_komari_memory_entity_group
        ON komari_memory_entity(group_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_komari_memory_entity_importance
        ON komari_memory_entity(importance DESC)
        """,
    )


def build_knowledge_schema_statements(embedding_dimension: int) -> tuple[str, ...]:
    """Build Komari Knowledge storage schema statements for a specific dimension."""
    dimension = _normalize_dimension(embedding_dimension)
    return (
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
        """
        CREATE INDEX IF NOT EXISTS idx_komari_knowledge_embedding
        ON komari_knowledge
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """,
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
    )


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
