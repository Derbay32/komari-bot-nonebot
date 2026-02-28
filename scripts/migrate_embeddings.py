# 向量嵌入数据迁移脚本
# 用于在更换 embedding 模型（由于维度或语义空间变化）时，重新计算数据库中常识库和记忆库的向量。

# 建议在运行前备份数据库。
# 运行方式：在项目根目录执行 `poetry run python scripts/migrate_embeddings.py`

import asyncio
import json
import logging
from pathlib import Path

import asyncpg

# [需要修改以保证安全导入]
# import src.plugins.embedding_provider...
from komari_bot.plugins.embedding_provider.config_schema import DynamicConfigSchema
from komari_bot.plugins.embedding_provider.embedding_service import EmbeddingService

# 配置日志
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("MigrateEmbeddings")


def load_embedding_config() -> DynamicConfigSchema:
    """[Helper] 加载 embedding provider 配置"""
    config_path = Path("config/config_manager/embedding_provider_config.json")
    if config_path.exists():
        with config_path.open(encoding="utf-8") as f:
            data = json.load(f)
            return DynamicConfigSchema(**data)
    else:
        logger.warning(
            f"Embedding Provider 配置文件不存在 ({config_path})，使用默认配置。请确保已在正确模式下配置API！"
        )
        return DynamicConfigSchema()


def load_db_config() -> dict:
    """[Helper] 尝试从 komari_knowledge_config.json 提取数据库配置"""
    config_path = Path("config/config_manager/komari_knowledge_config.json")
    if config_path.exists():
        with config_path.open(encoding="utf-8") as f:
            data = json.load(f)
            return {
                "host": data.get("pg_host", "localhost"),
                "port": data.get("pg_port", 5432),
                "user": data.get("pg_user", ""),
                "password": data.get("pg_password", ""),
                "database": data.get("pg_database", "komari_bot"),
            }
    return {}


async def migrate_komari_knowledge(
    pool: asyncpg.Pool, embedding_service: EmbeddingService
) -> None:
    """[Migrate] 重新嵌入 komari_knowledge 表的向量数据"""
    logger.info("开始迁移 komari_knowledge 向量数据...")

    async with pool.acquire() as conn:
        # 获取所有需要嵌入的内容
        rows = await conn.fetch("SELECT id, content FROM komari_knowledge")
        total = len(rows)
        logger.info(f"komari_knowledge 共需处理 {total} 条数据。")

        for idx, row in enumerate(rows):
            kid = row["id"]
            content = row["content"]

            try:
                # 重新计算向量
                embedding = await embedding_service.embed(content)

                # 更新数据库
                await conn.execute(
                    "UPDATE komari_knowledge SET embedding = $1::vector WHERE id = $2",
                    str(embedding),
                    kid,
                )

                if (idx + 1) % 10 == 0:
                    logger.info(f"komari_knowledge: 已处理 {idx + 1}/{total} 条数据")

            except Exception:
                logger.exception(f"处理 komari_knowledge ID {kid} 时出错:")

    logger.info("komari_knowledge 向量数据迁移完成！\n")


async def migrate_komari_memory(
    pool: asyncpg.Pool, embedding_service: EmbeddingService
) -> None:
    """[Migrate] 重新嵌入 komari_memory_conversation 表的向量数据"""
    logger.info("开始迁移 komari_memory_conversation 向量数据...")

    async with pool.acquire() as conn:
        # 检查表是否存在
        table_exists = await conn.fetchval(
            "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'komari_memory_conversation')"
        )
        if not table_exists:
            logger.warning("komari_memory_conversation 表不存在，跳过记忆库迁移。\n")
            return

        # 获取所有需要嵌入的对话摘要
        rows = await conn.fetch(
            "SELECT id, summary FROM komari_memory_conversation WHERE summary IS NOT NULL AND summary != ''"
        )
        total = len(rows)
        logger.info(f"komari_memory_conversation 共需处理 {total} 条数据。")

        for idx, row in enumerate(rows):
            cid = row["id"]
            summary = row["summary"]

            try:
                # 重新计算向量
                embedding = await embedding_service.embed(summary)

                # 更新数据库
                await conn.execute(
                    "UPDATE komari_memory_conversation SET embedding = $1::vector WHERE id = $2",
                    str(embedding),
                    cid,
                )

                if (idx + 1) % 10 == 0:
                    logger.info(
                        f"komari_memory_conversation: 已处理 {idx + 1}/{total} 条数据"
                    )

            except Exception:
                logger.exception(f"处理 komari_memory_conversation ID {cid} 时出错:")

    logger.info("komari_memory_conversation 向量数据迁移完成！\n")


async def main() -> None:
    """[Main] 迁移主函数"""
    logger.info("=== Komari Bot 向量嵌入重计算迁移工具 ===")

    # 1. 初始化 Embedding Service
    embed_config = load_embedding_config()
    logger.info(
        f"当前使用的 Embedding Provider 模式: {embed_config.embedding_source} (维度: {embed_config.embedding_dimension})"
    )

    embedding_service = EmbeddingService(embed_config)

    # 请注意：如果是修改了维度，你需要提前在 psql 中执行：
    # ALTER TABLE komari_knowledge ALTER COLUMN embedding TYPE vector(新维度);
    # ALTER TABLE komari_memory_conversation ALTER COLUMN embedding TYPE vector(新维度);
    # 否则插入时会报错维度不匹配。

    # 2. 连接数据库
    db_config = load_db_config()
    if not db_config.get("user") or not db_config.get("password"):
        logger.error(
            "无法从配置文件读取到 PostgreSQL 账户密码。请确保 komari_knowledge_config.json 中配置正确。"
        )
        return

    logger.info(
        f"正在连接到数据库 PostgreSQL {db_config['host']}:{db_config['port']} / {db_config['database']} ..."
    )

    try:
        pool = await asyncpg.create_pool(
            host=db_config["host"],
            port=db_config["port"],
            user=db_config["user"],
            password=db_config["password"],
            database=db_config["database"],
        )
    except Exception:
        logger.exception("数据库连接失败:")
        return

    logger.info("数据库连接成功。")

    try:
        # 3. 开始迁移常识库
        await migrate_komari_knowledge(pool, embedding_service)

        # 4. 开始迁移记忆库
        await migrate_komari_memory(pool, embedding_service)

        logger.info("=== 所有数据的向量重新嵌入与迁移已顺利结束 ===")
    finally:
        await pool.close()
        await embedding_service.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
