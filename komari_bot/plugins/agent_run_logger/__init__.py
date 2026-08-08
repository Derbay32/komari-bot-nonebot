"""独立的单任务 Agent Run 完整日志插件。"""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime
from typing import Any

from apscheduler.jobstores.base import JobLookupError
from nonebot import get_driver, logger
from nonebot.plugin import PluginMetadata, require

require("nonebot_plugin_apscheduler")

from nonebot_plugin_apscheduler import scheduler

from .api import register_agent_run_log_api
from .config_schema import (
    AgentRunLoggerConfigSchema,
    AgentRunLoggerEnvConfigSchema,
)
from .diagnostic import (
    AgentRunCollector,
    AgentRunOrigin,
    AgentRunStatus,
    AgentRunType,
    LLMCallTrace,
    LLMDiagnosticCollector,
    ToolExecutionTrace,
    completion_response_payload,
    record_completion_call,
    record_failed_call,
)
from .reader import reader
from .storage import storage

__plugin_meta__ = PluginMetadata(
    name="agent_run_logger",
    description="按业务任务记录完整 Agent Run JSONL，并以 PostgreSQL 提供轻量索引",
    usage="由 chat、总结和记忆插件显式下传 AgentRunCollector",
    config=AgentRunLoggerConfigSchema,
)

__all__ = [
    "AgentRunCollector",
    "AgentRunLoggerConfigSchema",
    "AgentRunOrigin",
    "AgentRunStatus",
    "AgentRunType",
    "LLMCallTrace",
    "LLMDiagnosticCollector",
    "ToolExecutionTrace",
    "completion_response_payload",
    "create_collector",
    "finalize_collector",
    "get_agent_run_log_reader",
    "record_completion_call",
    "record_failed_call",
    "register_agent_run_log_api",
]

require("config_manager")
from komari_bot.plugins import config_manager as config_manager_plugin

config_manager = config_manager_plugin.get_config_manager(
    "agent_run_logger",
    AgentRunLoggerConfigSchema,
    env_config_schema=AgentRunLoggerEnvConfigSchema,
)
driver = get_driver()

_MAINTENANCE_JOB_ID = "agent_run_logger_index_maintenance"
_CLEANUP_JOB_ID = "agent_run_logger_daily_cleanup"


def _runtime_config() -> AgentRunLoggerConfigSchema:
    value = config_manager.get()
    if isinstance(value, AgentRunLoggerConfigSchema):
        return value
    return AgentRunLoggerConfigSchema.model_validate(value)


def _retention_days() -> int:
    try:
        return _runtime_config().retention_days
    except Exception:
        logger.opt(exception=True).warning(
            "[AgentRunLogger] 读取保留期失败，使用独立默认值 1 天"
        )
        return 1


storage.configure(_retention_days)


def create_collector(
    *,
    run_type: AgentRunType,
    task_kind: str,
    trace_id: str | None = None,
    origin: AgentRunOrigin = "normal",
    input_data: object = None,
    force_collect: bool = False,
) -> AgentRunCollector | None:
    """创建显式下传收集器；关闭日志时 debug 仍可保留内存诊断。"""
    try:
        enabled = _runtime_config().log_enabled
    except Exception:
        logger.opt(exception=True).warning(
            "[AgentRunLogger] 读取日志开关失败，本次普通任务不采集"
        )
        enabled = False
    if not enabled and not force_collect:
        return None
    return AgentRunCollector(
        request_id=trace_id,
        run_type=run_type,
        task_kind=task_kind,
        origin=origin,
        input_data=input_data,
        persist=enabled,
    )


async def finalize_collector(
    collector: AgentRunCollector | None,
    *,
    status: AgentRunStatus,
    output: object = None,
    error: BaseException | str | None = None,
    skip_if_no_calls: bool = False,
) -> bool:
    """幂等结束并排入单行 writer；日志故障绝不改变业务结果。"""
    if collector is None:
        return False
    if not collector.mark_finished(status=status, output=output, error=error):
        return False
    if not collector.persist or (skip_if_no_calls and not collector.calls):
        return True
    try:
        return storage.enqueue(collector.build_record())
    except Exception:
        logger.opt(exception=True).warning(
            "[AgentRunLogger] Agent Run 结束记录入队失败"
        )
        return False


def get_agent_run_log_reader() -> Any:
    return reader


@driver.on_startup
async def _startup() -> None:
    """启动即补做清理和索引对账；PG 失败不阻止 JSONL。"""
    await storage.initialize()
    await storage.cleanup()
    await storage.reconcile()
    scheduler.add_job(
        storage.maintain,
        "interval",
        minutes=5,
        id=_MAINTENANCE_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        storage.cleanup,
        "cron",
        hour=4,
        minute=0,
        timezone=datetime.now().astimezone().tzinfo,
        id=_CLEANUP_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


@driver.on_shutdown
async def _shutdown() -> None:
    """停止维护任务、排空写入队列并关闭独立 PG 租约。"""
    for job_id in (_MAINTENANCE_JOB_ID, _CLEANUP_JOB_ID):
        with suppress(JobLookupError):
            scheduler.remove_job(job_id)
    await storage.shutdown()
