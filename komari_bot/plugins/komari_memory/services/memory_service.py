"""Komari Memory 记忆管理服务。"""

import asyncio
from pathlib import Path
from typing import Any

from fastembed import TextEmbedding
from nonebot import logger

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
        self._embed_model: TextEmbedding | None = None

    async def _get_embed_model(self) -> TextEmbedding:
        """延迟加载嵌入模型。

        Returns:
            TextEmbedding 实例
        """
        if self._embed_model is None:
            # 配置统一的缓存目录
            cache_dir = Path.home() / ".cache" / "komari_embeddings"
            cache_dir.mkdir(parents=True, exist_ok=True)

            # 在独立线程中加载模型，避免阻塞
            loop = asyncio.get_running_loop()
            self._embed_model = await loop.run_in_executor(
                None,
                lambda: TextEmbedding(
                    model_name=self.config.embedding_model,
                    cache_dir=str(cache_dir),
                ),
            )
            logger.info(
                f"[KomariMemory] 向量嵌入模型加载完成 (缓存: {cache_dir})"
            )
        assert self._embed_model is not None  # 为类型检查器确保非 None
        return self._embed_model

    async def _get_embedding(self, text: str) -> list[float]:
        """生成文本的向量嵌入。

        Args:
            text: 输入文本

        Returns:
            向量数组
        """
        embed_model = await self._get_embed_model()
        # fastembed 返回迭代器，转换为列表后取第一个
        loop = asyncio.get_running_loop()
        embeddings = await loop.run_in_executor(
            None, lambda: list(embed_model.embed([text]))
        )
        return embeddings[0].tolist()

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
        embedding = await self._get_embedding(summary)

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
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """向量检索对话（asyncpg 原生 SQL）。

        Args:
            query: 查询文本
            group_id: 群组 ID
            limit: 返回数量限制

        Returns:
            检索结果列表，包含 summary, similarity 等
        """
        # 业务逻辑：生成查询向量
        query_vec = await self._get_embedding(query)

        # 数据访问：委托给仓库
        return await self._conversation_repo.search_by_similarity(
            embedding=str(query_vec),
            group_id=group_id,
            limit=limit,
        )

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
        """获取实体列表。

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
