"""Komari Memory - 智能记忆与对话插件。"""

from typing import TYPE_CHECKING

from nonebot import get_driver, logger
from nonebot.plugin import PluginMetadata, require

from komari_bot.common.pgvector_schema import ensure_vector_column_dimension
from komari_bot.common.vector_storage_schema import (
    apply_schema_statements,
    build_memory_schema_statements,
)

if TYPE_CHECKING:
    from .config_schema import KomariMemoryConfigSchema

# 依赖插件
apscheduler_plugin = require("nonebot_plugin_apscheduler")

from .config_schema import KomariMemoryConfigSchema
from .database.connection import create_pool
from .handlers.forgetting_worker import (
    register_forgetting_task,
    unregister_forgetting_task,
)
from .handlers.summary_worker import register_summary_task, unregister_summary_task
from .repositories.conversation_repository import ConversationRepository
from .repositories.entity_repository import EntityRepository
from .services.config_interface import get_config
from .services.forgetting_service import ForgettingService
from .services.memory_service import MemoryService
from .services.redis_manager import RedisManager

__plugin_meta__ = PluginMetadata(
    name="小鞠记忆",
    description="智能记忆与对话插件，支持向量检索和常识库集成",
    usage="自动运行，无需命令",
)


class PluginManager:
    """插件管理器，负责组件的生命周期管理。"""

    def __init__(self, config: KomariMemoryConfigSchema) -> None:
        """初始化插件管理器。

        Args:
            config: 插件配置
        """
        self.config = config
        self.redis: RedisManager | None = None
        self.memory: MemoryService | None = None
        self.forgetting: ForgettingService | None = None
        self.pg_pool = None

    async def initialize(self) -> None:
        """初始化所有组件。"""
        logger.info("[KomariMemory] 正在初始化组件...")

        # 1. 初始化 PostgreSQL 连接池 (用于向量检索)
        try:
            self.pg_pool = await create_pool(self.config)
            logger.info("[KomariMemory] PostgreSQL 连接池已建立")
        except Exception:
            logger.exception("[KomariMemory] PostgreSQL 连接失败")
            raise

        expected_dimension = self._resolve_expected_embedding_dimension()
        try:
            await self._ensure_storage_schema(expected_dimension)
            await self._validate_embedding_dimension(expected_dimension)
        except Exception:
            logger.exception("[KomariMemory] PostgreSQL schema 检查失败")
            raise

        # 2. 初始化 Redis 管理器
        try:
            self.redis = RedisManager(self.config)
            await self.redis.initialize()
        except Exception:
            logger.exception("[KomariMemory] Redis 连接失败")
            raise

        # 3. 初始化数据访问层
        conversation_repo = ConversationRepository(self.pg_pool)
        entity_repo = EntityRepository(self.pg_pool)
        # 4. 初始化记忆服务
        self.memory = MemoryService(self.config, conversation_repo, entity_repo)

        # 5. 注册总结定时任务
        register_summary_task(self.redis, self.memory)

        # 6. 初始化忘却服务并注册定时任务
        self.forgetting = ForgettingService(self.config, self.pg_pool)
        register_forgetting_task(self.forgetting)

        logger.info("[KomariMemory] 组件初始化完成")

    def _resolve_expected_embedding_dimension(self) -> int | None:
        """解析当前 embedding_provider 的目标维度。"""
        embedding_provider = require("embedding_provider")
        get_dimension = getattr(embedding_provider, "get_embedding_dimension", None)
        expected_dimension: int | None = None
        if callable(get_dimension):
            raw_dimension = get_dimension()
            if isinstance(raw_dimension, int):
                expected_dimension = raw_dimension
            elif isinstance(raw_dimension, str):
                expected_dimension = int(raw_dimension)
            elif raw_dimension is not None:
                msg = f"embedding_provider 返回了无效维度类型: {type(raw_dimension)!r}"
                raise TypeError(msg)
        return expected_dimension

    async def _ensure_storage_schema(self, expected_dimension: int | None) -> None:
        """按当前 embedding 维度补齐 PostgreSQL 基础表结构。"""
        if self.pg_pool is None:
            msg = "PostgreSQL 连接池未初始化"
            raise RuntimeError(msg)
        if expected_dimension is None:
            msg = "无法确定 embedding 维度，不能初始化 memory schema"
            raise RuntimeError(msg)

        await apply_schema_statements(
            self.pg_pool,
            statements=build_memory_schema_statements(expected_dimension),
        )
        logger.info(
            "[KomariMemory] PostgreSQL schema 检查完成 (embedding=%s)",
            expected_dimension,
        )

    async def _validate_embedding_dimension(
        self,
        expected_dimension: int | None,
    ) -> None:
        """校验会话向量列与 embedding_provider 的维度一致。"""
        if self.pg_pool is None:
            msg = "PostgreSQL 连接池未初始化"
            raise RuntimeError(msg)

        await ensure_vector_column_dimension(
            self.pg_pool,
            table_name="komari_memory_conversations",
            column_name="embedding",
            expected_dimension=expected_dimension,
            label="KomariMemory",
        )

    async def shutdown(self) -> None:
        """关闭所有组件。"""
        # 取消定时任务
        unregister_summary_task()
        unregister_forgetting_task()
        # 清理记忆服务（释放 fastembed 模型）
        if self.memory:
            await self.memory.cleanup()

        # 关闭 Redis 连接
        if self.redis:
            await self.redis.close()

        # 关闭 PostgreSQL 连接池
        if self.pg_pool:
            await self.pg_pool.close()

        logger.info("[KomariMemory] 已关闭")


# 创建插件管理器实例
_plugin_manager: PluginManager | None = None


def get_plugin_manager() -> PluginManager | None:
    """获取插件管理器实例。

    Returns:
        插件管理器实例，如果未初始化则返回 None
    """
    return _plugin_manager


# 在插件加载时初始化
driver = get_driver()


@driver.on_startup
async def startup() -> None:
    """启动时初始化。"""
    global _plugin_manager  # noqa: PLW0603

    config = get_config()

    if config.plugin_enable:
        logger.info("[KomariMemory] 插件已启用（记忆/持久化子系统）")
        _plugin_manager = PluginManager(config)
        await _plugin_manager.initialize()
    else:
        logger.warning("[KomariMemory] 插件未启用，请在配置中设置 plugin_enable=true")


@driver.on_shutdown
async def shutdown() -> None:
    """关闭时清理。"""
    manager = get_plugin_manager()
    if manager:
        await manager.shutdown()
