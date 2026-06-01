"""Komari Memory 后台总结任务。"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from apscheduler.jobstores.base import JobLookupError
from nonebot import logger
from nonebot.plugin import require
from nonebot_plugin_apscheduler import scheduler

from ..agent import run_profile_agent
from ..core.retry import retry_async
from ..services.config_interface import get_config
from ..services.llm_service import summarize_conversation
from ..services.profile_compaction import (
    LoggerLike,
    compact_profile_with_llm,
    count_profile_traits,
    profile_json_length,
)

character_binding = require("character_binding")
llm_provider = require("llm_provider")

if TYPE_CHECKING:
    from ..config_schema import KomariMemoryConfigSchema
    from ..services.memory_service import MemoryService
    from ..services.redis_manager import RedisManager


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _default_profile(*, user_id: str, display_name: str) -> dict[str, Any]:
    return {
        "version": 1,
        "user_id": user_id,
        "display_name": display_name,
        "traits": {},
        "updated_at": _now_iso(),
    }


def _format_summary_message_line(
    message: Any,
    config: KomariMemoryConfigSchema,
) -> str:
    if getattr(message, "is_bot", False):
        return f"[bot] {config.bot_nickname}: {message.content}"
    return f"[user_id:{message.user_id}] {message.user_nickname}: {message.content}"


async def _refresh_character_binding_if_needed(*, group_id: str) -> bool:
    refresh_func = getattr(character_binding, "refresh_if_file_updated", None)
    if not callable(refresh_func):
        return False

    try:
        result = refresh_func()
        changed = await result if inspect.isawaitable(result) else result
    except Exception:
        logger.exception("[KomariMemory] binding 热刷新失败: group={}", group_id)
        return False

    if bool(changed):
        logger.info("[KomariMemory] 检测到 binding 更新，已在总结前刷新: group={}", group_id)
    return bool(changed)


def _refresh_existing_context_display_names(
    *,
    group_id: str,
    participants: list[str],
    nickname_map: dict[str, str],
    existing_profiles: dict[str, dict[str, Any]],
) -> None:
    updated_profiles = 0

    for uid in participants:
        display_name = str(
            character_binding.get_character_name(
                user_id=uid,
                fallback_nickname=nickname_map.get(uid),
            )
        ).strip() or nickname_map.get(uid, "").strip() or uid

        profile = existing_profiles.get(uid)
        if profile is not None and str(profile.get("display_name", "")).strip() != display_name:
            normalized_profile = dict(profile)
            normalized_profile["user_id"] = uid
            normalized_profile["display_name"] = display_name
            existing_profiles[uid] = normalized_profile
            updated_profiles += 1

    if updated_profiles:
        logger.info(
            "[KomariMemory] 总结前已按 binding 对齐画像 display_name: group={} profiles={}",
            group_id,
            updated_profiles,
        )


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


async def _enforce_profile_trait_limit(
    *,
    group_id: str,
    user_id: str,
    base_profile: dict[str, Any],
    merged_profile: dict[str, Any],
    config: KomariMemoryConfigSchema,
) -> dict[str, Any]:
    merged_trait_count = count_profile_traits(merged_profile)
    if merged_trait_count <= config.profile_trait_limit:
        return merged_profile

    base_trait_count = count_profile_traits(base_profile)
    trace_id = f"profilecap-{uuid4().hex[:8]}"
    logger.warning(
        "[KomariMemory] 用户画像超过上限，准备压缩: trace_id={} group={} user={} base_traits={} merged_traits={} base_chars={} merged_chars={} limit={}",
        trace_id,
        group_id,
        user_id,
        base_trait_count,
        merged_trait_count,
        profile_json_length(base_profile),
        profile_json_length(merged_profile),
        config.profile_trait_limit,
    )

    try:
        compacted_profile = await compact_profile_with_llm(
            profile=merged_profile,
            config=config,
            llm_generate_text=llm_provider.generate_text,
            trace_id=trace_id,
            source="summary_worker",
            log=cast("LoggerLike", logger),
        )
    except Exception:
        logger.exception(
            "[KomariMemory] 用户画像压缩失败，回退旧画像: trace_id={} group={} user={} fallback_traits={} fallback_chars={}",
            trace_id,
            group_id,
            user_id,
            base_trait_count,
            profile_json_length(base_profile),
        )
        return base_profile

    logger.info(
        "[KomariMemory] 用户画像压缩完成: trace_id={} group={} user={} before_traits={} after_traits={} before_chars={} after_chars={}",
        trace_id,
        group_id,
        user_id,
        merged_trait_count,
        count_profile_traits(compacted_profile),
        profile_json_length(merged_profile),
        profile_json_length(compacted_profile),
    )
    return compacted_profile


@retry_async(max_attempts=3, base_delay=1.0)
async def summary_worker_task(
    redis: RedisManager,
    memory: MemoryService,
) -> None:
    """定期检查并触发总结。"""
    group_ids = await redis.get_active_groups()
    if not group_ids:
        return

    logger.debug("[KomariMemory] 检查 {} 个群组的总结任务...", len(group_ids))
    for group_id in group_ids:
        if await redis.should_trigger_summary(group_id):
            await perform_summary(group_id, redis, memory)


async def perform_summary(
    group_id: str,
    redis: RedisManager,
    memory: MemoryService,
) -> None:
    """执行群组的对话总结。"""
    logger.info("[KomariMemory] 开始总结群组 {} 的对话", group_id)
    config = get_config()

    messages_buffer = await redis.get_buffer(group_id, limit=config.summary_max_buffer_size)
    if not messages_buffer:
        logger.warning("[KomariMemory] 群组 {} 消息缓冲为空", group_id)
        return

    await _refresh_character_binding_if_needed(group_id=group_id)

    participants = list({msg.user_id for msg in messages_buffer if not msg.is_bot})
    nickname_map: dict[str, str] = {}
    for msg in messages_buffer:
        if msg.is_bot:
            continue
        if msg.user_id not in nickname_map and msg.user_nickname:
            nickname_map[msg.user_id] = msg.user_nickname

    existing_profiles: dict[str, dict[str, Any]] = {}

    for uid in participants:
        profile = await memory.get_user_profile(user_id=uid, group_id=group_id)
        if profile is not None:
            existing_profiles[uid] = profile

    _refresh_existing_context_display_names(
        group_id=group_id,
        participants=participants,
        nickname_map=nickname_map,
        existing_profiles=existing_profiles,
    )

    bot_user_ids = _collect_bot_user_ids(
        messages_buffer=messages_buffer,
    )
    conversation_text = "\n".join(
        _format_summary_message_line(message, config) for message in messages_buffer
    )
    summary_result: dict[str, Any] | None = None
    try:
        summary_result = await summarize_conversation(
            messages_buffer,
            config,
            participants=participants,
            display_name_map=nickname_map,
        )
    except Exception:
        logger.exception("[KomariMemory] 群组 {} 对话总结失败，继续执行画像 Agent", group_id)

    conversation_ids: list[int] = []
    if summary_result is not None:
        memories = summary_result.get("memories", [])
        if not isinstance(memories, list) or not memories:
            logger.warning("[KomariMemory] 群组 {} 总结无记忆输出，跳过存储", group_id)
        else:
            for memory_item in memories:
                if not isinstance(memory_item, dict):
                    continue
                content = str(memory_item.get("content", "")).strip()
                if not content:
                    continue
                try:
                    importance = int(memory_item.get("importance", 3))
                except (TypeError, ValueError):
                    importance = 3
                conversation_id = await memory.store_conversation(
                    group_id=group_id,
                    summary=content,
                    participants=participants,
                    importance_initial=max(1, min(5, importance)),
                )
                conversation_ids.append(conversation_id)

    profile_result = await run_profile_agent(
        redis=redis.redis,
        memory=memory,
        group_id=group_id,
        conversation_text=conversation_text,
        participants=participants,
        display_name_map=nickname_map,
        bot_user_ids=bot_user_ids,
        config=config,
        trace_id=f"profile-agent-{uuid4().hex[:8]}",
    )

    for uid in sorted(profile_result.changed_user_ids - bot_user_ids):
        profile = await memory.get_user_profile(user_id=uid, group_id=group_id)
        if profile is None:
            continue
        compacted_profile = await _enforce_profile_trait_limit(
            group_id=group_id,
            user_id=uid,
            base_profile=existing_profiles.get(uid) or profile,
            merged_profile=profile,
            config=config,
        )
        if compacted_profile != profile:
            await memory.upsert_user_profile(
                user_id=uid,
                group_id=group_id,
                profile=compacted_profile,
                importance=4,
            )

    await redis.delete_buffer(group_id)
    await redis.update_last_summary(group_id)

    logger.info(
        "[KomariMemory] 群组 {} 总结完成: conversation_ids={} profile_changed_users={} raw_profile_operations={}",
        group_id,
        conversation_ids,
        len(profile_result.changed_user_ids - bot_user_ids),
        profile_result.committed_count,
    )


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
