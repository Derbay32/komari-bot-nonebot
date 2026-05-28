"""Embedding schema migration helpers."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol

from komari_bot.common.database_config import (
    DatabaseConfigSchema,
    load_database_config_from_file,
    merge_database_config,
)
from komari_bot.common.pgvector_schema import (
    get_vector_column_dimension_from_connection,
)
from komari_bot.common.vector_storage_schema import (
    PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS,
    build_knowledge_embedding_index_statement,
)

logger = logging.getLogger("migrate_embeddings")

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class EmbeddingServiceProtocol(Protocol):
    """Minimal embedding service contract used by the migration workflow."""

    async def embed(self, text: str) -> Any:
        """Return the embedding vector for text."""


@dataclass
class EmbeddingMigrationConfig:
    """Minimal embedding config needed by the migration tool."""

    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_api_url: str = ""
    embedding_api_key: str = ""
    embedding_dimension: int = 512


@dataclass(frozen=True)
class ManagedIndex:
    """A managed index that may need to be rebuilt during migration."""

    name: str
    create_sql: str | None = None
    create_sql_builder: Callable[[int], str | None] | None = None

    def render_create_sql(self, target_dimension: int) -> str | None:
        """Build the CREATE INDEX statement for the target dimension."""
        if self.create_sql_builder is not None:
            return self.create_sql_builder(target_dimension)
        return self.create_sql


@dataclass(frozen=True)
class TableMigrationSpec:
    """Embedding migration target definition."""

    target_name: str
    table_name: str
    id_column: str
    text_column: str
    embedding_column: str = "embedding"
    managed_indexes: tuple[ManagedIndex, ...] = ()


@dataclass(frozen=True)
class TableMigrationResult:
    """Summary of a single table migration."""

    target_name: str
    table_name: str
    dry_run: bool
    table_exists: bool
    current_dimension: int | None
    target_dimension: int
    schema_changed: bool
    row_total: int
    updated_rows: int
    failed_rows: int


KNOWLEDGE_MIGRATION_SPEC = TableMigrationSpec(
    target_name="knowledge",
    table_name="komari_knowledge",
    id_column="id",
    text_column="content",
    managed_indexes=(
        ManagedIndex(
            name="idx_komari_knowledge_embedding",
            create_sql_builder=build_knowledge_embedding_index_statement,
        ),
    ),
)

MEMORY_MIGRATION_SPEC = TableMigrationSpec(
    target_name="memory",
    table_name="komari_memory_conversations",
    id_column="id",
    text_column="summary",
)


def load_embedding_config(config_path: Path) -> EmbeddingMigrationConfig:
    """Load embedding provider config from JSON, or return defaults."""
    data = _load_optional_json(config_path)
    return EmbeddingMigrationConfig(
        embedding_model=str(data.get("embedding_model", "BAAI/bge-small-zh-v1.5")),
        embedding_api_url=str(data.get("embedding_api_url", "")),
        embedding_api_key=str(data.get("embedding_api_key", "")),
        embedding_dimension=int(data.get("embedding_dimension", 512)),
    )


def resolve_knowledge_database_config(
    *,
    shared_config_path: Path,
    knowledge_config_path: Path,
) -> DatabaseConfigSchema:
    """Resolve the final DB config for komari_knowledge."""
    knowledge_config = _load_optional_namespace(knowledge_config_path)
    return _resolve_database_config(
        shared_config_path=shared_config_path,
        local_config=knowledge_config,
    )


def resolve_memory_database_config(
    *,
    shared_config_path: Path,
    memory_config_path: Path,
) -> DatabaseConfigSchema:
    """Resolve the final DB config for komari_memory."""
    memory_config = _load_optional_namespace(memory_config_path)
    return _resolve_database_config(
        shared_config_path=shared_config_path,
        local_config=memory_config,
    )


def get_pool_key(config: DatabaseConfigSchema) -> tuple[str, int, str, str, str]:
    """Generate a stable key for pooling identical DB configs."""
    return (
        config.pg_host,
        int(config.pg_port),
        config.pg_database,
        config.pg_user,
        config.pg_password,
    )


async def migrate_table_embeddings(
    pool: Any,
    *,
    spec: TableMigrationSpec,
    target_dimension: int,
    dry_run: bool,
    embedding_service: EmbeddingServiceProtocol | None = None,
) -> TableMigrationResult:
    """Migrate embeddings for one table."""
    async with pool.acquire() as conn:
        exists = await _table_exists(conn, spec.table_name)
        if not exists:
            logger.warning("%s 表不存在，跳过。", spec.table_name)
            return TableMigrationResult(
                target_name=spec.target_name,
                table_name=spec.table_name,
                dry_run=dry_run,
                table_exists=False,
                current_dimension=None,
                target_dimension=target_dimension,
                schema_changed=False,
                row_total=0,
                updated_rows=0,
                failed_rows=0,
            )

        current_dimension = await get_vector_column_dimension_from_connection(
            conn,
            table_name=spec.table_name,
            column_name=spec.embedding_column,
        )
        schema_changed = current_dimension not in (None, target_dimension)
        if schema_changed:
            logger.info(
                "%s 向量列维度需要迁移: %s.%s %s -> %s",
                spec.target_name,
                spec.table_name,
                spec.embedding_column,
                current_dimension,
                target_dimension,
            )
            if dry_run:
                logger.info(
                    "[dry-run] 将重建 %s.%s 列类型并清空旧向量值",
                    spec.table_name,
                    spec.embedding_column,
                )
            else:
                await _rebuild_vector_column(
                    conn,
                    spec=spec,
                    target_dimension=target_dimension,
                )

        rows = await _fetch_embedding_rows(conn, spec)
        row_total = len(rows)
        logger.info("%s 共需处理 %s 条数据。", spec.table_name, row_total)

        if dry_run:
            return TableMigrationResult(
                target_name=spec.target_name,
                table_name=spec.table_name,
                dry_run=True,
                table_exists=True,
                current_dimension=current_dimension,
                target_dimension=target_dimension,
                schema_changed=schema_changed,
                row_total=row_total,
                updated_rows=0,
                failed_rows=0,
            )

        if embedding_service is None:
            msg = f"{spec.target_name} 迁移需要 embedding_service"
            raise RuntimeError(msg)

        updated_rows = 0
        failed_rows = 0
        for row_id, text in rows:
            try:
                embedding = await embedding_service.embed(text)
                await conn.execute(
                    (
                        f"UPDATE {_quote_identifier(spec.table_name)} "
                        f"SET {_quote_identifier(spec.embedding_column)} = $1::vector "
                        f"WHERE {_quote_identifier(spec.id_column)} = $2"
                    ),
                    str(embedding),
                    row_id,
                )
                updated_rows += 1
                if updated_rows % 10 == 0:
                    logger.info(
                        "%s: 已处理 %s/%s 条数据",
                        spec.table_name,
                        updated_rows,
                        row_total,
                    )
            except Exception:
                failed_rows += 1
                logger.exception("处理 %s ID %s 时出错", spec.table_name, row_id)

        if spec.managed_indexes:
            await _ensure_indexes(conn, spec, target_dimension)

        return TableMigrationResult(
            target_name=spec.target_name,
            table_name=spec.table_name,
            dry_run=False,
            table_exists=True,
            current_dimension=current_dimension,
            target_dimension=target_dimension,
            schema_changed=schema_changed,
            row_total=row_total,
            updated_rows=updated_rows,
            failed_rows=failed_rows,
        )


def _load_optional_namespace(config_path: Path) -> SimpleNamespace:
    return SimpleNamespace(**_load_optional_json(config_path))


def _load_optional_json(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return {}
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = f"配置文件格式错误，期望 JSON object: {config_path}"
        raise TypeError(msg)
    return data


def _resolve_database_config(
    *,
    shared_config_path: Path,
    local_config: Any,
) -> DatabaseConfigSchema:
    shared_config = (
        load_database_config_from_file(shared_config_path)
        if shared_config_path.exists()
        else DatabaseConfigSchema()
    )
    return merge_database_config(shared_config, local_config)


async def _table_exists(conn: Any, table_name: str) -> bool:
    exists = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = current_schema()
              AND table_name = $1
        )
        """,
        table_name,
    )
    return bool(exists)


