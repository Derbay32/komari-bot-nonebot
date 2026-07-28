"""Prompt 专用 PostgreSQL 存储与运行时加载辅助。"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, TypeVar

from nonebot import logger

from komari_bot.common.database_config import get_shared_database_config
from komari_bot.common.postgres import create_postgres_pool

if TYPE_CHECKING:
    from collections.abc import Coroutine, Mapping
    from datetime import datetime

    import asyncpg

T = TypeVar("T")

_PROMPT_STORAGE_TIMEOUT_SECONDS = 5.0

_PROMPT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS komari_prompt_configs (
    resource_id VARCHAR(128) PRIMARY KEY,
    display_name VARCHAR(128) NOT NULL,
    prompt_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    version VARCHAR(32) NOT NULL DEFAULT '1.0',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

_PROMPT_UPDATED_AT_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_komari_prompt_configs_updated_at
    ON komari_prompt_configs (updated_at DESC);
"""


class PromptResourceProtocol(Protocol):
    """Prompt 资源需要提供的字段。"""

    @property
    def resource_id(self) -> str: ...

    @property
    def display_name(self) -> str: ...

    @property
    def defaults(self) -> dict[str, str]: ...


@dataclass(frozen=True, slots=True)
class StoredPrompt:
    """已存储的 prompt 配置。"""

    resource_id: str
    display_name: str
    prompt_data: dict[str, Any]
    version: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PromptValues:
    """合并 defaults 后的 prompt 值。"""

    values: dict[str, str]
    stored: StoredPrompt | None


