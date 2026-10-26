"""embedding 插件的 PostgreSQL schema 语句离线构建器。

运行时不执行任何 DDL；数据库结构由 Alembic 迁移（migrations/）统一管理。
本模块仅供离线工具渲染/对照 SQL 使用；记忆向量 DDL 已随 TSK-197 退役。
"""

from __future__ import annotations

import textwrap

PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS = 2000


def render_schema_statements(statements: tuple[str, ...]) -> str:
    """把 schema 语句规范化为可由 psql 执行的离线 SQL 文本。"""
    rendered: list[str] = []
    for statement in statements:
        normalized = textwrap.dedent(statement).strip()
        if not normalized:
            continue
        rendered.append(normalized if normalized.endswith(";") else f"{normalized};")
    return "\n\n".join(rendered) + "\n"


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
            notes TEXT,
            source_key TEXT
        )
        """,
    ]
    embedding_index_statement = build_knowledge_embedding_index_statement(dimension)
    if embedding_index_statement is not None:
        statements.append(embedding_index_statement)
    statements.extend(
        [
            """
        ALTER TABLE komari_knowledge
        ADD COLUMN IF NOT EXISTS source_key TEXT
        """,
            """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_komari_knowledge_source_key
        ON komari_knowledge(source_key)
        WHERE source_key IS NOT NULL
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
            *_build_search_index_version_statements(
                table_name="komari_knowledge",
                index_name="komari_knowledge",
            ),
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
        """
        CREATE TABLE IF NOT EXISTS komari_help_scan_leases (
            lease_name TEXT PRIMARY KEY,
            owner_token TEXT NOT NULL,
            lease_expires_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        WITH ranked_auto_help AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY plugin_name
                    ORDER BY updated_at DESC, id DESC
                ) AS duplicate_rank
            FROM komari_help
            WHERE is_auto_generated = TRUE
              AND plugin_name IS NOT NULL
        )
        DELETE FROM komari_help AS help
        USING ranked_auto_help AS ranked
        WHERE help.id = ranked.id
          AND ranked.duplicate_rank > 1
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_komari_help_auto_plugin
        ON komari_help(plugin_name)
        WHERE is_auto_generated = TRUE
          AND plugin_name IS NOT NULL
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
            *_build_search_index_version_statements(
                table_name="komari_help",
                index_name="komari_help",
            ),
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


def _build_search_index_version_statements(
    *,
    table_name: str,
    index_name: str,
) -> tuple[str, ...]:
    """为关键词索引构建事务版本戳与语句级触发器。"""
    allowed_names = {"komari_knowledge", "komari_help"}
    if table_name not in allowed_names or index_name not in allowed_names:
        msg = f"不支持的搜索索引名称: table={table_name}, index={index_name}"
        raise ValueError(msg)

    trigger_name = f"trigger_{table_name}_index_version"
    return (
        """
        CREATE TABLE IF NOT EXISTS komari_search_index_versions (
            index_name TEXT PRIMARY KEY,
            version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        f"""
        INSERT INTO komari_search_index_versions (index_name, version)
        VALUES ('{index_name}', 0)
        ON CONFLICT (index_name) DO NOTHING
        """,
        """
        CREATE OR REPLACE FUNCTION bump_komari_search_index_version()
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
        """,
        f"""
        CREATE OR REPLACE TRIGGER {trigger_name}
        AFTER INSERT OR UPDATE OR DELETE ON {table_name}
        FOR EACH STATEMENT
        EXECUTE FUNCTION bump_komari_search_index_version('{index_name}')
        """,
    )


def _normalize_dimension(embedding_dimension: int) -> int:
    dimension = int(embedding_dimension)
    if dimension <= 0:
        msg = f"非法 embedding 维度: {embedding_dimension}"
        raise ValueError(msg)
    return dimension
