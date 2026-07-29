"""Komari Memory 后台总结任务。"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Never
from uuid import uuid4

from apscheduler.jobstores.base import JobLookupError
from nonebot import logger
from nonebot.plugin import require
from nonebot_plugin_apscheduler import scheduler

from ..agent import run_profile_agent
from ..core.retry import retry_async
from ..services.config_interface import get_config
from ..services.conversation_processing import ConversationLeaseLostError
from ..services.llm_service import summarize_conversation
from ..services.message_chunking import (
    MessageProcessingChunk,
    build_chunk_manifest,
    chunk_messages_for_memory_processing,
    collect_chunk_participants,
    format_message_line,
)

character_binding = require("character_binding")
agent_run_logger_plugin = require("agent_run_logger")


class InvalidSummaryResultError(RuntimeError):
    """对话总结结果不包含任何可持久化记忆。"""

    def __init__(self) -> None:
        super().__init__("对话总结结果不包含任何可持久化记忆")


class IncompleteProfileAgentError(RuntimeError):
    """画像 Agent 未完成提交，当前 processing 快照不得确认。"""

    def __init__(self, status: object) -> None:
        super().__init__(f"画像 Agent 未完成提交: status={status}")


class InvalidChunkLedgerError(RuntimeError):
    """processing 快照的持久化分块账本损坏或不匹配。"""

    def __init__(self, code: str, *, field: str = "") -> None:
        details = {
            "manifest_mismatch": "manifest 与当前 processing 快照不一致",
            "invalid_json": f"{field} 不是合法 JSON",
            "not_object": f"{field} 不是 JSON 对象",
            "memories_not_array": "summary.memories 不是数组",
            "memory_not_object": "summary.memories 含非对象条目",
            "invalid_memory_fields": "summary 条目的序号或重要性无效",
            "empty_memory_content": "summary 条目正文为空",
            "empty_memories": "summary.memories 为空",
        }
        detail = details.get(code, code)
        super().__init__(f"对话分块账本无效: {detail}")


def _raise_conversation_lease_lost(processing_key: str) -> Never:
    raise ConversationLeaseLostError(processing_key)


def _raise_invalid_chunk_ledger(
    code: str,
    *,
    field: str = "",
    cause: Exception | None = None,
) -> Never:
    error = InvalidChunkLedgerError(code, field=field)
    if cause is not None:
        raise error from cause
    raise error


def _normalize_summary_memories(
    summary_result: object,
) -> list[tuple[int, str, int]]:
    """提取可持久化记忆；空结果必须视为失败，不能确认消费快照。"""
    if not isinstance(summary_result, dict):
        raise InvalidSummaryResultError

    raw_memories = summary_result.get("memories")
    if not isinstance(raw_memories, list):
        raise InvalidSummaryResultError

    memories: list[tuple[int, str, int]] = []
    for index, memory_item in enumerate(raw_memories):
        if not isinstance(memory_item, dict):
            continue
        content = str(memory_item.get("content", "")).strip()
        if not content:
            continue
        try:
            importance = int(memory_item.get("importance", 3))
        except (TypeError, ValueError):
            importance = 3
        memories.append((index, content, max(1, min(5, importance))))

    if not memories:
        raise InvalidSummaryResultError
    return memories


def _resolve_message_time_range(messages: list[Any]) -> tuple[datetime, datetime]:
    """从真实消息时间戳计算 PostgreSQL ``TIMESTAMP`` 使用的 UTC 时间范围。"""
    timestamps: list[datetime] = []
    for message in messages:
        try:
            timestamp = float(message.timestamp)
            if not math.isfinite(timestamp) or timestamp < 0:
                continue
            value = datetime.fromtimestamp(timestamp, UTC).replace(tzinfo=None)
        except (AttributeError, OSError, OverflowError, TypeError, ValueError):
            continue
        timestamps.append(value)

    if not timestamps:
        now = datetime.now(UTC).replace(tzinfo=None)
        return now, now
    return min(timestamps), max(timestamps)


if TYPE_CHECKING:
    from komari_bot.plugins.agent_run_logger.diagnostic import AgentRunCollector

    from ..services.memory_service import MemoryService
    from ..services.redis_manager import RedisManager


def _build_processing_snapshot_fingerprint(
    group_id: str,
    messages_buffer: list[Any],
) -> str:
    """根据 processing 快照内容生成稳定指纹。"""
    payload = {
        "group_id": group_id,
        "messages": [
            {
                "index": index,
                "user_id": str(getattr(message, "user_id", "")),
                "user_nickname": str(getattr(message, "user_nickname", "")),
                "content": str(getattr(message, "content", "")),
                "timestamp": getattr(message, "timestamp", None),
                "message_id": str(getattr(message, "message_id", "")),
                "is_bot": bool(getattr(message, "is_bot", False)),
            }
            for index, message in enumerate(messages_buffer)
        ],
    }
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return sha256(raw.encode("utf-8")).hexdigest()


def _build_summary_dedup_key(
    group_id: str,
    snapshot_fingerprint: str,
    index: int,
    *,
    chunk_index: int = 0,
) -> str:
    """生成单条总结记忆的幂等键。"""
    memory_index = str(index) if chunk_index == 0 else f"{chunk_index}:{index}"
    raw = f"summary:{group_id}:{snapshot_fingerprint}:{memory_index}"
    return sha256(raw.encode("utf-8")).hexdigest()


def _collect_bot_user_ids(
    *,
    messages_buffer: list[Any],
) -> set[str]:
    bot_user_ids: set[str] = set()

    for msg in messages_buffer:
        if not getattr(msg, "is_bot", False):
            continue

        user_id = str(getattr(msg, "user_id", "")).strip()
        if user_id:
            bot_user_ids.add(user_id)

    return bot_user_ids


async def summary_worker_task(
    redis: RedisManager,
    memory: MemoryService,
) -> None:
    """定期检查并触发总结。"""
    try:
        orphaned = await redis.get_orphaned_conversation_processing_keys()
    except Exception as error:
        logger.error(
            "[KomariMemory] 扫描可接管的对话 processing 快照失败: error_type={}",
            type(error).__name__,
        )
        orphaned = []
    resumed_groups: set[str] = set()
    for group_id, processing_key in orphaned:
        try:
            await perform_summary(
                group_id,
                redis,
                memory,
                existing_processing_key=processing_key,
            )
        except Exception as error:
            logger.error(
                "[KomariMemory] 接管遗留对话快照失败: group={} key={} "
                "error_type={}",
                group_id,
                processing_key,
                type(error).__name__,
            )
        resumed_groups.add(group_id)

    group_ids = await redis.get_active_groups()
    group_ids = [group_id for group_id in group_ids if group_id not in resumed_groups]
    if not group_ids:
        return

    logger.debug("[KomariMemory] 检查 {} 个群组的总结任务...", len(group_ids))
    for group_id in group_ids:
        try:
            if await redis.should_trigger_summary(group_id):
                await perform_summary(group_id, redis, memory)
        except Exception as error:
            logger.error(
                "[KomariMemory] 群组总结失败: group={} error_type={}",
                group_id,
                type(error).__name__,
            )


async def _stop_summary_attempt(
    *,
    heartbeat_stop: asyncio.Event,
    heartbeat_task: asyncio.Task[None],
    processing_task: asyncio.Future[None],
) -> None:
    """停止当前处理与续租任务，并完整回收它们的异常。"""
    heartbeat_stop.set()
    if not processing_task.done():
        processing_task.cancel()
    await asyncio.gather(
        processing_task,
        heartbeat_task,
        return_exceptions=True,
    )


async def perform_summary(
    group_id: str,
    redis: RedisManager,
    memory: MemoryService,
    *,
    existing_processing_key: str | None = None,
) -> None:
    """执行群组的对话总结。"""
    logger.info("[KomariMemory] 开始总结群组 {} 的对话", group_id)
    owner_token = f"summary-{uuid4().hex}"
    if existing_processing_key is None:
        claim = await redis.claim_conversation_buffer(
            group_id,
            owner_token,
            uuid4().hex[:8],
        )
    else:
        claim = await redis.claim_existing_conversation_processing(
            group_id,
            existing_processing_key,
            owner_token,
        )
    if claim.status != "claimed" or not claim.processing_key:
        logger.info(
            "[KomariMemory] 群组 {} 对话快照未认领: status={}",
            group_id,
            claim.status,
        )
        return
    processing_key = claim.processing_key
    collector = agent_run_logger_plugin.create_collector(
        run_type="scheduled_summary",
        task_kind="conversation_processing",
        trace_id=f"conversation-summary-{processing_key}",
        input_data={"group_id": group_id, "processing_key": processing_key},
    )
    heartbeat_stop = asyncio.Event()
    lease_lost = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        _renew_conversation_lease(
            redis=redis,
            group_id=group_id,
            processing_key=processing_key,
            owner_token=owner_token,
            stop=heartbeat_stop,
            lease_lost=lease_lost,
        )
    )
    processing_task = asyncio.ensure_future(
        _perform_summary_from_processing(
            group_id,
            redis,
            memory,
            processing_key,
            owner_token,
            collector,
        )
    )
    lease_lost_wait = asyncio.create_task(lease_lost.wait())

    try:
        await asyncio.wait(
            {processing_task, lease_lost_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if lease_lost.is_set():
            processing_task.cancel()
            await asyncio.gather(processing_task, return_exceptions=True)
            _raise_conversation_lease_lost(processing_key)
        await processing_task
        heartbeat_stop.set()
        await heartbeat_task
        if lease_lost.is_set() or not await redis.renew_processing_conversation_lease(
            group_id,
            processing_key,
            owner_token,
        ):
            _raise_conversation_lease_lost(processing_key)
        if not await redis.ack_processing_conversation_buffer(
            group_id,
            processing_key,
            owner_token,
        ):
            _raise_conversation_lease_lost(processing_key)
    except asyncio.CancelledError as error:
        await agent_run_logger_plugin.finalize_collector(
            collector,
            status="cancelled",
            error=error,
            skip_if_no_calls=True,
        )
        await _stop_summary_attempt(
            heartbeat_stop=heartbeat_stop,
            heartbeat_task=heartbeat_task,
            processing_task=processing_task,
        )
        try:
            restored = await asyncio.shield(
                redis.restore_processing_conversation_buffer(
                    group_id,
                    processing_key,
                    owner_token,
                )
            )
        except Exception as cleanup_error:
            logger.warning(
                "[KomariMemory] 取消总结后的快照恢复失败: group={} key={} "
                "error_type={}",
                group_id,
                processing_key,
                type(cleanup_error).__name__,
            )
        else:
            if not restored:
                logger.warning(
                    "[KomariMemory] 取消总结后的快照恢复被拒绝，当前 worker 已失去 "
                    "owner: group={} key={}",
                    group_id,
                    processing_key,
                )
        raise
    except Exception as error:
        await agent_run_logger_plugin.finalize_collector(
            collector,
            status="error",
            error=error,
            skip_if_no_calls=True,
        )
        await _stop_summary_attempt(
            heartbeat_stop=heartbeat_stop,
            heartbeat_task=heartbeat_task,
            processing_task=processing_task,
        )
        if not isinstance(error, ConversationLeaseLostError):
            dead_lettered = False
            try:
                dead_lettered = (
                    await redis.dead_letter_processing_conversation_buffer(
                        group_id,
                        processing_key,
                        owner_token,
                        failure_code=type(error).__name__,
                        attempt_count=3,
                    )
                )
            except Exception as cleanup_error:
                logger.warning(
                    "[KomariMemory] 对话快照移入 dead-letter 失败: group={} key={} "
                    "error_type={}",
                    group_id,
                    processing_key,
                    type(cleanup_error).__name__,
                )
            if not dead_lettered:
                try:
                    restored = await redis.restore_processing_conversation_buffer(
                        group_id,
                        processing_key,
                        owner_token,
                    )
                except Exception as cleanup_error:
                    logger.warning(
                        "[KomariMemory] dead-letter 失败后的快照恢复失败: group={} "
                        "key={} error_type={}",
                        group_id,
                        processing_key,
                        type(cleanup_error).__name__,
                    )
                else:
                    if not restored:
                        logger.warning(
                            "[KomariMemory] dead-letter 失败后的快照恢复被拒绝，"
                            "当前 worker 已失去 owner: group={} key={}",
                            group_id,
                            processing_key,
                        )
        raise
    else:
        await agent_run_logger_plugin.finalize_collector(
            collector,
            status="success",
            output={
                "group_id": group_id,
                "processing_key": processing_key,
                "acknowledged": True,
            },
            skip_if_no_calls=True,
        )
    finally:
        heartbeat_stop.set()
        lease_lost_wait.cancel()
        await asyncio.gather(lease_lost_wait, return_exceptions=True)

    await redis.update_last_summary(group_id)


async def _renew_conversation_lease(
    *,
    redis: RedisManager,
    group_id: str,
    processing_key: str,
    owner_token: str,
    stop: asyncio.Event,
    lease_lost: asyncio.Event,
) -> None:
    """按租约三分之一周期续租；连续失败时阻止当前 worker 提交。"""
    interval = max(1.0, redis.config.conversation_processing_lease_seconds / 3)
    consecutive_errors = 0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
        else:
            return
        try:
            renewed = await redis.renew_processing_conversation_lease(
                group_id,
                processing_key,
                owner_token,
            )
        except Exception as error:
            consecutive_errors += 1
            logger.warning(
                "[KomariMemory] 对话 processing 续租异常: group={} key={} "
                "failures={} error_type={}",
                group_id,
                processing_key,
                consecutive_errors,
                type(error).__name__,
            )
            if consecutive_errors < 2:
                continue
        else:
            if renewed:
                consecutive_errors = 0
                continue
        lease_lost.set()
        return


@retry_async(max_attempts=3, base_delay=1.0)
async def _perform_summary_from_processing(
    group_id: str,
    redis: RedisManager,
    memory: MemoryService,
    processing_key: str,
    owner_token: str,
    collector: AgentRunCollector | None = None,
) -> None:
    """围绕同一个 processing 快照执行可重试的总结流程。"""
    config = get_config()

    messages_buffer = await redis.get_processing_conversation_buffer(
        group_id,
        processing_key,
        owner_token,
    )
    if not messages_buffer:
        logger.warning("[KomariMemory] 群组 {} 消息缓冲为空", group_id)
        return

    if collector is not None:
        collector.set_input_data(
            {
                "group_id": group_id,
                "processing_key": processing_key,
                "messages": messages_buffer,
            }
        )

    snapshot_fingerprint = _build_processing_snapshot_fingerprint(group_id, messages_buffer)
    chunks = chunk_messages_for_memory_processing(
        messages_buffer,
        snapshot_fingerprint=snapshot_fingerprint,
        bot_nickname=config.bot_nickname,
    )
    manifest = build_chunk_manifest(
        snapshot_fingerprint=snapshot_fingerprint,
        chunks=chunks,
    )
    manifest_json = _canonical_json(manifest)
    stored_manifest_json = await redis.initialize_conversation_chunk_manifest(
        group_id=group_id,
        processing_key=processing_key,
        owner_token=owner_token,
        manifest_json=manifest_json,
    )
    if _decode_json_object(stored_manifest_json, field="manifest") != manifest:
        _raise_invalid_chunk_ledger("manifest_mismatch")

    chunk_summaries: list[
        tuple[MessageProcessingChunk, list[tuple[int, str, int]], list[str]]
    ] = []
    profile_changed_user_ids: set[str] = set()
    profile_committed_count = 0
    all_bot_user_ids: set[str] = set()
    for chunk in chunks:
        chunk_messages = list(chunk.messages)
        participants, nickname_map = collect_chunk_participants(chunk.messages)
        bot_user_ids = _collect_bot_user_ids(messages_buffer=chunk_messages)
        all_bot_user_ids.update(bot_user_ids)
        summary_field = f"summary:{chunk.chunk_id}"
        cached_summary = await redis.get_conversation_chunk_state(
            group_id=group_id,
            processing_key=processing_key,
            owner_token=owner_token,
            field=summary_field,
        )
        if cached_summary is None:
            summary_result = await summarize_conversation(
                chunk_messages,
                config,
                participants=participants,
                display_name_map=nickname_map,
                collector=collector,
            )
            normalized_memories = _normalize_summary_memories(summary_result)
            await redis.set_conversation_chunk_state(
                group_id=group_id,
                processing_key=processing_key,
                owner_token=owner_token,
                field=summary_field,
                value=_encode_cached_summary_memories(normalized_memories),
            )
        else:
            normalized_memories = _decode_cached_summary_memories(cached_summary)

        profile_field = f"profile:{chunk.chunk_id}"
        cached_profile = await redis.get_conversation_chunk_state(
            group_id=group_id,
            processing_key=processing_key,
            owner_token=owner_token,
            field=profile_field,
        )
        if cached_profile is None:
            conversation_text = "\n".join(
                format_message_line(message, bot_nickname=config.bot_nickname)
                for message in chunk_messages
            )
            profile_result = await run_profile_agent(
                redis=redis.redis,
                memory=memory,
                group_id=group_id,
                conversation_text=conversation_text,
                participants=participants,
                display_name_map=nickname_map,
                bot_user_ids=bot_user_ids,
                config=config,
                trace_id=f"profile-agent-{chunk.chunk_id[:12]}",
                collector=collector,
            )
            if profile_result.status not in {"committed", "nothing_to_commit"}:
                raise IncompleteProfileAgentError(profile_result.status)
            profile_state = {
                "version": 1,
                "status": profile_result.status,
                "changed_user_ids": sorted(profile_result.changed_user_ids),
                "committed_count": profile_result.committed_count,
            }
            await redis.set_conversation_chunk_state(
                group_id=group_id,
                processing_key=processing_key,
                owner_token=owner_token,
                field=profile_field,
                value=_canonical_json(profile_state),
            )
        else:
            profile_state = _decode_json_object(cached_profile, field=profile_field)
        profile_changed_user_ids.update(
            str(user_id) for user_id in profile_state.get("changed_user_ids", [])
        )
        profile_committed_count += int(profile_state.get("committed_count", 0))
        chunk_summaries.append((chunk, normalized_memories, participants))

    conversation_ids: list[int] = []
    for chunk, normalized_memories, participants in chunk_summaries:
        chunk_start_time, chunk_end_time = _resolve_message_time_range(
            list(chunk.messages)
        )
        store_field = f"store:{chunk.chunk_id}"
        if await redis.get_conversation_chunk_state(
            group_id=group_id,
            processing_key=processing_key,
            owner_token=owner_token,
            field=store_field,
        ):
            continue
        for index, content, importance in normalized_memories:
            conversation_id = await memory.store_conversation(
                group_id=group_id,
                summary=content,
                participants=participants,
                importance_initial=importance,
                dedup_key=_build_summary_dedup_key(
                    group_id,
                    snapshot_fingerprint,
                    index,
                    chunk_index=chunk.index,
                ),
                start_time=chunk_start_time,
                end_time=chunk_end_time,
            )
            if conversation_id is None:
                logger.info(
                    "[KomariMemory] 群组 {} 总结记忆已存在，跳过重复写入: chunk={} index={}",
                    group_id,
                    chunk.index,
                    index,
                )
                continue
            conversation_ids.append(conversation_id)
        await redis.set_conversation_chunk_state(
            group_id=group_id,
            processing_key=processing_key,
            owner_token=owner_token,
            field=store_field,
            value=_canonical_json({"version": 1, "status": "completed"}),
        )

    logger.info(
        "[KomariMemory] 群组 {} 总结完成: chunks={} conversation_ids={} profile_changed_users={} raw_profile_operations={}",
        group_id,
        len(chunks),
        conversation_ids,
        len(profile_changed_user_ids - all_bot_user_ids),
        profile_committed_count,
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_json_object(value: str, *, field: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        _raise_invalid_chunk_ledger("invalid_json", field=field, cause=exc)
    if not isinstance(decoded, dict):
        _raise_invalid_chunk_ledger("not_object", field=field)
    return decoded


def _encode_cached_summary_memories(memories: list[tuple[int, str, int]]) -> str:
    return _canonical_json(
        {
            "version": 1,
            "memories": [
                {"index": index, "content": content, "importance": importance}
                for index, content, importance in memories
            ],
        }
    )


def _decode_cached_summary_memories(value: str) -> list[tuple[int, str, int]]:
    payload = _decode_json_object(value, field="summary")
    raw_memories = payload.get("memories")
    if not isinstance(raw_memories, list):
        _raise_invalid_chunk_ledger("memories_not_array")
    memories: list[tuple[int, str, int]] = []
    for item in raw_memories:
        if not isinstance(item, dict):
            _raise_invalid_chunk_ledger("memory_not_object")
        try:
            index = int(item["index"])
            importance = int(item["importance"])
        except (KeyError, TypeError, ValueError) as exc:
            _raise_invalid_chunk_ledger("invalid_memory_fields", cause=exc)
        content = str(item.get("content", "")).strip()
        if not content:
            _raise_invalid_chunk_ledger("empty_memory_content")
        memories.append((index, content, max(1, min(5, importance))))
    if not memories:
        _raise_invalid_chunk_ledger("empty_memories")
    return memories


def register_summary_task(
    redis: RedisManager,
    memory: MemoryService,
) -> None:
    """注册总结定时任务。"""
    scheduler.add_job(
        summary_worker_task,
        "interval",
        minutes=5,
        args=[redis, memory],
        id="komari_memory_summary_worker",
        replace_existing=True,
    )
    logger.info("[KomariMemory] 总结定时任务已注册")


def unregister_summary_task() -> None:
    """取消注册总结定时任务。"""
    try:
        scheduler.remove_job("komari_memory_summary_worker")
    except JobLookupError:
        logger.debug("[KomariMemory] 总结定时任务不存在，无需取消")
    except Exception:
        logger.exception("[KomariMemory] 总结定时任务取消失败")
    else:
        logger.info("[KomariMemory] 总结定时任务已取消")
