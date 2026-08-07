"""config_manager 的 PostgreSQL 强类型配置存储层。

每个动态配置资源对应一张 ``TypedConfigModel`` 单行表（主键 ``id=1``、
CAS 修订号 ``revision``、写入时间 ``updated_at``），由 Alembic 迁移统一
建表。本模块通过 nonebot-plugin-orm 的 ``get_session`` 在调用方/应用事件
循环上执行 SQLAlchemy AsyncSession 操作：

- 异步入口（``*_async``）直接在调用方事件循环执行；
- 同步入口在应用事件循环绑定后通过 ``run_coroutine_threadsafe`` 提交，
  绑定前（如插件加载、独立脚本）使用一次性事件循环与一次性引擎，用完
  即释放连接；禁止在事件循环内阻塞等待自身；
- 跨进程配置变更不再使用 asyncpg LISTEN/NOTIFY，而是应用事件循环上
  亚秒级轮询各配置表的 ``revision`` 并在变化或订阅陈旧时分发快照。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from nonebot import logger
from sqlalchemy import Table, literal, select, union_all, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.types import JSON as SQLAlchemyJSON  # noqa: N811

from komari_bot.config.typed_config import (
    TypedConfigModel,
    ensure_typed_config_model,
)
from komari_bot.db.orm_config import get_orm_database_url

if TYPE_CHECKING:
    from pydantic import BaseModel

T = TypeVar("T")

_CONFIG_STORAGE_TIMEOUT_SECONDS = 5.0
_CONFIG_WATCH_MIN_INTERVAL_SECONDS = 0.05
_CONFIG_WATCH_RETRY_SECONDS = 1.0

_StorageFactory = Callable[[AsyncSession], Coroutine[Any, Any, T]]


@dataclass(frozen=True, slots=True)
class StoredConfig:
    """已存储的插件配置快照（不含存储专用字段）。"""

    plugin_name: str
    config_data: dict[str, Any]
    revision: int
    updated_at: datetime


@dataclass(slots=True)
class _ConfigWatcher:
    """一个进程内配置快照订阅。"""

    callback: Callable[[StoredConfig], None]
    max_staleness_seconds: float
    last_checked_at: float = 0.0


def _utcnow() -> datetime:
    """返回带时区的当前时间。"""
    return datetime.now(UTC)


def _stored_config_from_model(
    plugin_name: str, entity: TypedConfigModel
) -> StoredConfig:
    """把单行表实体转换为不可变配置快照。"""
    return StoredConfig(
        plugin_name=plugin_name,
        config_data=entity.model_dump(mode="json"),
        revision=int(entity.revision),
        updated_at=entity.updated_at,
    )


def _model_table(model_cls: type[TypedConfigModel]) -> Table:
    """以 SQLAlchemy Table 视角访问配置模型列（静态类型友好）。"""
    return model_cls.__table__


def _write_values(
    model_cls: type[TypedConfigModel],
    config: BaseModel,
    field_names: set[str] | None = None,
) -> dict[str, Any]:
    """按列类型挑选写入值：JSONB 列写 JSON 值，其余列写 Python 值。"""
    python_data = config.model_dump()
    json_data = config.model_dump(mode="json")
    names = set(python_data) if field_names is None else set(field_names)
    table = _model_table(model_cls)
    values: dict[str, Any] = {}
    for name in sorted(names):
        column = table.c[name]
        if isinstance(column.type, SQLAlchemyJSON):
            values[name] = json_data[name]
        else:
            values[name] = python_data[name]
    return values


class ConfigStorage:
    """PostgreSQL 强类型配置存储门面。"""

    def __init__(self) -> None:
        self._state_lock = threading.RLock()
        self._closing = False
        self._closed = False
        self._app_loop: asyncio.AbstractEventLoop | None = None
        self._watchers: dict[str, list[_ConfigWatcher]] = {}
        self._pending_watch_names: set[str] = set()
        self._last_known_revisions: dict[str, int] = {}
        self._watch_event: asyncio.Event | None = None
        self._watch_task: asyncio.Task[None] | None = None

    # ------------------------------ 生命周期 ------------------------------

    def bind_app_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定应用事件循环并启动 revision 轮询任务。"""
        with self._state_lock:
            if self._closing or self._loop_is_stale(loop):
                return
            self._app_loop = loop
            self._start_watch_task(loop)

    def _loop_is_stale(self, loop: asyncio.AbstractEventLoop) -> bool:
        return self._app_loop is loop and loop.is_closed()

    def _start_watch_task(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._watch_task is not None and not self._watch_task.done():
            return
        self._watch_event = asyncio.Event()
        self._watch_task = loop.create_task(
            self._watch_config_changes(),
            name="komari-config-revision-watcher",
        )

    async def close_async(self) -> None:
        """停止轮询任务并等待其退出。"""
        with self._state_lock:
            if self._closing:
                return
            self._closing = True
            task = self._watch_task
            self._watch_task = None
            self._watch_event = None
            self._app_loop = None
            self._closed = True
        if task is not None:
            task.cancel()
            with suppress(BaseException):
                await task

    def close(self) -> None:
        """同步标记关闭；正常关闭优先走 ``close_async``。

        仅当未绑定应用事件循环时直接丢弃 watcher 引用；已绑定时向该循环
        调度任务取消（无法在此等待），避免轮询任务被悬挂。
        """
        with self._state_lock:
            task = self._watch_task
            loop = self._app_loop
            self._closing = True
            self._closed = True
            self._watch_task = None
            self._watch_event = None
            self._app_loop = None
        if task is not None and loop is not None:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)

    @property
    def closed(self) -> bool:
        """是否已关闭。"""
        return self._closed

    # ------------------------------ 会话桥接 ------------------------------

    @staticmethod
    def _open_app_session() -> AsyncSession:
        from nonebot_plugin_orm import get_session

        return get_session(expire_on_commit=False)

    async def _run_on_app_session(
        self, operation: _StorageFactory[T]
    ) -> T:
        session = self._open_app_session()
        try:
            return await operation(session)
        except BaseException:
            with suppress(Exception):
                await session.rollback()
            raise
        finally:
            await session.close()

    async def _run_on_private_engine(
        self, operation: _StorageFactory[T]
    ) -> T:
        """在一次性事件循环上用一次性引擎执行单步操作。

        URL 统一读取 nonebot-plugin-orm 权威的 ``SQLALCHEMY_DATABASE_URL``，
        不再消费已退役的 PG 连接配置。
        """
        engine = create_async_engine(get_orm_database_url())
        factory = async_sessionmaker(engine, expire_on_commit=False)
        session = factory()
        try:
            return await operation(session)
        except BaseException:
            with suppress(Exception):
                await session.rollback()
            raise
        finally:
            await session.close()
            await engine.dispose()

    def _run_sync(self, operation: _StorageFactory[T]) -> T:
        """同步入口：线程上桥接异步实现，事件循环内禁用。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            msg = (
                "事件循环内禁止阻塞等待配置存储，请改用对应的 _async 方法"
            )
            raise RuntimeError(msg)

        app_loop = self._app_loop
        if app_loop is not None and app_loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self._run_on_app_session(operation), app_loop
            )
            try:
                return future.result(timeout=_CONFIG_STORAGE_TIMEOUT_SECONDS)
            except FutureTimeoutError as exc:
                future.cancel()
                msg = "配置存储操作超时"
                raise RuntimeError(msg) from exc
        return asyncio.run(self._run_on_private_engine(operation))

    def _require_open(self) -> None:
        with self._state_lock:
            if self._closing or self._closed:
                msg = "配置存储已关闭"
                raise RuntimeError(msg)

    def _resolve_model(self, plugin_name: str) -> type[TypedConfigModel]:
        model_cls = ensure_typed_config_model(plugin_name)
        if model_cls is None:
            msg = f"配置资源 {plugin_name} 未注册强类型配置表"
            raise RuntimeError(msg)
        return model_cls

    # ------------------------------ 快照订阅 ------------------------------

    def register_watcher(
        self,
        plugin_name: str,
        callback: Callable[[StoredConfig], None],
        *,
        max_staleness_seconds: float,
    ) -> None:
        """订阅配置变更；由轮询任务按陈旧时间与 revision 变化分发快照。"""
        watcher = _ConfigWatcher(
            callback=callback,
            max_staleness_seconds=max(0.1, float(max_staleness_seconds)),
        )
        with self._state_lock:
            self._watchers.setdefault(plugin_name, []).append(watcher)
        event = self._watch_event
        loop = self._app_loop
        if event is not None and loop is not None and loop.is_running():
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(event.set)

    def _collect_due_watch_names(self) -> set[str]:
        now = monotonic()
        with self._state_lock:
            names = set(self._pending_watch_names)
            self._pending_watch_names.clear()
            for plugin_name, watchers in self._watchers.items():
                if any(
                    now - watcher.last_checked_at
                    >= watcher.max_staleness_seconds
                    for watcher in watchers
                ):
                    names.add(plugin_name)
        return names

    def _next_watch_timeout(self) -> float:
        now = monotonic()
        with self._state_lock:
            remaining = [
                watcher.max_staleness_seconds - (now - watcher.last_checked_at)
                for watchers in self._watchers.values()
                for watcher in watchers
            ]
        if not remaining:
            return _CONFIG_WATCH_RETRY_SECONDS
        return max(_CONFIG_WATCH_MIN_INTERVAL_SECONDS, min(remaining))

    async def _detect_revision_changes(self, session: AsyncSession) -> set[str]:
        with self._state_lock:
            watched = sorted(self._watchers)
        queries = []
        for plugin_name in watched:
            model_cls = ensure_typed_config_model(plugin_name)
            if model_cls is None:
                continue
            table = _model_table(model_cls)
            queries.append(
                select(
                    literal(plugin_name).label("plugin_name"),
                    table.c.revision.label("revision"),
                )
            )
        if not queries:
            return set()
        result = await session.execute(union_all(*queries))
        changed: set[str] = set()
        for row in result:
            revision = int(row.revision)
            last_known = self._last_known_revisions.get(row.plugin_name)
            if last_known is None or last_known == revision:
                self._last_known_revisions[row.plugin_name] = revision
                continue
            self._last_known_revisions[row.plugin_name] = revision
            changed.add(str(row.plugin_name))
        return changed

    async def _refresh_watchers(
        self,
        session: AsyncSession,
        plugin_names: set[str],
    ) -> None:
        if not plugin_names:
            return
        snapshots: dict[str, StoredConfig] = {}
        for plugin_name in sorted(plugin_names):
            model_cls = ensure_typed_config_model(plugin_name)
            if model_cls is None:
                continue
            entity = await session.get(model_cls, 1)
            if entity is None:
                continue
            snapshots[plugin_name] = _stored_config_from_model(
                plugin_name, entity
            )
        checked_at = monotonic()
        with self._state_lock:
            watcher_snapshots = {
                plugin_name: tuple(self._watchers.get(plugin_name, ()))
                for plugin_name in plugin_names
            }
            for watchers in watcher_snapshots.values():
                for watcher in watchers:
                    watcher.last_checked_at = checked_at

        for plugin_name, watchers in watcher_snapshots.items():
            snapshot = snapshots.get(plugin_name)
            if snapshot is None:
                continue
            for watcher in watchers:
                try:
                    watcher.callback(snapshot)
                except Exception as exc:
                    logger.warning(
                        "配置快照订阅回调失败: plugin={}, error={}",
                        plugin_name,
                        type(exc).__name__,
                    )

    async def _watch_config_changes(self) -> None:
        """轮询各配置表 revision，并按陈旧时间分发快照。"""
        while not self._closing:
            try:
                session = self._open_app_session()
                try:
                    changed = await self._detect_revision_changes(session)
                    if changed:
                        with self._state_lock:
                            self._pending_watch_names.update(
                                changed & set(self._watchers)
                            )
                    due = self._collect_due_watch_names()
                    await self._refresh_watchers(session, due)
                finally:
                    await session.close()

                event = self._watch_event
                if event is None:
                    return
                event.clear()
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        event.wait(),
                        timeout=self._next_watch_timeout(),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._closing:
                    logger.warning(
                        "配置变更轮询暂时中断，将自动重试: error={}",
                        type(exc).__name__,
                    )
                    await asyncio.sleep(_CONFIG_WATCH_RETRY_SECONDS)

    # ------------------------------ 存储操作 ------------------------------

    def _fetch_op(
        self, plugin_name: str
    ) -> _StorageFactory[StoredConfig | None]:
        async def operation(session: AsyncSession) -> StoredConfig | None:
            model_cls = self._resolve_model(plugin_name)
            entity = await session.get(model_cls, 1)
            if entity is None:
                return None
            return _stored_config_from_model(plugin_name, entity)

        return operation

    def _insert_if_absent_op(
        self,
        *,
        plugin_name: str,
        config: BaseModel,
    ) -> _StorageFactory[StoredConfig]:
        async def operation(session: AsyncSession) -> StoredConfig:
            model_cls = self._resolve_model(plugin_name)
            entity = await session.get(model_cls, 1)
            if entity is not None:
                return _stored_config_from_model(plugin_name, entity)
            statement = postgres_insert(model_cls).values(
                id=1,
                revision=1,
                updated_at=_utcnow(),
                **_write_values(model_cls, config),
            ).on_conflict_do_nothing(index_elements=["id"])
            await session.execute(statement)
            entity = await session.get(model_cls, 1)
            if entity is None:
                msg = f"配置初始化后未找到记录: {plugin_name}"
                raise RuntimeError(msg)
            stored = _stored_config_from_model(plugin_name, entity)
            await session.commit()
            return stored

        return operation

    def _update_if_unchanged_op(
        self,
        *,
        plugin_name: str,
        config: BaseModel,
        expected_updated_at: datetime,
    ) -> _StorageFactory[StoredConfig | None]:
        async def operation(session: AsyncSession) -> StoredConfig | None:
            model_cls = self._resolve_model(plugin_name)
            table = _model_table(model_cls)
            statement = (
                update(model_cls)
                .where(
                    table.c.id == 1,
                    table.c.updated_at == expected_updated_at,
                )
                .values(
                    revision=table.c.revision + 1,
                    updated_at=_utcnow(),
                    **_write_values(model_cls, config),
                )
                .returning(model_cls)
            )
            result = await session.execute(statement)
            entity = result.scalars().first()
            if entity is None:
                await session.commit()
                return None
            stored = _stored_config_from_model(plugin_name, entity)
            await session.commit()
            return stored

        return operation

    def _update_fields_if_revision_op(
        self,
        *,
        plugin_name: str,
        config: BaseModel,
        field_names: set[str],
        expected_revision: int,
    ) -> _StorageFactory[StoredConfig | None]:
        async def operation(session: AsyncSession) -> StoredConfig | None:
            model_cls = self._resolve_model(plugin_name)
            table = _model_table(model_cls)
            statement = (
                update(model_cls)
                .where(
                    table.c.id == 1,
                    table.c.revision == expected_revision,
                )
                .values(
                    revision=table.c.revision + 1,
                    updated_at=_utcnow(),
                    **_write_values(model_cls, config, field_names),
                )
                .returning(model_cls)
            )
            result = await session.execute(statement)
            entity = result.scalars().first()
            if entity is None:
                await session.commit()
                return None
            stored = _stored_config_from_model(plugin_name, entity)
            await session.commit()
            return stored

        return operation

    # ------------------------------ 公共 API ------------------------------

    def fetch(self, plugin_name: str) -> StoredConfig | None:
        """按插件名读取配置快照。"""
        self._require_open()
        return self._run_sync(self._fetch_op(plugin_name))

    async def fetch_async(self, plugin_name: str) -> StoredConfig | None:
        """按插件名异步读取配置快照。"""
        self._require_open()
        return await self._run_on_app_session(self._fetch_op(plugin_name))

    def insert_if_absent(
        self,
        *,
        plugin_name: str,
        config: BaseModel,
    ) -> StoredConfig:
        """仅在配置不存在时初始化，否则返回并发写入的现有快照。"""
        self._require_open()
        return self._run_sync(
            self._insert_if_absent_op(plugin_name=plugin_name, config=config)
        )

    async def insert_if_absent_async(
        self,
        *,
        plugin_name: str,
        config: BaseModel,
    ) -> StoredConfig:
        """异步执行只插入不覆盖的配置初始化。"""
        self._require_open()
        return await self._run_on_app_session(
            self._insert_if_absent_op(plugin_name=plugin_name, config=config)
        )

    def update_if_unchanged(
        self,
        *,
        plugin_name: str,
        config: BaseModel,
        expected_updated_at: datetime,
    ) -> StoredConfig | None:
        """仅在记录未被其他写入修改时更新整份配置。"""
        self._require_open()
        return self._run_sync(
            self._update_if_unchanged_op(
                plugin_name=plugin_name,
                config=config,
                expected_updated_at=expected_updated_at,
            )
        )

    async def update_if_unchanged_async(
        self,
        *,
        plugin_name: str,
        config: BaseModel,
        expected_updated_at: datetime,
    ) -> StoredConfig | None:
        """记录未变化时异步更新整份配置。"""
        self._require_open()
        return await self._run_on_app_session(
            self._update_if_unchanged_op(
                plugin_name=plugin_name,
                config=config,
                expected_updated_at=expected_updated_at,
            )
        )

    def update_fields_if_revision(
        self,
        *,
        plugin_name: str,
        config: BaseModel,
        field_names: set[str],
        expected_revision: int,
    ) -> StoredConfig | None:
        """仅在修订号匹配时原子更新指定字段。"""
        self._require_open()
        return self._run_sync(
            self._update_fields_if_revision_op(
                plugin_name=plugin_name,
                config=config,
                field_names=field_names,
                expected_revision=expected_revision,
            )
        )

    async def update_fields_if_revision_async(
        self,
        *,
        plugin_name: str,
        config: BaseModel,
        field_names: set[str],
        expected_revision: int,
    ) -> StoredConfig | None:
        """异步原子更新指定字段，并校验修订号。"""
        self._require_open()
        return await self._run_on_app_session(
            self._update_fields_if_revision_op(
                plugin_name=plugin_name,
                config=config,
                field_names=field_names,
                expected_revision=expected_revision,
            )
        )


class _StorageState:
    storage: ClassVar[ConfigStorage | None] = None
    lock: ClassVar[threading.RLock] = threading.RLock()


def get_config_storage() -> ConfigStorage:
    """获取全局配置存储。"""
    if _StorageState.storage is None:
        with _StorageState.lock:
            if _StorageState.storage is None:
                _StorageState.storage = ConfigStorage()
    assert _StorageState.storage is not None
    return _StorageState.storage


async def close_config_storage_if_created() -> None:
    """关闭已创建的全局配置存储，不在关闭阶段创建新实例。"""
    with _StorageState.lock:
        storage = _StorageState.storage
        if storage is None:
            return
        _StorageState.storage = None
    await storage.close_async()
