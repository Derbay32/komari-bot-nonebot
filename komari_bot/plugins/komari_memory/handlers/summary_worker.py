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


_MAX_INTERACTION_RECORDS = 6
_BOT_UID_ALIASES = frozenset({"bot", "assistant", "system", "self", "[bot]"})


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


def _normalize_identity_key(value: str) -> str:
    return value.strip().casefold()


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
    existing_interactions: dict[str, dict[str, Any]],
) -> None:
    updated_profiles = 0
    updated_interactions = 0

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

        interaction = existing_interactions.get(uid)
        if (
            interaction is not None
            and str(interaction.get("display_name", "")).strip() != display_name
        ):
            normalized_interaction = dict(interaction)
            normalized_interaction["user_id"] = uid
            normalized_interaction["display_name"] = display_name
            existing_interactions[uid] = normalized_interaction
            updated_interactions += 1

    if updated_profiles or updated_interactions:
        logger.info(
            "[KomariMemory] 总结前已按 binding 对齐上下文 display_name: group={} profiles={} interactions={}",
            group_id,
            updated_profiles,
            updated_interactions,
        )


def _collect_bot_identities(
    *,
    messages_buffer: list[Any],
    config: KomariMemoryConfigSchema,
) -> tuple[set[str], set[str]]:
    bot_user_ids: set[str] = set()
    bot_display_names = {
        _normalize_identity_key(str(config.bot_nickname)),
        *{
            _normalize_identity_key(str(alias))
            for alias in config.bot_aliases
            if str(alias).strip()
        },
    }

    for msg in messages_buffer:
        if not getattr(msg, "is_bot", False):
            continue

        user_id = str(getattr(msg, "user_id", "")).strip()
        if user_id:
            bot_user_ids.add(user_id)

        candidates = {
            str(getattr(msg, "user_nickname", "")).strip(),
            str(
                character_binding.get_character_name(
                    user_id=user_id,
                    fallback_nickname=getattr(msg, "user_nickname", ""),
                )
            ).strip(),
        }
        for candidate in candidates:
            if candidate:
                bot_display_names.add(_normalize_identity_key(candidate))

    bot_display_names.discard("")
    return bot_user_ids, bot_display_names


def _should_skip_bot_summary_entry(
    *,
    raw_uid: str,
    display_name: str,
    participant_set: set[str],
    bot_user_ids: set[str],
    bot_display_names: set[str],
    group_id: str,
    source: str,
) -> bool:
    normalized_uid = str(raw_uid).strip()
    normalized_display_name = str(display_name).strip()

    if normalized_uid in bot_user_ids or _normalize_identity_key(normalized_uid) in _BOT_UID_ALIASES:
        logger.warning(
            "[KomariMemory] 总结结果丢弃机器人条目: group={} source={} raw_uid={} display_name={}",
            group_id,
            source,
            normalized_uid or "-",
            normalized_display_name or "-",
        )
        return True

    if (
        normalized_display_name
        and _normalize_identity_key(normalized_display_name) in bot_display_names
        and normalized_uid not in participant_set
    ):
        logger.warning(
            "[KomariMemory] 总结结果按机器人名称丢弃条目: group={} source={} raw_uid={} display_name={}",
            group_id,
            source,
            normalized_uid or "-",
            normalized_display_name,
        )
        return True

    return False


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
    interaction["records"] = _normalize_interaction_records(interaction.get("records"))
    interaction["summary"] = str(interaction.get("summary", "")).strip()
    interaction["updated_at"] = _now_iso()
    return interaction


def _normalize_interaction_records(raw_records: Any) -> list[dict[str, str]]:
    normalized_records: list[dict[str, str]] = []
    if not isinstance(raw_records, list):
        return normalized_records

    for raw_record in raw_records:
        if not isinstance(raw_record, dict):
            continue
        normalized_record = {
            "event": str(raw_record.get("event", "")).strip(),
            "result": str(raw_record.get("result", "")).strip(),
            "emotion": str(raw_record.get("emotion", "")).strip(),
        }
        if not any(normalized_record.values()):
            continue
        normalized_records.append(normalized_record)

    return normalized_records


