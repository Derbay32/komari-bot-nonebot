"""独立向量嵌入迁移工具。

默认 dry-run；实际写入必须显式传入 ``--apply``。本脚本刻意不导入 ``komari_bot`` 包，
用于在运行时插件不可加载时仍能重建知识库与记忆向量。v2.0.0 起连接串统一读取
nonebot-plugin-orm 权威的 ``SQLALCHEMY_DATABASE_URL``（``--dsn`` 可显式覆盖）。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import asyncpg

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS = 2000

logger = logging.getLogger("migrate_embeddings")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


@dataclass(frozen=True)
class EmbeddingConfig:
    model: str
    dimension: int
    api_url: str
    api_key: str


@dataclass(frozen=True)
class MigrationTarget:
    name: str
    source_table: str
    id_column: str
    text_column: str
    embedding_table: str | None
    embedding_owner_column: str | None
    embedding_column: str = "embedding"
    content_hash_column: str = "content_hash"
    embedding_dim_column: str = "embedding_dim"
    vector_index_name: str | None = None
    conflict_column: str | None = None


@dataclass(frozen=True)
class MigrationResult:
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


CONVERSATION_MEMORY_TARGET = MigrationTarget(
    name="memory_conversations",
    source_table="komari_memory_conversations",
    id_column="id",
    text_column="summary",
    embedding_table="komari_memory_conversation_embeddings",
    embedding_owner_column="conversation_id",
    vector_index_name="idx_komari_memory_conv_embedding_vector",
    conflict_column="conversation_id",
)
INTERACTION_MEMORY_TARGET = MigrationTarget(
    name="memory_interactions",
    source_table="komari_memory_interaction_history",
    id_column="id",
    text_column="event_summary",
    embedding_table="komari_memory_interaction_embeddings",
    embedding_owner_column="interaction_id",
    vector_index_name="idx_komari_memory_interaction_embedding_vector",
    conflict_column="interaction_id",
)
KNOWLEDGE_TARGET = MigrationTarget(
    name="knowledge",
    source_table="komari_knowledge",
    id_column="id",
    text_column="content",
    embedding_table=None,
    embedding_owner_column=None,
    vector_index_name="idx_komari_knowledge_embedding",
)

TARGET_GROUPS: dict[str, tuple[MigrationTarget, ...]] = {
    "memory": (CONVERSATION_MEMORY_TARGET, INTERACTION_MEMORY_TARGET),
    "knowledge": (KNOWLEDGE_TARGET,),
    "all": (CONVERSATION_MEMORY_TARGET, INTERACTION_MEMORY_TARGET, KNOWLEDGE_TARGET),
}


def _load_dotenv_file(env_path: Path) -> None:
    """加载最小 dotenv 配置，且不覆盖已有环境变量。"""
    if not env_path.exists():
        logger.info("dotenv 文件不存在，跳过: %s", env_path)
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env_value(*keys: str, default: str = "") -> str:
    for key in keys:
        if value := os.getenv(key):
            return value
    return default


def _env_int(*keys: str, default: int) -> int:
    raw_value = _env_value(*keys, default=str(default))
    try:
        return int(raw_value)
    except ValueError as exc:
        joined_keys = "/".join(keys)
        msg = f"环境变量 {joined_keys} 必须是整数: {raw_value}"
        raise ValueError(msg) from exc


def parse_args() -> argparse.Namespace:
    env_parser = argparse.ArgumentParser(add_help=False)
    env_parser.add_argument(
        "--env-file",
        "--dotenv",
        dest="env_file",
        type=Path,
        default=PROJECT_ROOT / ".env",
        help="dotenv 配置文件路径，默认读取项目根目录 .env",
    )
    env_args, _ = env_parser.parse_known_args()
    _load_dotenv_file(env_args.env_file)

    parser = argparse.ArgumentParser(
        description="Komari Bot 独立向量嵌入迁移工具",
        parents=[env_parser],
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("SQLALCHEMY_DATABASE_URL", ""),
        help="SQLAlchemy 数据库连接串；缺省读取 SQLALCHEMY_DATABASE_URL 环境变量",
    )
    parser.add_argument(
        "--database-config-path",
        type=Path,
        default=None,
        help="兼容旧参数；本脚本不读取项目配置文件",
    )
    parser.add_argument(
        "--embedding-model",
        default=_env_value("EMBEDDING_MODEL", default="BAAI/bge-small-zh-v1.5"),
    )
    parser.add_argument(
        "--embedding-dimension",
        type=int,
        default=_env_int("EMBEDDING_DIMENSION", default=512),
    )
    parser.add_argument("--embedding-api-url", default=_env_value("EMBEDDING_API_URL"))
    parser.add_argument("--embedding-api-key", default=_env_value("EMBEDDING_API_KEY"))
    parser.add_argument(
        "--target",
        dest="targets",
        action="append",
        choices=("knowledge", "memory", "all"),
        help="迁移目标，可重复传入；默认 all",
    )
    parser.add_argument(
        "--apply", action="store_true", help="执行数据库写入（默认 dry-run）"
    )
    return parser.parse_args()


def resolve_dsn(args: argparse.Namespace) -> str:
    """返回数据库连接串（nonebot-plugin-orm 权威的 SQLALCHEMY_DATABASE_URL）。"""
    dsn = args.dsn or os.environ.get("SQLALCHEMY_DATABASE_URL", "")
    if not dsn:
        msg = (
            "未配置 SQLALCHEMY_DATABASE_URL，"
            "请通过 --dsn 或环境变量设置 nonebot-plugin-orm 的连接串"
        )
        raise RuntimeError(msg)
    return dsn


def build_embedding_config(args: argparse.Namespace) -> EmbeddingConfig:
    if args.embedding_dimension <= 0:
        msg = f"非法 embedding 维度: {args.embedding_dimension}"
        raise ValueError(msg)
    return EmbeddingConfig(
        model=args.embedding_model,
        dimension=args.embedding_dimension,
        api_url=args.embedding_api_url,
        api_key=args.embedding_api_key,
    )


def expand_targets(targets: set[str]) -> tuple[MigrationTarget, ...]:
    selected = targets or {"all"}
    expanded: list[MigrationTarget] = []
    for target_name in (
        ("memory", "knowledge") if "all" in selected else sorted(selected)
    ):
        for target in TARGET_GROUPS[target_name]:
            if target not in expanded:
                expanded.append(target)
    return tuple(expanded)


async def main_async(
    *,
    shared_db_config_path: Path | None = None,
    targets: set[str] | None = None,
    apply: bool,
    dsn: str | None = None,
    embedding_config: EmbeddingConfig | None = None,
) -> None:
    del shared_db_config_path
    if dsn is None or embedding_config is None:
        args = parse_args()
        dsn = dsn or resolve_dsn(args)
        embedding_config = embedding_config or build_embedding_config(args)
        targets = targets or set(args.targets or {"all"})

    logger.info(
        "当前 Embedding Provider: model=%s dimension=%s",
        embedding_config.model,
        embedding_config.dimension,
    )
    pool = await asyncpg.create_pool(dsn=dsn, command_timeout=60)
    try:
        results = [
            await migrate_target(
                pool,
                target=target,
                embedding_config=embedding_config,
                dry_run=not apply,
            )
            for target in expand_targets(targets or {"all"})
        ]
        _log_summary(results, apply=apply)
    finally:
        await pool.close()


async def migrate_target(
    pool: asyncpg.Pool,
    *,
    target: MigrationTarget,
    embedding_config: EmbeddingConfig,
    dry_run: bool,
) -> MigrationResult:
    async with pool.acquire() as conn:
        table_exists = await _table_exists(conn, target.source_table)
        if not table_exists:
            return MigrationResult(
                target_name=target.name,
                table_name=target.source_table,
                dry_run=dry_run,
                table_exists=False,
                current_dimension=None,
                target_dimension=embedding_config.dimension,
                schema_changed=False,
                row_total=0,
                updated_rows=0,
                failed_rows=0,
            )

        current_dimension = await _current_dimension(conn, target)
        row_total = await conn.fetchval(f"SELECT COUNT(*) FROM {target.source_table}")
        schema_changed = current_dimension != embedding_config.dimension

        if dry_run:
            return MigrationResult(
                target_name=target.name,
                table_name=target.source_table,
                dry_run=True,
                table_exists=True,
                current_dimension=current_dimension,
                target_dimension=embedding_config.dimension,
                schema_changed=schema_changed,
                row_total=int(row_total or 0),
                updated_rows=0,
                failed_rows=0,
            )

        async with conn.transaction():
            await _ensure_target_schema(conn, target, embedding_config.dimension)

    updated_rows = 0
    failed_rows = 0
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {target.id_column} AS row_id, {target.text_column} AS text FROM {target.source_table} ORDER BY {target.id_column}"
        )
        for row in rows:
            text = str(row["text"] or "")
            try:
                embedding = await request_embedding(text, embedding_config)
                await _write_embedding(
                    conn, target, int(row["row_id"]), text, embedding
                )
                updated_rows += 1
            except Exception:
                failed_rows += 1
                logger.exception("%s: 迁移行失败 id=%s", target.name, row["row_id"])

    return MigrationResult(
        target_name=target.name,
        table_name=target.source_table,
        dry_run=False,
        table_exists=True,
        current_dimension=current_dimension,
        target_dimension=embedding_config.dimension,
        schema_changed=schema_changed,
        row_total=int(row_total or 0),
        updated_rows=updated_rows,
        failed_rows=failed_rows,
    )


async def request_embedding(text: str, config: EmbeddingConfig) -> str:
    if not config.api_url or not config.api_key:
        msg = "执行 apply 需要提供 EMBEDDING_API_URL 与 EMBEDDING_API_KEY"
        raise RuntimeError(msg)
    payload = {"model": config.model, "input": text}
    headers = {"Authorization": f"Bearer {config.api_key}"}
    async with (
        aiohttp.ClientSession() as session,
        session.post(config.api_url, json=payload, headers=headers) as response,
    ):
        response.raise_for_status()
        data = await response.json()
    embedding = data["data"][0]["embedding"]
    return "[" + ",".join(str(float(value)) for value in embedding) + "]"


async def _table_exists(conn: Any, table_name: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table_name))


async def _column_exists(conn: Any, table_name: str, column_name: str) -> bool:
    if not await _table_exists(conn, table_name):
        return False

    return bool(
        await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_attribute
                WHERE attrelid = $1::regclass
                  AND attname = $2
                  AND NOT attisdropped
            )
            """,
            table_name,
            column_name,
        )
    )


