"""Komari Memory - 智能记忆与对话插件。"""

from typing import TYPE_CHECKING

from nonebot import get_driver, logger, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.adapters.onebot.v11.message import MessageSegment
from nonebot.plugin import PluginMetadata, require

if TYPE_CHECKING:
    from .config_schema import KomariMemoryConfigSchema

# 依赖插件
permission_manager_plugin = require("permission_manager")
apscheduler_plugin = require("nonebot_plugin_apscheduler")
require("komari_knowledge")  # 常识库集成

from .config_schema import KomariMemoryConfigSchema
from .database.connection import create_pool
from .handlers.forgetting_worker import (
    register_forgetting_task,
    unregister_forgetting_task,
)
from .handlers.message_handler import MessageHandler
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
        self.handler: MessageHandler | None = None
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

        # 5. 初始化消息处理器
        self.handler = MessageHandler(
            redis=self.redis,
            memory=self.memory,
        )

        # 6. 注册总结定时任务
        register_summary_task(self.redis, self.memory)

        # 7. 初始化忘却服务并注册定时任务
        self.forgetting = ForgettingService(self.config, self.pg_pool)
        register_forgetting_task(self.forgetting)

        logger.info("[KomariMemory] 组件初始化完成")

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


# 消息处理器
matcher = on_message(priority=10, block=False)


@matcher.handle()
async def handle_group_message(bot: Bot, event: GroupMessageEvent) -> None:
    """处理群聊消息。"""
    # 获取最新配置
    config = get_config()

    # 检查插件是否启用
    if not config.plugin_enable:
        return

    manager = get_plugin_manager()
    if manager is None or manager.handler is None:
        return

    # 白名单检查：只有白名单内的群组才启用功能（白名单为空则禁用所有功能）
    group_id = str(event.group_id)
    if group_id not in config.group_whitelist:
        return

    # 权限检查
    can_use, _ = await permission_manager_plugin.check_runtime_permission(bot, event, config)
    if not can_use:
        return

    try:
        result = await manager.handler.process_message(event)

        if result:
            reply = result.get("reply")
            reply_to_message_id = result.get("reply_to_message_id")
            if reply_to_message_id:
                # 使用 QQ 原生回复功能
                reply_message = MessageSegment.reply(reply_to_message_id) + reply
                await matcher.send(reply_message)
            else:
                await matcher.send(reply)

    except Exception:
        logger.exception("[KomariMemory] 消息处理失败")


# 在插件加载时初始化
driver = get_driver()


@driver.on_startup
async def startup() -> None:
    """启动时初始化。"""
    global _plugin_manager  # noqa: PLW0603

    config = get_config()

    if config.plugin_enable:
        # 检查白名单是否为空
        if not config.group_whitelist:
            logger.warning(
                "[KomariMemory] 群组白名单为空，插件将不会处理任何消息。"
                "请在配置中设置 group_whitelist"
            )
        else:
            logger.info(
                f"[KomariMemory] 插件已启用，白名单群组: {config.group_whitelist}"
            )

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
