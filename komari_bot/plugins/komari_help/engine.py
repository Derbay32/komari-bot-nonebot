"""Komari Help 帮助文档核心引擎。"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections import defaultdict
from typing import Any, Final, cast

from komari_bot.db.orm_connection import get_shared_orm_connection_pool
from komari_bot.db.pgvector_schema import ensure_vector_column_dimension
from komari_bot.db.sql_like_utils import escape_like_pattern
from komari_bot.db.versioned_keyword_index import VersionedKeywordIndex
from komari_bot.llm.content_budget import (
    CONTENT_TEXT_BUDGET,
    IDENTIFIER_TEXT_BUDGET,
    KEYWORD_TEXT_BUDGET,
    NOTES_TEXT_BUDGET,
    QUERY_TEXT_BUDGET,
    TITLE_TEXT_BUDGET,
    normalize_keywords,
    normalize_optional_text,
    normalize_required_text,
    validate_text_budget,
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
_engine_initialize_lock: asyncio.Lock | None = None
_engine_initialize_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_engine_initialize_lock() -> asyncio.Lock:
    """获取绑定当前事件循环的全局初始化锁。"""
    global _engine_initialize_lock, _engine_initialize_lock_loop  # noqa: PLW0603
    loop = asyncio.get_running_loop()
    if _engine_initialize_lock is None or _engine_initialize_lock_loop is not loop:
        _engine_initialize_lock = asyncio.Lock()
        _engine_initialize_lock_loop = loop
    return _engine_initialize_lock

if state.nonebot_mode:
    try:
        from nonebot import logger as nb_logger
        from nonebot.plugin import require

        require("config_manager")
        from komari_bot.plugins import config_manager as config_manager_plugin

        config_manager = config_manager_plugin.get_config_manager(
            "komari_help", DynamicConfigSchema
        )
        state.logger = nb_logger
    except (ImportError, RuntimeError):
        state.nonebot_mode = False
        config_manager = None
        state.logger = logging.getLogger("komari_help")


def _load_standalone_config() -> DynamicConfigSchema:
    try:
        from komari_bot.plugins.config_manager.storage import get_config_storage

        stored = get_config_storage().fetch("komari_help")
    except Exception as exc:
        state.logger.warning(f"[Komari Help] PG 配置读取失败: {exc}，使用默认配置")
        return DynamicConfigSchema()
    if stored is None:
        state.logger.warning("[Komari Help] PG 中无配置，使用默认配置")
        return DynamicConfigSchema()
    state.logger.info("[Komari Help] 已从 PostgreSQL 加载独立模式配置")
    return DynamicConfigSchema(**stored.config_data)


def get_config() -> DynamicConfigSchema:
    if state.nonebot_mode:
        assert config_manager is not None, (
            "config_manager 应该在 NoneBot 模式下已初始化"
        )
        return cast("DynamicConfigSchema", config_manager.get())
    if state.standalone_config is None:
        state.standalone_config = _load_standalone_config()
    return state.standalone_config


def get_disabled_auto_help_plugins() -> set[str]:
    raw_value = getattr(get_config(), "disabled_auto_help_plugins", [])
    if not isinstance(raw_value, list):
        return set()
    return {str(plugin_name) for plugin_name in raw_value if str(plugin_name).strip()}


UNSET: Final[object] = object()
HELP_SCAN_LEASE_NAME: Final[str] = "plugin_metadata"
HELP_SCAN_LEASE_SECONDS_MIN: Final[int] = 10
HELP_SCAN_LEASE_SECONDS_MAX: Final[int] = 3600
HELP_UPDATE_MAX_RETRIES: Final[int] = 5


class HelpEngine:
    """帮助文档检索与管理引擎。"""

    def __init__(self) -> None:
        self._pool: Any = None
        self._embedding_service: Any = None
        self._keyword_index = VersionedKeywordIndex("komari_help")
        self._initialize_lock: asyncio.Lock | None = None
        self._initialize_lock_loop: asyncio.AbstractEventLoop | None = None
        self._initialized = False

    async def initialize(self) -> None:
        """单飞初始化帮助引擎。"""
        async with self._get_initialize_lock():
            if self._initialized:
                return
            await self._initialize_once()

    def _get_initialize_lock(self) -> asyncio.Lock:
        """获取绑定当前事件循环的实例初始化锁。"""
        loop = asyncio.get_running_loop()
        if self._initialize_lock is None or self._initialize_lock_loop is not loop:
            self._initialize_lock = asyncio.Lock()
            self._initialize_lock_loop = loop
        return self._initialize_lock

    async def _initialize_once(self) -> None:
        state.logger.info("[Komari Help] 正在初始化帮助引擎...")
        get_config()

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

                try:
                    from komari_bot.plugins.config_manager.storage import (
                        get_config_storage,
                    )

                    stored = get_config_storage().fetch("embedding_provider")
                except Exception as exc:
                    state.logger.warning(
                        f"[Komari Help] embedding PG 配置读取失败: {exc}，使用默认配置"
                    )
                    stored = None
                embed_config = (
                    EmbedConfigSchema(**stored.config_data)
                    if stored is not None
                    else EmbedConfigSchema()
                )

                self._embedding_service = EmbeddingService(embed_config)
                state.logger.info("[Komari Help] 独立嵌入服务初始化完成")

            if self._pool is None:
                self._pool = get_shared_orm_connection_pool()
                state.logger.info("[Komari Help] 已接入共享数据库引擎")
                expected_dimension = self._resolve_expected_embedding_dimension()
                await self._validate_embedding_dimension(expected_dimension)

            await self._build_keyword_index()
            self._initialized = True
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
            from komari_bot.plugins import embedding_provider

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

        await self._keyword_index.rebuild(
            self._pool,
            self._load_keyword_index_entries,
        )

    async def rebuild_keyword_index(self) -> None:
        """公开的索引重建入口，避免扫描器依赖引擎私有实现。"""
        await self._build_keyword_index()

    @staticmethod
    def _validate_scan_lease(owner_token: str, lease_seconds: int) -> str:
        normalized_owner = owner_token.strip()
        if not normalized_owner:
            message = "扫描租约 owner_token 不能为空"
            raise ValueError(message)
        validate_text_budget(
            normalized_owner,
            label="扫描租约 owner_token",
            budget=IDENTIFIER_TEXT_BUDGET,
        )
        if not (
            HELP_SCAN_LEASE_SECONDS_MIN
            <= lease_seconds
            <= HELP_SCAN_LEASE_SECONDS_MAX
        ):
            message = (
                "扫描租约时长必须在 "
                f"{HELP_SCAN_LEASE_SECONDS_MIN} 到 {HELP_SCAN_LEASE_SECONDS_MAX} 秒之间"
            )
            raise ValueError(message)
        return normalized_owner

    async def acquire_scan_lease(
        self,
        owner_token: str,
        *,
        lease_seconds: int,
    ) -> bool:
        """跨 worker 原子抢占插件元数据扫描租约。"""
        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化")
        normalized_owner = self._validate_scan_lease(owner_token, lease_seconds)
        async with self._pool.acquire() as conn:
            claimed_owner = await conn.fetchval(
                """
                INSERT INTO komari_help_scan_leases (
                    lease_name,
                    owner_token,
                    lease_expires_at,
                    updated_at
                )
                VALUES (
                    $1,
                    $2,
                    CURRENT_TIMESTAMP + ($3::double precision * INTERVAL '1 second'),
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT (lease_name) DO UPDATE
                SET owner_token = EXCLUDED.owner_token,
                    lease_expires_at = EXCLUDED.lease_expires_at,
                    updated_at = CURRENT_TIMESTAMP
                WHERE komari_help_scan_leases.lease_expires_at <= CURRENT_TIMESTAMP
                   OR komari_help_scan_leases.owner_token = EXCLUDED.owner_token
                RETURNING owner_token
                """,
                HELP_SCAN_LEASE_NAME,
                normalized_owner,
                lease_seconds,
            )
        return claimed_owner == normalized_owner

    async def renew_scan_lease(
        self,
        owner_token: str,
        *,
        lease_seconds: int,
    ) -> bool:
        """仅允许当前且尚未过期的 owner 续租扫描任务。"""
        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化")
        normalized_owner = self._validate_scan_lease(owner_token, lease_seconds)
        async with self._pool.acquire() as conn:
            renewed_owner = await conn.fetchval(
                """
                UPDATE komari_help_scan_leases
                SET lease_expires_at = (
                        CURRENT_TIMESTAMP
                        + ($3::double precision * INTERVAL '1 second')
                    ),
                    updated_at = CURRENT_TIMESTAMP
                WHERE lease_name = $1
                  AND owner_token = $2
                  AND lease_expires_at > CURRENT_TIMESTAMP
                RETURNING owner_token
                """,
                HELP_SCAN_LEASE_NAME,
                normalized_owner,
                lease_seconds,
            )
        return renewed_owner == normalized_owner

    async def release_scan_lease(self, owner_token: str) -> None:
        """幂等释放属于当前 owner 的扫描租约。"""
        if self._pool is None:
            return
        normalized_owner = owner_token.strip()
        if not normalized_owner:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                DELETE FROM komari_help_scan_leases
                WHERE lease_name = $1
                  AND owner_token = $2
                """,
                HELP_SCAN_LEASE_NAME,
                normalized_owner,
            )

    async def _load_keyword_index_entries(self, conn: Any) -> dict[str, set[int]]:
        """从同一数据库快照加载帮助关键词映射。"""
        rows = await conn.fetch(
            """
            SELECT id, title, plugin_name, keywords
            FROM komari_help
            """
        )
        entries: dict[str, set[int]] = defaultdict(set)
        for row in rows:
            help_id = int(row["id"])
            pieces = [
                *list(row.get("keywords") or []),
                str(row.get("title") or ""),
                str(row.get("plugin_name") or ""),
            ]
            for piece in pieces:
                for token in self._tokenize(piece):
                    entries[token].add(help_id)
        return entries

    async def _ensure_keyword_index_fresh(self) -> None:
        """按版本戳刷新其他 worker 已修改的索引。"""
        if self._pool is None:
            return
        await self._keyword_index.ensure_fresh(
            self._pool,
            self._load_keyword_index_entries,
        )

    async def _get_embedding(self, text: str) -> list[float]:
        if state.nonebot_mode:
            from komari_bot.plugins import embedding_provider

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
        raw_query = query
        query = normalize_required_text(
            query,
            label="查询文本",
            budget=QUERY_TEXT_BUDGET,
        )
        if query != raw_query:
            query_vec = None
        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化")

        config = get_config()
        result_limit = config.total_limit if limit is None else limit
        assert isinstance(result_limit, int), "limit should be int"

        original_query = query
        query = self._rewrite_query(query)
        validate_text_budget(
            query,
            label="改写后查询文本",
            budget=QUERY_TEXT_BUDGET,
        )
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
        if not keyword.strip():
            return []
        keyword = normalize_required_text(
            keyword,
            label="关键词查询",
            budget=KEYWORD_TEXT_BUDGET,
        )
        await self._ensure_keyword_index_fresh()
        if not self._keyword_index.loaded:
            return []
        keyword_lower = keyword.lower().strip()
        entries = self._keyword_index.entries
        if keyword_lower not in entries:
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
                list(entries[keyword_lower]),
            )
        return [
            self._build_search_result(dict(row), similarity=1.0, source="keyword")
            for row in rows
        ]

    async def _layer1_keyword_search(
        self, query: str, limit: int
    ) -> list[HelpSearchResult]:
        await self._ensure_keyword_index_fresh()
        if not self._keyword_index.loaded or limit <= 0:
            return []
        query_tokens = self._tokenize(query)
        matched_ids: set[int] = set()
        entries = self._keyword_index.entries
        for token in query_tokens:
            matched_ids.update(entries.get(token, frozenset()))
            for indexed_token, help_ids in entries.items():
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
        title = normalize_required_text(
            title,
            label="帮助标题",
            budget=TITLE_TEXT_BUDGET,
        )
        content = normalize_required_text(
            content,
            label="帮助内容",
            budget=CONTENT_TEXT_BUDGET,
        )
        keywords = normalize_keywords(keywords, require_nonempty=False)
        plugin_name = normalize_optional_text(
            plugin_name,
            label="插件名",
            budget=IDENTIFIER_TEXT_BUDGET,
        )
        notes = normalize_optional_text(
            notes,
            label="备注",
            budget=NOTES_TEXT_BUDGET,
        )
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
        rebuild_index: bool = True,
    ) -> bool:
        if plugin_name in get_disabled_auto_help_plugins():
            return False

        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化")

        plugin_name = normalize_required_text(
            plugin_name,
            label="插件名",
            budget=IDENTIFIER_TEXT_BUDGET,
        )
        title = normalize_required_text(
            title,
            label="帮助标题",
            budget=TITLE_TEXT_BUDGET,
        )
        content = normalize_required_text(
            content,
            label="帮助内容",
            budget=CONTENT_TEXT_BUDGET,
        )
        keywords = normalize_keywords(keywords, require_nonempty=False)
        notes = normalize_optional_text(
            notes,
            label="备注",
            budget=NOTES_TEXT_BUDGET,
        )

        async with self._pool.acquire() as conn:
            existing_row = await conn.fetchrow(
                """
                SELECT
                    id,
                    is_auto_generated,
                    category,
                    title,
                    content,
                    keywords,
                    notes
                FROM komari_help
                WHERE plugin_name = $1
                ORDER BY is_auto_generated ASC, id ASC
                LIMIT 1
                """,
                plugin_name,
            )

        if existing_row is not None and not bool(existing_row["is_auto_generated"]):
            return False

        if existing_row is not None:
            existing_keywords = set(existing_row["keywords"] or [])
            new_keywords = set(keywords)
            if (
                str(existing_row["category"] or "other") == category
                and str(existing_row["title"] or "") == title
                and str(existing_row["content"] or "") == content
                and existing_keywords == new_keywords
                and existing_row["notes"] == notes
            ):
                return False

        # 外部 embedding 调用不能占用 PostgreSQL 连接；写回由部分唯一索引和
        # UPSERT 保证多个 worker 即使同时到达也只保留一条自动帮助。
        embedding = await self._get_embedding(f"{title}\n{content}")
        async with self._pool.acquire() as conn:
            changed_id = await conn.fetchval(
                """
                INSERT INTO komari_help (
                    category,
                    plugin_name,
                    keywords,
                    title,
                    content,
                    notes,
                    is_auto_generated,
                    embedding
                )
                SELECT $1, $2, $3, $4, $5, $6, TRUE, $7
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM komari_help
                    WHERE plugin_name = $2
                      AND is_auto_generated = FALSE
                )
                ON CONFLICT (plugin_name)
                WHERE is_auto_generated = TRUE
                  AND plugin_name IS NOT NULL
                DO UPDATE
                SET category = EXCLUDED.category,
                    keywords = EXCLUDED.keywords,
                    title = EXCLUDED.title,
                    content = EXCLUDED.content,
                    notes = EXCLUDED.notes,
                    embedding = EXCLUDED.embedding,
                    updated_at = CURRENT_TIMESTAMP
                WHERE (
                    komari_help.category,
                    komari_help.keywords,
                    komari_help.title,
                    komari_help.content,
                    komari_help.notes
                ) IS DISTINCT FROM (
                    EXCLUDED.category,
                    EXCLUDED.keywords,
                    EXCLUDED.title,
                    EXCLUDED.content,
                    EXCLUDED.notes
                )
                RETURNING id
                """,
                category,
                plugin_name,
                keywords,
                title,
                content,
                notes,
                str(embedding),
            )
        if changed_id is None:
            return False
        if rebuild_index:
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
            keyword = query.strip()
            if keyword:
                validate_text_budget(
                    keyword,
                    label="列表查询文本",
                    budget=QUERY_TEXT_BUDGET,
                )
                pattern = f"%{escape_like_pattern(keyword)}%"
                conditions.append(
                    f"""
                    (
                        title ILIKE ${param_idx} ESCAPE '\\'
                        OR content ILIKE ${param_idx} ESCAPE '\\'
                        OR COALESCE(plugin_name, '') ILIKE ${param_idx} ESCAPE '\\'
                        OR EXISTS (
                            SELECT 1
                            FROM unnest(COALESCE(keywords, ARRAY[]::text[])) AS keyword
                            WHERE keyword ILIKE ${param_idx} ESCAPE '\\'
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

    async def delete_auto_generated_help_by_plugins(
        self,
        plugin_names: set[str],
        *,
        rebuild_index: bool = True,
    ) -> int:
        normalized_plugin_names = {
            plugin_name.strip() for plugin_name in plugin_names if plugin_name.strip()
        }
        if not normalized_plugin_names:
            return 0
        if self._pool is None:
            raise RuntimeError("数据库连接池未初始化")

        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM komari_help
                WHERE plugin_name = ANY($1::text[])
                  AND is_auto_generated = TRUE
                """,
                sorted(normalized_plugin_names),
            )

        deleted_count = int(str(result).split()[-1]) if result else 0
        if deleted_count > 0 and rebuild_index:
            await self._build_keyword_index()
        return deleted_count

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

        has_title_update = title is not UNSET
        title_value = ""
        if has_title_update:
            assert isinstance(title, str)
            title_value = normalize_required_text(
                title,
                label="帮助标题",
                budget=TITLE_TEXT_BUDGET,
            )

        has_content_update = content is not UNSET
        content_value = ""
        if has_content_update:
            assert isinstance(content, str)
            content_value = normalize_required_text(
                content,
                label="帮助内容",
                budget=CONTENT_TEXT_BUDGET,
            )

        has_keywords_update = keywords is not UNSET
        keywords_value: list[str] = []
        if has_keywords_update:
            assert isinstance(keywords, list)
            keywords_value = normalize_keywords(keywords, require_nonempty=False)

        has_category_update = category is not UNSET
        category_value: HelpCategory = "other"
        if has_category_update:
            assert isinstance(category, str)
            category_value = cast("HelpCategory", category)

        has_plugin_name_update = plugin_name is not UNSET
        plugin_name_value: str | None = None
        if has_plugin_name_update:
            assert plugin_name is None or isinstance(plugin_name, str)
            plugin_name_value = normalize_optional_text(
                plugin_name,
                label="插件名",
                budget=IDENTIFIER_TEXT_BUDGET,
            )

        has_notes_update = notes is not UNSET
        notes_value: str | None = None
        if has_notes_update:
            assert notes is None or isinstance(notes, str)
            notes_value = normalize_optional_text(
                notes,
                label="备注",
                budget=NOTES_TEXT_BUDGET,
            )

        for _attempt in range(HELP_UPDATE_MAX_RETRIES):
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT
                        title,
                        content,
                        keywords,
                        category,
                        plugin_name,
                        notes,
                        xmin::text AS row_version
                    FROM komari_help
                    WHERE id = $1
                    """,
                    hid,
                )
            if row is None:
                return False

            next_title = title_value if has_title_update else str(row["title"])
            next_content = (
                content_value if has_content_update else str(row["content"])
            )
            row_version = str(row["row_version"])
            updates: list[str] = []
            params: list[object] = []
            param_idx = 3

            if has_title_update:
                updates.append(f"title = ${param_idx}")
                params.append(title_value)
                param_idx += 1
            if has_content_update:
                updates.append(f"content = ${param_idx}")
                params.append(content_value)
                param_idx += 1
            if has_keywords_update:
                updates.append(f"keywords = ${param_idx}")
                params.append(keywords_value)
                param_idx += 1
            if has_category_update:
                updates.append(f"category = ${param_idx}")
                params.append(category_value)
                param_idx += 1
            if has_plugin_name_update:
                updates.append(f"plugin_name = ${param_idx}")
                params.append(plugin_name_value)
                param_idx += 1
            if has_notes_update:
                updates.append(f"notes = ${param_idx}")
                params.append(notes_value)
                param_idx += 1

            if has_title_update or has_content_update:
                # embedding 属于外部网络调用，必须在释放数据库连接后执行。
                embedding = await self._get_embedding(f"{next_title}\n{next_content}")
                updates.append(f"embedding = ${param_idx}")
                params.append(str(embedding))

            updates.append("updated_at = CURRENT_TIMESTAMP")
            async with self._pool.acquire() as conn:
                updated_id = await conn.fetchval(
                    f"""
                    UPDATE komari_help
                    SET {", ".join(updates)}
                    WHERE id = $1
                      AND xmin::text = $2
                    RETURNING id
                    """,
                    hid,
                    row_version,
                    *params,
                )
            if updated_id is not None:
                await self._build_keyword_index()
                return True

        raise RuntimeError("帮助条目被并发修改次数过多，请稍后重试")

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

        await self._keyword_index.reset()
        self._initialized = False
        if state.engine is self:
            state.engine = None
        if errors:
            raise errors[0]


def get_engine() -> HelpEngine | None:
    """获取全局引擎实例。"""
    return state.engine


async def initialize_engine() -> HelpEngine:
    """初始化全局引擎实例。"""
    async with _get_engine_initialize_lock():
        if state.engine is None:
            engine = HelpEngine()
            await engine.initialize()
            state.engine = engine
    return state.engine
