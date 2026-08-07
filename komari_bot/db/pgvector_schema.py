"""pgvector schema validation helpers."""

from __future__ import annotations

import re
from typing import Any

_VECTOR_TYPE_PATTERN = re.compile(r"^vector(?:\((\d+)\))?$")


def parse_vector_type_dimension(type_name: str) -> int | None:
    """Parse a PostgreSQL vector type declaration."""
    normalized = type_name.strip().lower()
    match = _VECTOR_TYPE_PATTERN.fullmatch(normalized)
    if match is None:
        msg = f"不是合法的 vector 类型声明: {type_name}"
        raise ValueError(msg)
    group = match.group(1)
    if group is None:
        return None
    return int(group)


async def get_vector_column_dimension(
    pg_pool: Any,
    *,
    table_name: str,
    column_name: str,
) -> int | None:
    """Return the declared dimension of a pgvector column."""
    async with pg_pool.acquire() as conn:
        return await get_vector_column_dimension_from_connection(
            conn,
            table_name=table_name,
            column_name=column_name,
        )


async def get_vector_column_dimension_from_connection(
    conn: Any,
    *,
    table_name: str,
    column_name: str,
) -> int | None:
    """Return the declared dimension of a pgvector column using an existing connection."""
    row = await conn.fetchrow(
        """
        SELECT pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = $1
          AND a.attname = $2
          AND a.attnum > 0
          AND NOT a.attisdropped
        ORDER BY CASE WHEN n.nspname = current_schema() THEN 0 ELSE 1 END, n.nspname
        LIMIT 1
        """,
        table_name,
        column_name,
    )
    if row is None:
        msg = f"找不到向量列: {table_name}.{column_name}"
        raise RuntimeError(msg)

    data_type = str(row["data_type"])
    return parse_vector_type_dimension(data_type)


async def ensure_vector_column_dimension(
    pg_pool: Any,
    *,
    table_name: str,
    column_name: str,
    expected_dimension: int | None,
    label: str,
) -> None:
    """Validate that a pgvector column matches the expected embedding dimension."""
    if expected_dimension is None:
        return

    actual_dimension = await get_vector_column_dimension(
        pg_pool,
        table_name=table_name,
        column_name=column_name,
    )
    if actual_dimension is None or actual_dimension == expected_dimension:
        return

    msg = (
        f"{label} 向量维度不匹配: {table_name}.{column_name}="
        f"{actual_dimension}, embedding_provider={expected_dimension}"
    )
    raise RuntimeError(msg)
