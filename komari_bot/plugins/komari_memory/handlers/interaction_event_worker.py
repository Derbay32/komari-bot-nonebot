"""跨群互动事件后台总结任务。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from apscheduler.jobstores.base import JobLookupError
from nonebot import logger
from nonebot_plugin_apscheduler import scheduler

from ..core.retry import retry_async
from ..services.config_interface import get_config
from ..services.interaction_event_summary_service import summarize_interaction_events

if TYPE_CHECKING:
    from ..services.memory_service import MemoryService
    from ..services.redis_manager import RedisManager

_JOB_ID = "komari_memory_interaction_event_worker"
_DAILY_JOB_ID = "komari_memory_interaction_event_daily_flush"


def _record_timestamp(record: dict[str, Any]) -> float:
    try:
        return float(record.get("timestamp", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _record_datetime(record: dict[str, Any]) -> datetime:
    timestamp = _record_timestamp(record)
    if timestamp <= 0:
        return datetime.now(UTC)
    return datetime.fromtimestamp(timestamp, tz=UTC)


def _resolve_display_name(user_id: str, records: list[dict[str, Any]]) -> str:
    for record in reversed(records):
        display_name = str(record.get("display_name", "")).strip()
        if display_name:
            return display_name
    return user_id


def _filter_valid_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valid_records: list[dict[str, Any]] = []
    for record in records:
        event = str(record.get("event", "")).strip()
        result = str(record.get("result", "")).strip()
        emotion = str(record.get("emotion", "")).strip()
        if not event or not result or not emotion:
            continue
        valid_records.append(record)
    return sorted(valid_records, key=_record_timestamp)


async def interaction_event_worker_task(
    redis: RedisManager,
    memory: MemoryService,
) -> None:
    """轮询 pending 集合并总结跨群互动事件。"""
    config = get_config()
    if not config.global_interaction_enabled:
        return

    user_ids = await redis.pop_pending_interaction_summaries(count=100)
    if not user_ids:
        return

    logger.debug("[KomariMemory] 检查 {} 个待总结跨群互动用户", len(user_ids))
    for user_id in user_ids:
        token = uuid4().hex
        processing_key = await redis.snapshot_global_interactions(user_id, token)
        if processing_key is None:
            continue
        try:
            await _summarize_processing_key(
                redis=redis,
                memory=memory,
                user_id=user_id,
                processing_key=processing_key,
            )
        except Exception:
            logger.exception("[KomariMemory] 跨群互动事件总结最终失败，恢复快照: user={}", user_id)
            await redis.restore_processing_global_interactions(user_id, processing_key)


@retry_async(max_attempts=3, base_delay=1.0)
async def _summarize_processing_key(
    *,
    redis: RedisManager,
    memory: MemoryService,
    user_id: str,
    processing_key: str,
) -> None:
    """处理一个 processing 快照；失败由 retry 装饰器重试。"""
    raw_records = await redis.get_processing_global_interactions(processing_key)
    records = _filter_valid_records(raw_records)
    if not records:
        await redis.delete_processing_global_interactions(processing_key)
        return

    display_name = _resolve_display_name(user_id, records)
    summary = await summarize_interaction_events(
        user_id=user_id,
        display_name=display_name,
        records=records,
    )
    first_seen_at = _record_datetime(records[0])
    last_seen_at = _record_datetime(records[-1])
    await memory.insert_interaction_event(
        user_id=user_id,
        display_name=display_name,
        event_summary=summary.event_summary,
        source_message_count=len(records),
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at,
        importance_initial=summary.importance,
    )
    await redis.delete_processing_global_interactions(processing_key)


async def daily_enqueue_global_interactions(redis: RedisManager) -> None:
    """每日 4 点兜底：把所有非空跨群互动缓冲加入 pending。"""
    config = get_config()
    if not config.global_interaction_enabled:
        return

    user_ids = await redis.get_users_with_global_interaction_buffer()
    for user_id in user_ids:
        await redis.add_pending_interaction_summary(user_id)
    if user_ids:
        logger.info("[KomariMemory] 4 点兜底加入跨群互动总结 pending: {} 个用户", len(user_ids))


def register_interaction_event_task(redis: RedisManager, memory: MemoryService) -> None:
    """注册跨群互动事件总结任务。"""
    config = get_config()
    if not config.global_interaction_enabled:
        logger.info("[KomariMemory] 跨群互动事件缓冲未启用，跳过 worker 注册")
        return

    scheduler.add_job(
        interaction_event_worker_task,
        "interval",
        minutes=config.global_interaction_summary_interval_minutes,
        args=[redis, memory],
        id=_JOB_ID,
        replace_existing=True,
    )
    scheduler.add_job(
        daily_enqueue_global_interactions,
        "cron",
        hour=4,
        minute=0,
        args=[redis],
        id=_DAILY_JOB_ID,
        replace_existing=True,
    )
    logger.info("[KomariMemory] 跨群互动事件总结任务已注册")


def unregister_interaction_event_task() -> None:
    """取消跨群互动事件总结任务。"""
    for job_id in (_JOB_ID, _DAILY_JOB_ID):
        try:
            scheduler.remove_job(job_id)
        except JobLookupError:
            logger.debug("[KomariMemory] 跨群互动事件任务不存在，无需取消: {}", job_id)
        except Exception:
            logger.exception("[KomariMemory] 跨群互动事件任务取消失败: {}", job_id)
