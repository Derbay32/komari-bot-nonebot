"""
Komari Memory 常识库核心引擎。

提供混合检索功能：
- Layer 1: 关键词精确匹配（内存，微秒级）
- Layer 2: 向量语义检索（PostgreSQL pgvector，毫秒级）

支持两种运行模式：
1. NoneBot 模式：使用 config_manager 插件加载配置
2. 独立模式：直接从 JSON 文件加载配置（用于 WebUI）
"""
import asyncio
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastembed import TextEmbedding
from pydantic import BaseModel

from .config_schema import DynamicConfigSchema

# 检测运行模式：通过检查是否在 NoneBot 环境中
_nonebot_mode = "nonebot" in sys.modules
config_manager = None
_logger: logging.Logger | Any = logging.getLogger("komari_memory")

# 只有在 NoneBot 环境中才尝试加载 config_manager
if _nonebot_mode:
    try:
        from nonebot import logger as nb_logger
        from nonebot.plugin import require

        config_manager_plugin = require("config_manager")
        config_manager = config_manager_plugin.get_config_manager(
            "komari_memory", DynamicConfigSchema
        )
        _logger = nb_logger
    except (ImportError, RuntimeError):
        # 回退到独立模式
        _nonebot_mode = False
        config_manager = None
        _logger = logging.getLogger("komari_memory")


# 独立模式配置加载器
_standalone_config: DynamicConfigSchema | None = None


def _load_standalone_config() -> DynamicConfigSchema:
    """独立模式：从 JSON 文件加载配置。"""
    config_path = Path("config/config_manager/komari_memory_config.json")
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return DynamicConfigSchema(**data)
        except (json.JSONDecodeError, TypeError) as e:
            _logger.warning(f"配置文件解析失败: {e}，使用默认配置")
            return DynamicConfigSchema()
    else:
        _logger.warning(f"配置文件不存在: {config_path}，使用默认配置")
        return DynamicConfigSchema()


def get_config() -> DynamicConfigSchema:
    """获取当前配置（兼容两种模式）。

    Returns:
        当前配置对象
    """
    if _nonebot_mode:
        # 告诉 pylance config_manager 不可能为空
        assert config_manager is not None, "config_manager 应该在 NoneBot 模式下已初始化"
        return config_manager.get()
    else:
        global _standalone_config
        if _standalone_config is None:
            _standalone_config = _load_standalone_config()
        return _standalone_config


class SearchResult(BaseModel):
    """检索结果。"""

    id: int
    category: str
    content: str
    similarity: float = 0.0
    source: str = "keyword"  # "keyword" 或 "vector"


