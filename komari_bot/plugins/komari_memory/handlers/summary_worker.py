"""Komari Memory 后台总结任务。

TSK-151 起：生命周期编排（认领/心跳/门控 ack/恢复/dead-letter/孤儿接管循环）整段
移入 services/conversation_processing_lifecycle.ConversationProcessingLifecycle，
本文件只保留无状态业务处理器（ConversationSummaryProcessor）、观测 provider
（SummaryCollectorProvider）与调度注册壳。
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast

from apscheduler.jobstores.base import JobLookupError
from nonebot import logger
from nonebot.plugin import require
from nonebot_plugin_apscheduler import scheduler

from ..agent import run_profile_agent
from ..services.config_interface import get_config
from ..services.conversation_processing import InvalidChunkLedgerError
from ..services.conversation_processing_lifecycle import (
    ConversationProcessingLifecycle,
    ProcessingSession,
)
from ..services.llm_service import summarize_conversation
from ..services.message_chunking import (
    MessageProcessingChunk,
    build_chunk_manifest,
    chunk_messages_for_memory_processing,
    collect_chunk_participants,
    format_message_line,
)

require("character_binding")
require("agent_run_logger")

from komari_bot.plugins import agent_run_logger


class InvalidSummaryResultError(RuntimeError):
    """对话总结结果不包含任何可持久化记忆。"""

    def __init__(self) -> None:
        super().__init__("对话总结结果不包含任何可持久化记忆")


class IncompleteProfileAgentError(RuntimeError):
    """画像 Agent 未完成提交，当前 processing 快照不得确认。"""

    def __init__(self, status: object) -> None:
        super().__init__(f"画像 Agent 未完成提交: status={status}")


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
    from komari_bot.plugins.agent_run_logger.diagnostic import AgentRunStatus

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


class SummaryCollectorProvider:
    """为总结任务创建与三态收尾观测 collector（F25）。

    create 时记住 group/processing_key，供 success finalize 构造 ack output。
    """

    def __init__(self) -> None:
        self._group_id: str | None = None
        self._processing_key: str | None = None

    def create(self, group_id: str, processing_key: str) -> object | None:
        self._group_id = group_id
        self._processing_key = processing_key
        return agent_run_logger.create_collector(
            run_type="scheduled_summary",
            task_kind="conversation_processing",
            trace_id=f"conversation-summary-{processing_key}",
            input_data={"group_id": group_id, "processing_key": processing_key},
        )

    async def finalize(
        self,
        collector: Any,
        *,
        status: str,
        error: BaseException | None = None,
    ) -> bool:
        if status == "success":
            return await agent_run_logger.finalize_collector(
                collector,
                status=cast("AgentRunStatus", status),
                output={
                    "group_id": self._group_id,
                    "processing_key": self._processing_key,
                    "acknowledged": True,
                },
                skip_if_no_calls=True,
            )
        return await agent_run_logger.finalize_collector(
            collector,
            status=cast("AgentRunStatus", status),
            error=error,
            skip_if_no_calls=True,
        )


class ConversationSummaryProcessor:
    """无状态总结业务处理器：消费一次 processing 快照的 session。

    重试与生命周期编排归 module（ConversationProcessingLifecycle），本类只有
    单次尝试语义：账本读写只经 session.ledger，消息只经 session.messages，
    业务侧唯一保留的 redis 用途是画像 agent 的 raw client（redis.redis）。
    """

    def __init__(self, redis: Any, memory: Any) -> None:
        # 依赖类型放宽为 Any：业务 seam 只消费 redis.redis 与
        # memory.store_conversation 最小形状，测试以协议级 fake 注入
        #（pyright 1.1.409 对可变协议成员双向互检的限制同 module 的处理）。
        self._redis = redis
        self._memory = memory

    async def process(self, session: ProcessingSession) -> None:
        """围绕同一个 processing 快照执行一次总结流程。"""
        config = get_config()

        messages_buffer = session.messages
        if not messages_buffer:
            logger.warning("[KomariMemory] 群组 {} 消息缓冲为空", session.group_id)
            return

        collector = session.collector
        if collector is not None:
            collector.set_input_data(
                {
                    "group_id": session.group_id,
                    "processing_key": session.processing_key,
                    "messages": messages_buffer,
                }
            )

        snapshot_fingerprint = _build_processing_snapshot_fingerprint(
            session.group_id,
            messages_buffer,
        )
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
        # F18：manifest 门控内化于 StorageChunkLedger——initialize_manifest 读回
        # 逐字节比对，不一致抛 InvalidChunkLedgerError，先于任何 LLM 调用。
        await session.ledger.initialize_manifest(manifest_json)

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
            cached_summary = await session.ledger.get(summary_field)
            if cached_summary is None:
                summary_result = await summarize_conversation(
                    chunk_messages,
                    config,
                    participants=participants,
                    display_name_map=nickname_map,
                    collector=collector,
                )
                normalized_memories = _normalize_summary_memories(summary_result)
                await session.ledger.set(
                    summary_field,
                    value=_encode_cached_summary_memories(normalized_memories),
                )
            else:
                normalized_memories = _decode_cached_summary_memories(cached_summary)

            profile_field = f"profile:{chunk.chunk_id}"
            cached_profile = await session.ledger.get(profile_field)
            if cached_profile is None:
                conversation_text = "\n".join(
                    format_message_line(message, bot_nickname=config.bot_nickname)
                    for message in chunk_messages
                )
                profile_result = await run_profile_agent(
                    redis=self._redis.redis,
                    memory=self._memory,
                    group_id=session.group_id,
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
                await session.ledger.set(
                    profile_field,
                    value=_canonical_json(profile_state),
                )
            else:
                profile_state = _decode_json_object(cached_profile)
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
            if await session.ledger.get(store_field):
                continue
            for index, content, importance in normalized_memories:
                conversation_id = await self._memory.store_conversation(
                    group_id=session.group_id,
                    summary=content,
                    participants=participants,
                    importance_initial=importance,
                    dedup_key=_build_summary_dedup_key(
                        session.group_id,
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
                        session.group_id,
                        chunk.index,
                        index,
                    )
                    continue
                conversation_ids.append(conversation_id)
            await session.ledger.set(
                store_field,
                value=_canonical_json({"version": 1, "status": "completed"}),
            )

        logger.info(
            "[KomariMemory] 群组 {} 总结完成: chunks={} conversation_ids={} "
            "profile_changed_users={} raw_profile_operations={}",
            session.group_id,
            len(chunks),
            conversation_ids,
            len(profile_changed_user_ids - all_bot_user_ids),
            profile_committed_count,
        )


async def summary_worker_task(
    redis: RedisManager,
    memory: MemoryService,
) -> None:
    """定期检查并触发总结。"""
    lifecycle = ConversationProcessingLifecycle(
        storage=redis,
        collector_provider=SummaryCollectorProvider(),
    )
    await lifecycle.run_worker_cycle(
        lambda: ConversationSummaryProcessor(redis, memory)
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_json_object(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise InvalidChunkLedgerError("invalid_json") from exc
    if not isinstance(decoded, dict):
        raise InvalidChunkLedgerError("not_object")
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
    payload = _decode_json_object(value)
    raw_memories = payload.get("memories")
    if not isinstance(raw_memories, list):
        raise InvalidChunkLedgerError("memories_not_array")
    memories: list[tuple[int, str, int]] = []
    for item in raw_memories:
        if not isinstance(item, dict):
            raise InvalidChunkLedgerError("memory_not_object")
        try:
            index = int(item["index"])
            importance = int(item["importance"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidChunkLedgerError("invalid_memory_fields") from exc
        content = str(item.get("content", "")).strip()
        if not content:
            raise InvalidChunkLedgerError("empty_memory_content")
        memories.append((index, content, max(1, min(5, importance))))
    if not memories:
        raise InvalidChunkLedgerError("empty_memories")
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