async def _current_dimension(conn: Any, target: MigrationTarget) -> int | None:
    table_name = target.embedding_table or target.source_table
    if not await _table_exists(conn, table_name):
        return None

    row = await conn.fetchrow(
        """
        SELECT atttypmod
        FROM pg_attribute
        WHERE attrelid = $1::regclass
          AND attname = $2
          AND NOT attisdropped
        """,
        table_name,
        target.embedding_column,
    )
    if row is None:
        return None
    typmod = int(row["atttypmod"])
    return typmod if typmod > 0 else None


async def _ensure_target_schema(
    conn: Any, target: MigrationTarget, dimension: int
) -> None:
    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    if target.embedding_table is None:
        await conn.execute(
            f"ALTER TABLE {target.source_table} DROP COLUMN IF EXISTS {target.embedding_column}"
        )
        await conn.execute(
            f"ALTER TABLE {target.source_table} ADD COLUMN {target.embedding_column} VECTOR({dimension})"
        )
    else:
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {target.embedding_table} (
                id BIGSERIAL PRIMARY KEY,
                {target.embedding_owner_column} INT NOT NULL REFERENCES {target.source_table}({target.id_column}) ON DELETE CASCADE,
                {target.content_hash_column} TEXT NOT NULL,
                {target.embedding_column} VECTOR({dimension}) NOT NULL,
                {target.embedding_dim_column} INT NOT NULL,
                embedded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE ({target.embedding_owner_column})
            )
            """
        )
        await conn.execute(
            f"ALTER TABLE {target.embedding_table} ALTER COLUMN {target.embedding_column} TYPE VECTOR({dimension})"
        )
        if await _column_exists(conn, target.source_table, target.embedding_column):
            await conn.execute(
                f"ALTER TABLE {target.source_table} DROP COLUMN {target.embedding_column}"
            )
    if target.vector_index_name:
        await conn.execute(f"DROP INDEX IF EXISTS {target.vector_index_name}")
        if dimension <= PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS:
            index_table = target.embedding_table or target.source_table
            await conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS {target.vector_index_name}
                ON {index_table}
                USING hnsw ({target.embedding_column} vector_cosine_ops)
                WITH (m = 16, ef_construction = 64)
                """
            )


