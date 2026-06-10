"""config_manager 的 PostgreSQL 存储层。"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from nonebot import logger

from komari_bot.common.database_config import get_shared_database_config
from komari_bot.common.postgres import create_postgres_pool

T = TypeVar("T")

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from datetime import datetime

    import asyncpg

_CONFIG_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS komari_plugin_configs (
    plugin_name VARCHAR(128) PRIMARY KEY,
    schema_name VARCHAR(128) NOT NULL,
    config_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    version VARCHAR(32) NOT NULL DEFAULT '1.0',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

_CONFIG_TABLE_UPDATED_AT_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_komari_plugin_configs_updated_at
    ON komari_plugin_configs (updated_at DESC);
"""
_CONFIG_STORAGE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class StoredConfig:
    """已存储的插件配置。"""

    plugin_name: str
    schema_name: str
    config_data: dict[str, Any]
    version: str
    updated_at: datetime


class ConfigStorage:
    """PostgreSQL 配置存储同步门面。

    ConfigManager 的对外接口仍是同步方法，因此这里使用后台事件循环承载
    asyncpg 连接池，并通过 ``run_coroutine_threadsafe`` 完成同步桥接。
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="komari-config-storage",
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
            return future.result(timeout=_CONFIG_STORAGE_TIMEOUT_SECONDS)
        except FutureTimeoutError as exc:
            future.cancel()
            msg = "配置存储操作超时"
            raise RuntimeError(msg) from exc

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            config = get_shared_database_config()
            self._pool = await create_postgres_pool(config)
            await self._ensure_schema()
        return self._pool

    async def _ensure_schema(self) -> None:
        if self._pool is None:
            msg = "配置存储连接池未初始化"
            raise RuntimeError(msg)
        async with self._pool.acquire() as conn:
            await conn.execute(_CONFIG_TABLE_DDL)
            await conn.execute(_CONFIG_TABLE_UPDATED_AT_INDEX_DDL)

    def ensure_schema(self) -> None:
        """确保配置表存在。"""
        with self._init_lock:
            self._run(self._get_pool())

    def fetch(self, plugin_name: str) -> StoredConfig | None:
        """按插件名读取配置。"""
        return self._run(self._fetch(plugin_name))

    async def _fetch(self, plugin_name: str) -> StoredConfig | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT plugin_name, schema_name, config_data, version, updated_at
                FROM komari_plugin_configs
                WHERE plugin_name = $1
                """,
                plugin_name,
            )
        if row is None:
            return None
        raw_config = row["config_data"]
        config_data = json.loads(raw_config) if isinstance(raw_config, str) else raw_config
        return StoredConfig(
            plugin_name=str(row["plugin_name"]),
            schema_name=str(row["schema_name"]),
            config_data=dict(config_data),
            version=str(row["version"]),
            updated_at=row["updated_at"],
        )

    def upsert(
        self,
        *,
        plugin_name: str,
        schema_name: str,
        config_data: dict[str, Any],
        version: str,
    ) -> StoredConfig:
        """写入或更新插件配置。"""
        return self._run(
            self._upsert(
                plugin_name=plugin_name,
                schema_name=schema_name,
                config_data=config_data,
                version=version,
            )
        )

    async def _upsert(
        self,
        *,
        plugin_name: str,
        schema_name: str,
        config_data: dict[str, Any],
        version: str,
    ) -> StoredConfig:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO komari_plugin_configs (
                    plugin_name,
                    schema_name,
                    config_data,
                    version
                )
                VALUES ($1, $2, $3::jsonb, $4)
                ON CONFLICT (plugin_name) DO UPDATE SET
                    schema_name = EXCLUDED.schema_name,
                    config_data = EXCLUDED.config_data,
                    version = EXCLUDED.version,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING plugin_name, schema_name, config_data, version, updated_at
                """,
                plugin_name,
                schema_name,
                json.dumps(config_data, ensure_ascii=False),
                version,
            )
        if row is None:
            msg = f"配置写入后未返回记录: {plugin_name}"
            raise RuntimeError(msg)
        raw_config = row["config_data"]
        stored_data = json.loads(raw_config) if isinstance(raw_config, str) else raw_config
        return StoredConfig(
            plugin_name=str(row["plugin_name"]),
            schema_name=str(row["schema_name"]),
            config_data=dict(stored_data),
            version=str(row["version"]),
            updated_at=row["updated_at"],
        )

    def close(self) -> None:
        """关闭后台连接池和事件循环。"""
        if not self._loop.is_running():
            return
        try:
            if self._pool is not None:
                self._run(self._pool.close())
                self._pool = None
        except Exception as exc:
            logger.warning(f"配置存储关闭连接池失败: {exc}")
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)


class _StorageState:
    storage: ClassVar[ConfigStorage | None] = None
    lock: ClassVar[threading.RLock] = threading.RLock()


def get_config_storage() -> ConfigStorage:
    """获取全局 PostgreSQL 配置存储。"""
    if _StorageState.storage is None:
        with _StorageState.lock:
            if _StorageState.storage is None:
                _StorageState.storage = ConfigStorage()
    assert _StorageState.storage is not None
    return _StorageState.storage
