"""Komari Memory 记忆管理服务。"""

from __future__ import annotations

from datetime import UTC, datetime
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
        self._embedding_plugin = require("embedding_provider")

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
        fetch_limit = limit * 3 if self._embedding_plugin.is_rerank_enabled() else limit

        # 数据访问：委托给仓库（传递 user_id 用于加权）
        results = await self._conversation_repo.search_by_similarity(
            embedding=str(query_vec),
            group_id=group_id,
            user_id=user_id,
            limit=fetch_limit,
        )

        if self._embedding_plugin.is_rerank_enabled() and results:
            documents = [r["summary"] for r in results]
            reranked = await self._embedding_plugin.rerank(
                query, documents, top_n=limit
            )
            results = [results[rr.index] for rr in reranked]

        return results[:limit]

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