async def _fetch_embedding_rows(
    conn: Any,
    spec: TableMigrationSpec,
) -> list[tuple[int, str]]:
    query = (
        f"SELECT {_quote_identifier(spec.id_column)} AS row_id, "
        f"{_quote_identifier(spec.text_column)} AS text_value "
        f"FROM {_quote_identifier(spec.table_name)} "
        f"WHERE {_quote_identifier(spec.text_column)} IS NOT NULL "
        f"AND {_quote_identifier(spec.text_column)} != '' "
        f"ORDER BY {_quote_identifier(spec.id_column)}"
    )
    rows = await conn.fetch(query)
    return [(int(row["row_id"]), str(row["text_value"])) for row in rows]


async def _rebuild_vector_column(
    conn: Any,
    *,
    spec: TableMigrationSpec,
    target_dimension: int,
) -> None:
    for index in spec.managed_indexes:
        await conn.execute(f"DROP INDEX IF EXISTS {_quote_identifier(index.name)}")

    table_name = _quote_identifier(spec.table_name)
    column_name = _quote_identifier(spec.embedding_column)
    await conn.execute(
        f"ALTER TABLE {table_name} "
        f"ALTER COLUMN {column_name} "
        f"TYPE vector({target_dimension}) "
        f"USING CASE "
        f"WHEN {column_name} IS NULL THEN NULL "
        f"ELSE NULL::vector({target_dimension}) "
        f"END"
    )


async def _ensure_indexes(
    conn: Any,
    spec: TableMigrationSpec,
    target_dimension: int,
) -> None:
    for index in spec.managed_indexes:
        create_sql = index.render_create_sql(target_dimension)
        if create_sql is None:
            logger.warning(
                "%s 向量索引 %s 已跳过：embedding 维度 %s 超过 pgvector HNSW 上限 %s，"
                "语义检索将退化为顺序扫描。",
                spec.target_name,
                index.name,
                target_dimension,
                PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS,
            )
            continue
        await conn.execute(create_sql)


def _quote_identifier(identifier: str) -> str:
    if _IDENTIFIER_PATTERN.fullmatch(identifier) is None:
        msg = f"非法 SQL 标识符: {identifier}"
        raise ValueError(msg)
    return f'"{identifier}"'
