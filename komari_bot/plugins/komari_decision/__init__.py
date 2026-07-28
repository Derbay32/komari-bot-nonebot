"""Komari Decision - 回复/记忆判定与 scene 运行时插件。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nonebot import logger
from nonebot.plugin import PluginMetadata

from .services.config_interface import get_config

if TYPE_CHECKING:
    from .repositories.scene_repository import SceneRepository
    from .services.scene_admin_service import SceneAdminService
    from .services.scene_embedding_worker import SceneEmbeddingWorker
    from .services.scene_runtime_service import SceneRuntimeService
    from .services.scene_sync_service import SceneSyncService

__plugin_meta__ = PluginMetadata(
    name="小鞠判定",
    description="向量检索重排判定与 scene 运行时子系统",
    usage="被其他插件通过服务接口调用",
)


class PluginManager:
    """判定插件管理器，负责 scene 子系统生命周期。"""

    def __init__(self) -> None:
        self.scene_repository: SceneRepository | None = None
        self.scene_admin: SceneAdminService | None = None
        self.scene_runtime: SceneRuntimeService | None = None
        self.scene_sync: SceneSyncService | None = None
        self.scene_embedding_worker: SceneEmbeddingWorker | None = None

    async def initialize(self) -> None:
        """初始化 scene 运行时与同步任务。"""
        from nonebot.plugin import require

        from komari_bot.plugins.komari_memory import (
            get_plugin_manager as get_memory_plugin_manager,
        )

        from .handlers.scene_sync_worker import (
            bootstrap_scene_sync_task,
            register_scene_sync_task,
            unregister_scene_sync_task,
        )
        from .repositories.scene_repository import SceneRepository
        from .services.scene_admin_service import SceneAdminService
        from .services.scene_embedding_worker import SceneEmbeddingWorker
        from .services.scene_runtime_service import SceneRuntimeService
        from .services.scene_sync_service import SceneSyncService

        require("nonebot_plugin_apscheduler")
        require("komari_memory")

        config = get_config()
        if not config.scene_persist_enabled:
            logger.info("[KomariDecision] scene 持久化未启用，跳过初始化")
            return

        memory_manager = get_memory_plugin_manager()
        if memory_manager is None or memory_manager.pg_pool is None:
            logger.warning("[KomariDecision] KomariMemory 未就绪，scene 子系统初始化跳过")
            return

        scene_repository = SceneRepository(memory_manager.pg_pool)
        scene_runtime = SceneRuntimeService(scene_repository)
        scene_sync = SceneSyncService(scene_repository)
        scene_embedding_worker = SceneEmbeddingWorker(scene_repository, batch_size=16)
        scene_admin = SceneAdminService(
            scene_repository,
            scene_runtime,
            scene_embedding_worker,
        )

        self.scene_repository = scene_repository
        self.scene_admin = scene_admin
        self.scene_runtime = scene_runtime
        self.scene_sync = scene_sync
        self.scene_embedding_worker = scene_embedding_worker

        try:
            await self.scene_repository.ensure_schema()
            loaded = await self.scene_runtime.load_active_set_cache()
            if loaded:
                logger.info("[KomariDecision] scene runtime cache 初始化成功")
            else:
                logger.warning("[KomariDecision] 当前无 active scene set，runtime cache 为空")
            register_scene_sync_task(
                scene_repository,
                scene_admin,
                scene_sync,
                scene_embedding_worker,
                scene_runtime,
            )
            await bootstrap_scene_sync_task()
        except Exception:
            unregister_scene_sync_task()
            self.scene_repository = None
            self.scene_admin = None
            self.scene_runtime = None
            self.scene_sync = None
            self.scene_embedding_worker = None
            logger.exception("[KomariDecision] scene 子系统初始化失败")
            return

        logger.info("[KomariDecision] scene 子系统初始化完成")

    async def shutdown(self) -> None:
        """关闭 scene 同步任务。"""
        from .handlers.scene_sync_worker import unregister_scene_sync_task

        unregister_scene_sync_task()
        self.scene_repository = None
        self.scene_admin = None
        self.scene_runtime = None
        self.scene_sync = None
        self.scene_embedding_worker = None
        logger.info("[KomariDecision] 已关闭")


_plugin_manager: PluginManager | None = None


def get_plugin_manager() -> PluginManager | None:
    """获取插件管理器实例。"""
    return _plugin_manager


def get_scene_admin_service() -> SceneAdminService | None:
    """获取 scene 运维服务。"""
    manager = get_plugin_manager()
    if manager is None:
        return None
    return manager.scene_admin


try:
    from nonebot import get_driver

    driver = get_driver()
except ValueError:
    driver = None

if driver is not None:

    @driver.on_startup
    async def startup() -> None:
        """启动时初始化。"""
        global _plugin_manager  # noqa: PLW0603

        config = get_config()
        if not config.plugin_enable:
            logger.warning("[KomariDecision] KomariMemory 未启用，跳过初始化")
            return

        manager = PluginManager()
        await manager.initialize()
        _plugin_manager = manager


    @driver.on_shutdown
    async def shutdown() -> None:
        """关闭时清理。"""
        global _plugin_manager  # noqa: PLW0603
        manager = get_plugin_manager()
        if manager:
            await manager.shutdown()
        _plugin_manager = None
