"""Komari Memory 记忆管理服务。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from nonebot.plugin import require

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ..repositories.conversation_repository import ConversationRepository
    from ..repositories.entity_repository import (
        EntityRepository,
        UserProfileBatchUpsertResult,
        UserProfileTraitsPatchPayload,
        UserProfileUpsertPayload,
    )
    from ..repositories.interaction_event_repository import InteractionEventRepository


class MemoryService:
    """记忆管理服务。"""

    def __init__(
        self,
        conversation_repo: ConversationRepository,
        entity_repo: EntityRepository,
        interaction_event_repo: InteractionEventRepository | None = None,
    ) -> None:
        """初始化记忆服务。

        Args:
            conversation_repo: 对话仓库
            entity_repo: 实体仓库
        """
        self._conversation_repo = conversation_repo
        self._entity_repo = entity_repo
        self._interaction_event_repo = interaction_event_repo
        self._embedding_plugin: Any = require("embedding_provider")

    @property
    def pg_pool(self) -> Any:
        """获取底层 PostgreSQL 连接池，供跨组件生命周期锁复用。"""
        return self._entity_repo.pg_pool

    async def store_conversation(
        self,
        group_id: str,
        summary: str,
        participants: list[str],
        importance_initial: int = 3,
        dedup_key: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> int | None:
        """存储对话总结（向量检索用 asyncpg）。

        Args:
            group_id: 群组 ID
            summary: 总结文本
            participants: 参与者列表
            importance_initial: 初始重要性评分（1-5）
            dedup_key: 幂等键，同一 processing 快照重复写入时用于去重
            start_time: 被总结消息的最早时间
            end_time: 被总结消息的最晚时间

        Returns:
            创建的对话 ID；幂等冲突时返回 None
        """
        # 业务逻辑：生成向量
        embedding = await self._embedding_plugin.embed(summary)
        normalized_start, normalized_end = self._resolve_conversation_range(
            start_time=start_time,
            end_time=end_time,
        )

        # 数据访问：委托给仓库
        return await self._conversation_repo.insert_conversation(
            group_id=group_id,
            summary=summary,
            embedding=str(embedding),
            participants=participants,
            importance_initial=importance_initial,
            dedup_key=dedup_key,
            start_time=normalized_start,
            end_time=normalized_end,
        )

    async def search_conversations(
        self,
        query: str,
        group_id: str,
        user_id: str | None = None,
        limit: int = 5,
        query_embedding: list[float] | None = None,
    ) -> list[dict[str, Any]]:
        """向量检索对话（支持用户相关性加权）。

        Args:
            query: 查询文本
            group_id: 群组 ID
            user_id: 当前用户 ID（用于加权该用户参与的记忆）
            limit: 返回数量限制
            query_embedding: 预先计算好的查询特征向量，若提供则跳过模型推理

        Returns:
            检索结果列表，包含 summary, similarity 等
        """
        # 业务逻辑：生成查询向量
        query_vec = (
            query_embedding
            if query_embedding is not None
            else await self._embedding_plugin.embed(query)
        )

        # rerank 启用时多取候选
        rerank_enabled = self._embedding_plugin.is_rerank_enabled()
        fetch_limit = limit * 3 if rerank_enabled else limit

        # 数据访问：委托给仓库（传递 user_id 用于加权）
        results = await self._conversation_repo.search_by_similarity(
            embedding=str(query_vec),
            group_id=group_id,
            user_id=user_id,
            limit=fetch_limit,
            touch_results=not rerank_enabled,
        )

        if rerank_enabled and results:
            documents = [r["summary"] for r in results]
            reranked = await self._embedding_plugin.rerank(
                query, documents, top_n=limit
            )
            results = [results[rr.index] for rr in reranked]
            await self._conversation_repo.touch_conversations(
                [int(result["id"]) for result in results],
            )

        return results[:limit]

    async def list_conversations(
        self,
        *,
        limit: int,
        offset: int,
        group_id: str | None = None,
        participant: str | None = None,
        query: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """分页获取对话记忆。"""
        return await self._conversation_repo.list_conversations(
            limit=limit,
            offset=offset,
            group_id=group_id,
            participant=participant,
            query=query,
        )

    async def get_conversation_entry(
        self,
        conversation_id: int,
    ) -> dict[str, Any] | None:
        """按 ID 获取对话记忆。"""
        return await self._conversation_repo.get_conversation(conversation_id)

    async def create_conversation_entry(
        self,
        *,
        group_id: str,
        summary: str,
        participants: list[str],
        importance_initial: int = 3,
        importance_current: int | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        last_accessed: datetime | None = None,
    ) -> dict[str, Any]:
        """创建可管理的对话记忆。"""
        normalized_start, normalized_end = self._resolve_conversation_range(
            start_time=start_time,
            end_time=end_time,
        )
        normalized_last_accessed = (
            self._normalize_datetime(last_accessed) or normalized_end
        )
        embedding = await self._embedding_plugin.embed(summary)
        return await self._conversation_repo.create_conversation(
            group_id=group_id,
            summary=summary,
            embedding=str(embedding),
            participants=participants,
            start_time=normalized_start,
            end_time=normalized_end,
            importance_initial=importance_initial,
            importance_current=int(
                importance_current
                if importance_current is not None
                else importance_initial
            ),
            last_accessed=normalized_last_accessed,
        )

    async def update_conversation_entry(
        self,
        conversation_id: int,
        *,
        group_id: str | None = None,
        summary: str | None = None,
        participants: list[str] | None = None,
        importance_initial: int | None = None,
        importance_current: int | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        last_accessed: datetime | None = None,
    ) -> dict[str, Any] | None:
        """更新单条对话记忆。"""
        existing = await self._conversation_repo.get_conversation(conversation_id)
        if existing is None:
            return None

        normalized_start = self._normalize_datetime(start_time)
        normalized_end = self._normalize_datetime(end_time)
        merged_start = normalized_start or existing["start_time"]
        merged_end = normalized_end or existing["end_time"]
        if merged_end < merged_start:
            msg = "end_time 不能早于 start_time"
            raise ValueError(msg)

        embedding: str | None = None
        if summary is not None:
            embedding = str(await self._embedding_plugin.embed(summary))

        return await self._conversation_repo.update_conversation(
            conversation_id,
            group_id=group_id,
            summary=summary,
            embedding=embedding,
            participants=participants,
            start_time=normalized_start,
            end_time=normalized_end,
            importance_initial=importance_initial,
            importance_current=importance_current,
            last_accessed=self._normalize_datetime(last_accessed),
        )

    async def delete_conversation_entry(self, conversation_id: int) -> bool:
        """删除单条对话记忆。"""
        return await self._conversation_repo.delete_conversation(conversation_id)

    async def upsert_user_profile(
        self,
        user_id: str,
        group_id: str,
        profile: dict[str, Any],
        importance: int = 4,
    ) -> None:
        """创建或更新用户画像实体。

        Args:
            user_id: 用户 ID
            group_id: 群组 ID
            profile: 用户画像 JSON
            importance: 重要性 (1-5)
        """
        profile_with_meta = dict(profile)
        profile_with_meta.setdefault("updated_at", self._now_iso())
        await self.batch_upsert_user_profiles(
            [
                {
                    "user_id": user_id,
                    "group_id": group_id,
                    "display_name": str(profile_with_meta.get("display_name", "")).strip()
                    or user_id,
                    "set_traits": self._normalize_traits_patch(
                        profile_with_meta.get("traits")
                    ),
                    "delete_keys": [],
                    "updated_at": profile_with_meta.get("updated_at"),
                    "snapshot_updated_at": None,
                    "importance": importance,
                }
            ]
        )

    async def batch_upsert_user_profiles(
        self,
        profiles: Sequence[UserProfileTraitsPatchPayload | UserProfileUpsertPayload],
    ) -> UserProfileBatchUpsertResult:
        """批量创建或更新用户画像实体。"""
        payloads = [
            self._normalize_profile_patch_payload(raw_payload)
            for raw_payload in profiles
        ]
        return await self._entity_repo.batch_upsert_user_profiles(payloads)

    async def upsert_interaction_history(
        self,
        user_id: str,
        group_id: str,
        interaction: dict[str, Any],
        importance: int = 5,
    ) -> None:
        """旧 PG JSONB 互动历史入口已停用。"""

    async def get_user_profile(
        self,
        user_id: str,
        group_id: str,
    ) -> dict[str, Any] | None:
        """获取用户画像 JSON。"""
        return await self._entity_repo.get_user_profile(
            user_id=user_id,
            group_id=group_id,
        )

    async def get_interaction_history(
        self,
        user_id: str,
        group_id: str,
    ) -> dict[str, Any] | None:
        """旧 PG JSONB 互动历史入口已停用，prompt_builder 本轮保持空注入。"""
        del user_id, group_id
        return None

    async def insert_interaction_event(
        self,
        *,
        user_id: str,
        display_name: str,
        event_summary: str,
        source_message_count: int,
        first_seen_at: datetime,
        last_seen_at: datetime,
        dedup_key: str,
        importance_initial: int = 4,
    ) -> int:
        """生成向量并写入一条跨群互动事件记忆。"""
        embedding = await self._embedding_plugin.embed(event_summary)
        return await self._get_interaction_event_repo().insert_interaction_event(
            user_id=user_id,
            display_name=display_name,
            event_summary=event_summary,
            embedding=str(embedding),
            source_message_count=source_message_count,
            first_seen_at=first_seen_at,
            last_seen_at=last_seen_at,
            importance_initial=importance_initial,
            dedup_key=dedup_key,
        )

    async def get_interaction_event_id_by_dedup_key(
        self,
        dedup_key: str,
    ) -> int | None:
        """按来源快照幂等键查询已经写入的事件。"""
        return await self._get_interaction_event_repo().get_event_id_by_dedup_key(
            dedup_key
        )

    async def search_interaction_events(
        self,
        *,
        user_id: str,
        query: str,
        limit: int = 3,
        query_embedding: list[float] | None = None,
    ) -> list[dict[str, Any]]:
        """检索当前用户的跨群互动事件，并刷新已召回事件重要度。"""
        query_vec = (
            query_embedding
            if query_embedding is not None
            else await self._embedding_plugin.embed(query)
        )
        repo = self._get_interaction_event_repo()
        results = await repo.search_interaction_events(
            user_id=user_id,
            embedding=str(query_vec),
            limit=limit,
        )
        if results:
            await repo.touch_interaction_events(
                [int(result["id"]) for result in results],
            )
        return results

    async def list_user_profile_rows(
        self,
        *,
        limit: int,
        offset: int,
        group_id: str | None = None,
        user_id: str | None = None,
        query: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """分页获取用户画像文档行。"""
        return await self._entity_repo.list_user_profiles(
            limit=limit,
            offset=offset,
            group_id=group_id,
            user_id=user_id,
            query=query,
        )

    async def list_interaction_history_rows(
        self,
        *,
        limit: int,
        offset: int,
        group_id: str | None = None,
        user_id: str | None = None,
        query: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """分页获取跨群互动事件行（保留旧方法名给 API 过渡使用）。"""
        del group_id
        rows, total = await self._get_interaction_event_repo().list_interaction_events(
            limit=limit,
            offset=offset,
            user_id=user_id,
            query=query,
        )
        return [self._interaction_event_to_entity(row) for row in rows], total

    async def get_user_profile_row(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> dict[str, Any] | None:
        """获取带元数据的用户画像行。"""
        return await self._entity_repo.get_user_profile_row(
            user_id=user_id,
            group_id=group_id,
        )

    async def get_interaction_history_row(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> dict[str, Any] | None:
        """旧 user+group 互动历史行入口已停用。"""
        del user_id, group_id
        return None

    async def upsert_user_profile_row(
        self,
        *,
        user_id: str,
        group_id: str,
        profile: dict[str, Any],
        importance: int = 4,
    ) -> dict[str, Any] | None:
        """写入用户画像并返回最新行。"""
        await self.upsert_user_profile(
            user_id=user_id,
            group_id=group_id,
            profile=profile,
            importance=importance,
        )
        return await self.get_user_profile_row(user_id=user_id, group_id=group_id)

    async def upsert_interaction_history_row(
        self,
        *,
        user_id: str,
        group_id: str,
        interaction: dict[str, Any],
        importance: int = 5,
    ) -> dict[str, Any] | None:
        """旧 user+group 互动历史写入入口已停用。"""
        del user_id, group_id, interaction, importance
        return None

    async def delete_user_profile(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> bool:
        """删除用户画像文档。"""
        return await self._entity_repo.delete_user_profile(
            user_id=user_id,
            group_id=group_id,
        )

    async def delete_interaction_history(
        self,
        *,
        user_id: str,
        group_id: str,
    ) -> bool:
        """旧 user+group 互动历史删除入口已停用。"""
        del user_id, group_id
        return False

    async def get_interaction_event_entry(self, event_id: int) -> dict[str, Any] | None:
        """读取跨群互动事件管理行。"""
        return await self._get_interaction_event_repo().get_interaction_event(event_id)

    async def delete_interaction_event_entry(self, event_id: int) -> bool:
        """删除跨群互动事件管理行。"""
        return await self._get_interaction_event_repo().delete_interaction_event(event_id)

    async def update_interaction_event_entry(
        self,
        event_id: int,
        *,
        event_summary: str | None = None,
        importance_initial: int | None = None,
        importance_current: int | None = None,
    ) -> dict[str, Any] | None:
        """更新跨群互动事件管理行。"""
        embedding: str | None = None
        if event_summary is not None:
            embedding = str(await self._embedding_plugin.embed(event_summary))
        return await self._get_interaction_event_repo().update_interaction_event(
            event_id,
            event_summary=event_summary,
            embedding=embedding,
            importance_initial=importance_initial,
            importance_current=importance_current,
        )

    async def ensure_user_memory_rows(
        self,
        *,
        user_id: str,
        group_id: str,
        display_name: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """确保用户画像与互动历史两行都存在。"""
        profile = await self.get_user_profile(user_id=user_id, group_id=group_id)
        if profile is None:
            profile = {
                "version": 1,
                "user_id": user_id,
                "display_name": display_name,
                "traits": {},
                "updated_at": self._now_iso(),
            }
            await self.upsert_user_profile(
                user_id=user_id,
                group_id=group_id,
                profile=profile,
            )

        return profile, {}

    async def cleanup(self) -> None:
        """清理资源。"""

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).isoformat()

    def _normalize_profile_patch_payload(
        self,
        payload: UserProfileTraitsPatchPayload | UserProfileUpsertPayload,
    ) -> UserProfileTraitsPatchPayload:
        user_id = payload["user_id"]
        if "profile" in payload:
            profile = payload["profile"]
            return {
                "user_id": user_id,
                "group_id": payload["group_id"],
                "display_name": str(profile.get("display_name", "")).strip() or user_id,
                "set_traits": self._normalize_traits_patch(profile.get("traits")),
                "delete_keys": [],
                "updated_at": profile.get("updated_at") or self._now_iso(),
                "snapshot_updated_at": None,
                "importance": payload["importance"],
            }

        return {
            "user_id": user_id,
            "group_id": payload["group_id"],
            "display_name": str(payload.get("display_name", "")).strip() or user_id,
            "set_traits": self._normalize_traits_patch(payload.get("set_traits")),
            "delete_keys": self._normalize_delete_keys(payload.get("delete_keys")),
            "updated_at": payload.get("updated_at") or self._now_iso(),
            "snapshot_updated_at": payload.get("snapshot_updated_at"),
            "importance": payload["importance"],
        }

    @staticmethod
    def _normalize_traits_patch(value: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(value, dict):
            return {}
        normalized: dict[str, dict[str, Any]] = {}
        for raw_key, raw_payload in value.items():
            key = str(raw_key).strip()
            if key and isinstance(raw_payload, dict):
                normalized[key] = dict(raw_payload)
        return normalized

    @staticmethod
    def _normalize_delete_keys(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        seen: set[str] = set()
        keys: list[str] = []
        for raw_key in value:
            key = str(raw_key).strip()
            if key and key not in seen:
                keys.append(key)
                seen.add(key)
        return keys

    @staticmethod
    def _now_naive() -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)

    def _resolve_conversation_range(
        self,
        *,
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> tuple[datetime, datetime]:
        normalized_start = self._normalize_datetime(start_time)
        normalized_end = self._normalize_datetime(end_time)
        if normalized_end is None:
            normalized_end = (
                self._now_naive() if normalized_start is None else normalized_start
            )
        if normalized_start is None:
            normalized_start = normalized_end - timedelta(hours=1)

        if normalized_end < normalized_start:
            msg = "end_time 不能早于 start_time"
            raise ValueError(msg)
        return normalized_start, normalized_end

    @staticmethod
    def _normalize_datetime(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(UTC).replace(tzinfo=None)

    def _get_interaction_event_repo(self) -> InteractionEventRepository:
        if self._interaction_event_repo is None:
            msg = "跨群互动事件仓库未初始化"
            raise RuntimeError(msg)
        return self._interaction_event_repo

    @staticmethod
    def _interaction_event_to_entity(row: dict[str, Any]) -> dict[str, Any]:
        user_id = str(row.get("user_id", "")).strip()
        return {
            "user_id": user_id,
            "group_id": "",
            "key": f"interaction_event:{row.get('id')}",
            "category": "interaction_event",
            "importance": int(row.get("importance", 4) or 4),
            "access_count": 0,
            "last_accessed": row.get("last_accessed"),
            "value": {
                "version": 1,
                "id": row.get("id"),
                "user_id": user_id,
                "display_name": str(row.get("display_name", "")).strip() or user_id,
                "event_summary": str(row.get("event_summary", "")).strip(),
                "source_message_count": int(row.get("source_message_count", 0) or 0),
                "first_seen_at": row.get("first_seen_at"),
                "last_seen_at": row.get("last_seen_at"),
                "importance_initial": int(row.get("importance_initial", 4) or 4),
                "importance_current": int(row.get("importance_current", 4) or 4),
                "is_fuzzy": bool(row.get("is_fuzzy", False)),
                "created_at": row.get("created_at"),
            },
        }
