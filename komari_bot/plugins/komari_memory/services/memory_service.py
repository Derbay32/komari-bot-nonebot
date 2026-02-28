"""Komari Memory 记忆管理服务。"""

from typing import Any

from nonebot.plugin import require

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
    ) -> list[dict[str, Any]]:
        """向量检索对话（支持用户相关性加权）。

        Args:
            query: 查询文本
            group_id: 群组 ID
            user_id: 当前用户 ID（用于加权该用户参与的记忆）
            limit: 返回数量限制

        Returns:
            检索结果列表，包含 summary, similarity 等
        """
        # 业务逻辑：生成查询向量
        query_vec = await self._embedding_plugin.embed(query)

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

    async def upsert_entity(
        self,
        user_id: str,
        group_id: str,
        key: str,
        value: str,
        category: str,
        importance: int = 3,
    ) -> None:
        """创建或更新实体（使用 ORM）。

        Args:
            user_id: 用户 ID
            group_id: 群组 ID
            key: 实体键
            value: 实体值
            category: 分类
            importance: 重要性 (1-5)
        """
        # 数据访问：委托给仓库
        await self._entity_repo.upsert(
            user_id=user_id,
            group_id=group_id,
            key=key,
            value=value,
            category=category,
            importance=importance,
        )

    async def get_entities(
        self,
        user_id: str | None = None,
        group_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """获取实体列表（已排除 interaction_history 记录）。

        Args:
            user_id: 过滤用户 ID
            group_id: 过滤群组 ID
            limit: 返回数量限制

        Returns:
            实体列表
        """
        # 数据访问：委托给仓库
        return await self._entity_repo.list(
            user_id=user_id,
            group_id=group_id,
            limit=limit,
        )

    async def get_interaction_history(
        self,
        user_id: str,
        group_id: str,
    ) -> dict[str, Any] | None:
        """专门获取用户的互动历史实体，不占用常规实体检索名额。

        Args:
            user_id: 用户 ID
            group_id: 群组 ID

        Returns:
            实体字典或 None
        """
        return await self._entity_repo.get_interaction_history(
            user_id=user_id,
            group_id=group_id,
        )

    async def cleanup(self) -> None:
        """清理资源。"""
