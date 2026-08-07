"""Dynamic storage schema bootstrap tests."""

from __future__ import annotations

import pytest

from komari_bot.db.vector_storage_schema import (
    PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS,
    build_help_embedding_index_statement,
    build_help_schema_statements,
    build_knowledge_embedding_index_statement,
    build_knowledge_schema_statements,
    build_memory_schema_statements,
    render_schema_statements,
)


def test_build_memory_schema_statements_uses_requested_dimension() -> None:
    statements = build_memory_schema_statements(1536)
    assert "VECTOR(1536)" not in statements[1]
    assert "importance_current INT DEFAULT 3" in statements[1]
    assert "dedup_key VARCHAR(64)" in statements[1]
    assert any(
        "CREATE TABLE IF NOT EXISTS komari_memory_conversation_embeddings" in statement
        and "embedding VECTOR(1536) NOT NULL" in statement
        for statement in statements
    )
    assert any(
        "CREATE TABLE IF NOT EXISTS komari_memory_interaction_embeddings" in statement
        and "embedding VECTOR(1536) NOT NULL" in statement
        for statement in statements
    )
    assert any("ADD COLUMN IF NOT EXISTS dedup_key VARCHAR(64)" in statement for statement in statements)
    assert any(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_komari_memory_conv_dedup_key" in statement
        and "WHERE dedup_key IS NOT NULL" in statement
        for statement in statements
    )
    assert any(
        "CREATE TABLE IF NOT EXISTS komari_memory_jobs" in statement
        and "PRIMARY KEY (job_name, run_date)" in statement
        and "lease_until TIMESTAMPTZ NOT NULL" in statement
        for statement in statements
    )
    assert any(
        "CREATE INDEX IF NOT EXISTS idx_komari_memory_jobs_lease" in statement
        and "WHERE stage <> 'completed'" in statement
        for statement in statements
    )
    assert any(
        "ALTER COLUMN importance_current TYPE INTEGER" in statement
        for statement in statements
    )
    assert any("komari_memory_user_profile" in statement for statement in statements)
    assert any(
        "komari_memory_interaction_history" in statement for statement in statements
    )
    assert any(
        "ADD COLUMN IF NOT EXISTS source_dedup_key VARCHAR(64)" in statement
        for statement in statements
    )
    assert any(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "idx_komari_memory_interaction_source_dedup" in statement
        and "WHERE source_dedup_key IS NOT NULL" in statement
        for statement in statements
    )
    assert any(
        "DROP INDEX IF EXISTS idx_komari_memory_interaction_embedding" in statement
        for statement in statements
    )
    assert any(
        "DROP COLUMN IF EXISTS embedding" in statement for statement in statements
    )
    assert any(
        "idx_komari_memory_conv_embedding_vector" in statement for statement in statements
    )
    assert any(
        "idx_komari_memory_interaction_embedding_vector" in statement
        for statement in statements
    )
    legacy_migration = next(
        statement
        for statement in statements
        if "旧互动历史表已重命名" in statement
    )
    assert legacy_migration.count("table_schema = current_schema()") == 3


def test_render_schema_statements_preserves_blocks_and_adds_semicolons() -> None:
    rendered = render_schema_statements(
        (
            " SELECT 1 ",
            """
            DO $$
            BEGIN
                PERFORM 1;
            END
            $$;
            """,
        )
    )

    assert rendered == "SELECT 1;\n\nDO $$\nBEGIN\n    PERFORM 1;\nEND\n$$;\n"


def test_build_knowledge_schema_statements_uses_requested_dimension() -> None:
    statements = build_knowledge_schema_statements(1536)
    assert "VECTOR(1536)" in statements[1]
    assert any(
        "CREATE INDEX IF NOT EXISTS idx_komari_knowledge_embedding" in statement
        for statement in statements
    )
    assert any(
        "trigger_komari_knowledge_updated_at" in statement for statement in statements
    )
    assert any(
        "CREATE TABLE IF NOT EXISTS komari_search_index_versions" in statement
        for statement in statements
    )
    assert "trigger_komari_knowledge_index_version" in statements[-1]


def test_build_knowledge_schema_statements_skips_hnsw_for_unsupported_dimension() -> (
    None
):
    statements = build_knowledge_schema_statements(
        PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS + 1
    )
    assert f"VECTOR({PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS + 1})" in statements[1]
    assert not any(
        "CREATE INDEX IF NOT EXISTS idx_komari_knowledge_embedding" in statement
        for statement in statements
    )
    assert (
        build_knowledge_embedding_index_statement(
            PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS + 1
        )
        is None
    )


def test_build_help_schema_statements_uses_requested_dimension() -> None:
    statements = build_help_schema_statements(1536)
    assert "VECTOR(1536)" in statements[1]
    assert any("komari_help_scan_leases" in statement for statement in statements)
    assert any("uq_komari_help_auto_plugin" in statement for statement in statements)
    assert any("ranked_auto_help" in statement for statement in statements)
    assert any(
        "CREATE INDEX IF NOT EXISTS idx_komari_help_embedding" in statement
        for statement in statements
    )
    assert any("trigger_komari_help_updated_at" in statement for statement in statements)
    assert any(
        "CREATE TABLE IF NOT EXISTS komari_search_index_versions" in statement
        for statement in statements
    )
    assert "trigger_komari_help_index_version" in statements[-1]


def test_build_help_schema_statements_skip_hnsw_for_unsupported_dimension() -> None:
    statements = build_help_schema_statements(PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS + 1)
    assert f"VECTOR({PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS + 1})" in statements[1]
    assert not any(
        "CREATE INDEX IF NOT EXISTS idx_komari_help_embedding" in statement
        for statement in statements
    )
    assert (
        build_help_embedding_index_statement(PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS + 1)
        is None
    )


def test_build_schema_statements_reject_invalid_dimension() -> None:
    with pytest.raises(ValueError, match="非法 embedding 维度"):
        build_memory_schema_statements(0)
