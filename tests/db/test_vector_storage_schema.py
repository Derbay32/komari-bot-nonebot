"""Dynamic storage schema bootstrap tests."""

from __future__ import annotations

import pytest

from komari_bot.db.vector_storage_schema import (
    PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS,
    build_help_embedding_index_statement,
    build_help_schema_statements,
    build_knowledge_embedding_index_statement,
    build_knowledge_schema_statements,
    render_schema_statements,
)


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
        build_knowledge_schema_statements(0)
