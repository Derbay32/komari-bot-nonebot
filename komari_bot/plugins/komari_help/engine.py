"""Komari Help 帮助文档核心引擎。"""

from __future__ import annotations

import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Final

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
    build_help_embedding_index_statement,
    build_help_schema_statements,
)

from .config_schema import DynamicConfigSchema
from .models import HelpCategory, HelpEntry, HelpSearchResult


class PluginState:
    """存放插件全局运行状态的容器。"""

    def __init__(self) -> None:
        self.nonebot_mode: bool = "nonebot" in sys.modules
        self.standalone_config: DynamicConfigSchema | None = None
        self.engine: HelpEngine | None = None
        self.logger: logging.Logger | Any = logging.getLogger("komari_help")


state = PluginState()

if state.nonebot_mode:
    try:
        from nonebot import logger as nb_logger
        from nonebot.plugin import require

        config_manager_plugin = require("config_manager")
        config_manager = config_manager_plugin.get_config_manager(
            "komari_help", DynamicConfigSchema
        )
        state.logger = nb_logger
    except (ImportError, RuntimeError):
        state.nonebot_mode = False
        config_manager = None
        state.logger = logging.getLogger("komari_help")


def _load_standalone_config() -> DynamicConfigSchema:
    config_path = Path("config/config_manager/komari_help_config.json")
    if config_path.exists():
        try:
            return DynamicConfigSchema(
                **json.loads(config_path.read_text(encoding="utf-8"))
            )
        except (json.JSONDecodeError, TypeError) as exc:
            state.logger.warning(
                "[Komari Help] 配置文件解析失败: %s，使用默认配置", exc
            )
    else:
        state.logger.warning(
            "[Komari Help] 配置文件不存在: %s，使用默认配置", config_path
        )
    return DynamicConfigSchema()


def get_config() -> DynamicConfigSchema:
    if state.nonebot_mode:
        assert config_manager is not None, (
            "config_manager 应该在 NoneBot 模式下已初始化"
        )
        return config_manager.get()
    if state.standalone_config is None:
        state.standalone_config = _load_standalone_config()
    return state.standalone_config


def get_db_config(config: DynamicConfigSchema) -> DatabaseConfigSchema:
    if state.nonebot_mode:
        return get_effective_database_config(config)

    shared_config_path = Path("config/config_manager/database_config.json")
    if shared_config_path.exists():
        try:
            shared = load_database_config_from_file(shared_config_path)
            return merge_database_config(shared, config)
        except Exception as exc:
            state.logger.warning(
                "[Komari Help] 共享数据库配置解析失败: %s，回退到本地配置",
                exc,
            )
    return merge_database_config(DatabaseConfigSchema(), config)


UNSET: Final[object] = object()


class EmbeddingDimensionMissingError(RuntimeError):
    """缺少可用的 embedding 维度配置。"""