class MemoryEngine:
    """
    常识库核心引擎。

    负责管理数据库连接池、向量模型和检索逻辑。
    """

    def __init__(self):
        """初始化引擎。"""
        self._pool: Any = None
        self._embed_model: TextEmbedding | None = None
        self._keyword_index: dict[str, set[int]] = defaultdict(set)
        self._index_loaded = False

    async def initialize(self) -> None:
        """初始化引擎（加载模型、建立连接池、构建索引）。"""
        _logger.info("[Komari Memory] 正在初始化常识库引擎...")

        # 获取配置
        config = get_config()

        # 1. 加载向量嵌入模型
        if self._embed_model is None:
            _logger.info(f"[Komari Memory] 加载嵌入模型: {config.embedding_model}")
            # 在独立线程中加载模型，避免阻塞
            loop = asyncio.get_event_loop()
            self._embed_model = await loop.run_in_executor(
                None,
                lambda: TextEmbedding(model_name=config.embedding_model),
            )
            _logger.info("[Komari Memory] 嵌入模型加载完成")

        # 2. 建立数据库连接池
        if self._pool is None:
            import asyncpg

            self._pool = await asyncpg.create_pool(
                host=config.pg_host,
                port=config.pg_port,
                database=config.pg_database,
                user=config.pg_user,
                password=config.pg_password,
                min_size=2,
                max_size=5,
                command_timeout=30,
            )
            _logger.info("[Komari Memory] 数据库连接池已建立")

        # 3. 构建关键词索引（内存预热）
        await self._build_keyword_index()
        _logger.info("[Komari Memory] 常识库引擎初始化完成")

    async def _build_keyword_index(self) -> None:
        """构建关键词内存索引。

        启动时将所有关键词加载到内存字典中，实现微秒级匹配。
        """
        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化")

        _logger.info("[Komari Memory] 正在构建关键词索引...")

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
        _logger.info(f"[Komari Memory] 关键词索引构建完成，共 {len(rows)} 条知识")

    def _get_embedding(self, text: str) -> list[float]:
        """生成文本的向量嵌入。

        Args:
            text: 输入文本

        Returns:
            向量数组
        """
        if self._embed_model is None:
            raise RuntimeError("嵌入模型未初始化")

        # fastembed 返回迭代器，转换为列表后取第一个
        embeddings = list(self._embed_model.embed([text]))
        return embeddings[0].tolist()

    async def search(self, query: str, limit: int | None = None) -> list[SearchResult]:
        """
        混合检索：关键词 + 向量。

        Args:
            query: 用户查询文本
            limit: 最大返回数量，None 使用配置默认值

        Returns:
            检索结果列表，按相关性排序
        """
        if not query or not query.strip():
            return []

        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化，请先调用 initialize()")

        # 确保 limit 是 int 类型
        if limit is None:
            limit = get_config().total_limit

        # pylance 不知道 config 插件会存取什么东西，写个 assert 告诉它 limit 此时一定是 int
        assert isinstance(limit, int), "limit should be int"

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
                query, limit - len(results), seen_ids
            )
            results.extend(vector_hits)

        _logger.debug(
            f"[Komari Memory] 检索 '{query[:20]}...' -> "
            f"关键词命中 {len(keyword_hits)} 条，向量补充 {len(vector_hits)} 条"
        )

        return results

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

        results: list[SearchResult] = []
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

            for row in rows:
                results.append(
                    SearchResult(
                        id=row["id"],
                        category=row["category"],
                        content=row["content"],
                        similarity=1.0,  # 关键词匹配视为完全相关
                        source="keyword",
                    )
                )

        return results

    async def _layer2_vector_search(
        self, query: str, limit: int, exclude_ids: set[int]
    ) -> list[SearchResult]:
        """
        Layer 2: 向量语义检索。

        使用 pgvector 计算余弦相似度。

        Args:
            query: 查询文本
            limit: 最大返回数量
            exclude_ids: 要排除的知识 ID（已被关键词匹配）

        Returns:
            检索结果列表
        """
        if self._pool is None:
            return []

        # 获取配置
        config = get_config()

        # 生成查询向量（在独立线程中执行）
        loop = asyncio.get_event_loop()
        query_vec = await loop.run_in_executor(None, self._get_embedding, query)

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

            results: list[SearchResult] = []
            for row in rows:
                # 应用相似度阈值过滤
                if row["similarity"] >= config.similarity_threshold:
                    results.append(
                        SearchResult(
                            id=row["id"],
                            category=row["category"],
                            content=row["content"],
                            similarity=row["similarity"],
                            source="vector",
                        )
                    )

            return results

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
        loop = asyncio.get_event_loop()
        embedding = await loop.run_in_executor(None, self._get_embedding, content)

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

        _logger.info(f"[Komari Memory] 添加知识: ID={kid}, keywords={keywords}")
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

        _logger.info(f"[Komari Memory] 删除知识: ID={kid}")
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
                loop = asyncio.get_event_loop()
                embedding = await loop.run_in_executor(None, self._get_embedding, content)
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

        _logger.info(f"[Komari Memory] 更新知识: ID={kid}")
        return True

    async def close(self) -> None:
        """关闭连接池。"""
        if self._pool:
            await self._pool.close()
            self._pool = None
            _logger.info("[Komari Memory] 连接池已关闭")


# 全局单例
_engine: MemoryEngine | None = None


def get_engine() -> MemoryEngine | None:
    """获取全局引擎实例。"""
    return _engine


async def initialize_engine() -> MemoryEngine:
    """初始化全局引擎实例。

    Returns:
        引擎实例
    """
    global _engine

    if _engine is None:
        _engine = MemoryEngine()
        await _engine.initialize()

    return _engine
