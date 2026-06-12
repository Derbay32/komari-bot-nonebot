"""用户画像 Agent 的 Redis 暂存层。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from komari_bot.common.profile_operations import (
    CommitResult,
    PreviewResult,
    ProfileConflict,
    ProfileDiffItem,
    ProfileOperation,
    StageResult,
    apply_profile_operations,
    build_profile_traits_patch,
    normalize_profile_operations,
)

from ..repositories.entity_repository import (
    UserProfileBatchUpsertError,
    UserProfileConcurrentUpdateError,
)
from ..services.redis_keys import RedisKeys
from .profile_snapshot import get_snapshot_group_profile, get_snapshot_group_profiles

if TYPE_CHECKING:
    import redis.asyncio as aioredis

    from ..repositories.entity_repository import (
        UserProfileBatchUpsertResult,
        UserProfileTraitsPatchPayload,
    )
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
        snapshot_key: str | None = None,
    ) -> None:
        self._redis = redis
        self._session_id = session_id
        self._group_id = group_id
        self._memory = memory
        self._ttl_seconds = ttl_seconds
        self._key = RedisKeys.staging_profile(session_id)
        self._snapshot_key = snapshot_key
        self._snapshot_conflicted_user_ids: set[str] = set()

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
        profile, source = await self._load_profile(user_id)
        if include_staged:
            staged = [item for item in await self._load_items() if item.user_id == user_id]
            profile = apply_profile_operations(
                profile,
                staged,
                user_id=user_id,
                display_name=str((profile or {}).get("display_name", "")).strip() or user_id,
            )
            source = f"{source}+staged"
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
            source=source,
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

        affected_user_ids = {item.user_id for item in diff}
        conflicts = await self._detect_snapshot_conflicts(affected_user_ids)
        if conflicts:
            self._snapshot_conflicted_user_ids.update(
                str(conflict["user_id"]) for conflict in conflicts
            )
            return CommitResult(
                status="conflict",
                committed_count=0,
                summary="检测到画像在 Agent 会话期间被外部修改，请重新读取并整合后再次提交",
                conflicts=conflicts,
            )

        payloads: list[UserProfileTraitsPatchPayload] = []
        for user_id in sorted(affected_user_ids):
            user_diff = [item for item in diff if item.user_id == user_id]
            base_profile, source = await self._load_profile(user_id)
            display_name = str((base_profile or {}).get("display_name", "")).strip() or user_id
            snapshot_updated_at = (
                (base_profile or {}).get("updated_at") if source == "snapshot" else None
            )
            set_traits, delete_keys = build_profile_traits_patch(user_diff)
            payloads.append(
                {
                    "user_id": user_id,
                    "group_id": self._group_id,
                    "display_name": display_name,
                    "set_traits": set_traits,
                    "delete_keys": delete_keys,
                    "updated_at": datetime.now(UTC).isoformat(),
                    "snapshot_updated_at": snapshot_updated_at,
                    "importance": 4,
                }
            )

        try:
            batch_result = await self._memory.batch_upsert_user_profiles(payloads)
        except UserProfileBatchUpsertError as exc:
            return await self._handle_batch_errors(
                diff=diff,
                batch_result=exc.result,
            )
        except UserProfileConcurrentUpdateError as exc:
            self._snapshot_conflicted_user_ids.add(exc.user_id)
            return CommitResult(
                status="conflict",
                committed_count=0,
                summary="画像提交时检测到并发更新，请重新读取并整合后再次提交",
                conflicts=[
                    {
                        "user_id": exc.user_id,
                        "group_id": exc.group_id,
                        "reason": "画像提交时检测到并发更新",
                    }
                ],
            )

        if batch_result.conflicts:
            return await self._handle_batch_conflicts(
                diff=diff,
                batch_result=batch_result,
            )

        await self.discard()
        return CommitResult(
            status="committed",
            committed_count=len(diff),
            changed_user_ids=affected_user_ids,
            summary=f"已写入 {len(diff)} 条画像操作",
        )

    async def _handle_batch_conflicts(
        self,
        *,
        diff: list[ProfileDiffItem],
        batch_result: UserProfileBatchUpsertResult,
    ) -> CommitResult:
        """处理仓库层部分提交后的乐观锁冲突。"""
        conflict_user_ids = {conflict.user_id for conflict in batch_result.conflicts}
        upserted_user_ids = {row.user_id for row in batch_result.upserted}
        self._snapshot_conflicted_user_ids.update(conflict_user_ids)

        remaining_diff = [item for item in diff if item.user_id in conflict_user_ids]
        if remaining_diff:
            await self._save_items(remaining_diff)
        else:
            await self.discard()

        committed_count = sum(1 for item in diff if item.user_id in upserted_user_ids)
        return CommitResult(
            status="conflict",
            committed_count=committed_count,
            changed_user_ids=upserted_user_ids,
            summary=f"已写入 {committed_count} 条画像操作，{len(conflict_user_ids)} 个用户存在并发冲突",
            conflicts=[
                {
                    "user_id": conflict.user_id,
                    "group_id": conflict.group_id,
                    "snapshot_updated_at": conflict.snapshot_updated_at,
                    "reason": "画像提交时检测到并发更新",
                }
                for conflict in batch_result.conflicts
            ],
        )

    async def _handle_batch_errors(
        self,
        *,
        diff: list[ProfileDiffItem],
        batch_result: UserProfileBatchUpsertResult,
    ) -> CommitResult:
        """处理仓库层部分提交后的单条数据库错误。"""
        error_user_ids = {error.user_id for error in batch_result.errors}
        conflict_user_ids = {conflict.user_id for conflict in batch_result.conflicts}
        retry_user_ids = error_user_ids | conflict_user_ids
        upserted_user_ids = {row.user_id for row in batch_result.upserted}
        self._snapshot_conflicted_user_ids.update(conflict_user_ids)

        remaining_diff = [item for item in diff if item.user_id in retry_user_ids]
        if remaining_diff:
            await self._save_items(remaining_diff)
        else:
            await self.discard()

        committed_count = sum(1 for item in diff if item.user_id in upserted_user_ids)
        issues: list[dict[str, Any]] = [
            {
                "user_id": error.user_id,
                "group_id": error.group_id,
                "reason": f"画像提交时数据库写入失败: {error.message}",
            }
            for error in batch_result.errors
        ]
        issues.extend(
            {
                "user_id": conflict.user_id,
                "group_id": conflict.group_id,
                "snapshot_updated_at": conflict.snapshot_updated_at,
                "reason": "画像提交时检测到并发更新",
            }
            for conflict in batch_result.conflicts
        )

        return CommitResult(
            status="partial_error",
            committed_count=committed_count,
            changed_user_ids=upserted_user_ids,
            summary=f"已写入 {committed_count} 条画像操作，{len(error_user_ids)} 个用户写入失败",
            conflicts=issues,
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
        snapshot_profiles: dict[str, dict[str, Any]] = {}
        if self._snapshot_key:
            snapshot_profiles = await get_snapshot_group_profiles(
                self._redis,
                self._snapshot_key,
                user_ids - self._snapshot_conflicted_user_ids,
            )
        for user_id in sorted(user_ids):
            profile = snapshot_profiles.get(user_id)
            if profile is None:
                profile = await self._memory.get_user_profile(user_id=user_id, group_id=self._group_id)
            traits = (profile or {}).get("traits")
            existing[user_id] = dict(traits) if isinstance(traits, dict) else {}
        return existing

    async def _load_profile(self, user_id: str) -> tuple[dict[str, Any] | None, str]:
        if self._snapshot_key and user_id not in self._snapshot_conflicted_user_ids:
            snapshot_profile = await get_snapshot_group_profile(
                self._redis,
                self._snapshot_key,
                user_id,
            )
            if snapshot_profile is not None:
                return snapshot_profile, "snapshot"
        profile = await self._memory.get_user_profile(user_id=user_id, group_id=self._group_id)
        return profile, "database"

    async def _detect_snapshot_conflicts(
        self,
        user_ids: set[str],
    ) -> list[dict[str, Any]]:
        if not self._snapshot_key:
            return []
        snapshot_profiles = await get_snapshot_group_profiles(
            self._redis,
            self._snapshot_key,
            user_ids,
        )
        conflicts: list[dict[str, Any]] = []
        for user_id in sorted(user_ids):
            snapshot_updated_at = _parse_datetime(
                (snapshot_profiles.get(user_id) or {}).get("updated_at")
            )
            if snapshot_updated_at is None:
                continue
            pg_profile = await self._memory.get_user_profile(user_id=user_id, group_id=self._group_id)
            pg_updated_at_raw = (pg_profile or {}).get("updated_at")
            pg_updated_at = _parse_datetime(pg_updated_at_raw)
            if pg_updated_at is None or pg_updated_at <= snapshot_updated_at:
                continue
            conflicts.append(
                {
                    "user_id": user_id,
                    "snapshot_updated_at": snapshot_updated_at.isoformat(),
                    "pg_updated_at": pg_updated_at.isoformat(),
                    "reason": "画像在 Agent 会话期间被外部修改",
                    "pg_current_traits": _compact_conflict_traits(
                        (pg_profile or {}).get("traits")
                    ),
                }
            )
        return conflicts


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


def _compact_conflict_traits(raw_traits: Any) -> dict[str, dict[str, Any]]:
    """提取冲突返回中给 LLM 看的当前 PG 画像摘要。"""
    if not isinstance(raw_traits, Mapping):
        return {}
    compacted: dict[str, dict[str, Any]] = {}
    for key, raw in raw_traits.items():
        if not isinstance(raw, Mapping):
            continue
        value = str(raw.get("value", "")).strip()
        if not value:
            continue
        compacted[str(key)] = {
            "value": value,
            "category": str(raw.get("category", "general")).strip() or "general",
            "importance": int(raw.get("importance", 3) or 3),
        }
    return compacted


def _trait_value(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    value = str(raw.get("value", "")).strip()
    return value or None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _stage_summary(*, staged_count: int, conflicts: list[ProfileConflict]) -> str:
    if not conflicts:
        return f"已暂存 {staged_count} 条操作"
    if staged_count:
        return f"已暂存 {staged_count} 条，{len(conflicts)} 条冲突"
    return "无操作被暂存"
