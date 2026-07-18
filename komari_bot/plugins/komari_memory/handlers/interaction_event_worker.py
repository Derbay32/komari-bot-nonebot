"""跨群互动事件后台总结任务。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from apscheduler.jobstores.base import JobLookupError
from nonebot import logger
from nonebot_plugin_apscheduler import scheduler

from ..core.retry import retry_async
from ..services.config_interface import get_config
from ..services.interaction_event_summary_service import (
    MAX_INTERACTION_SUMMARY_RECORDS,
    summarize_interaction_events,
)

if TYPE_CHECKING:
    from ..services.memory_service import MemoryService
    from ..services.redis_manager import RedisManager

_JOB_ID = "komari_memory_interaction_event_worker"
_DAILY_JOB_ID = "komari_memory_interaction_event_daily_flush"
_MAX_USERS_PER_RUN = 100
_MAX_HEARTBEAT_INTERVAL_SECONDS = 30.0


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


def _build_snapshot_dedup_key(
    user_id: str,
    records: list[dict[str, Any]],
) -> str:
    """根据用户与规范化快照生成稳定幂等键。"""
    payload = json.dumps(
        {"user_id": user_id, "records": records},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def interaction_event_worker_task(
    redis: RedisManager,
    memory: MemoryService,
) -> None:
    """轮询 pending 集合并总结跨群互动事件。"""
    config = get_config()
    if not config.global_interaction_enabled:
        return

    owner_token = uuid4().hex
    processed_count = 0
    while processed_count < _MAX_USERS_PER_RUN:
        user_ids = await redis.claim_pending_interaction_summaries(
            owner_token=owner_token,
            count=1,
            lease_seconds=config.global_interaction_processing_lease_seconds,
        )
        if not user_ids:
            break
        user_id = user_ids[0]
        processed_count += 1
        completed = await _process_claimed_user(
            redis=redis,
            memory=memory,
            user_id=user_id,
            owner_token=owner_token,
            lease_seconds=config.global_interaction_processing_lease_seconds,
        )
        if not completed:
            break

    if processed_count:
        logger.debug(
            "[KomariMemory] 本轮处理跨群互动用户: count={}",
            processed_count,
        )


async def _process_claimed_user(
    *,
    redis: RedisManager,
    memory: MemoryService,
    user_id: str,
    owner_token: str,
    lease_seconds: int,
) -> bool:
    """处理单个已认领用户，处理期间持续续租。"""
    stop_heartbeat = asyncio.Event()
    lease_lost = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        _heartbeat_interaction_lease(
            redis=redis,
            user_id=user_id,
            owner_token=owner_token,
            lease_seconds=lease_seconds,
            stop=stop_heartbeat,
            lease_lost=lease_lost,
        )
    )
    token = uuid4().hex
    processing_key: str | None = None
    completed = False
    try:
        processing_key = await redis.snapshot_global_interactions(user_id, token)
        if processing_key is None:
            if not lease_lost.is_set():
                completed = await redis.ack_processing_global_interactions(
                    user_id=user_id,
                    owner_token=owner_token,
                    processing_key="",
                )
                if not completed:
                    logger.warning(
                        "[KomariMemory] 空互动快照确认失败，租约已被接管: user={}",
                        user_id,
                    )
        else:
            await _summarize_processing_key(
                redis=redis,
                memory=memory,
                user_id=user_id,
                processing_key=processing_key,
            )
            if lease_lost.is_set():
                logger.warning(
                    "[KomariMemory] 互动总结完成前租约已丢失，放弃确认: user={}",
                    user_id,
                )
            else:
                completed = await redis.ack_processing_global_interactions(
                    user_id=user_id,
                    owner_token=owner_token,
                    processing_key=processing_key,
                )
                if not completed:
                    logger.warning(
                        "[KomariMemory] 互动快照确认失败，租约已被接管: user={}",
                        user_id,
                    )
    except Exception:
        logger.exception(
            "[KomariMemory] 跨群互动事件总结最终失败，重新入队: user={}",
            user_id,
        )
        if not lease_lost.is_set():
            try:
                requeued = await redis.requeue_processing_global_interactions(
                    user_id=user_id,
                    owner_token=owner_token,
                    processing_key=processing_key or "",
                )
            except Exception:
                logger.exception(
                    "[KomariMemory] 互动快照重新入队失败，等待租约到期接管: user={}",
                    user_id,
                )
            else:
                if not requeued:
                    logger.warning(
                        "[KomariMemory] 互动快照重新入队被拒绝，租约已被接管: user={}",
                        user_id,
                    )
    finally:
        stop_heartbeat.set()
        await heartbeat_task
    return completed


async def _heartbeat_interaction_lease(
    *,
    redis: RedisManager,
    user_id: str,
    owner_token: str,
    lease_seconds: int,
    stop: asyncio.Event,
    lease_lost: asyncio.Event,
) -> None:
    interval = min(
        max(1.0, lease_seconds / 3),
        _MAX_HEARTBEAT_INTERVAL_SECONDS,
    )
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            try:
                renewed = await redis.renew_interaction_summary_lease(
                    user_id=user_id,
                    owner_token=owner_token,
                    lease_seconds=lease_seconds,
                )
            except Exception:
                logger.exception(
                    "[KomariMemory] 互动总结租约续期失败: user={}",
                    user_id,
                )
                lease_lost.set()
                return
            if not renewed:
                logger.warning(
                    "[KomariMemory] 互动总结租约续期被拒绝: user={}",
                    user_id,
                )
                lease_lost.set()
                return
        else:
            return


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
        return
    if len(records) > MAX_INTERACTION_SUMMARY_RECORDS:
        omitted_count = len(records) - MAX_INTERACTION_SUMMARY_RECORDS
        records = records[-MAX_INTERACTION_SUMMARY_RECORDS:]
        logger.warning(
            "[KomariMemory] 互动快照超过处理上限，保留最近记录: omitted={} kept={}",
            omitted_count,
            len(records),
        )

    dedup_key = _build_snapshot_dedup_key(user_id, records)
    existing_event_id = await memory.get_interaction_event_id_by_dedup_key(dedup_key)
    if existing_event_id is not None:
        logger.info(
            "[KomariMemory] 互动快照已落库，跳过重复总结: user={} event_id={}",
            user_id,
            existing_event_id,
        )
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
        dedup_key=dedup_key,
    )


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
    """注册跨群互动事件总结任务；禁用时保留休眠任务以支持即时启用。"""
    config = get_config()
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
    if config.global_interaction_enabled:
        logger.info("[KomariMemory] 跨群互动事件总结任务已注册")
    else:
        logger.info("[KomariMemory] 跨群互动事件总结任务已休眠注册，启用后即时生效")


def unregister_interaction_event_task() -> None:
    """取消跨群互动事件总结任务。"""
    for job_id in (_JOB_ID, _DAILY_JOB_ID):
        try:
            scheduler.remove_job(job_id)
        except JobLookupError:
            logger.debug("[KomariMemory] 跨群互动事件任务不存在，无需取消: {}", job_id)
        except Exception:
            logger.exception("[KomariMemory] 跨群互动事件任务取消失败: {}", job_id)