def _merge_interaction_update(
    base_interaction: dict[str, Any] | None,
    interaction_update: dict[str, Any] | None,
    *,
    user_id: str,
    display_name: str,
) -> dict[str, Any]:
    merged_interaction = _normalize_interaction(
        base_interaction,
        user_id=user_id,
        display_name=display_name,
    )
    if not isinstance(interaction_update, dict):
        return merged_interaction

    appended_records = _normalize_interaction_records(interaction_update.get("records"))
    if appended_records:
        existing_records = merged_interaction.get("records", [])
        merged_interaction["records"] = [
            *(existing_records if isinstance(existing_records, list) else []),
            *appended_records,
        ][-_MAX_INTERACTION_RECORDS:]

    updated_summary = str(interaction_update.get("summary", "")).strip()
    if updated_summary:
        merged_interaction["summary"] = updated_summary

    merged_interaction["updated_at"] = _now_iso()
    return merged_interaction


def _merge_user_operation_payloads(
    base_payload: dict[str, Any] | None,
    incoming_payload: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(base_payload or {})
    merged["user_id"] = str(
        incoming_payload.get("user_id", merged.get("user_id", ""))
    ).strip()

    display_name = str(incoming_payload.get("display_name", "")).strip()
    if display_name:
        merged["display_name"] = display_name

    operations: list[dict[str, Any]] = []
    existing_operations = merged.get("operations")
    if isinstance(existing_operations, list):
        operations.extend(op for op in existing_operations if isinstance(op, dict))

    incoming_operations = incoming_payload.get("operations")
    if isinstance(incoming_operations, list):
        operations.extend(op for op in incoming_operations if isinstance(op, dict))

    merged["operations"] = operations
    return merged


def _apply_profile_operations(
    base_profile: dict[str, Any],
    *,
    user_id: str,
    display_name: str,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    profile = dict(base_profile)
    profile["version"] = 1
    profile["user_id"] = user_id
    resolved_display_name = (
        str(display_name).strip()
        or str(profile.get("display_name", "")).strip()
        or user_id
    )
    traits_raw = profile.get("traits")
    traits = dict(traits_raw) if isinstance(traits_raw, dict) else {}

    for operation in operations:
        if not isinstance(operation, dict):
            continue
        op = str(operation.get("op", "")).strip()
        field = str(operation.get("field", "")).strip()

        if field != "trait":
            continue

        key = str(operation.get("key", "")).strip()
        if not key:
            continue
        if op == "delete":
            traits.pop(key, None)
            continue

        value = str(operation.get("value", "")).strip()
        if not value:
            continue
        if op == "add" and key in traits:
            continue
        try:
            importance = int(operation.get("importance", 3))
        except (TypeError, ValueError):
            importance = 3
        traits[key] = {
            "value": value,
            "category": str(operation.get("category", "general")).strip() or "general",
            "importance": max(1, min(5, importance)),
            "updated_at": _now_iso(),
        }

    profile["display_name"] = resolved_display_name
    profile["traits"] = traits
    profile["updated_at"] = _now_iso()
    return profile


def _apply_interaction_operations(
    base_interaction: dict[str, Any] | None,
    *,
    user_id: str,
    display_name: str,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    merged_interaction = _normalize_interaction(
        base_interaction,
        user_id=user_id,
        display_name=display_name,
    )

    records = merged_interaction.get("records", [])
    normalized_records = list(records) if isinstance(records, list) else []

    for operation in operations:
        if not isinstance(operation, dict):
            continue
        op = str(operation.get("op", "")).strip()
        field = str(operation.get("field", "")).strip()

        if field in {"file_type", "description", "summary"}:
            if op == "delete":
                merged_interaction[field] = (
                    "用户的近期对鞠行为备忘录" if field == "file_type" else ""
                )
                continue
            value = str(operation.get("value", "")).strip()
            if not value:
                continue
            if op == "add" and str(merged_interaction.get(field, "")).strip():
                continue
            if op in {"add", "replace"}:
                merged_interaction[field] = value
            continue

        if field != "record":
            continue

        candidate_records = _normalize_interaction_records([operation.get("value")])
        if not candidate_records:
            continue
        candidate_record = candidate_records[0]
        if op == "delete":
            normalized_records = [
                record for record in normalized_records if record != candidate_record
            ]
            continue
        if op == "add" and candidate_record not in normalized_records:
            normalized_records.append(candidate_record)

    merged_interaction["records"] = normalized_records[-_MAX_INTERACTION_RECORDS:]
    merged_interaction["updated_at"] = _now_iso()
    return merged_interaction


def _build_display_name_uid_map(
    *,
    participants: list[str],
    nickname_map: dict[str, str],
) -> dict[str, str]:
    display_name_candidates: dict[str, set[str]] = {}
    for uid in participants:
        for candidate in {
            str(nickname_map.get(uid, "")).strip(),
            str(
                character_binding.get_character_name(
                    user_id=uid,
                    fallback_nickname=nickname_map.get(uid),
                )
            ).strip(),
        }:
            if not candidate:
                continue
            display_name_candidates.setdefault(candidate, set()).add(uid)

    return {
        display_name: next(iter(uid_set))
        for display_name, uid_set in display_name_candidates.items()
        if len(uid_set) == 1
    }


def _resolve_summary_uid(
    *,
    raw_uid: str,
    display_name: str,
    participant_set: set[str],
    display_name_uid_map: dict[str, str],
    uid_alias_map: dict[str, str],
    group_id: str,
    source: str,
) -> str | None:
    normalized_uid = str(raw_uid).strip()
    if not normalized_uid:
        return None

    cached_uid = uid_alias_map.get(normalized_uid)
    if cached_uid:
        return cached_uid

    if normalized_uid in participant_set:
        uid_alias_map[normalized_uid] = normalized_uid
        return normalized_uid

    normalized_display_name = str(display_name).strip()
    if normalized_display_name:
        mapped_uid = display_name_uid_map.get(normalized_display_name)
        if mapped_uid:
            uid_alias_map[normalized_uid] = mapped_uid
            logger.warning(
                "[KomariMemory] 总结结果出现异常 uid，按名称重定向: group={} source={} raw_uid={} display_name={} canonical_uid={}",
                group_id,
                source,
                normalized_uid,
                normalized_display_name,
                mapped_uid,
            )
            return mapped_uid

    logger.warning(
        "[KomariMemory] 总结结果丢弃未知 uid: group={} source={} raw_uid={} display_name={}",
        group_id,
        source,
        normalized_uid,
        normalized_display_name or "-",
    )
    return None


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

    messages_buffer = await redis.get_buffer(group_id, limit=config.summary_max_messages)
    if not messages_buffer:
        logger.warning("[KomariMemory] 群组 {} 消息缓冲为空", group_id)
        return

    await _refresh_character_binding_if_needed(group_id=group_id)

    participants = list({msg.user_id for msg in messages_buffer if not msg.is_bot})
    participant_set = set(participants)
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

    _refresh_existing_context_display_names(
        group_id=group_id,
        participants=participants,
        nickname_map=nickname_map,
        existing_profiles=existing_profiles,
        existing_interactions=existing_interactions,
    )

    result = await summarize_conversation(
        messages_buffer,
        config,
        existing_profiles=list(existing_profiles.values()),
        existing_interactions=list(existing_interactions.values()),
    )

    summary = str(result.get("summary", "")).strip()
    importance = int(result.get("importance", 3))
    user_profile_operations = result.get("user_profile_operations", [])
    user_interaction_operations = result.get("user_interaction_operations", [])

    if not summary:
        logger.warning("[KomariMemory] 群组 {} 总结为空，跳过存储", group_id)
        return

    conversation_id = await memory.store_conversation(
        group_id=group_id,
        summary=summary,
        participants=participants,
        importance_initial=max(1, min(5, importance)),
    )

    display_name_uid_map = _build_display_name_uid_map(
        participants=participants,
        nickname_map=nickname_map,
    )
    uid_alias_map: dict[str, str] = {}
    bot_user_ids, bot_display_names = _collect_bot_identities(
        messages_buffer=messages_buffer,
        config=config,
    )

    profile_operations_by_user: dict[str, dict[str, Any]] = {}
    if isinstance(user_profile_operations, list):
        for payload in user_profile_operations:
            if not isinstance(payload, dict):
                continue
            raw_uid = str(payload.get("user_id", "")).strip()
            display_name = str(payload.get("display_name", "")).strip()
            if _should_skip_bot_summary_entry(
                raw_uid=raw_uid,
                display_name=display_name,
                participant_set=participant_set,
                bot_user_ids=bot_user_ids,
                bot_display_names=bot_display_names,
                group_id=group_id,
                source="user_profile_operation",
            ):
                continue
            uid = _resolve_summary_uid(
                raw_uid=raw_uid,
                display_name=display_name,
                participant_set=participant_set,
                display_name_uid_map=display_name_uid_map,
                uid_alias_map=uid_alias_map,
                group_id=group_id,
                source="user_profile_operation",
            )
            if uid is None or uid in bot_user_ids:
                continue
            normalized_payload = dict(payload)
            normalized_payload["user_id"] = uid
            profile_operations_by_user[uid] = _merge_user_operation_payloads(
                profile_operations_by_user.get(uid),
                normalized_payload,
            )

    interaction_operations_by_user: dict[str, dict[str, Any]] = {}
    if isinstance(user_interaction_operations, list):
        for payload in user_interaction_operations:
            if not isinstance(payload, dict):
                continue
            raw_uid = str(payload.get("user_id", "")).strip()
            display_name = str(payload.get("display_name", "")).strip()
            if _should_skip_bot_summary_entry(
                raw_uid=raw_uid,
                display_name=display_name,
                participant_set=participant_set,
                bot_user_ids=bot_user_ids,
                bot_display_names=bot_display_names,
                group_id=group_id,
                source="user_interaction_operation",
            ):
                continue
            uid = _resolve_summary_uid(
                raw_uid=raw_uid,
                display_name=display_name,
                participant_set=participant_set,
                display_name_uid_map=display_name_uid_map,
                uid_alias_map=uid_alias_map,
                group_id=group_id,
                source="user_interaction_operation",
            )
            if uid is None or uid in bot_user_ids:
                continue
            normalized_payload = dict(payload)
            normalized_payload["user_id"] = uid
            interaction_operations_by_user[uid] = _merge_user_operation_payloads(
                interaction_operations_by_user.get(uid),
                normalized_payload,
            )

    target_users = (
        set(participants)
        | set(profile_operations_by_user)
        | set(interaction_operations_by_user)
    ) - bot_user_ids
    for uid in sorted(target_users):
        display_name = character_binding.get_character_name(
            user_id=uid,
            fallback_nickname=nickname_map.get(uid),
        )
        profile_operation_payload = profile_operations_by_user.get(uid)
        base_profile = existing_profiles.get(uid) or _default_profile(
            user_id=uid,
            display_name=display_name,
        )
        profile_operations = (
            profile_operation_payload.get("operations", [])
            if isinstance(profile_operation_payload, dict)
            else []
        )
        merged_profile = _apply_profile_operations(
            base_profile,
            user_id=uid,
            display_name=display_name,
            operations=profile_operations if isinstance(profile_operations, list) else [],
        )
        merged_profile = await _enforce_profile_trait_limit(
            group_id=group_id,
            user_id=uid,
            base_profile=base_profile,
            merged_profile=merged_profile,
            config=config,
        )
        await memory.upsert_user_profile(
            user_id=uid,
            group_id=group_id,
            profile=merged_profile,
            importance=4,
        )

        interaction_operation_payload = interaction_operations_by_user.get(uid)
        interaction_operations = (
            interaction_operation_payload.get("operations", [])
            if isinstance(interaction_operation_payload, dict)
            else []
        )
        merged_interaction = _apply_interaction_operations(
            existing_interactions.get(uid),
            user_id=uid,
            display_name=display_name,
            operations=(
                interaction_operations if isinstance(interaction_operations, list) else []
            ),
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
        "[KomariMemory] 群组 {} 总结完成: conversation_id={} users={} raw_profile_operations={}",
        group_id,
        conversation_id,
        len(target_users),
        len(user_profile_operations) if isinstance(user_profile_operations, list) else 0,
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
