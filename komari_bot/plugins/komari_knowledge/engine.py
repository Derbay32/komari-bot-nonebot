"""
Komari Knowledge 常识库核心引擎。

提供混合检索功能：
- Layer 1: 关键词精确匹配（内存，微秒级）
- Layer 2: 向量语义检索（PostgreSQL pgvector，毫秒级）

支持两种运行模式：
1. NoneBot 模式：使用 config_manager 插件加载配置
2. 独立模式：直接从 JSON 文件加载配置（用于 WebUI）
"""

import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from komari_bot.common.database_config import (
    DatabaseConfigSchema,
    get_effective_database_config,
    load_database_config_from_file,
    merge_database_config,
)
from komari_bot.common.pgvector_schema import ensure_vector_column_dimension
from komari_bot.common.postgres import create_postgres_pool
from komari_bot.common.vector_storage_schema import (
    PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS,
    apply_schema_statements,
    build_knowledge_embedding_index_statement,
    build_knowledge_schema_statements,
)

from .config_schema import DynamicConfigSchema


class PluginState:
    """存放插件全局运行状态的容器"""

    def __init__(self) -> None:
        self.nonebot_mode: bool = "nonebot" in sys.modules
        self.config_manager: Any = None
        self.standalone_config: DynamicConfigSchema | None = None
        self.engine: KnowledgeEngine | None = None
        self.logger: logging.Logger | Any = logging.getLogger("komari_knowledge")


# 初始化全局状态单例
state = PluginState()

# 只有在 NoneBot 环境中才尝试加载 config_manager
if state.nonebot_mode:
    try:
        from nonebot import logger as nb_logger
        from nonebot.plugin import require

        config_manager_plugin = require("config_manager")
        config_manager = config_manager_plugin.get_config_manager(
            "komari_knowledge", DynamicConfigSchema
        )
        state.logger = nb_logger
    except (ImportError, RuntimeError):
        # 回退到独立模式
        state.nonebot_mode = False
        config_manager = None
        state.logger = logging.getLogger("komari_knowledge")


def _load_standalone_config() -> DynamicConfigSchema:
    """独立模式：从 JSON 文件加载配置。"""
    config_path = Path("config/config_manager/komari_knowledge_config.json")
    if config_path.exists():
        try:
            with Path.open(config_path, encoding="utf-8") as f:
                data = json.load(f)
            return DynamicConfigSchema(**data)
        except (json.JSONDecodeError, TypeError) as e:
            state.logger.warning(f"配置文件解析失败: {e}，使用默认配置")
            return DynamicConfigSchema()
    else:
        state.logger.warning(f"配置文件不存在: {config_path}，使用默认配置")
        return DynamicConfigSchema()


def get_config() -> DynamicConfigSchema:
    """获取当前配置（兼容两种模式）。

    Returns:
        当前配置对象
    """
    if state.nonebot_mode:
        # 告诉 pylance config_manager 不可能为空
        assert config_manager is not None, (
            "config_manager 应该在 NoneBot 模式下已初始化"
        )
        return config_manager.get()
    if state.standalone_config is None:
        state.standalone_config = _load_standalone_config()
    return state.standalone_config


def get_db_config(config: DynamicConfigSchema) -> DatabaseConfigSchema:
    """获取最终生效的数据库配置（兼容两种模式）。"""
    if state.nonebot_mode:
        return get_effective_database_config(config)

    shared_config_path = Path("config/config_manager/database_config.json")
    if shared_config_path.exists():
        try:
            shared = load_database_config_from_file(shared_config_path)
            return merge_database_config(shared, config)
        except Exception as e:
            state.logger.warning(
                "[Komari Knowledge] 共享数据库配置解析失败: %s，回退到本地配置",
                e,
            )

    return merge_database_config(DatabaseConfigSchema(), config)


class SearchResult(BaseModel):
    """检索结果。"""

    id: int
    category: str
    content: str
    similarity: float = 0.0
    source: str = "keyword"  # "keyword" 或 "vector"


