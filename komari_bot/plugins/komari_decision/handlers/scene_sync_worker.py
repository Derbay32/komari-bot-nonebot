"""Scene 同步与嵌入后台任务。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nonebot import logger
from nonebot_plugin_apscheduler import scheduler

from ..services.config_interface import get_config

if TYPE_CHECKING:
    from ..repositories.scene_repository import SceneRepository
    from ..services.scene_embedding_worker import SceneEmbeddingWorker
    from ..services.scene_runtime_service import SceneRuntimeService
    from ..services.scene_sync_service import SceneSyncService


class SceneSyncTaskManager:
    """Scene 同步定时任务管理器（单例）。"""

    _MAX_BATCHES_PER_TICK = 3
    _MAX_BATCHES_BOOTSTRAP = 128
    _JOB_ID = "komari_decision_scene_sync_worker"

    def __init__(self) -> None:
        self._repository: SceneRepository | None = None
        self._sync_service: SceneSyncService | None = None
        self._embedding_worker: SceneEmbeddingWorker | None = None
        self._runtime_service: SceneRuntimeService | None = None

    def register(
        self,
        repository: SceneRepository,
        sync_service: SceneSyncService,
        embedding_worker: SceneEmbeddingWorker,
        runtime_service: SceneRuntimeService,
    ) -> None:
        """注册 scene 同步定时任务。"""
        self._repository = repository
        self._sync_service = sync_service
        self._embedding_worker = embedding_worker
        self._runtime_service = runtime_service

        config = get_config()
        scheduler.add_job(
            self._execute_task,
            "interval",
            seconds=config.scene_sync_poll_seconds,
            id=self._JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(
            "[KomariDecision] scene 同步定时任务已注册: interval=%ss",
            config.scene_sync_poll_seconds,
        )

    def unregister(self) -> None:
        """取消 scene 同步定时任务。"""
        try:
            scheduler.remove_job(self._JOB_ID)
            logger.info("[KomariDecision] scene 同步定时任务已取消")
        except Exception:
            pass

        self._repository = None
        self._sync_service = None
        self._embedding_worker = None
        self._runtime_service = None

    async def bootstrap(self) -> None:
        """启动期补齐首个 active set。"""
        config = get_config()
        if not config.scene_persist_enabled:
            return

        if (
            self._repository is None
            or self._sync_service is None
            or self._embedding_worker is None
            or self._runtime_service is None
        ):
            logger.warning("[KomariDecision] scene task manager 未初始化，跳过 bootstrap")
            return

        try:
            await self._runtime_service.load_active_set_cache()
            await self._execute_task(max_batches=self._MAX_BATCHES_BOOTSTRAP)
        except Exception:
            logger.exception("[KomariDecision] scene bootstrap 失败")

    async def _activate_if_ready(self, set_id: int) -> bool:
        if self._repository is None or self._runtime_service is None:
            return False

        scene_set = await self._repository.get_scene_set(set_id)
        if scene_set is None or str(scene_set.get("status")) != "READY":
            return False

        active = await self._repository.get_active_set()
        if active is not None and int(active["id"]) == set_id:
            return False

        await self._runtime_service.switch_active_set(set_id)
        logger.info("[KomariDecision] scene active set 已切换: id=%s", set_id)
        return True

    async def _drain_pending(self, set_id: int, *, max_batches: int) -> None:
        if self._embedding_worker is None:
            return

        remaining = max(1, max_batches)
        while remaining > 0:
            batch = await self._embedding_worker.embed_pending_batch(set_id)
            if batch.pending_count <= 0 or batch.fetched_count <= 0:
                return
            remaining -= 1

        logger.warning(
            "[KomariDecision] scene set 仍有 pending，等待下一轮: set=%s",
            set_id,
        )

    async def _execute_task(self, max_batches: int | None = None) -> None:
        """执行 scene 同步任务。"""
        config = get_config()
        if not config.scene_persist_enabled:
            return

        if (
            self._sync_service is None
            or self._embedding_worker is None
            or self._runtime_service is None
        ):
            logger.warning("[KomariDecision] scene 服务未就绪，跳过本轮同步")
            return

        try:
            sync_result = await self._sync_service.build_scene_set()
            if sync_result.pending_count > 0:
                await self._drain_pending(
                    sync_result.set_id,
                    max_batches=max_batches or self._MAX_BATCHES_PER_TICK,
                )

            await self._activate_if_ready(sync_result.set_id)
            await self._runtime_service.refresh_if_runtime_updated()
        except Exception:
            logger.exception("[KomariDecision] scene 同步任务执行失败")


_task_manager = SceneSyncTaskManager()


def register_scene_sync_task(
    repository: SceneRepository,
    sync_service: SceneSyncService,
    embedding_worker: SceneEmbeddingWorker,
    runtime_service: SceneRuntimeService,
) -> None:
    """注册 scene 同步定时任务。"""
    _task_manager.register(
        repository,
        sync_service,
        embedding_worker,
        runtime_service,
    )


def unregister_scene_sync_task() -> None:
    """取消 scene 同步定时任务。"""
    _task_manager.unregister()


async def bootstrap_scene_sync_task() -> None:
    """执行启动期 scene bootstrap。"""
    await _task_manager.bootstrap()
