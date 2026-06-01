"""用户画像 Agent 的 Redis 暂存层。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from komari_bot.common.profile_operations import (
    CommitResult,
    PreviewResult,
    ProfileConflict,
    ProfileDiffItem,
    ProfileOperation,
    StageResult,
    apply_profile_operations,
    normalize_profile_operations,
)

from ..services.redis_keys import RedisKeys

if TYPE_CHECKING:
    import redis.asyncio as aioredis

    from ..services.memory_service import MemoryService


@dataclass(frozen=True)
class ProfileReadResult:
    """read_profile 工具返回值。"""

    user_id: str
    group_id: str
    display_name: str
    traits: list[dict[str, Any]]
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "group_id": self.group_id,
            "display_name": self.display_name,
            "traits": self.traits,
            "source": self.source,
        }


class ProfileStaging:
    """基于 Redis 的画像暂存区。"""

    def __init__(
        self,
        redis: aioredis.Redis,
        session_id: str,
        group_id: str,
        memory: MemoryService,
        *,
        ttl_seconds: int,
    ) -> None:
        self._redis = redis
        self._session_id = session_id
        self._group_id = group_id
        self._memory = memory
        self._ttl_seconds = ttl_seconds
        self._key = RedisKeys.staging_profile(session_id)

    async def stage(self, operations: list[ProfileOperation]) -> StageResult:
        """校验、冲突检测并暂存画像操作。"""
        stored_items = await self._load_items()
        existing_by_user = await self._load_existing_traits_by_user(
            {operation.user_id for operation in operations} | {item.user_id for item in stored_items}
        )
        conflicts: list[ProfileConflict] = []
        accepted: list[ProfileOperation] = []
        staged_identities = {(item.user_id, item.key) for item in stored_items}

        for operation in operations:
            traits = existing_by_user.get(operation.user_id, {})
            if operation.op == "add" and operation.key in traits:
                old_value = _trait_value(traits.get(operation.key)) or ""
                conflicts.append(
                    ProfileConflict(
                        op=operation.op,
                        user_id=operation.user_id,
                        key=operation.key,
                        reason=f"key 已存在，当前值为'{old_value}'，请改用 op=set",
                    )
                )
                continue
            if operation.op in {"add", "set"} and (operation.user_id, operation.key) in staged_identities:
                conflicts.append(
                    ProfileConflict(
                        op=operation.op,
                        user_id=operation.user_id,
                        key=operation.key,
                        reason="本会话暂存区已有同名 key，请先用 preview_profile 查看并整合后再写入",
                    )
                )
                continue
            accepted.append(operation)
            staged_identities.add((operation.user_id, operation.key))

        diff = normalize_profile_operations(
            accepted,
            existing_traits_by_user=existing_by_user,
            staged_items=stored_items,
        )
        if accepted:
            await self._save_items(diff)

        status = "staged"
        if conflicts and accepted:
            status = "partial_conflict"
        elif conflicts:
            status = "conflict"
        summary = _stage_summary(staged_count=len(diff), conflicts=conflicts)
        return StageResult(
            status=status,
            staged_count=len(diff),
            diff=diff,
            conflicts=conflicts,
            summary=summary,
        )

    async def read_profile(
        self,
        user_id: str,
        keys: list[str] | None = None,
        *,
        include_staged: bool = False,
    ) -> ProfileReadResult:
        """读取当前群的用户画像。"""
        profile = await self._memory.get_user_profile(user_id=user_id, group_id=self._group_id)
        if include_staged:
            staged = [item for item in await self._load_items() if item.user_id == user_id]
            profile = apply_profile_operations(
                profile,
                staged,
                user_id=user_id,
                display_name=str((profile or {}).get("display_name", "")).strip() or user_id,
            )
        display_name = str((profile or {}).get("display_name", "")).strip() or user_id
        traits = _traits_to_list((profile or {}).get("traits"))
        if keys:
            wanted = {str(key).strip() for key in keys if str(key).strip()}
            traits = [trait for trait in traits if trait["key"] in wanted]
        return ProfileReadResult(
            user_id=user_id,
            group_id=self._group_id,
            display_name=display_name,
            traits=traits,
            source="effective" if include_staged else "database",
        )

    async def preview(self) -> PreviewResult:
        """返回暂存区当前 diff。"""
        diff = await self._load_items()
        summary = f"暂存区共 {len(diff)} 条待提交操作" if diff else "暂存区为空"
        return PreviewResult(staged_count=len(diff), diff=diff, summary=summary)

    async def commit(self) -> CommitResult:
        """把暂存区提交到 PostgreSQL。"""
        diff = await self._load_items()
        if not diff:
            return CommitResult(
                status="nothing_to_commit",
                committed_count=0,
                summary="暂存区为空，无操作可提交",
            )

        changed_user_ids: set[str] = set()
        for user_id in sorted({item.user_id for item in diff}):
            user_diff = [item for item in diff if item.user_id == user_id]
            base_profile = await self._memory.get_user_profile(user_id=user_id, group_id=self._group_id)
            display_name = str((base_profile or {}).get("display_name", "")).strip() or user_id
            merged_profile = apply_profile_operations(
                base_profile,
                user_diff,
                user_id=user_id,
                display_name=display_name,
            )
            await self._memory.upsert_user_profile(
                user_id=user_id,
                group_id=self._group_id,
                profile=merged_profile,
                importance=4,
            )
            changed_user_ids.add(user_id)

        await self.discard()
        return CommitResult(
            status="committed",
            committed_count=len(diff),
            changed_user_ids=changed_user_ids,
            summary=f"已写入 {len(diff)} 条画像操作",
        )

    async def discard(self) -> None:
        """丢弃暂存区。"""
        await self._redis.delete(self._key)

    async def _load_items(self) -> list[ProfileDiffItem]:
        raw = await self._redis.get(self._key)
        if not raw:
            return []
        try:
            data = json.loads(str(raw))
        except json.JSONDecodeError:
            return []
        operations = data.get("operations") if isinstance(data, dict) else None
        if not isinstance(operations, dict):
            return []
        items: list[ProfileDiffItem] = []
        for raw_item in operations.values():
            if not isinstance(raw_item, dict):
                continue
            item = ProfileDiffItem.from_mapping(raw_item)
            if item is not None:
                items.append(item)
        return sorted(items, key=lambda item: (item.user_id, item.key))

    async def _save_items(self, items: list[ProfileDiffItem]) -> None:
        payload = {
            "session_id": self._session_id,
            "group_id": self._group_id,
            "operations": {
                f"{item.user_id}:{item.key}": item.to_dict() for item in items
            },
        }
        await self._redis.set(
            self._key,
            json.dumps(payload, ensure_ascii=False),
            ex=self._ttl_seconds,
        )

    async def _load_existing_traits_by_user(
        self,
        user_ids: set[str],
    ) -> dict[str, dict[str, Any]]:
        existing: dict[str, dict[str, Any]] = {}
        for user_id in sorted(user_ids):
            profile = await self._memory.get_user_profile(user_id=user_id, group_id=self._group_id)
            traits = (profile or {}).get("traits")
            existing[user_id] = dict(traits) if isinstance(traits, dict) else {}
        return existing


def _traits_to_list(raw_traits: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_traits, dict):
        return []
    traits: list[dict[str, Any]] = []
    for key, raw in raw_traits.items():
        if not isinstance(raw, dict):
            continue
        value = str(raw.get("value", "")).strip()
        if not value:
            continue
        traits.append(
            {
                "key": str(key),
                "value": value,
                "category": str(raw.get("category", "general")).strip() or "general",
                "importance": int(raw.get("importance", 3) or 3),
            }
        )
    return traits


def _trait_value(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    value = str(raw.get("value", "")).strip()
    return value or None


def _stage_summary(*, staged_count: int, conflicts: list[ProfileConflict]) -> str:
    if not conflicts:
        return f"已暂存 {staged_count} 条操作"
    if staged_count:
        return f"已暂存 {staged_count} 条，{len(conflicts)} 条冲突"
    return "无操作被暂存"