class KnowledgeEngine:
    """
    常识库核心引擎。

    负责管理数据库连接池、向量模型和检索逻辑。
    """

    def __init__(self) -> None:
        """初始化引擎。"""
        self._pool: Any = None
        self._embedding_service: Any = None
        self._keyword_index: dict[str, set[int]] = defaultdict(set)
        self._index_loaded = False

    async def initialize(self) -> None:
        """初始化引擎（加载模型、建立连接池、构建索引）。"""
        state.logger.info("[Komari Knowledge] 正在初始化常识库引擎...")

        # 获取配置
        config = get_config()

        try:
            # 1. 加载向量嵌入模型
            if state.nonebot_mode:
                state.logger.info("[Komari Knowledge] 使用全局 EmbeddingProvider 服务")
            elif getattr(self, "_embedding_service", None) is None:
                state.logger.info("[Komari Knowledge] 加载独立嵌入服务...")
                from komari_bot.plugins.embedding_provider.config_schema import (
                    DynamicConfigSchema as EmbedConfigSchema,
                )
                from komari_bot.plugins.embedding_provider.embedding_service import (
                    EmbeddingService,
                )

                config_path = Path("config/config_manager/embedding_provider_config.json")
                if config_path.exists():
                    with Path.open(config_path, encoding="utf-8") as f:
                        data = json.load(f)
                    embed_config = EmbedConfigSchema(**data)
                else:
                    embed_config = EmbedConfigSchema()

                self._embedding_service = EmbeddingService(embed_config)
                state.logger.info("[Komari Knowledge] 独立嵌入服务初始化完成")

            # 2. 建立数据库连接池
            if self._pool is None:
                db_config = get_db_config(config)
                self._pool = await create_postgres_pool(db_config, command_timeout=30)
                state.logger.info("[Komari Knowledge] 数据库连接池已建立")
                expected_dimension = self._resolve_expected_embedding_dimension()
                await self._ensure_storage_schema(expected_dimension)
                await self._validate_embedding_dimension(expected_dimension)

            # 3. 构建关键词索引（内存预热）
            await self._build_keyword_index()
            state.logger.info("[Komari Knowledge] 常识库引擎初始化完成")
        except Exception:
            try:
                await self.close()
            except Exception:
                state.logger.exception("[Komari Knowledge] 初始化失败后的清理失败")
            raise

    def _resolve_expected_embedding_dimension(self) -> int | None:
        """解析当前 embedding 配置的目标维度。"""
        expected_dimension: int | None = None
        if state.nonebot_mode:
            from nonebot.plugin import require

            embedding_provider = require("embedding_provider")
            get_dimension = getattr(embedding_provider, "get_embedding_dimension", None)
            if callable(get_dimension):
                raw_dimension = get_dimension()
                if isinstance(raw_dimension, int):
                    expected_dimension = raw_dimension
                elif isinstance(raw_dimension, str):
                    expected_dimension = int(raw_dimension)
                elif raw_dimension is not None:
                    msg = f"embedding_provider 返回了无效维度类型: {type(raw_dimension)!r}"
                    raise TypeError(msg)
        elif getattr(self, "_embedding_service", None) is not None:
            expected_dimension = int(self._embedding_service.config.embedding_dimension)
        return expected_dimension

    async def _ensure_storage_schema(self, expected_dimension: int | None) -> None:
        """按当前 embedding 维度补齐常识库基础表结构。"""
        if self._pool is None:
            msg = "数据库连接池未初始化"
            raise RuntimeError(msg)
        if expected_dimension is None:
            msg = "无法确定 embedding 维度，不能初始化 knowledge schema"
            raise RuntimeError(msg)

        await apply_schema_statements(
            self._pool,
            statements=build_knowledge_schema_statements(expected_dimension),
        )
        if build_knowledge_embedding_index_statement(expected_dimension) is None:
            state.logger.warning(
                "[Komari Knowledge] embedding 维度 {} 超过 pgvector HNSW 上限 {}，"
                "已跳过 idx_komari_knowledge_embedding，语义检索将退化为顺序扫描。",
                expected_dimension,
                PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS,
            )
        state.logger.info(
            "[Komari Knowledge] PostgreSQL schema 检查完成 (embedding={})",
            expected_dimension,
        )

    async def _validate_embedding_dimension(
        self,
        expected_dimension: int | None,
    ) -> None:
        """校验知识库向量列与当前 embedding 配置一致。"""
        if self._pool is None:
            msg = "数据库连接池未初始化"
            raise RuntimeError(msg)

        await ensure_vector_column_dimension(
            self._pool,
            table_name="komari_knowledge",
            column_name="embedding",
            expected_dimension=expected_dimension,
            label="KomariKnowledge",
        )

    async def _build_keyword_index(self) -> None:
        """构建关键词内存索引。

        启动时将所有关键词加载到内存字典中，实现微秒级匹配。
        """
        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化")

        state.logger.info("[Komari Knowledge] 正在构建关键词索引...")
        self._keyword_index.clear()
        self._index_loaded = False

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, keywords
                FROM komari_knowledge
                WHERE keywords IS NOT NULL AND array_length(keywords, 1) > 0
                """
            )

            for row in rows:
                kid: int = row["id"]
                keywords: list[str] = row["keywords"]

                for kw in keywords:
                    kw_lower = kw.lower()
                    self._keyword_index[kw_lower].add(kid)

        self._index_loaded = True
        state.logger.info(
            f"[Komari Knowledge] 关键词索引构建完成，共 {len(rows)} 条知识"
        )

    async def _get_embedding(self, text: str) -> list[float]:
        """生成文本的向量嵌入。

        Args:
            text: 输入文本

        Returns:
            向量数组
        """
        if state.nonebot_mode:
            from nonebot.plugin import require

            embedding_provider = require("embedding_provider")
            return await embedding_provider.embed(text)
        if getattr(self, "_embedding_service", None) is None:
            raise RuntimeError("独立嵌入服务未初始化")
        return await self._embedding_service.embed(text)

    def _rewrite_query(self, query: str) -> str:
        """应用查询重写规则。

        将用户查询中的代词替换为具体实体，提高检索准确率。
        例如："你喜欢什么" -> "小鞠喜欢什么"

        Args:
            query: 原始查询

        Returns:
            重写后的查询
        """
        rules = get_config().query_rewrite_rules
        rewritten = query
        for old, new in rules.items():
            rewritten = rewritten.replace(old, new)
        return rewritten

    async def search(
        self, query: str, limit: int | None = None, query_vec: list[float] | None = None
    ) -> list[SearchResult]:
        """
        混合检索：关键词 + 向量。

        Args:
            query: 用户查询文本
            limit: 最大返回数量，None 使用配置默认值
            query_vec: 预先计算好的查询特征向量，若提供则跳过模型推理


        Returns:
            检索结果列表，按相关性排序
        """
        if not query or not query.strip():
            return []

        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化，请先调用 initialize()")  # noqa:TRY003

        # 确保 limit 是 int 类型
        if limit is None:
            limit = get_config().total_limit

        # pylance 不知道 config 插件会存取什么东西，写个 assert 告诉它 limit 此时一定是 int
        assert isinstance(limit, int), "limit should be int"

        # 应用查询重写
        original_query = query
        query = self._rewrite_query(query)

        if query != original_query:
            state.logger.info(
                f"[Komari Knowledge] 查询重写: '{original_query}' -> '{query}'"
            )

        results: list[SearchResult] = []
        seen_ids: set[int] = set()

        # --- Layer 1: 关键词精确匹配（内存，微秒级） ---
        keyword_hits = await self._layer1_keyword_search(query, limit)
        for hit in keyword_hits:
            if hit.id not in seen_ids:
                results.append(hit)
                seen_ids.add(hit.id)

        # --- Layer 2: 向量语义检索（补漏） ---
        vector_hits: list[SearchResult] = []
        if len(results) < limit:
            vector_hits = await self._layer2_vector_search(
                query, limit - len(results), seen_ids, query_vec=query_vec
            )
            results.extend(vector_hits)

        state.logger.debug(
            f"[Komari Knowledge] 检索 '{query[:20]}...' -> "
            f"关键词命中 {len(keyword_hits)} 条，向量补充 {len(vector_hits)} 条"
        )

        return results

    async def search_by_keyword(self, keyword: str) -> list[SearchResult]:
        """通过关键词精确查询知识。

        Args:
            keyword: 关键词（如用户 UID）

        Returns:
            检索结果列表
        """
        if not self._index_loaded:
            return []

        # 在内存索引中查找
        keyword_lower = keyword.lower()
        if keyword_lower not in self._keyword_index:
            return []

        matched_ids = self._keyword_index[keyword_lower]

        # 从数据库获取完整内容
        if self._pool is None:
            return []

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, category, content
                FROM komari_knowledge
                WHERE id = ANY($1)
                """,
                list(matched_ids),
            )

        return [
            SearchResult(
                id=row["id"],
                category=row["category"],
                content=row["content"],
                similarity=1.0,
                source="keyword",
            )
            for row in rows
        ]

    async def _layer1_keyword_search(
        self, query: str, limit: int
    ) -> list[SearchResult]:
        """
        Layer 1: 关键词匹配。

        在内存索引中查找包含查询关键词的知识。

        Args:
            query: 查询文本
            limit: 最大返回数量

        Returns:
            检索结果列表
        """
        if not self._index_loaded:
            return []

        matched_ids: set[int] = set()

        # 检查查询中是否包含已知关键词
        query_lower = query.lower()
        for kw, kid_set in self._keyword_index.items():
            if kw in query_lower:
                matched_ids.update(kid_set)

        if not matched_ids:
            return []

        # 从数据库获取完整内容
        if self._pool is None:
            return []

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, category, content
                FROM komari_knowledge
                WHERE id = ANY($1)
                LIMIT $2
                """,
                list(matched_ids),
                limit,
            )
        return [
            SearchResult(
                id=row["id"],
                category=row["category"],
                content=row["content"],
                similarity=1.0,  # 关键词匹配视为完全相关
                source="keyword",
            )
            for row in rows
        ]

    async def _layer2_vector_search(
        self,
        query: str,
        limit: int,
        exclude_ids: set[int],
        query_vec: list[float] | None = None,
    ) -> list[SearchResult]:
        """
        Layer 2: 向量语义检索。

        使用 pgvector 计算余弦相似度。

        Args:
            query: 查询文本
            limit: 最大返回数量
            exclude_ids: 要排除的知识 ID（已被关键词匹配）
            query_vec: 预先计算好的查询向量


        Returns:
            检索结果列表
        """
        if self._pool is None:
            return []

        # 获取配置
        config = get_config()

        # 生成查询向量
        if query_vec is None:
            query_vec = await self._get_embedding(query)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    id,
                    category,
                    content,
                    1 - (embedding <=> $1::vector) as similarity
                FROM komari_knowledge
                WHERE
                    embedding IS NOT NULL
                    AND id != ALL($2)
                ORDER BY embedding <=> $1::vector
                LIMIT $3
                """,
                str(query_vec),
                list(exclude_ids) if exclude_ids else [-1],
                limit,
            )

            # 应用相似度阈值过滤
            return [
                SearchResult(
                    id=row["id"],
                    category=row["category"],
                    content=row["content"],
                    similarity=row["similarity"],
                    source="vector",
                )
                for row in rows
                if row["similarity"] >= config.similarity_threshold
            ]

    async def add_knowledge(
        self,
        content: str,
        keywords: list[str],
        category: str = "general",
        notes: str | None = None,
    ) -> int:
        """
        添加知识到数据库。

        Args:
            content: 知识内容
            keywords: 关键词列表
            category: 分类
            notes: 备注

        Returns:
            新知识的 ID
        """
        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化")

        # 生成向量
        embedding = await self._get_embedding(content)

        async with self._pool.acquire() as conn:
            kid = await conn.fetchval(
                """
                INSERT INTO komari_knowledge (content, keywords, category, embedding, notes)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                """,
                content,
                keywords,
                category,
                str(embedding),
                notes,
            )

        # 更新内存索引
        for kw in keywords:
            kw_lower = kw.lower()
            self._keyword_index[kw_lower].add(kid)

        state.logger.info(f"[Komari Knowledge] 添加知识: ID={kid}, keywords={keywords}")
        return kid

    async def get_all_knowledge(self) -> list[dict[str, Any]]:
        """获取所有知识（用于 WebUI 展示）。"""
        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化")

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, category, keywords, content, notes, created_at, updated_at
                FROM komari_knowledge
                ORDER BY created_at DESC
                """
            )

            return [dict(row) for row in rows]

    async def delete_knowledge(self, kid: int) -> bool:
        """删除知识。

        Args:
            kid: 知识 ID

        Returns:
            是否删除成功
        """
        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化")

        async with self._pool.acquire() as conn:
            # 先获取关键词，用于更新内存索引
            row = await conn.fetchrow(
                "SELECT keywords FROM komari_knowledge WHERE id = $1", kid
            )

            if row is None:
                return False

            keywords = row["keywords"] or []

            # 删除记录
            await conn.execute("DELETE FROM komari_knowledge WHERE id = $1", kid)

        # 更新内存索引
        for kw in keywords:
            kw_lower = kw.lower()
            if kw_lower in self._keyword_index:
                self._keyword_index[kw_lower].discard(kid)

        state.logger.info(f"[Komari Knowledge] 删除知识: ID={kid}")
        return True

    async def update_knowledge(
        self,
        kid: int,
        content: str | None = None,
        keywords: list[str] | None = None,
        category: str | None = None,
        notes: str | None = None,
    ) -> bool:
        """
        更新知识。

        Args:
            kid: 知识 ID
            content: 新内容（None 表示不修改）
            keywords: 新关键词（None 表示不修改）
            category: 新分类（None 表示不修改）
            notes: 新备注（None 表示不修改）

        Returns:
            是否更新成功
        """
        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化")

        # 至少需要一个更新字段
        if all(v is None for v in [content, keywords, category, notes]):
            raise ValueError("至少提供一个要更新的字段")

        async with self._pool.acquire() as conn:
            # 获取现有数据
            row = await conn.fetchrow(
                "SELECT content, keywords, category, notes FROM komari_knowledge WHERE id = $1",
                kid,
            )

            if row is None:
                return False

            # 构建更新字段
            updates = []
            params = []
            param_idx = 2  # $1 是 kid

            # 内容改变需要重新生成向量
            if content is not None:
                embedding = await self._get_embedding(content)
                updates.append(f"content = ${param_idx}")
                params.append(content)
                param_idx += 1
                updates.append(f"embedding = ${param_idx}")
                params.append(str(embedding))
                param_idx += 1

            if keywords is not None:
                updates.append(f"keywords = ${param_idx}")
                params.append(keywords)
                param_idx += 1

            if category is not None:
                updates.append(f"category = ${param_idx}")
                params.append(category)
                param_idx += 1

            if notes is not None:
                updates.append(f"notes = ${param_idx}")
                params.append(notes)
                param_idx += 1

            # 更新 updated_at
            updates.append("updated_at = CURRENT_TIMESTAMP")

            # 执行更新
            query = f"UPDATE komari_knowledge SET {', '.join(updates)} WHERE id = $1"
            await conn.execute(query, kid, *params)

        # 更新内存关键词索引
        old_keywords = row["keywords"] or []
        new_keywords = keywords if keywords is not None else old_keywords

        # 移除旧关键词索引
        for kw in old_keywords:
            kw_lower = kw.lower()
            if kw_lower in self._keyword_index:
                self._keyword_index[kw_lower].discard(kid)

        # 添加新关键词索引
        for kw in new_keywords:
            kw_lower = kw.lower()
            self._keyword_index[kw_lower].add(kid)

        state.logger.info(f"[Komari Knowledge] 更新知识: ID={kid}")
        return True

    async def close(self) -> None:
        """关闭连接池并清理资源。"""
        errors: list[BaseException] = []

        if self._embedding_service is not None:
            try:
                await self._embedding_service.cleanup()
                state.logger.info("[Komari Knowledge] 独立嵌入服务已关闭")
            except Exception as e:
                errors.append(e)
                state.logger.exception("[Komari Knowledge] 关闭独立嵌入服务失败")
            finally:
                self._embedding_service = None

        if self._pool:
            try:
                await self._pool.close()
                state.logger.info("[Komari Knowledge] 连接池已关闭")
            except Exception as e:
                errors.append(e)
                state.logger.exception("[Komari Knowledge] 关闭连接池失败")
            finally:
                self._pool = None

        self._keyword_index.clear()
        self._index_loaded = False
        if state.engine is self:
            state.engine = None

        if errors:
            raise errors[0]


def get_engine() -> KnowledgeEngine | None:
    """获取全局引擎实例。"""
    return state.engine


async def initialize_engine() -> KnowledgeEngine:
    """初始化全局引擎实例。

    Returns:
        引擎实例
    """
    if state.engine is None:
        engine = KnowledgeEngine()
        await engine.initialize()
        state.engine = engine

    return state.engine
