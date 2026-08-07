"""Komari Decision - 回复/记忆判定与 scene 运行时插件。"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Protocol, cast

from nonebot import logger
from nonebot.plugin import PluginMetadata, require

from komari_bot.decision import (
    CandidateSchema,
    DecisionRuntimeState,
    DecisionRuntimeStatus,
    UnifiedRerankResult,
)

from .services.config_interface import get_config, get_config_async
from .services.decision_engine import DecisionEngine
from .services.unified_candidate_rerank import UnifiedCandidateRerankService

if TYPE_CHECKING:
    from collections.abc import Callable

    from asyncpg import Pool

    from komari_bot.plugins.komari_memory.services.redis_manager import RedisManager

    from .repositories.scene_repository import SceneRepository
    from .services.scene_admin_service import SceneAdminService
    from .services.scene_embedding_worker import SceneEmbeddingWorker
    from .services.scene_runtime_service import SceneRuntimeService
    from .services.scene_sync_service import SceneSyncService


class MemoryPluginManagerProtocol(Protocol):
    """Komari Memory 插件管理器协议。"""

    pg_pool: Pool | None
    redis: RedisManager | None


def _get_ready_memory_pool(memory_plugin: object) -> Pool:
    """读取已就绪的 Memory PostgreSQL 连接池。"""
    get_memory_plugin_manager = getattr(memory_plugin, "get_plugin_manager", None)
    if not callable(get_memory_plugin_manager):
        msg = "KomariMemory 未导出 get_plugin_manager"
        raise TypeError(msg)

    memory_manager = cast(
        "MemoryPluginManagerProtocol | None",
        get_memory_plugin_manager(),
    )
    memory_pool = None if memory_manager is None else memory_manager.pg_pool
    if memory_pool is None:
        msg = "KomariMemory PostgreSQL 未就绪"
        raise RuntimeError(msg)
    return memory_pool


# 先加载 embedding 与 memory
require("embedding_provider")
require("komari_memory")

__plugin_meta__ = PluginMetadata(
    name="小鞠判定",
    description="向量检索重排判定与 scene 运行时子系统",
    usage="被其他插件通过服务接口调用",
)

__all__ = [
    "CandidateSchema",
    "DecisionRuntimeState",
    "DecisionRuntimeStatus",
    "PluginManager",
    "UnifiedCandidateRerankService",
    "UnifiedRerankResult",
    "get_decision_engine",
    "get_plugin_manager",
    "get_scene_admin_service",
]


class PluginManager:
    """判定插件管理器，负责 scene 子系统生命周期。"""

    def __init__(self) -> None:
        self.scene_repository: SceneRepository | None = None
        self.scene_admin: SceneAdminService | None = None
        self.scene_runtime: SceneRuntimeService | None = None
        self.scene_sync: SceneSyncService | None = None
        self.scene_embedding_worker: SceneEmbeddingWorker | None = None
        self._runtime_state = DecisionRuntimeState.failed("插件尚未初始化")

    @property
    def runtime_state(self) -> DecisionRuntimeState:
        """返回与动态开关及 scene 快照一致的当前状态。"""
        config = get_config()
        if not config.plugin_enable:
            return DecisionRuntimeState.disabled("komari_decision 已关闭")
        if not config.scene_persist_enabled:
            return DecisionRuntimeState.disabled("scene 持久化已关闭")
        if self.scene_runtime is None:
            if self._runtime_state.status is DecisionRuntimeStatus.DISABLED:
                return DecisionRuntimeState.failed(
                    "配置已启用但运行时尚未初始化，需要重启服务"
                )
            return self._runtime_state
        if self.scene_runtime.get_scene_candidates() is None:
            return DecisionRuntimeState.failed("scene runtime snapshot 暂不可用")
        return DecisionRuntimeState.ready()

    def _clear_services(self) -> None:
        self.scene_repository = None
        self.scene_admin = None
        self.scene_runtime = None
        self.scene_sync = None
        self.scene_embedding_worker = None

    async def initialize(self) -> None:
        """初始化 scene 运行时与同步任务。"""
        config = await get_config_async()
        if not config.plugin_enable:
            self._runtime_state = DecisionRuntimeState.disabled(
                "komari_decision 已关闭"
            )
            logger.info(
                "[KomariDecision] 插件未启用；主动回复判定关闭，显式触发聊天保留"
            )
            return
        if not config.scene_persist_enabled:
            self._runtime_state = DecisionRuntimeState.disabled("scene 持久化已关闭")
            logger.info(
                "[KomariDecision] scene 持久化未启用；主动回复判定关闭，显式触发聊天保留"
            )
            return
        unregister_task: Callable[[], None] | None = None
        task_registered = False
        try:
            from nonebot.plugin import require as runtime_require

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

            unregister_task = unregister_scene_sync_task
            runtime_require("nonebot_plugin_apscheduler")
            from komari_bot.plugins import komari_memory as memory_plugin

            memory_pool = _get_ready_memory_pool(memory_plugin)

            scene_repository = SceneRepository(memory_pool)
            scene_runtime = SceneRuntimeService(scene_repository)
            scene_sync = SceneSyncService(scene_repository)
            scene_embedding_worker = SceneEmbeddingWorker(
                scene_repository,
                batch_size=16,
            )
            scene_admin = SceneAdminService(
                scene_repository,
                scene_runtime,
                scene_embedding_worker,
            )

            # scene 表结构由 Alembic 迁移统一管理，此处不再执行 DDL。
            if not await scene_repository.has_any_scene():
                logger.warning(
                    "[KomariDecision] komari_decision_scenes 为空；请运行迁移脚本或通过管理 API 初始化 scenes"
                )
            try:
                loaded = await scene_runtime.load_active_set_cache()
            except RuntimeError as exc:
                logger.warning(
                    "[KomariDecision] 旧 active scene set 不可用，将进入 bootstrap 重建: {}",
                    exc,
                )
            else:
                if loaded:
                    logger.info("[KomariDecision] scene runtime cache 初始化成功")
                else:
                    logger.warning(
                        "[KomariDecision] 当前无 active scene set，runtime cache 为空"
                    )

            self.scene_repository = scene_repository
            self.scene_admin = scene_admin
            self.scene_runtime = scene_runtime
            self.scene_sync = scene_sync
            self.scene_embedding_worker = scene_embedding_worker
            task_registered = True
            register_scene_sync_task(
                scene_repository,
                scene_admin,
                scene_sync,
                scene_embedding_worker,
                scene_runtime,
            )
            await bootstrap_scene_sync_task()
        except Exception as exc:
            if task_registered and unregister_task is not None:
                unregister_task()
            self._clear_services()
            self._runtime_state = DecisionRuntimeState.failed(
                f"scene 子系统初始化失败：{type(exc).__name__}"
            )
            logger.exception(
                "[KomariDecision] scene 子系统初始化失败，主动回复判定进入 failed 状态"
            )
            return

        self._runtime_state = self.runtime_state
        if not self._runtime_state.is_ready:
            logger.error(
                "[KomariDecision] scene 子系统初始化未就绪：{}；主动回复判定进入 failed 状态",
                self._runtime_state.reason,
            )
            return

        logger.info("[KomariDecision] scene 子系统初始化完成，运行时状态 ready")

    async def shutdown(self) -> None:
        """关闭 scene 同步任务。"""
        from .handlers.scene_sync_worker import unregister_scene_sync_task

        unregister_scene_sync_task()
        self._clear_services()
        self._runtime_state = DecisionRuntimeState.disabled("进程正在关闭")
        logger.info("[KomariDecision] 已关闭")


_plugin_manager: PluginManager | None = None


def get_plugin_manager() -> PluginManager | None:
    """获取插件管理器实例。"""
    return _plugin_manager


def _get_runtime_state() -> DecisionRuntimeState:
    """获取判定运行时三态快照。"""
    manager = get_plugin_manager()
    if manager is not None:
        return manager.runtime_state

    config = get_config()
    if not config.plugin_enable:
        return DecisionRuntimeState.disabled("komari_decision 已关闭")
    if not config.scene_persist_enabled:
        return DecisionRuntimeState.disabled("scene 持久化已关闭")
    return DecisionRuntimeState.failed("插件管理器尚未初始化")


def get_scene_admin_service() -> SceneAdminService | None:
    """获取 scene 运维服务。"""
    manager = get_plugin_manager()
    if manager is None:
        return None
    return manager.scene_admin


_cached_decision_engine: DecisionEngine | None = None
_cached_engine_redis: RedisManager | None = None
_cached_engine_scene_runtime: SceneRuntimeService | None = None


def _get_ready_memory_redis() -> RedisManager | None:
    """逐调用惰性解析 Memory 插件管理器，返回已就绪的 Redis 客户端。"""
    memory_plugin = sys.modules.get("komari_bot.plugins.komari_memory")
    if memory_plugin is None:
        return None
    get_memory_plugin_manager = getattr(memory_plugin, "get_plugin_manager", None)
    if not callable(get_memory_plugin_manager):
        return None
    memory_manager = cast(
        "MemoryPluginManagerProtocol | None",
        get_memory_plugin_manager(),
    )
    if memory_manager is None:
        return None
    return memory_manager.redis


def get_decision_engine() -> DecisionEngine | None:
    """获取惰性构建并缓存的决策引擎；Redis 未就绪时返回 None。

    Redis 短暂未就绪只让当次调用返回 None，不清缓存；
    同身份恢复后仍返回原缓存引擎（与聊天插件懒构建语义一致）。
    """
    global _cached_decision_engine  # noqa: PLW0603
    global _cached_engine_redis  # noqa: PLW0603
    global _cached_engine_scene_runtime  # noqa: PLW0603

    redis = _get_ready_memory_redis()
    if redis is None:
        return None

    decision_manager = get_plugin_manager()
    scene_runtime = (
        None if decision_manager is None else decision_manager.scene_runtime
    )

    if (
        _cached_decision_engine is not None
        and _cached_engine_redis is redis
        and _cached_engine_scene_runtime is scene_runtime
    ):
        return _cached_decision_engine

    engine = DecisionEngine(
        redis,
        scene_runtime,
        runtime_state_provider=_get_runtime_state,
    )
    _cached_decision_engine = engine
    _cached_engine_redis = redis
    _cached_engine_scene_runtime = scene_runtime
    return engine


def _clear_decision_engine_cache() -> None:
    """清空决策引擎缓存（进程关闭语义）。"""
    global _cached_decision_engine  # noqa: PLW0603
    global _cached_engine_redis  # noqa: PLW0603
    global _cached_engine_scene_runtime  # noqa: PLW0603

    _cached_decision_engine = None
    _cached_engine_redis = None
    _cached_engine_scene_runtime = None


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

        manager = PluginManager()
        _plugin_manager = manager
        await manager.initialize()

    @driver.on_shutdown
    async def shutdown() -> None:
        """关闭时清理。"""
        global _plugin_manager  # noqa: PLW0603
        manager = get_plugin_manager()
        if manager:
            await manager.shutdown()
        _plugin_manager = None
        _clear_decision_engine_cache()
