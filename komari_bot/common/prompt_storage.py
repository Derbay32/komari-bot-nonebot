"""Prompt 专用 PostgreSQL 存储与运行时加载辅助。"""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, TypeVar

import asyncpg
from nonebot import logger

from komari_bot.common.content_budget import (
    CONTENT_TEXT_BUDGET,
    validate_text_budget,
)
from komari_bot.common.database_config import get_shared_database_config
from komari_bot.common.postgres import PostgresConfig, create_postgres_pool

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping
    from datetime import datetime

T = TypeVar("T")

_PROMPT_STORAGE_TIMEOUT_SECONDS = 5.0

_PROMPT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS komari_prompt_configs (
    resource_id VARCHAR(128) PRIMARY KEY,
    display_name VARCHAR(128) NOT NULL,
    prompt_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    version VARCHAR(32) NOT NULL DEFAULT '1.0',
    revision BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

_PROMPT_REVISION_DDL = """
ALTER TABLE komari_prompt_configs
    ADD COLUMN IF NOT EXISTS revision BIGINT NOT NULL DEFAULT 1;
"""

_PROMPT_UPDATED_AT_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_komari_prompt_configs_updated_at
    ON komari_prompt_configs (updated_at DESC);
"""
_PROMPT_NOTIFY_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION komari_notify_prompt_config_change()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify('komari_prompt_config_changed', NEW.resource_id);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""
_PROMPT_NOTIFY_TRIGGER_DDL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgname = 'trg_komari_prompt_config_changed'
          AND tgrelid = 'komari_prompt_configs'::regclass
    ) THEN
        CREATE TRIGGER trg_komari_prompt_config_changed
        AFTER INSERT OR UPDATE ON komari_prompt_configs
        FOR EACH ROW
        EXECUTE FUNCTION komari_notify_prompt_config_change();
    END IF;
END;
$$;
"""
_PROMPT_NOTIFY_CHANNEL = "komari_prompt_config_changed"
_PROMPT_NOTIFY_RETRY_SECONDS = 1.0
_PROMPT_CACHE_MAX_STALENESS_SECONDS = 1.0


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
    revision: int = 1


@dataclass(frozen=True, slots=True)
class PromptValues:
    """合并 defaults 后的 prompt 值。"""

    values: dict[str, str]
    stored: StoredPrompt | None


class PromptStorage:
    """PostgreSQL Prompt 存储，提供同步兼容桥与非阻塞异步接口。"""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._started = threading.Event()
        self._stopped = threading.Event()
        self._lifecycle_lock = threading.RLock()
        self._closing = False
        self._closed = False
        self._thread = threading.Thread(
            target=self._run_loop,
            name="komari-prompt-storage",
            daemon=True,
        )
        self._pool: asyncpg.Pool | None = None
        self._pool_lock = asyncio.Lock()
        self._init_lock = threading.RLock()
        self._invalidators: dict[str, list[Callable[[], None]]] = {}
        self._invalidators_lock = threading.RLock()
        self._listener_task: asyncio.Task[None] | None = None
        self._listener_stop_event: asyncio.Event | None = None
        self._listener_ready_event: asyncio.Event | None = None
        self._thread.start()
        if not self._started.wait(timeout=_PROMPT_STORAGE_TIMEOUT_SECONDS):
            self.close()
            msg = "Prompt 存储后台线程启动超时"
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
        with self._lifecycle_lock:
            if self._closing or self._closed or self._loop.is_closed():
                coro.close()
                msg = "Prompt 存储已关闭"
                raise RuntimeError(msg)
            try:
                return asyncio.run_coroutine_threadsafe(coro, self._loop)
            except RuntimeError:
                coro.close()
                raise

    def _run(self, coro: Coroutine[Any, Any, T]) -> T:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            coro.close()
            msg = "事件循环内禁止同步访问 Prompt 存储，请使用异步接口"
            raise RuntimeError(msg)

        future = self._submit(coro)
        try:
            return future.result(timeout=_PROMPT_STORAGE_TIMEOUT_SECONDS)
        except FutureTimeoutError as exc:
            future.cancel()
            msg = "Prompt 存储操作超时"
            raise RuntimeError(msg) from exc

    async def _run_async(self, coro: Coroutine[Any, Any, T]) -> T:
        future = self._submit(coro)
        try:
            return await asyncio.wait_for(
                asyncio.wrap_future(future),
                timeout=_PROMPT_STORAGE_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            future.cancel()
            msg = "Prompt 存储操作超时"
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
                self._start_listener(config)
        assert self._pool is not None
        return self._pool

    async def _ensure_schema(self, pool: asyncpg.Pool) -> None:
        async with pool.acquire() as conn:
            await conn.execute(_PROMPT_TABLE_DDL)
            await conn.execute(_PROMPT_REVISION_DDL)
            await conn.execute(_PROMPT_UPDATED_AT_INDEX_DDL)
            await conn.execute(_PROMPT_NOTIFY_FUNCTION_DDL)
            await conn.execute(_PROMPT_NOTIFY_TRIGGER_DDL)

    def register_invalidator(
        self,
        resource_id: str,
        callback: Callable[[], None],
    ) -> None:
        """注册当前进程 Prompt 缓存失效回调。"""
        with self._invalidators_lock:
            callbacks = self._invalidators.setdefault(resource_id, [])
            if callback not in callbacks:
                callbacks.append(callback)

    def _start_listener(self, config: PostgresConfig) -> None:
        if self._listener_task is not None and not self._listener_task.done():
            return
        self._listener_stop_event = asyncio.Event()
        self._listener_ready_event = asyncio.Event()
        self._listener_task = asyncio.create_task(
            self._listen_for_invalidations(config),
            name="komari-prompt-revision-listener",
        )

    def _handle_prompt_notification(
        self,
        _connection: asyncpg.Connection,
        _process_id: int,
        _channel: str,
        payload: str,
    ) -> None:
        if not payload or len(payload) > 128:
            return
        with self._invalidators_lock:
            callbacks = tuple(self._invalidators.get(payload, ()))
        for callback in callbacks:
            try:
                callback()
            except Exception as exc:
                logger.warning(
                    "Prompt 缓存失效回调失败: resource_id={}, error={}",
                    payload,
                    type(exc).__name__,
                )

    async def _listen_for_invalidations(self, config: PostgresConfig) -> None:
        while not self._closing:
            conn: asyncpg.Connection | None = None
            try:
                conn = await asyncpg.connect(
                    host=config.pg_host,
                    port=config.pg_port,
                    database=config.pg_database,
                    user=config.pg_user,
                    password=config.pg_password,
                    command_timeout=30,
                )
                assert conn is not None
                await conn.add_listener(
                    _PROMPT_NOTIFY_CHANNEL,
                    self._handle_prompt_notification,
                )
                ready_event = self._listener_ready_event
                if ready_event is not None:
                    ready_event.set()
                stop_event = self._listener_stop_event
                if stop_event is None:
                    return
                await stop_event.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._closing:
                    logger.warning(
                        "Prompt 变更监听暂时中断，将自动重试: error={}",
                        type(exc).__name__,
                    )
                    await asyncio.sleep(_PROMPT_NOTIFY_RETRY_SECONDS)
            finally:
                if self._listener_ready_event is not None:
                    self._listener_ready_event.clear()
                if conn is not None and not conn.is_closed():
                    with suppress(Exception):
                        await conn.remove_listener(
                            _PROMPT_NOTIFY_CHANNEL,
                            self._handle_prompt_notification,
                        )
                    await conn.close()

    async def wait_for_listener_ready(self, *, timeout_seconds: float = 5.0) -> bool:
        """等待跨 worker 失效监听真正订阅完成。"""

        async def _wait() -> bool:
            event = self._listener_ready_event
            if event is None:
                return False
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
            except TimeoutError:
                return False
            return True

        return await self._run_async(_wait())

    def ensure_schema(self) -> None:
        """确保 prompt 配置表存在。"""
        with self._init_lock:
            self._run(self._get_pool())

    def fetch(self, resource_id: str) -> StoredPrompt | None:
        """按资源 ID 读取 prompt 配置。"""
        return self._run(self._fetch(resource_id))

    async def fetch_async(self, resource_id: str) -> StoredPrompt | None:
        """异步读取 Prompt 配置，不阻塞调用方事件循环。"""
        return await self._run_async(self._fetch(resource_id))

    async def _fetch(self, resource_id: str) -> StoredPrompt | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    resource_id,
                    display_name,
                    prompt_data,
                    version,
                    updated_at,
                    revision
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

    async def upsert_async(
        self,
        *,
        resource_id: str,
        display_name: str,
        prompt_data: dict[str, str],
        version: str = "1.0",
    ) -> StoredPrompt:
        """异步完整写入 Prompt 配置。"""
        return await self._run_async(
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
                    revision = komari_prompt_configs.revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING
                    resource_id,
                    display_name,
                    prompt_data,
                    version,
                    updated_at,
                    revision
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

    async def update_if_unchanged_async(
        self,
        *,
        resource_id: str,
        display_name: str,
        prompt_data: dict[str, str],
        version: str,
        expected_updated_at: datetime,
    ) -> StoredPrompt | None:
        """记录未变化时异步更新完整 Prompt。"""
        return await self._run_async(
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
                    revision = revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE resource_id = $1
                  AND updated_at = $5
                RETURNING
                    resource_id,
                    display_name,
                    prompt_data,
                    version,
                    updated_at,
                    revision
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

    async def replace_if_revision_async(
        self,
        *,
        resource_id: str,
        display_name: str,
        prompt_data: dict[str, str],
        version: str,
        expected_revision: int,
    ) -> StoredPrompt | None:
        """仅在 revision 匹配时替换完整 Prompt；0 表示仅允许首次创建。"""
        return await self._run_async(
            self._replace_if_revision(
                resource_id=resource_id,
                display_name=display_name,
                prompt_data=prompt_data,
                version=version,
                expected_revision=expected_revision,
            )
        )

    async def _replace_if_revision(
        self,
        *,
        resource_id: str,
        display_name: str,
        prompt_data: dict[str, str],
        version: str,
        expected_revision: int,
    ) -> StoredPrompt | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if expected_revision == 0:
                row = await conn.fetchrow(
                    """
                    INSERT INTO komari_prompt_configs (
                        resource_id,
                        display_name,
                        prompt_data,
                        version
                    )
                    VALUES ($1, $2, $3::jsonb, $4)
                    ON CONFLICT (resource_id) DO NOTHING
                    RETURNING
                        resource_id,
                        display_name,
                        prompt_data,
                        version,
                        updated_at,
                        revision
                    """,
                    resource_id,
                    display_name,
                    json.dumps(prompt_data, ensure_ascii=False),
                    version,
                )
            else:
                row = await conn.fetchrow(
                    """
                    UPDATE komari_prompt_configs
                    SET
                        display_name = $2,
                        prompt_data = $3::jsonb,
                        version = $4,
                        revision = revision + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE resource_id = $1
                      AND revision = $5
                    RETURNING
                        resource_id,
                        display_name,
                        prompt_data,
                        version,
                        updated_at,
                        revision
                    """,
                    resource_id,
                    display_name,
                    json.dumps(prompt_data, ensure_ascii=False),
                    version,
                    expected_revision,
                )
        if row is None:
            return None
        return _stored_prompt_from_row(row)

    async def update_field_if_revision_async(
        self,
        *,
        resource_id: str,
        display_name: str,
        field_name: str,
        value: str,
        version: str,
        expected_revision: int,
    ) -> StoredPrompt | None:
        """按 revision 原子更新一个 JSONB 顶层字段。"""
        return await self._run_async(
            self._update_field_if_revision(
                resource_id=resource_id,
                display_name=display_name,
                field_name=field_name,
                value=value,
                version=version,
                expected_revision=expected_revision,
            )
        )

    async def _update_field_if_revision(
        self,
        *,
        resource_id: str,
        display_name: str,
        field_name: str,
        value: str,
        version: str,
        expected_revision: int,
    ) -> StoredPrompt | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE komari_prompt_configs
                SET
                    display_name = $2,
                    prompt_data = prompt_data || $3::jsonb,
                    version = $4,
                    revision = revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE resource_id = $1
                  AND revision = $5
                RETURNING
                    resource_id,
                    display_name,
                    prompt_data,
                    version,
                    updated_at,
                    revision
                """,
                resource_id,
                display_name,
                json.dumps({field_name: value}, ensure_ascii=False),
                version,
                expected_revision,
            )
        if row is None:
            return None
        return _stored_prompt_from_row(row)

    async def _close_pool(self) -> None:
        if self._listener_stop_event is not None:
            self._listener_stop_event.set()
        listener_task = self._listener_task
        self._listener_task = None
        if listener_task is not None:
            listener_task.cancel()
            with suppress(asyncio.CancelledError):
                await listener_task
        self._listener_stop_event = None
        self._listener_ready_event = None
        pool = self._pool
        self._pool = None
        if pool is not None:
            await pool.close()

    def close(self) -> None:
        """关闭连接池，停止并回收后台线程及事件循环。"""
        with self._lifecycle_lock:
            if self._closed:
                return
            if self._closing:
                wait_for_other_close = True
            else:
                self._closing = True
                wait_for_other_close = False

        shutdown_timeout = _PROMPT_STORAGE_TIMEOUT_SECONDS + 1.0
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
                    future.result(timeout=_PROMPT_STORAGE_TIMEOUT_SECONDS)
                except FutureTimeoutError as exc:
                    future.cancel()
                    logger.warning(
                        "Prompt 存储关闭连接池超时: {}", type(exc).__name__
                    )
                except Exception as exc:
                    logger.warning(
                        "Prompt 存储关闭连接池失败: {}", type(exc).__name__
                    )
        except Exception as exc:
            logger.warning("Prompt 存储提交关闭任务失败: {}", type(exc).__name__)
        finally:
            if not self._loop.is_closed():
                with suppress(RuntimeError):
                    self._loop.call_soon_threadsafe(self._loop.stop)
            if threading.current_thread() is not self._thread:
                self._thread.join(timeout=shutdown_timeout)
                if self._thread.is_alive():
                    logger.error("Prompt 存储后台线程未在超时内停止")


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
        revision=int(row["revision"]),
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
        normalized = value.rstrip("\n")
        validate_text_budget(
            normalized,
            label=f"提示词字段 {key}",
            budget=CONTENT_TEXT_BUDGET,
        )
        cleaned[key] = normalized
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


async def load_prompt_values_async(
    resource: PromptResourceProtocol,
) -> PromptValues:
    """异步读取 Prompt，并以 CAS 补齐新增默认字段。"""
    storage = get_prompt_storage()
    stored = await storage.fetch_async(resource.resource_id)
    values = merge_prompt_values(
        defaults=resource.defaults,
        prompt_data=stored.prompt_data if stored is not None else None,
    )
    if stored is None:
        return PromptValues(values=values, stored=None)

    stored_keys = set(stored.prompt_data)
    merged_keys = set(values)
    added_keys = merged_keys - stored_keys
    removed_keys = stored_keys - merged_keys
    value_changed = any(
        stored.prompt_data.get(key) != values[key]
        for key in stored_keys & merged_keys
    )
    if not added_keys and not value_changed:
        return PromptValues(values=values, stored=stored)

    synced: StoredPrompt | None = None
    try:
        prompt_data = dict(stored.prompt_data)
        prompt_data.update(validate_prompt_values(resource.defaults, values))
        synced = await storage.update_if_unchanged_async(
            resource_id=resource.resource_id,
            display_name=resource.display_name,
            prompt_data=prompt_data,
            version=stored.version,
            expected_updated_at=stored.updated_at,
        )
        if synced is None:
            latest = await storage.fetch_async(resource.resource_id)
            if latest is not None:
                stored = latest
                values = merge_prompt_values(
                    defaults=resource.defaults,
                    prompt_data=latest.prompt_data,
                )
            logger.warning(
                "Prompt 配置自动同步跳过: resource_id={}, reason=stored_changed",
                resource.resource_id,
            )
        else:
            stored = synced
    except Exception as exc:
        logger.warning(
            "Prompt 配置自动同步失败: resource_id={}, added_keys={}, "
            "removed_keys={}, sync_result=failed, error={}",
            resource.resource_id,
            sorted(added_keys),
            sorted(removed_keys),
            type(exc).__name__,
        )
    else:
        if synced is not None:
            logger.info(
                "Prompt 配置已自动同步: resource_id={}, added_keys={}, "
                "removed_keys={}, sync_result=success",
                resource.resource_id,
                sorted(added_keys),
                sorted(removed_keys),
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


async def save_prompt_values_async(
    resource: PromptResourceProtocol,
    values: Mapping[str, object],
) -> StoredPrompt:
    """异步校验并完整保存 Prompt，供迁移与显式覆盖场景使用。"""
    cleaned = validate_prompt_values(resource.defaults, values)
    return await get_prompt_storage().upsert_async(
        resource_id=resource.resource_id,
        display_name=resource.display_name,
        prompt_data=cleaned,
    )


async def replace_prompt_values_async(
    resource: PromptResourceProtocol,
    values: Mapping[str, object],
    *,
    expected_revision: int,
) -> StoredPrompt | None:
    """校验后按 revision 替换完整 Prompt。"""
    cleaned = validate_prompt_values(resource.defaults, values)
    return await get_prompt_storage().replace_if_revision_async(
        resource_id=resource.resource_id,
        display_name=resource.display_name,
        prompt_data=cleaned,
        version="1.0",
        expected_revision=expected_revision,
    )


async def update_prompt_field_async(
    resource: PromptResourceProtocol,
    field_name: str,
    value: object,
    *,
    expected_revision: int,
) -> StoredPrompt | None:
    """校验并按 revision 更新单个 Prompt 字段，不覆盖其他字段。"""
    if field_name not in resource.defaults:
        msg = f"存在未知提示词字段: {field_name}"
        raise ValueError(msg)
    cleaned = validate_prompt_values(resource.defaults, {field_name: value})
    if expected_revision == 0:
        return await get_prompt_storage().replace_if_revision_async(
            resource_id=resource.resource_id,
            display_name=resource.display_name,
            prompt_data=cleaned,
            version="1.0",
            expected_revision=0,
        )
    return await get_prompt_storage().update_field_if_revision_async(
        resource_id=resource.resource_id,
        display_name=resource.display_name,
        field_name=field_name,
        value=cleaned[field_name],
        version="1.0",
        expected_revision=expected_revision,
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
        self._cache_revision = 0
        self._cache_checked_at = 0.0
        self._invalidated = True
        self._registered_storage_id: int | None = None
        self._cache_lock = threading.RLock()
        self._async_lock = asyncio.Lock()

    def _invalidate(self) -> None:
        with self._cache_lock:
            self._invalidated = True

    def _register_invalidator(self, storage: PromptStorage) -> None:
        storage_id = id(storage)
        with self._cache_lock:
            if self._registered_storage_id == storage_id:
                return
            register = getattr(storage, "register_invalidator", None)
            if callable(register):
                register(self.resource_id, self._invalidate)
            self._registered_storage_id = storage_id

    def _cached_if_fresh(self) -> dict[str, str] | None:
        with self._cache_lock:
            if (
                self._cache
                and not self._invalidated
                and monotonic() - self._cache_checked_at
                < _PROMPT_CACHE_MAX_STALENESS_SECONDS
            ):
                return dict(self._cache)
        return None

    def _fallback_template(self) -> dict[str, str]:
        with self._cache_lock:
            if not self._cache:
                self._cache = dict(self.defaults)
            self._cache_checked_at = monotonic()
            self._invalidated = False
            return dict(self._cache)

    def _accept_loaded(self, loaded: PromptValues) -> dict[str, str]:
        updated_at = loaded.stored.updated_at if loaded.stored is not None else None
        revision = loaded.stored.revision if loaded.stored is not None else 0
        with self._cache_lock:
            changed = not self._cache or revision != self._cache_revision
            self._cache = dict(loaded.values)
            self._cache_updated_at = updated_at
            self._cache_revision = revision
            self._cache_checked_at = monotonic()
            self._invalidated = False
            result = dict(self._cache)

        if changed:
            if loaded.stored is None:
                logger.warning(
                    "{} Prompt 配置未写入 PostgreSQL，使用默认值",
                    self._log_prefix,
                )
            else:
                logger.info(
                    "{} Prompt 配置已从 PostgreSQL 加载: {}, revision={}",
                    self._log_prefix,
                    self.resource_id,
                    revision,
                )
        return result

    def get_template(self) -> dict[str, str]:
        """同步获取模板，仅供非事件循环脚本兼容使用。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            msg = "事件循环内禁止同步读取 Prompt，请使用 get_template_async()"
            raise RuntimeError(msg)

        storage = get_prompt_storage()
        self._register_invalidator(storage)
        cached = self._cached_if_fresh()
        if cached is not None:
            return cached
        try:
            loaded = load_prompt_values(self)
        except Exception:
            logger.warning(
                "{} Prompt 配置读取失败，使用缓存或默认值",
                self._log_prefix,
                exc_info=True,
            )
            return self._fallback_template()
        return self._accept_loaded(loaded)

    async def get_template_async(self) -> dict[str, str]:
        """异步获取模板；缓存命中零 SQL，失效检查使用单飞。"""
        storage = get_prompt_storage()
        self._register_invalidator(storage)
        cached = self._cached_if_fresh()
        if cached is not None:
            return cached

        async with self._async_lock:
            cached = self._cached_if_fresh()
            if cached is not None:
                return cached
            try:
                loaded = await load_prompt_values_async(self)
            except Exception:
                logger.warning(
                    "{} Prompt 配置异步读取失败，使用缓存或默认值",
                    self._log_prefix,
                    exc_info=True,
                )
                return self._fallback_template()
            return self._accept_loaded(loaded)