class HelpEngine:
    """帮助文档检索与管理引擎。"""

    def __init__(self) -> None:
        self._pool: Any = None
        self._embedding_service: Any = None
        self._keyword_index: dict[str, set[int]] = defaultdict(set)
        self._index_loaded = False

    async def initialize(self) -> None:
        state.logger.info("[Komari Help] 正在初始化帮助引擎...")
        config = get_config()

        try:
            if state.nonebot_mode:
                state.logger.info("[Komari Help] 使用全局 EmbeddingProvider 服务")
            elif getattr(self, "_embedding_service", None) is None:
                from komari_bot.plugins.embedding_provider.config_schema import (
                    DynamicConfigSchema as EmbedConfigSchema,
                )
                from komari_bot.plugins.embedding_provider.embedding_service import (
                    EmbeddingService,
                )

                config_path = Path(
                    "config/config_manager/embedding_provider_config.json"
                )
                if config_path.exists():
                    embed_config = EmbedConfigSchema(
                        **json.loads(config_path.read_text(encoding="utf-8"))
                    )
                else:
                    embed_config = EmbedConfigSchema()

                self._embedding_service = EmbeddingService(embed_config)
                state.logger.info("[Komari Help] 独立嵌入服务初始化完成")

            if self._pool is None:
                db_config = get_db_config(config)
                self._pool = await create_postgres_pool(db_config, command_timeout=30)
                expected_dimension = self._resolve_expected_embedding_dimension()
                await self._ensure_storage_schema(expected_dimension)
                await self._validate_embedding_dimension(expected_dimension)

            await self._build_keyword_index()
            state.logger.info("[Komari Help] 帮助引擎初始化完成")
        except Exception:
            try:
                await self.close()
            except Exception:
                state.logger.exception("[Komari Help] 初始化失败后的清理失败")
            raise

    def _resolve_expected_embedding_dimension(self) -> int | None:
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
        elif self._embedding_service is not None:
            expected_dimension = int(self._embedding_service.config.embedding_dimension)
        return expected_dimension

    async def _ensure_storage_schema(self, expected_dimension: int | None) -> None:
        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化")
        if expected_dimension is None:
            raise EmbeddingDimensionMissingError

        await apply_schema_statements(
            self._pool,
            statements=build_help_schema_statements(expected_dimension),
        )
        if build_help_embedding_index_statement(expected_dimension) is None:
            state.logger.warning(
                "[Komari Help] embedding 维度 %s 超过 pgvector HNSW 上限 %s，已跳过 idx_komari_help_embedding，语义检索将退化为顺序扫描。",
                expected_dimension,
                PGVECTOR_VECTOR_HNSW_MAX_DIMENSIONS,
            )

    async def _validate_embedding_dimension(
        self, expected_dimension: int | None
    ) -> None:
        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化")
        await ensure_vector_column_dimension(
            self._pool,
            table_name="komari_help",
            column_name="embedding",
            expected_dimension=expected_dimension,
            label="KomariHelp",
        )

    async def _build_keyword_index(self) -> None:
        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化")

        self._keyword_index.clear()
        self._index_loaded = False
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, title, plugin_name, keywords
                FROM komari_help
                """
            )
        for row in rows:
            help_id = int(row["id"])
            pieces = [
                *list(row.get("keywords") or []),
                str(row.get("title") or ""),
                str(row.get("plugin_name") or ""),
            ]
            for piece in pieces:
                for token in self._tokenize(piece):
                    self._keyword_index[token].add(help_id)
        self._index_loaded = True

    async def _get_embedding(self, text: str) -> list[float]:
        if state.nonebot_mode:
            from nonebot.plugin import require

            embedding_provider = require("embedding_provider")
            return await embedding_provider.embed(text)
        if self._embedding_service is None:
            raise RuntimeError("独立嵌入服务未初始化")
        return await self._embedding_service.embed(text)

    def _rewrite_query(self, query: str) -> str:
        rewritten = query
        for old, new in get_config().query_rewrite_rules.items():
            rewritten = rewritten.replace(old, new)
        return rewritten

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        cleaned = text.strip().lower()
        if not cleaned:
            return []
        parts = {cleaned}
        for chunk in cleaned.replace("/", " ").replace("-", " ").split():
            stripped = chunk.strip()
            if stripped:
                parts.add(stripped)
        return sorted(parts)

    async def search(
        self,
        query: str,
        limit: int | None = None,
        query_vec: list[float] | None = None,
    ) -> list[HelpSearchResult]:
        if not query or not query.strip():
            return []
        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化")

        config = get_config()
        result_limit = config.total_limit if limit is None else limit
        assert isinstance(result_limit, int), "limit should be int"

        original_query = query
        query = self._rewrite_query(query)
        if query != original_query and query_vec is not None:
            query_vec = None

        results: list[HelpSearchResult] = []
        seen_ids: set[int] = set()

        keyword_hits = await self._layer1_keyword_search(
            query, min(result_limit, config.layer1_limit)
        )
        for hit in keyword_hits:
            if hit.id in seen_ids:
                continue
            results.append(hit)
            seen_ids.add(hit.id)

        vector_limit = min(max(result_limit - len(results), 0), config.layer2_limit)
        if vector_limit > 0:
            vector_hits = await self._layer2_vector_search(
                query,
                vector_limit,
                seen_ids,
                query_vec=query_vec,
            )
            for hit in vector_hits:
                if hit.id in seen_ids:
                    continue
                results.append(hit)
                seen_ids.add(hit.id)

        return results[:result_limit]

    async def search_by_keyword(self, keyword: str) -> list[HelpSearchResult]:
        if not self._index_loaded:
            return []
        keyword_lower = keyword.lower().strip()
        if keyword_lower not in self._keyword_index:
            return []
        if self._pool is None:
            return []

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, category, plugin_name, title, content
                FROM komari_help
                WHERE id = ANY($1)
                ORDER BY created_at DESC
                """,
                list(self._keyword_index[keyword_lower]),
            )
        return [
            self._build_search_result(dict(row), similarity=1.0, source="keyword")
            for row in rows
        ]

    async def _layer1_keyword_search(
        self, query: str, limit: int
    ) -> list[HelpSearchResult]:
        if not self._index_loaded or limit <= 0:
            return []
        query_tokens = self._tokenize(query)
        matched_ids: set[int] = set()
        for token in query_tokens:
            matched_ids.update(self._keyword_index.get(token, set()))
            for indexed_token, help_ids in self._keyword_index.items():
                if token and token in indexed_token:
                    matched_ids.update(help_ids)
        if not matched_ids or self._pool is None:
            return []

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, category, plugin_name, title, content
                FROM komari_help
                WHERE id = ANY($1)
                ORDER BY created_at DESC
                LIMIT $2
                """,
                list(matched_ids),
                limit,
            )
        return [
            self._build_search_result(dict(row), similarity=1.0, source="keyword")
            for row in rows
        ]

    async def _layer2_vector_search(
        self,
        query: str,
        limit: int,
        exclude_ids: set[int],
        query_vec: list[float] | None = None,
    ) -> list[HelpSearchResult]:
        if self._pool is None or limit <= 0:
            return []

        config = get_config()
        if query_vec is None:
            query_vec = await self._get_embedding(query)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    id,
                    category,
                    plugin_name,
                    title,
                    content,
                    1 - (embedding <=> $1::vector) AS similarity
                FROM komari_help
                WHERE embedding IS NOT NULL AND id != ALL($2)
                ORDER BY embedding <=> $1::vector
                LIMIT $3
                """,
                str(query_vec),
                list(exclude_ids) if exclude_ids else [-1],
                limit,
            )
        return [
            self._build_search_result(
                dict(row), similarity=row["similarity"], source="vector"
            )
            for row in rows
            if float(row["similarity"]) >= config.similarity_threshold
        ]

    async def add_help(
        self,
        title: str,
        content: str,
        keywords: list[str],
        category: HelpCategory = "other",
        plugin_name: str | None = None,
        notes: str | None = None,
        *,
        is_auto_generated: bool = False,
    ) -> int:
        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化")
        embedding = await self._get_embedding(f"{title}\n{content}")
        async with self._pool.acquire() as conn:
            help_id = await conn.fetchval(
                """
                INSERT INTO komari_help (
                    title, content, keywords, category, plugin_name, notes, is_auto_generated, embedding
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id
                """,
                title,
                content,
                keywords,
                category,
                plugin_name,
                notes,
                is_auto_generated,
                str(embedding),
            )
        await self._build_keyword_index()
        return int(help_id)

    async def sync_auto_generated_help(
        self,
        *,
        plugin_name: str,
        title: str,
        content: str,
        keywords: list[str],
        category: HelpCategory = "feature",
        notes: str | None = None,
    ) -> bool:
        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化")

        async with self._pool.acquire() as conn:
            existing_row = await conn.fetchrow(
                """
                SELECT id, is_auto_generated
                FROM komari_help
                WHERE plugin_name = $1
                ORDER BY is_auto_generated DESC, id ASC
                LIMIT 1
                """,
                plugin_name,
            )

            if existing_row is not None and not bool(existing_row["is_auto_generated"]):
                return False

            embedding = await self._get_embedding(f"{title}\n{content}")
            if existing_row is None:
                await conn.execute(
                    """
                    INSERT INTO komari_help (
                        category, plugin_name, keywords, title, content, notes,
                        is_auto_generated, embedding
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, TRUE, $7)
                    """,
                    category,
                    plugin_name,
                    keywords,
                    title,
                    content,
                    notes,
                    str(embedding),
                )
            else:
                await conn.execute(
                    """
                    UPDATE komari_help
                    SET category = $2,
                        keywords = $3,
                        title = $4,
                        content = $5,
                        notes = $6,
                        embedding = $7,
                        is_auto_generated = TRUE,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = $1
                    """,
                    int(existing_row["id"]),
                    category,
                    keywords,
                    title,
                    content,
                    notes,
                    str(embedding),
                )
        await self._build_keyword_index()
        return True

    async def get_help(self, hid: int) -> HelpEntry | None:
        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化")
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, category, plugin_name, keywords, title, content, notes,
                       is_auto_generated, created_at, updated_at
                FROM komari_help
                WHERE id = $1
                """,
                hid,
            )
        if row is None:
            return None
        return self._build_help_entry(dict(row))

    async def list_help(
        self,
        *,
        limit: int,
        offset: int,
        query: str | None = None,
        category: HelpCategory | None = None,
    ) -> tuple[list[HelpEntry], int]:
        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化")

        conditions: list[str] = []
        params: list[object] = []
        param_idx = 1

        if query is not None:
            pattern = f"%{query.strip()}%"
            if pattern != "%%":
                conditions.append(
                    f"""
                    (
                        title ILIKE ${param_idx}
                        OR content ILIKE ${param_idx}
                        OR COALESCE(plugin_name, '') ILIKE ${param_idx}
                        OR EXISTS (
                            SELECT 1
                            FROM unnest(COALESCE(keywords, ARRAY[]::text[])) AS keyword
                            WHERE keyword ILIKE ${param_idx}
                        )
                    )
                    """
                )
                params.append(pattern)
                param_idx += 1

        if category is not None:
            conditions.append(f"category = ${param_idx}")
            params.append(category)
            param_idx += 1

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        count_query = f"""
            SELECT COUNT(*)
            FROM komari_help
            {where_clause}
        """
        data_query = f"""
            SELECT id, category, plugin_name, keywords, title, content, notes,
                   is_auto_generated, created_at, updated_at
            FROM komari_help
            {where_clause}
            ORDER BY created_at DESC
            LIMIT ${param_idx}
            OFFSET ${param_idx + 1}
        """

        async with self._pool.acquire() as conn:
            total = await conn.fetchval(count_query, *params)
            rows = await conn.fetch(data_query, *params, limit, offset)
        return [self._build_help_entry(dict(row)) for row in rows], int(total)

    async def delete_help(self, hid: int) -> bool:
        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化")
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id FROM komari_help WHERE id = $1", hid)
            if row is None:
                return False
            await conn.execute("DELETE FROM komari_help WHERE id = $1", hid)
        await self._build_keyword_index()
        return True

    async def update_help(
        self,
        hid: int,
        *,
        title: str | object = UNSET,
        content: str | object = UNSET,
        keywords: list[str] | object = UNSET,
        category: HelpCategory | object = UNSET,
        plugin_name: str | None | object = UNSET,
        notes: str | None | object = UNSET,
    ) -> bool:
        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化")
        if all(
            value is UNSET
            for value in [title, content, keywords, category, plugin_name, notes]
        ):
            raise ValueError("至少提供一个要更新的字段")

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT title, content, keywords, category, plugin_name, notes
                FROM komari_help
                WHERE id = $1
                """,
                hid,
            )
            if row is None:
                return False

            current_title = str(row["title"])
            current_content = str(row["content"])
            next_title = title if title is not UNSET else current_title
            next_content = content if content is not UNSET else current_content
            assert isinstance(next_title, str)
            assert isinstance(next_content, str)

            updates: list[str] = []
            params: list[object] = []
            param_idx = 2

            if title is not UNSET:
                updates.append(f"title = ${param_idx}")
                params.append(next_title)
                param_idx += 1
            if content is not UNSET:
                updates.append(f"content = ${param_idx}")
                params.append(next_content)
                param_idx += 1
            if keywords is not UNSET:
                updates.append(f"keywords = ${param_idx}")
                params.append(keywords)
                param_idx += 1
            if category is not UNSET:
                updates.append(f"category = ${param_idx}")
                params.append(category)
                param_idx += 1
            if plugin_name is not UNSET:
                updates.append(f"plugin_name = ${param_idx}")
                params.append(plugin_name)
                param_idx += 1
            if notes is not UNSET:
                updates.append(f"notes = ${param_idx}")
                params.append(notes)
                param_idx += 1

            if title is not UNSET or content is not UNSET:
                embedding = await self._get_embedding(f"{next_title}\n{next_content}")
                updates.append(f"embedding = ${param_idx}")
                params.append(str(embedding))
                param_idx += 1

            updates.append("updated_at = CURRENT_TIMESTAMP")
            await conn.execute(
                f"UPDATE komari_help SET {', '.join(updates)} WHERE id = $1",
                hid,
                *params,
            )
        await self._build_keyword_index()
        return True

    def _build_help_entry(self, payload: dict[str, Any]) -> HelpEntry:
        return HelpEntry(
            id=int(payload["id"]),
            category=payload["category"],
            plugin_name=payload.get("plugin_name"),
            keywords=list(payload.get("keywords") or []),
            title=str(payload["title"]),
            content=str(payload["content"]),
            notes=payload.get("notes"),
            is_auto_generated=bool(payload.get("is_auto_generated", False)),
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
        )

    def _build_search_result(
        self,
        payload: dict[str, Any],
        *,
        similarity: float,
        source: str,
    ) -> HelpSearchResult:
        return HelpSearchResult(
            id=int(payload["id"]),
            category=payload["category"],
            plugin_name=payload.get("plugin_name"),
            title=str(payload["title"]),
            content=str(payload["content"]),
            similarity=float(similarity),
            source=source,  # type: ignore[arg-type]
        )

    async def close(self) -> None:
        errors: list[BaseException] = []
        if self._embedding_service is not None:
            try:
                await self._embedding_service.cleanup()
            except Exception as exc:
                errors.append(exc)
                state.logger.exception("[Komari Help] 关闭独立嵌入服务失败")
            finally:
                self._embedding_service = None

        if self._pool is not None:
            try:
                await self._pool.close()
            except Exception as exc:
                errors.append(exc)
                state.logger.exception("[Komari Help] 关闭连接池失败")
            finally:
                self._pool = None

        self._keyword_index.clear()
        self._index_loaded = False
        if state.engine is self:
            state.engine = None
        if errors:
            raise errors[0]


def get_engine() -> HelpEngine | None:
    """获取全局引擎实例。"""
    return state.engine


async def initialize_engine() -> HelpEngine:
    """初始化全局引擎实例。"""
    if state.engine is None:
        engine = HelpEngine()
        await engine.initialize()
        state.engine = engine
    return state.engine
