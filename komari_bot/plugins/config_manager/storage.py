"""config_manager 的 PostgreSQL 存储层。"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
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
    revision BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

_CONFIG_TABLE_REVISION_DDL = """
ALTER TABLE komari_plugin_configs
    ADD COLUMN IF NOT EXISTS revision BIGINT NOT NULL DEFAULT 1;
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
    revision: int
    updated_at: datetime


class ConfigStorage:
    """PostgreSQL 配置存储门面。

    后台事件循环承载 asyncpg 连接池；同步启动代码与异步业务代码分别通过
    阻塞和非阻塞桥接方法访问同一连接池。
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._started = threading.Event()
        self._stopped = threading.Event()
        self._lifecycle_lock = threading.RLock()
        self._closing = False
        self._closed = False
        self._thread = threading.Thread(
            target=self._run_loop,
            name="komari-config-storage",
            daemon=True,
        )
        self._pool: asyncpg.Pool | None = None
        self._pool_lock = asyncio.Lock()
        self._init_lock = threading.RLock()
        self._thread.start()
        if not self._started.wait(timeout=_CONFIG_STORAGE_TIMEOUT_SECONDS):
            self.close()
            msg = "配置存储后台线程启动超时"
            raise RuntimeError(msg)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._started.set()
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            asyncio.set_event_loop(None)
            self._loop.close()
            with self._lifecycle_lock:
                self._closed = True
            self._stopped.set()

    def _submit(self, coro: Coroutine[Any, Any, T]) -> Future[T]:
        """向存储循环提交任务，并拒绝关闭阶段的新操作。"""
        with self._lifecycle_lock:
            if self._closing or self._closed or self._loop.is_closed():
                coro.close()
                msg = "配置存储已关闭"
                raise RuntimeError(msg)
            try:
                return asyncio.run_coroutine_threadsafe(coro, self._loop)
            except RuntimeError:
                coro.close()
                raise

    def _run(self, coro: Coroutine[Any, Any, T]) -> T:
        future = self._submit(coro)
        try:
            return future.result(timeout=_CONFIG_STORAGE_TIMEOUT_SECONDS)
        except FutureTimeoutError as exc:
            future.cancel()
            msg = "配置存储操作超时"
            raise RuntimeError(msg) from exc

    async def _run_async(self, coro: Coroutine[Any, Any, T]) -> T:
        """在调用方事件循环中无阻塞地等待后台存储操作。"""
        future = self._submit(coro)
        try:
            return await asyncio.wait_for(
                asyncio.wrap_future(future),
                timeout=_CONFIG_STORAGE_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            future.cancel()
            msg = "配置存储操作超时"
            raise RuntimeError(msg) from exc

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is not None:
            return self._pool

        async with self._pool_lock:
            if self._pool is None:
                config = get_shared_database_config()
                pool = await create_postgres_pool(config)
                try:
                    await self._ensure_schema(pool)
                except Exception:
                    await pool.close()
                    raise
                self._pool = pool
        assert self._pool is not None
        return self._pool

    async def _ensure_schema(self, pool: asyncpg.Pool) -> None:
        async with pool.acquire() as conn:
            await conn.execute(_CONFIG_TABLE_DDL)
            await conn.execute(_CONFIG_TABLE_REVISION_DDL)
            await conn.execute(_CONFIG_TABLE_UPDATED_AT_INDEX_DDL)

    def ensure_schema(self) -> None:
        """确保配置表存在。"""
        with self._init_lock:
            self._run(self._get_pool())

    def fetch(self, plugin_name: str) -> StoredConfig | None:
        """按插件名读取配置。"""
        return self._run(self._fetch(plugin_name))

    async def fetch_async(self, plugin_name: str) -> StoredConfig | None:
        """按插件名异步读取配置。"""
        return await self._run_async(self._fetch(plugin_name))

    async def _fetch(self, plugin_name: str) -> StoredConfig | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    plugin_name,
                    schema_name,
                    config_data,
                    version,
                    revision,
                    updated_at
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
            revision=int(row["revision"]),
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

    async def upsert_async(
        self,
        *,
        plugin_name: str,
        schema_name: str,
        config_data: dict[str, Any],
        version: str,
    ) -> StoredConfig:
        """异步写入或更新插件配置。"""
        return await self._run_async(
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
                    revision = komari_plugin_configs.revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING
                    plugin_name,
                    schema_name,
                    config_data,
                    version,
                    revision,
                    updated_at
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
            revision=int(row["revision"]),
            updated_at=row["updated_at"],
        )

    def update_if_unchanged(
        self,
        *,
        plugin_name: str,
        schema_name: str,
        config_data: dict[str, Any],
        version: str,
        expected_updated_at: datetime,
    ) -> StoredConfig | None:
        """仅在记录未被其他写入修改时更新插件配置。"""
        return self._run(
            self._update_if_unchanged(
                plugin_name=plugin_name,
                schema_name=schema_name,
                config_data=config_data,
                version=version,
                expected_updated_at=expected_updated_at,
            )
        )

    async def update_if_unchanged_async(
        self,
        *,
        plugin_name: str,
        schema_name: str,
        config_data: dict[str, Any],
        version: str,
        expected_updated_at: datetime,
    ) -> StoredConfig | None:
        """记录未变化时异步更新整份配置。"""
        return await self._run_async(
            self._update_if_unchanged(
                plugin_name=plugin_name,
                schema_name=schema_name,
                config_data=config_data,
                version=version,
                expected_updated_at=expected_updated_at,
            )
        )

    async def _update_if_unchanged(
        self,
        *,
        plugin_name: str,
        schema_name: str,
        config_data: dict[str, Any],
        version: str,
        expected_updated_at: datetime,
    ) -> StoredConfig | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE komari_plugin_configs
                SET
                    schema_name = $2,
                    config_data = $3::jsonb,
                    version = $4,
                    revision = revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE plugin_name = $1
                  AND updated_at = $5
                RETURNING
                    plugin_name,
                    schema_name,
                    config_data,
                    version,
                    revision,
                    updated_at
                """,
                plugin_name,
                schema_name,
                json.dumps(config_data, ensure_ascii=False),
                version,
                expected_updated_at,
            )
        if row is None:
            return None
        raw_config = row["config_data"]
        stored_data = json.loads(raw_config) if isinstance(raw_config, str) else raw_config
        return StoredConfig(
            plugin_name=str(row["plugin_name"]),
            schema_name=str(row["schema_name"]),
            config_data=dict(stored_data),
            version=str(row["version"]),
            revision=int(row["revision"]),
            updated_at=row["updated_at"],
        )

    def update_fields_if_revision(
        self,
        *,
        plugin_name: str,
        schema_name: str,
        config_patch: dict[str, Any],
        version: str,
        expected_revision: int,
    ) -> StoredConfig | None:
        """仅在修订号匹配时原子更新指定顶层字段。"""
        return self._run(
            self._update_fields_if_revision(
                plugin_name=plugin_name,
                schema_name=schema_name,
                config_patch=config_patch,
                version=version,
                expected_revision=expected_revision,
            )
        )

    async def update_fields_if_revision_async(
        self,
        *,
        plugin_name: str,
        schema_name: str,
        config_patch: dict[str, Any],
        version: str,
        expected_revision: int,
    ) -> StoredConfig | None:
        """异步原子更新指定顶层字段，并校验修订号。"""
        return await self._run_async(
            self._update_fields_if_revision(
                plugin_name=plugin_name,
                schema_name=schema_name,
                config_patch=config_patch,
                version=version,
                expected_revision=expected_revision,
            )
        )

    async def _update_fields_if_revision(
        self,
        *,
        plugin_name: str,
        schema_name: str,
        config_patch: dict[str, Any],
        version: str,
        expected_revision: int,
    ) -> StoredConfig | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE komari_plugin_configs
                SET
                    schema_name = $2,
                    config_data = config_data || $3::jsonb,
                    version = $4,
                    revision = revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE plugin_name = $1
                  AND revision = $5
                RETURNING
                    plugin_name,
                    schema_name,
                    config_data,
                    version,
                    revision,
                    updated_at
                """,
                plugin_name,
                schema_name,
                json.dumps(config_patch, ensure_ascii=False),
                version,
                expected_revision,
            )
        if row is None:
            return None
        raw_config = row["config_data"]
        stored_data = json.loads(raw_config) if isinstance(raw_config, str) else raw_config
        return StoredConfig(
            plugin_name=str(row["plugin_name"]),
            schema_name=str(row["schema_name"]),
            config_data=dict(stored_data),
            version=str(row["version"]),
            revision=int(row["revision"]),
            updated_at=row["updated_at"],
        )

    async def _close_pool(self) -> None:
        """在连接池所属事件循环中关闭并解除引用。"""
        pool = self._pool
        self._pool = None
        if pool is not None:
            await pool.close()

    def close(self) -> None:
        """关闭连接池，停止并回收后台线程及其事件循环。"""
        with self._lifecycle_lock:
            if self._closed:
                return
            if self._closing:
                wait_for_other_close = True
            else:
                self._closing = True
                wait_for_other_close = False

        shutdown_timeout = _CONFIG_STORAGE_TIMEOUT_SECONDS + 1.0
        if wait_for_other_close:
            self._stopped.wait(timeout=shutdown_timeout)
            return

        try:
            if not self._loop.is_closed():
                future = asyncio.run_coroutine_threadsafe(
                    self._close_pool(),
                    self._loop,
                )
                try:
                    future.result(timeout=_CONFIG_STORAGE_TIMEOUT_SECONDS)
                except FutureTimeoutError as exc:
                    future.cancel()
                    logger.warning("配置存储关闭连接池超时: {}", type(exc).__name__)
                except Exception as exc:
                    logger.warning("配置存储关闭连接池失败: {}", type(exc).__name__)
        except Exception as exc:
            logger.warning("配置存储提交关闭任务失败: {}", type(exc).__name__)
        finally:
            if not self._loop.is_closed():
                with suppress(RuntimeError):
                    self._loop.call_soon_threadsafe(self._loop.stop)

            if threading.current_thread() is not self._thread:
                self._thread.join(timeout=shutdown_timeout)
                if self._thread.is_alive():
                    logger.error("配置存储后台线程未在超时内停止")


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


def close_config_storage_if_created() -> None:
    """关闭已创建的全局配置存储，不在关闭阶段创建新实例。"""
    with _StorageState.lock:
        storage = _StorageState.storage
        if storage is not None:
            storage.close()
            _StorageState.storage = None
