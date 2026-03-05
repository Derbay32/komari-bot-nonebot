"""Komari Memory 后台总结任务。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

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


def _profile_to_entity_rows(profile: dict[str, Any]) -> list[dict[str, Any]]:
    traits = profile.get("traits")
    if not isinstance(traits, dict):
        return []

    rows: list[dict[str, Any]] = []
    user_id = str(profile.get("user_id") or "")
    for key, raw in traits.items():
        if not isinstance(raw, dict):
            continue
        key_text = str(key).strip()
        value_text = str(raw.get("value", "")).strip()
        if not key_text or not value_text:
            continue
        rows.append(
            {
                "user_id": user_id,
                "key": key_text,
                "value": value_text,
                "category": str(raw.get("category", "general")),
                "importance": int(raw.get("importance", 3)),
            }
        )
    return rows


def _merge_entities_into_profile(
    base_profile: dict[str, Any],
    *,
    display_name: str,
    entities: list[dict[str, Any]],
) -> dict[str, Any]:
    profile = dict(base_profile)
    profile["version"] = 1
    profile["display_name"] = display_name

    traits_raw = profile.get("traits")
    traits = dict(traits_raw) if isinstance(traits_raw, dict) else {}
    for entity in entities:
        key = str(entity.get("key", "")).strip()
        value = str(entity.get("value", "")).strip()
        if not key or not value:
            continue
        traits[key] = {
            "value": value,
            "category": str(entity.get("category", "general")),
            "importance": int(entity.get("importance", 3)),
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
    existing_entities_rows: list[dict[str, Any]] = []
    existing_interaction_rows: list[dict[str, Any]] = []

    for uid in participants:
        profile = await memory.get_user_profile(user_id=uid, group_id=group_id)
        if profile is not None:
            existing_profiles[uid] = profile
            existing_entities_rows.extend(_profile_to_entity_rows(profile))

        interaction = await memory.get_interaction_history(user_id=uid, group_id=group_id)
        if interaction is not None:
            existing_interactions[uid] = interaction
            existing_interaction_rows.append(
                {"user_id": uid, "value": json.dumps(interaction, ensure_ascii=False)}
            )

    result = await summarize_conversation(
        messages_buffer,
        config,
        existing_entities=existing_entities_rows,
        existing_interactions=existing_interaction_rows,
    )

    summary = str(result.get("summary", "")).strip()
    importance = int(result.get("importance", 3))
    entities = result.get("entities", [])
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

    entities_by_user: dict[str, list[dict[str, Any]]] = {}
    if isinstance(entities, list):
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            uid = str(entity.get("user_id", "")).strip()
            if not uid:
                continue
            entities_by_user.setdefault(uid, []).append(entity)

    interactions_by_user: dict[str, dict[str, Any]] = {}
    if isinstance(user_interactions, list):
        for interaction in user_interactions:
            if not isinstance(interaction, dict):
                continue
            uid = str(interaction.get("user_id", "")).strip()
            if not uid:
                continue
            interactions_by_user[uid] = interaction

    target_users = set(participants) | set(entities_by_user) | set(interactions_by_user)
    for uid in sorted(target_users):
        display_name = character_binding.get_character_name(
            user_id=uid,
            fallback_nickname=nickname_map.get(uid),
        )
        base_profile = existing_profiles.get(uid) or _default_profile(
            user_id=uid,
            display_name=display_name,
        )
        merged_profile = _merge_entities_into_profile(
            base_profile,
            display_name=display_name,
            entities=entities_by_user.get(uid, []),
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
        "[KomariMemory] 群组 %s 总结完成: conversation_id=%s users=%s raw_entities=%s",
        group_id,
        conversation_id,
        len(target_users),
        len(entities) if isinstance(entities, list) else 0,
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
        logger.info("[KomariMemory] 总结定时任务已取消")
    except Exception:
        pass
