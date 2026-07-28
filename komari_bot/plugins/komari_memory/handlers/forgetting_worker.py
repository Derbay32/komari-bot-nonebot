"""记忆忘却定时任务。"""

from nonebot import logger
from nonebot_plugin_apscheduler import scheduler

from ..services.forgetting_service import ForgettingService


class ForgettingTaskManager:
    """忘却定时任务管理器（单例）。"""

    _instance: "ForgettingTaskManager | None" = None
    _service: ForgettingService | None = None

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

    def register(self, service: ForgettingService) -> None:
        """注册忘却定时任务。

        Args:
            service: 忘却服务实例
        """
        self._service = service

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
        try:
            scheduler.remove_job("komari_memory_forgetting_worker")
            logger.info("[KomariMemory] 忘却定时任务已取消")
        except Exception:
            pass


# 创建单例实例
_task_manager = ForgettingTaskManager()


def register_forgetting_task(service: ForgettingService) -> None:
    """注册忘却定时任务。

    Args:
        service: 忘却服务实例
    """
    _task_manager.register(service)


def unregister_forgetting_task() -> None:
    """取消注册忘却定时任务。"""
    _task_manager.unregister()
