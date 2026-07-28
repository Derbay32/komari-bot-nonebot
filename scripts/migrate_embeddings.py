"""向量嵌入迁移工具。

默认以 dry-run 模式运行，只打印配置解析结果、表状态和预计改动。
执行实际迁移时请显式传入 ``--apply``。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from komari_bot.common.embedding_migration import (
    KNOWLEDGE_MIGRATION_SPEC,
    MEMORY_MIGRATION_SPEC,
    TableMigrationResult,
    get_pool_key,
    load_embedding_config,
    migrate_table_embeddings,
    resolve_knowledge_database_config,
    resolve_memory_database_config,
)
from komari_bot.common.postgres import create_postgres_pool

logger = logging.getLogger("migrate_embeddings")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


async def main_async(
    *,
    shared_db_config_path: Path,
    knowledge_config_path: Path,
    memory_config_path: Path,
    embedding_config_path: Path,
    targets: set[str],
    apply: bool,
) -> None:
    embed_config = load_embedding_config(embedding_config_path)
    target_dimension = int(embed_config.embedding_dimension)
    logger.info(
        "当前 Embedding Provider: source=%s model=%s dimension=%s",
        embed_config.embedding_source,
        embed_config.embedding_model,
        target_dimension,
    )

    knowledge_db_config = resolve_knowledge_database_config(
        shared_config_path=shared_db_config_path,
        knowledge_config_path=knowledge_config_path,
    )
    memory_db_config = resolve_memory_database_config(
        shared_config_path=shared_db_config_path,
        memory_config_path=memory_config_path,
    )

    for label, config in (
        ("knowledge", knowledge_db_config),
        ("memory", memory_db_config),
    ):
        if label not in targets:
            continue
        if not config.pg_user or not config.pg_password:
            msg = f"{label} 数据库配置缺少 pg_user 或 pg_password"
            raise RuntimeError(msg)

    pools: dict[tuple[str, int, str, str, str], Any] = {}
    embedding_service: Any | None = None
    if apply:
        from komari_bot.plugins.embedding_provider.embedding_service import (
            EmbeddingService,
        )

        embedding_service = EmbeddingService(embed_config)
    try:
        async def get_pool(config_key: str) -> Any:
            config = knowledge_db_config if config_key == "knowledge" else memory_db_config
            pool_key = get_pool_key(config)
            if pool_key not in pools:
                logger.info(
                    "连接数据库(%s): %s:%s/%s",
                    config_key,
                    config.pg_host,
                    config.pg_port,
                    config.pg_database,
                )
                pools[pool_key] = await create_postgres_pool(config, command_timeout=60)
            return pools[pool_key]

        results: list[TableMigrationResult] = []
        if "knowledge" in targets:
            pool = await get_pool("knowledge")
            results.append(
                await migrate_table_embeddings(
                    pool,
                    spec=KNOWLEDGE_MIGRATION_SPEC,
                    target_dimension=target_dimension,
                    dry_run=not apply,
                    embedding_service=embedding_service,
                )
            )

        if "memory" in targets:
            pool = await get_pool("memory")
            results.append(
                await migrate_table_embeddings(
                    pool,
                    spec=MEMORY_MIGRATION_SPEC,
                    target_dimension=target_dimension,
                    dry_run=not apply,
                    embedding_service=embedding_service,
                )
            )

        _log_summary(results, apply=apply)
    finally:
        for pool in pools.values():
            await pool.close()
        if embedding_service is not None:
            await embedding_service.cleanup()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Komari Bot 向量嵌入迁移工具")
    parser.add_argument(
        "--database-config-path",
        type=Path,
        default=Path("config/config_manager/database_config.json"),
        help="共享数据库配置路径",
    )
    parser.add_argument(
        "--knowledge-config-path",
        type=Path,
        default=Path("config/config_manager/komari_knowledge_config.json"),
        help="komari_knowledge 配置路径",
    )
    parser.add_argument(
        "--memory-config-path",
        type=Path,
        default=Path("config/config_manager/komari_memory_config.json"),
        help="komari_memory 配置路径",
    )
    parser.add_argument(
        "--embedding-config-path",
        type=Path,
        default=Path("config/config_manager/embedding_provider_config.json"),
        help="embedding_provider 配置路径",
    )
    parser.add_argument(
        "--target",
        dest="targets",
        action="append",
        choices=("knowledge", "memory"),
        help="限制迁移目标，可重复传入；默认同时处理 knowledge 和 memory",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="执行数据库写入（默认 dry-run）",
    )
    return parser.parse_args()


def _log_summary(results: list[TableMigrationResult], *, apply: bool) -> None:
    mode = "apply" if apply else "dry-run"
    logger.info("=== 迁移总结 (%s) ===", mode)
    for result in results:
        if not result.table_exists:
            logger.info("%s: 表不存在，已跳过", result.table_name)
            continue
        logger.info(
            "%s: current_dim=%s target_dim=%s schema_changed=%s rows=%s updated=%s failed=%s",
            result.table_name,
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
            shared_db_config_path=args.database_config_path,
            knowledge_config_path=args.knowledge_config_path,
            memory_config_path=args.memory_config_path,
            embedding_config_path=args.embedding_config_path,
            targets=set(args.targets or {"knowledge", "memory"}),
            apply=args.apply,
        )
    )


if __name__ == "__main__":
    main()
