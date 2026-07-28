"""画像 Agent 基线快照 Redis HASH 操作。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from nonebot import logger

_META_FIELD = "__meta__"


async def set_profile_snapshot(
    redis: Any,
    snapshot_key: str,
    *,
    token: str,
    group_id: str,
    profiles: dict[str, dict[str, Any]],
    ttl_seconds: int,
) -> None:
    """写入画像基线快照 HASH。"""
    meta = {
        "token": token,
        "group_id": group_id,
        "created_at": datetime.now(UTC).isoformat(),
    }
    mapping: dict[str, str] = {
        _META_FIELD: json.dumps(meta, ensure_ascii=False),
    }
    for user_id, profile in profiles.items():
        mapping[str(user_id)] = json.dumps(profile, ensure_ascii=False)

    pipe = redis.pipeline()
    pipe.hset(snapshot_key, mapping=mapping)
    pipe.expire(snapshot_key, ttl_seconds)
    await pipe.execute()


async def get_snapshot_group_profile(
    redis: Any,
    snapshot_key: str,
    user_id: str,
) -> dict[str, Any] | None:
    """读取单个用户的画像基线快照。"""
    raw = await redis.hget(snapshot_key, user_id)
    return _decode_profile(raw, user_id=user_id)


async def get_snapshot_group_profiles(
    redis: Any,
    snapshot_key: str,
    user_ids: set[str],
) -> dict[str, dict[str, Any]]:
    """批量读取多个用户的画像基线快照。"""
    ordered_user_ids = sorted(user_ids)
    if not ordered_user_ids:
        return {}

    raw_values = await redis.hmget(snapshot_key, ordered_user_ids)
    profiles: dict[str, dict[str, Any]] = {}
    for user_id, raw in zip(ordered_user_ids, raw_values, strict=False):
        profile = _decode_profile(raw, user_id=user_id)
        if profile is not None:
            profiles[user_id] = profile
    return profiles


async def delete_profile_snapshot(redis: Any, snapshot_key: str) -> None:
    """删除画像基线快照。"""
    await redis.delete(snapshot_key)


def _decode_profile(raw: Any, *, user_id: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        profile = json.loads(raw.decode() if isinstance(raw, bytes) else str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.warning("[KomariMemory] 画像快照 JSON 解析失败: user_id={}", user_id)
        return None
    return profile if isinstance(profile, dict) else None