async def _write_embedding(
    conn: Any, target: MigrationTarget, row_id: int, text: str, embedding: str
) -> None:
    if target.embedding_table is None:
        await conn.execute(
            f"UPDATE {target.source_table} SET {target.embedding_column} = $1 WHERE {target.id_column} = $2",
            embedding,
            row_id,
        )
        return

    await conn.execute(
        f"""
        INSERT INTO {target.embedding_table}
            ({target.embedding_owner_column}, {target.content_hash_column}, {target.embedding_column}, {target.embedding_dim_column})
        VALUES ($1, $2, $3, vector_dims($3::vector))
        ON CONFLICT ({target.conflict_column}) DO UPDATE SET
            {target.content_hash_column} = EXCLUDED.{target.content_hash_column},
            {target.embedding_column} = EXCLUDED.{target.embedding_column},
            {target.embedding_dim_column} = EXCLUDED.{target.embedding_dim_column},
            embedded_at = CURRENT_TIMESTAMP
        """,
        row_id,
        hashlib.sha256(text.encode("utf-8")).hexdigest(),
        embedding,
    )


def _log_summary(results: list[MigrationResult], *, apply: bool) -> None:
    mode = "apply" if apply else "dry-run"
    logger.info("=== 迁移总结 (%s) ===", mode)
    for result in results:
        if not result.table_exists:
            logger.info("%s: 表不存在，已跳过", result.table_name)
            continue
        logger.info(
            "%s: current_dim=%s target_dim=%s schema_changed=%s rows=%s updated=%s failed=%s",
            result.target_name,
            result.current_dimension,
            result.target_dimension,
            result.schema_changed,
            result.row_total,
            result.updated_rows,
            result.failed_rows,
        )
    if not apply:
        logger.info("当前为 dry-run，未写入数据库。使用 --apply 执行迁移。")


def main() -> None:
    args = parse_args()
    asyncio.run(
        main_async(
            targets=set(args.targets or {"all"}),
            apply=args.apply,
            dsn=resolve_dsn(args),
            embedding_config=build_embedding_config(args),
        )
    )


if __name__ == "__main__":
    main()