class PromptStorage:
    """PostgreSQL prompt 存储同步门面。"""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="komari-prompt-storage",
            daemon=True,
        )
        self._pool: asyncpg.Pool | None = None
        self._init_lock = threading.RLock()
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run(self, coro: Coroutine[Any, Any, T]) -> T:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=_PROMPT_STORAGE_TIMEOUT_SECONDS)
        except FutureTimeoutError as exc:
            future.cancel()
            msg = "Prompt 存储操作超时"
            raise RuntimeError(msg) from exc

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            config = get_shared_database_config()
            pool = await create_postgres_pool(config)
            try:
                await self._ensure_schema(pool)
            except Exception:
                await pool.close()
                raise
            self._pool = pool
        return self._pool

    async def _ensure_schema(self, pool: asyncpg.Pool) -> None:
        async with pool.acquire() as conn:
            await conn.execute(_PROMPT_TABLE_DDL)
            await conn.execute(_PROMPT_UPDATED_AT_INDEX_DDL)

    def ensure_schema(self) -> None:
        """确保 prompt 配置表存在。"""
        with self._init_lock:
            self._run(self._get_pool())

    def fetch(self, resource_id: str) -> StoredPrompt | None:
        """按资源 ID 读取 prompt 配置。"""
        return self._run(self._fetch(resource_id))

    async def _fetch(self, resource_id: str) -> StoredPrompt | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT resource_id, display_name, prompt_data, version, updated_at
                FROM komari_prompt_configs
                WHERE resource_id = $1
                """,
                resource_id,
            )
        if row is None:
            return None
        return _stored_prompt_from_row(row)

    def upsert(
        self,
        *,
        resource_id: str,
        display_name: str,
        prompt_data: dict[str, str],
        version: str = "1.0",
    ) -> StoredPrompt:
        """写入或更新 prompt 配置。"""
        return self._run(
            self._upsert(
                resource_id=resource_id,
                display_name=display_name,
                prompt_data=prompt_data,
                version=version,
            )
        )

    async def _upsert(
        self,
        *,
        resource_id: str,
        display_name: str,
        prompt_data: dict[str, str],
        version: str,
    ) -> StoredPrompt:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO komari_prompt_configs (
                    resource_id,
                    display_name,
                    prompt_data,
                    version
                )
                VALUES ($1, $2, $3::jsonb, $4)
                ON CONFLICT (resource_id) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    prompt_data = EXCLUDED.prompt_data,
                    version = EXCLUDED.version,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING resource_id, display_name, prompt_data, version, updated_at
                """,
                resource_id,
                display_name,
                json.dumps(prompt_data, ensure_ascii=False),
                version,
            )
        if row is None:
            msg = f"Prompt 写入后未返回记录: {resource_id}"
            raise RuntimeError(msg)
        return _stored_prompt_from_row(row)

    def update_if_unchanged(
        self,
        *,
        resource_id: str,
        display_name: str,
        prompt_data: dict[str, str],
        version: str,
        expected_updated_at: datetime,
    ) -> StoredPrompt | None:
        """仅在记录未被其他写入修改时更新 prompt 配置。"""
        return self._run(
            self._update_if_unchanged(
                resource_id=resource_id,
                display_name=display_name,
                prompt_data=prompt_data,
                version=version,
                expected_updated_at=expected_updated_at,
            )
        )

    async def _update_if_unchanged(
        self,
        *,
        resource_id: str,
        display_name: str,
        prompt_data: dict[str, str],
        version: str,
        expected_updated_at: datetime,
    ) -> StoredPrompt | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE komari_prompt_configs
                SET
                    display_name = $2,
                    prompt_data = $3::jsonb,
                    version = $4,
                    updated_at = CURRENT_TIMESTAMP
                WHERE resource_id = $1
                  AND updated_at = $5
                RETURNING resource_id, display_name, prompt_data, version, updated_at
                """,
                resource_id,
                display_name,
                json.dumps(prompt_data, ensure_ascii=False),
                version,
                expected_updated_at,
            )
        if row is None:
            return None
        return _stored_prompt_from_row(row)

    def close(self) -> None:
        """关闭后台连接池和事件循环。"""
        if not self._loop.is_running():
            return
        try:
            if self._pool is not None:
                self._run(self._pool.close())
                self._pool = None
        except Exception as exc:
            logger.warning(f"Prompt 存储关闭连接池失败: {exc}")
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)


class _StorageState:
    storage: ClassVar[PromptStorage | None] = None
    lock: ClassVar[threading.RLock] = threading.RLock()


def _stored_prompt_from_row(row: Any) -> StoredPrompt:
    raw_data = row["prompt_data"]
    prompt_data = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
    return StoredPrompt(
        resource_id=str(row["resource_id"]),
        display_name=str(row["display_name"]),
        prompt_data=dict(prompt_data),
        version=str(row["version"]),
        updated_at=row["updated_at"],
    )


def get_prompt_storage() -> PromptStorage:
    """获取全局 PostgreSQL prompt 存储。"""
    if _StorageState.storage is None:
        with _StorageState.lock:
            if _StorageState.storage is None:
                _StorageState.storage = PromptStorage()
    assert _StorageState.storage is not None
    return _StorageState.storage


def close_prompt_storage_if_created() -> None:
    """关闭已创建的全局 Prompt 存储，不在关闭阶段创建新实例。"""
    with _StorageState.lock:
        storage = _StorageState.storage
        if storage is not None:
            storage.close()
            _StorageState.storage = None


def merge_prompt_values(
    defaults: dict[str, str],
    prompt_data: dict[str, Any] | None,
) -> dict[str, str]:
    """将存储值按允许字段合并到 defaults。"""
    values = dict(defaults)
    if prompt_data is None:
        return values
    for key in defaults:
        value = prompt_data.get(key)
        if isinstance(value, str):
            values[key] = value.rstrip("\n")
    return values


def validate_prompt_values(
    defaults: dict[str, str],
    values: Mapping[str, object],
) -> dict[str, str]:
    """校验 prompt 字段并返回清洗后的完整数据。"""
    unknown_fields = sorted(set(values) - set(defaults))
    if unknown_fields:
        fields = ", ".join(unknown_fields)
        msg = f"存在未知提示词字段: {fields}"
        raise ValueError(msg)

    cleaned = dict(defaults)
    for key, value in values.items():
        if not isinstance(value, str) or not value.strip():
            msg = f"提示词字段 {key} 必须是非空字符串"
            raise ValueError(msg)
        cleaned[key] = value.rstrip("\n")
    return cleaned


def load_prompt_values(resource: PromptResourceProtocol) -> PromptValues:
    """从 PG 读取 prompt，并与 defaults 合并。"""
    storage = get_prompt_storage()
    stored = storage.fetch(resource.resource_id)
    values = merge_prompt_values(
        defaults=resource.defaults,
        prompt_data=stored.prompt_data if stored is not None else None,
    )
    if stored is not None:
        stored_keys = set(stored.prompt_data)
        merged_keys = set(values)
        added_keys = merged_keys - stored_keys
        removed_keys = stored_keys - merged_keys
        value_changed = any(
            stored.prompt_data.get(key) != values[key]
            for key in stored_keys & merged_keys
        )
        if added_keys or value_changed:
            synced: StoredPrompt | None = None
            try:
                prompt_data = dict(stored.prompt_data)
                prompt_data.update(validate_prompt_values(resource.defaults, values))
                synced = storage.update_if_unchanged(
                    resource_id=resource.resource_id,
                    display_name=resource.display_name,
                    prompt_data=prompt_data,
                    version=stored.version,
                    expected_updated_at=stored.updated_at,
                )
                if synced is None:
                    latest = storage.fetch(resource.resource_id)
                    if latest is not None:
                        stored = latest
                        values = merge_prompt_values(
                            defaults=resource.defaults,
                            prompt_data=latest.prompt_data,
                        )
                    logger.warning(
                        f"Prompt 配置自动同步跳过: "
                        f"resource_id={resource.resource_id}, "
                        "reason=stored_changed"
                    )
                else:
                    stored = synced
            except Exception as exc:
                logger.warning(
                    f"Prompt 配置自动同步失败: "
                    f"resource_id={resource.resource_id}, "
                    f"added_keys={sorted(added_keys)}, "
                    f"removed_keys={sorted(removed_keys)}, "
                    f"sync_result=failed, error={exc}"
                )
            else:
                if synced is not None:
                    logger.info(
                        f"Prompt 配置已自动同步: "
                        f"resource_id={resource.resource_id}, "
                        f"added_keys={sorted(added_keys)}, "
                        f"removed_keys={sorted(removed_keys)}, "
                        "sync_result=success"
                    )
    return PromptValues(values=values, stored=stored)


def save_prompt_values(
    resource: PromptResourceProtocol,
    values: Mapping[str, object],
) -> StoredPrompt:
    """校验并保存 prompt 配置。"""
    cleaned = validate_prompt_values(resource.defaults, values)
    return get_prompt_storage().upsert(
        resource_id=resource.resource_id,
        display_name=resource.display_name,
        prompt_data=cleaned,
    )


class PromptTemplateLoader:
    """运行时 prompt 模板加载器。"""

    def __init__(
        self,
        *,
        resource_id: str,
        display_name: str,
        defaults: dict[str, str],
        log_prefix: str,
    ) -> None:
        self.resource_id = resource_id
        self.display_name = display_name
        self.defaults = defaults
        self._log_prefix = log_prefix
        self._cache: dict[str, str] = {}
        self._cache_updated_at: datetime | None = None

    def get_template(self) -> dict[str, str]:
        """获取最新 prompt 模板，PG 失败时回退缓存或 defaults。"""
        try:
            loaded = load_prompt_values(self)
        except Exception:
            logger.warning(
                "{} Prompt 配置读取失败，使用缓存或默认值",
                self._log_prefix,
                exc_info=True,
            )
            if not self._cache:
                self._cache = dict(self.defaults)
            return dict(self._cache)

        updated_at = loaded.stored.updated_at if loaded.stored is not None else None
        if self._cache and updated_at == self._cache_updated_at:
            return dict(self._cache)

        self._cache = loaded.values
        self._cache_updated_at = updated_at
        if loaded.stored is None:
            logger.warning(
                "{} Prompt 配置未写入 PostgreSQL，使用默认值",
                self._log_prefix,
            )
        else:
            logger.info(
                "{} Prompt 配置已从 PostgreSQL 加载: {}",
                self._log_prefix,
                self.resource_id,
            )
        return dict(self._cache)
