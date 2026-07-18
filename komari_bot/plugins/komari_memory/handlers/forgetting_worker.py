"""记忆忘却定时任务。"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from apscheduler.jobstores.base import JobLookupError
from nonebot import logger
from nonebot_plugin_apscheduler import scheduler

if TYPE_CHECKING:
    from ..services.forgetting_service import ForgettingService
    from ..services.redis_manager import RedisManager


class ForgettingTaskManager:
    """忘却定时任务管理器（单例）。"""

    _instance: "ForgettingTaskManager | None" = None
    _service: ForgettingService | None = None
    _redis_manager: RedisManager | None = None

    def __new__(cls) -> "ForgettingTaskManager":
        """单例模式。"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    async def _execute_task(self) -> None:
        """执行定时忘却任务。"""
        if self._service is None:
            logger.warning("[KomariMemory] 忘却服务未初始化")
            return

        await self._service.decay_and_cleanup()
        await self._recover_orphaned_conversation_processing()

    async def _recover_orphaned_conversation_processing(self) -> None:
        """恢复遗留的对话 processing 快照。"""
        if self._redis_manager is None:
            return
        try:
            orphaned_keys = await self._redis_manager.get_orphaned_conversation_processing_keys()
        except Exception:
            logger.exception("[KomariMemory] 扫描孤立对话 processing 快照失败")
            return

        for group_id, processing_key in orphaned_keys:
            owner_token = f"orphan-recovery-{uuid4().hex}"
            try:
                claim = await self._redis_manager.claim_existing_conversation_processing(
                    group_id,
                    processing_key,
                    owner_token,
                )
                if claim.status != "claimed":
                    continue
                restored = await self._redis_manager.restore_processing_conversation_buffer(
                    group_id,
                    processing_key,
                    owner_token,
                )
                if not restored:
                    continue
            except Exception:
                logger.exception(
                    "[KomariMemory] 恢复孤立对话 processing 快照失败: group={} key={}",
                    group_id,
                    processing_key,
                )
                continue
            logger.info(
                "[KomariMemory] 已恢复孤立对话 processing 快照: group={} key={}",
                group_id,
                processing_key,
            )

    def register(
        self,
        service: ForgettingService,
        redis_manager: RedisManager | None = None,
    ) -> None:
        """注册忘却定时任务。

        Args:
            service: 忘却服务实例
            redis_manager: Redis 管理器实例，用于巡检 processing 残留
        """
        self._service = service
        self._redis_manager = redis_manager

        # 每天凌晨4点执行
        scheduler.add_job(
            self._execute_task,
            "cron",
            hour=4,
            minute=0,
            id="komari_memory_forgetting_worker",
            replace_existing=True,
        )
        logger.info("[KomariMemory] 忘却定时任务已注册(每天04:00)")

    def unregister(self) -> None:
        """取消注册忘却定时任务。"""
        self._service = None
        self._redis_manager = None
        try:
            scheduler.remove_job("komari_memory_forgetting_worker")
        except JobLookupError:
            logger.debug("[KomariMemory] 忘却定时任务不存在，无需取消")
        except Exception:
            logger.exception("[KomariMemory] 忘却定时任务取消失败")
        else:
            logger.info("[KomariMemory] 忘却定时任务已取消")


# 创建单例实例
_task_manager = ForgettingTaskManager()


def register_forgetting_task(
    service: ForgettingService,
    redis_manager: RedisManager | None = None,
) -> None:
    """注册忘却定时任务。

    Args:
        service: 忘却服务实例
        redis_manager: Redis 管理器实例，用于巡检 processing 残留
    """
    _task_manager.register(service, redis_manager)


def unregister_forgetting_task() -> None:
    """取消注册忘却定时任务。"""
    _task_manager.unregister()
