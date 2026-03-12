"""Komari Memory 后台总结任务。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from apscheduler.jobstores.base import JobLookupError
from nonebot import logger
from nonebot.plugin import require
from nonebot_plugin_apscheduler import scheduler

from ..core.retry import retry_async
from ..services.config_interface import get_config
from ..services.llm_service import summarize_conversation

character_binding = require("character_binding")

if TYPE_CHECKING:
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


def _default_interaction(*, user_id: str, display_name: str) -> dict[str, Any]:
    return {
        "version": 1,
        "user_id": user_id,
        "display_name": display_name,
        "file_type": "用户的近期对鞠行为备忘录",
        "description": "暂无互动记录",
        "records": [],
        "summary": "",
        "updated_at": _now_iso(),
    }


def _merge_traits_into_profile(
    base_profile: dict[str, Any],
    *,
    display_name: str,
    traits_payload: list[dict[str, Any]],
) -> dict[str, Any]:
    profile = dict(base_profile)
    profile["version"] = 1
    profile["display_name"] = display_name

    traits_raw = profile.get("traits")
    traits = dict(traits_raw) if isinstance(traits_raw, dict) else {}
    for trait in traits_payload:
        key = str(trait.get("key", "")).strip()
        value = str(trait.get("value", "")).strip()
        if not key or not value:
            continue
        try:
            importance = int(trait.get("importance", 3))
        except (TypeError, ValueError):
            importance = 3
        traits[key] = {
            "value": value,
            "category": str(trait.get("category", "general")),
            "importance": max(1, min(5, importance)),
            "updated_at": _now_iso(),
        }

    profile["traits"] = traits
    profile["updated_at"] = _now_iso()
    return profile


def _normalize_interaction(
    raw: dict[str, Any] | None,
    *,
    user_id: str,
    display_name: str,
) -> dict[str, Any]:
    if raw is None:
        return _default_interaction(user_id=user_id, display_name=display_name)
    interaction = dict(raw)
    interaction["version"] = 1
    interaction["user_id"] = user_id
    interaction["display_name"] = display_name
    interaction["file_type"] = str(
        interaction.get("file_type", "用户的近期对鞠行为备忘录")
    )
    interaction["description"] = str(interaction.get("description", ""))
    records = interaction.get("records")
    interaction["records"] = records if isinstance(records, list) else []
    interaction["summary"] = str(interaction.get("summary", ""))
    interaction["updated_at"] = _now_iso()
    return interaction


@retry_async(max_attempts=3, base_delay=1.0)
async def summary_worker_task(
    redis: RedisManager,
    memory: MemoryService,
) -> None:
    """定期检查并触发总结。"""
    group_ids = await redis.get_active_groups()
    if not group_ids:
        return

    logger.debug("[KomariMemory] 检查 %s 个群组的总结任务...", len(group_ids))
    for group_id in group_ids:
        if await redis.should_trigger_summary(group_id):
            await perform_summary(group_id, redis, memory)


async def perform_summary(
    group_id: str,
    redis: RedisManager,
    memory: MemoryService,
) -> None:
    """执行群组的对话总结。"""
    logger.info("[KomariMemory] 开始总结群组 %s 的对话", group_id)
    config = get_config()

    messages_buffer = await redis.get_buffer(group_id, limit=config.summary_max_messages)
    if not messages_buffer:
        logger.warning("[KomariMemory] 群组 %s 消息缓冲为空", group_id)
        return

    participants = list({msg.user_id for msg in messages_buffer if not msg.is_bot})
    nickname_map: dict[str, str] = {}
    for msg in messages_buffer:
        if msg.is_bot:
            continue
        if msg.user_id not in nickname_map and msg.user_nickname:
            nickname_map[msg.user_id] = msg.user_nickname

    existing_profiles: dict[str, dict[str, Any]] = {}
    existing_interactions: dict[str, dict[str, Any]] = {}

    for uid in participants:
        profile = await memory.get_user_profile(user_id=uid, group_id=group_id)
        if profile is not None:
            existing_profiles[uid] = profile

        interaction = await memory.get_interaction_history(user_id=uid, group_id=group_id)
        if interaction is not None:
            existing_interactions[uid] = interaction

    result = await summarize_conversation(
        messages_buffer,
        config,
        existing_profiles=list(existing_profiles.values()),
        existing_interactions=list(existing_interactions.values()),
    )

    summary = str(result.get("summary", "")).strip()
    importance = int(result.get("importance", 3))
    user_profiles = result.get("user_profiles", [])
    user_interactions = result.get("user_interactions", [])

    if not summary:
        logger.warning("[KomariMemory] 群组 %s 总结为空，跳过存储", group_id)
        return

    conversation_id = await memory.store_conversation(
        group_id=group_id,
        summary=summary,
        participants=participants,
        importance_initial=max(1, min(5, importance)),
    )

    profiles_by_user: dict[str, dict[str, Any]] = {}
    if isinstance(user_profiles, list):
        for profile in user_profiles:
            if not isinstance(profile, dict):
                continue
            uid = str(profile.get("user_id", "")).strip()
            if not uid:
                continue
            profiles_by_user[uid] = profile

    interactions_by_user: dict[str, dict[str, Any]] = {}
    if isinstance(user_interactions, list):
        for interaction in user_interactions:
            if not isinstance(interaction, dict):
                continue
            uid = str(interaction.get("user_id", "")).strip()
            if not uid:
                continue
            interactions_by_user[uid] = interaction

    target_users = set(participants) | set(profiles_by_user) | set(interactions_by_user)
    for uid in sorted(target_users):
        model_display_name = ""
        profile_payload = profiles_by_user.get(uid)
        if profile_payload is not None:
            model_display_name = str(profile_payload.get("display_name", "")).strip()
        display_name = character_binding.get_character_name(
            user_id=uid,
            fallback_nickname=nickname_map.get(uid) or model_display_name,
        )
        base_profile = existing_profiles.get(uid) or _default_profile(
            user_id=uid,
            display_name=display_name,
        )
        traits_payload = (
            profile_payload.get("traits", [])
            if isinstance(profile_payload, dict)
            else []
        )
        merged_profile = _merge_traits_into_profile(
            base_profile,
            display_name=display_name,
            traits_payload=traits_payload if isinstance(traits_payload, list) else [],
        )
        await memory.upsert_user_profile(
            user_id=uid,
            group_id=group_id,
            profile=merged_profile,
            importance=4,
        )

        raw_interaction = interactions_by_user.get(uid) or existing_interactions.get(uid)
        merged_interaction = _normalize_interaction(
            raw_interaction,
            user_id=uid,
            display_name=display_name,
        )
        await memory.upsert_interaction_history(
            user_id=uid,
            group_id=group_id,
            interaction=merged_interaction,
            importance=5,
        )

    await redis.reset_message_count(group_id)
    await redis.reset_tokens(group_id)
    await redis.delete_buffer(group_id)
    await redis.update_last_summary(group_id)

    logger.info(
        "[KomariMemory] 群组 %s 总结完成: conversation_id=%s users=%s raw_profiles=%s",
        group_id,
        conversation_id,
        len(target_users),
        len(user_profiles) if isinstance(user_profiles, list) else 0,
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
