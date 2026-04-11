"""Komari Memory 记忆管理服务。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from nonebot.plugin import require

if TYPE_CHECKING:
    from ..config_schema import KomariMemoryConfigSchema
    from ..repositories.conversation_repository import ConversationRepository
    from ..repositories.entity_repository import EntityRepository


class MemoryService:
    """记忆管理服务。"""

    def __init__(
        self,
        config: KomariMemoryConfigSchema,
        conversation_repo: ConversationRepository,
        entity_repo: EntityRepository,
    ) -> None:
        """初始化记忆服务。

        Args:
            config: 插件配置
            conversation_repo: 对话仓库
            entity_repo: 实体仓库
        """
        self.config = config
        self._conversation_repo = conversation_repo
        self._entity_repo = entity_repo
        self._embedding_plugin: Any = require("embedding_provider")

    async def store_conversation(
        self,
        group_id: str,
        summary: str,
        participants: list[str],
        importance_initial: int = 3,
    ) -> int:
        """存储对话总结（向量检索用 asyncpg）。

        Args:
            group_id: 群组 ID
            summary: 总结文本
            participants: 参与者列表
            importance_initial: 初始重要性评分（1-5）

        Returns:
            创建的对话 ID
        """
        # 业务逻辑：生成向量
        embedding = await self._embedding_plugin.embed(summary)

        # 数据访问：委托给仓库
        return await self._conversation_repo.insert_conversation(
            group_id=group_id,
            summary=summary,
            embedding=str(embedding),
            participants=participants,
            importance_initial=importance_initial,
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
            access_boost=self.config.forgetting_access_boost,
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
                access_boost=self.config.forgetting_access_boost,
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
        normalized_last_accessed = self._normalize_datetime(last_accessed) or normalized_end
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
        profile_with_meta.setdefault("version", 1)
        profile_with_meta.setdefault("user_id", user_id)
        profile_with_meta.setdefault("updated_at", self._now_iso())
        profile_with_meta.setdefault("traits", {})
        await self._entity_repo.upsert_user_profile(
            user_id=user_id,
            group_id=group_id,
            profile=profile_with_meta,
            importance=importance,
        )

    async def upsert_interaction_history(
        self,
        user_id: str,
        group_id: str,
        interaction: dict[str, Any],
        importance: int = 5,
    ) -> None:
        """创建或更新互动历史实体。"""
        interaction_with_meta = dict(interaction)
        interaction_with_meta.setdefault("version", 1)
        interaction_with_meta.setdefault("user_id", user_id)
        interaction_with_meta.setdefault("updated_at", self._now_iso())
        interaction_with_meta.setdefault("records", [])
        interaction_with_meta.setdefault("summary", "")
        await self._entity_repo.upsert_interaction_history(
            user_id=user_id,
            group_id=group_id,
            interaction=interaction_with_meta,
            importance=importance,
        )

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
        """获取用户互动历史 JSON。"""
        return await self._entity_repo.get_interaction_history(
            user_id=user_id,
            group_id=group_id,
        )

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
        """分页获取互动历史文档行。"""
        return await self._entity_repo.list_interaction_histories(
            limit=limit,
            offset=offset,
            group_id=group_id,
            user_id=user_id,
            query=query,
        )

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
        """获取带元数据的互动历史行。"""
        return await self._entity_repo.get_interaction_history_row(
            user_id=user_id,
            group_id=group_id,
        )

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
        """写入互动历史并返回最新行。"""
        await self.upsert_interaction_history(
            user_id=user_id,
            group_id=group_id,
            interaction=interaction,
            importance=importance,
        )
        return await self.get_interaction_history_row(user_id=user_id, group_id=group_id)

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
        """删除互动历史文档。"""
        return await self._entity_repo.delete_interaction_history(
            user_id=user_id,
            group_id=group_id,
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

        interaction = await self.get_interaction_history(user_id=user_id, group_id=group_id)
        if interaction is None:
            interaction = {
                "version": 1,
                "user_id": user_id,
                "display_name": display_name,
                "file_type": "用户的近期对鞠行为备忘录",
                "description": "暂无互动记录",
                "records": [],
                "summary": "",
                "updated_at": self._now_iso(),
            }
            await self.upsert_interaction_history(
                user_id=user_id,
                group_id=group_id,
                interaction=interaction,
            )

        return profile, interaction

    async def cleanup(self) -> None:
        """清理资源。"""

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).isoformat()

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
        if normalized_start is None and normalized_end is None:
            normalized_end = self._now_naive()
            normalized_start = normalized_end - timedelta(hours=1)
        elif normalized_start is None:
            normalized_start = normalized_end - timedelta(hours=1)
        elif normalized_end is None:
            normalized_end = normalized_start

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
