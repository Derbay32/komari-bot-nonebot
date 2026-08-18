"""PostgreSQL 强类型 Prompt 存储与运行时加载辅助。

每个 Prompt 资源对应一张 ``TypedPromptModel`` 单行表（主键 ``id=1``、
CAS 修订号 ``revision``、写入时间 ``updated_at``），由 Alembic 迁移统一
建表。本模块通过 nonebot-plugin-orm 的 ``get_session`` 在调用方/应用事件
循环上执行 SQLAlchemy AsyncSession 操作：

- 异步入口（``*_async``）直接在调用方事件循环执行；
- 同步入口在应用事件循环绑定后通过 ``run_coroutine_threadsafe`` 提交，
  绑定前（如独立脚本）使用一次性事件循环与一次性引擎，用完即释放连接；
  禁止在事件循环内阻塞等待自身；
- 跨进程 Prompt 变更传播不再使用 asyncpg LISTEN/NOTIFY：
  ``PromptTemplateLoader`` 缓存本身有 1 秒陈限上限，传播延迟 ≤1 秒级；
  本进程写入通过 ``register_invalidator`` 回调立即失效本地缓存。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any, ClassVar, Protocol, TypeVar

from nonebot import logger
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from komari_bot.config.typed_config import (
    TypedConfigModel,
    ensure_typed_prompt_model,
)
from komari_bot.db.orm_config import get_orm_database_url
from komari_bot.llm.content_budget import (
    CONTENT_TEXT_BUDGET,
    ContentValidationError,
    validate_text_budget,
)

T = TypeVar("T")

_PROMPT_STORAGE_TIMEOUT_SECONDS = 5.0
_PROMPT_CACHE_MAX_STALENESS_SECONDS = 1.0

_INTERNAL_STORAGE_FIELDS = frozenset({"id", "revision", "updated_at"})
"""继承自 TypedConfigModel 的存储专用字段，不属于 Prompt 正文。"""

_PromptOperation = Callable[[AsyncSession], Coroutine[Any, Any, T]]


class PromptResourceProtocol(Protocol):
    """Prompt 资源需要提供的字段（TSK-191：不再携带 Python 默认正文）。"""

    @property
    def resource_id(self) -> str: ...

    @property
    def display_name(self) -> str: ...


@dataclass(frozen=True, slots=True)
class StoredPrompt:
    """已存储的 Prompt 快照（不含存储专用字段）。

    结构（Schema）版本的权威来源是 Alembic 迁移链，快照不再携带 version。
    """

    resource_id: str
    prompt_data: dict[str, str]
    revision: int
    updated_at: datetime


def _utcnow() -> datetime:
    """返回带时区的当前时间。"""
    return datetime.now(UTC)


def _public_field_names(model_cls: type[TypedConfigModel]) -> set[str]:
    """返回 Prompt 表的正文字段名集合（不含存储专用字段）。"""
    return set(model_cls.model_fields) - _INTERNAL_STORAGE_FIELDS


def _stored_prompt_from_entity(
    resource_id: str, entity: TypedConfigModel
) -> StoredPrompt:
    """把单行表实体转换为不可变 Prompt 快照。"""
    data = entity.model_dump()
    return StoredPrompt(
        resource_id=resource_id,
        prompt_data={name: str(value) for name, value in data.items()},
        revision=int(entity.revision),
        updated_at=entity.updated_at,
    )


def _validated_write_values(
    model_cls: type[TypedConfigModel],
    prompt_data: Mapping[str, str],
) -> dict[str, str]:
    """校验写入载荷与表正文字段一一对应，返回按列名排序的写入值。"""
    allowed = _public_field_names(model_cls)
    unknown_fields = sorted(set(prompt_data) - allowed)
    if unknown_fields:
        msg = f"存在未知提示词字段: {', '.join(unknown_fields)}"
        raise ValueError(msg)
    missing_fields = sorted(allowed - set(prompt_data))
    if missing_fields:
        msg = f"提示词写入缺少字段: {', '.join(missing_fields)}"
        raise ValueError(msg)
    return {name: prompt_data[name] for name in sorted(allowed)}


def _resolve_prompt_model(resource_id: str) -> type[TypedConfigModel]:
    """返回 Prompt 资源的强类型表模型；缺失时明确失败。"""
    model_cls = ensure_typed_prompt_model(resource_id)
    if model_cls is None:
        msg = f"Prompt 资源 {resource_id} 未注册强类型 Prompt 表"
        raise RuntimeError(msg)
    return model_cls


def prompt_resource_field_names(resource_id: str) -> set[str]:
    """返回 Prompt 资源的正文字段名集合（不含存储专用字段）。

    真源是 resource_id 对应的强类型 Prompt Schema（TSK-191）；管理 API、
    loader 与 save/replace/update/load 等写入 seam 统一从本入口派生白名单，
    不再依赖任何 Python 默认正文。
    """
    return _public_field_names(_resolve_prompt_model(resource_id))


class PromptStorage:
    """PostgreSQL 强类型 Prompt 存储门面。"""

    def __init__(self) -> None:
        self._state_lock = threading.RLock()
        self._closing = False
        self._closed = False
        self._app_loop: asyncio.AbstractEventLoop | None = None
        self._invalidators: dict[str, list[Callable[[], None]]] = {}
        self._invalidators_lock = threading.RLock()

    # ------------------------------ 生命周期 ------------------------------

    def bind_app_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定应用事件循环，供同步桥跨线程提交使用。"""
        with self._state_lock:
            if self._closing:
                return
            self._app_loop = loop

    def close(self) -> None:
        """关闭存储并拒绝后续操作。

        存储不再持有连接池或后台任务，同步桥的会话/引擎均为一次性资源，
        关闭只需标记状态。
        """
        with self._state_lock:
            self._closing = True
            self._closed = True
            self._app_loop = None

    @property
    def closed(self) -> bool:
        """是否已关闭。"""
        return self._closed

    # ------------------------------ 会话桥接 ------------------------------

    @staticmethod
    def _open_app_session() -> AsyncSession:
        from nonebot_plugin_orm import get_session

        return get_session(expire_on_commit=False)

    async def _run_on_app_session(self, operation: _PromptOperation[T]) -> T:
        session = self._open_app_session()
        try:
            return await operation(session)
        except BaseException:
            with suppress(Exception):
                await session.rollback()
            raise
        finally:
            await session.close()

    async def _run_on_private_engine(self, operation: _PromptOperation[T]) -> T:
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

    def _run_sync(self, operation: _PromptOperation[T]) -> T:
        """同步入口：线程上桥接异步实现，事件循环内禁用。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            msg = (
                "事件循环内禁止阻塞等待 Prompt 存储，请改用对应的 _async 方法"
            )
            raise RuntimeError(msg)

        app_loop = self._app_loop
        if app_loop is not None and app_loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self._run_on_app_session(operation), app_loop
            )
            try:
                return future.result(timeout=_PROMPT_STORAGE_TIMEOUT_SECONDS)
            except FutureTimeoutError as exc:
                future.cancel()
                msg = "Prompt 存储操作超时"
                raise RuntimeError(msg) from exc
        return asyncio.run(self._run_on_private_engine(operation))

    def _require_open(self) -> None:
        with self._state_lock:
            if self._closing or self._closed:
                msg = "Prompt 存储已关闭"
                raise RuntimeError(msg)

    def _resolve_model(self, resource_id: str) -> type[TypedConfigModel]:
        model_cls = ensure_typed_prompt_model(resource_id)
        if model_cls is None:
            msg = f"Prompt 资源 {resource_id} 未注册强类型 Prompt 表"
            raise RuntimeError(msg)
        return model_cls

    # ------------------------------ 本地失效 ------------------------------

    def register_invalidator(
        self,
        resource_id: str,
        callback: Callable[[], None],
    ) -> None:
        """注册当前进程 Prompt 缓存失效回调。

        移除 LISTEN/NOTIFY 后仅承担本地失效：本存储实例每次写入成功后
        立即触发对应资源的回调；跨进程变更由加载器缓存 1 秒陈限覆盖。
        """
        with self._invalidators_lock:
            callbacks = self._invalidators.setdefault(resource_id, [])
            if callback not in callbacks:
                callbacks.append(callback)

    def _notify_invalidators(self, resource_id: str) -> None:
        with self._invalidators_lock:
            callbacks = tuple(self._invalidators.get(resource_id, ()))
        for callback in callbacks:
            try:
                callback()
            except Exception as exc:
                logger.warning(
                    "Prompt 缓存失效回调失败: resource_id={}, error={}",
                    resource_id,
                    type(exc).__name__,
                )

    # ------------------------------ 存储操作 ------------------------------

    def _fetch_op(
        self, resource_id: str
    ) -> _PromptOperation[StoredPrompt | None]:
        async def operation(session: AsyncSession) -> StoredPrompt | None:
            model_cls = self._resolve_model(resource_id)
            entity = await session.get(model_cls, 1)
            if entity is None:
                return None
            return _stored_prompt_from_entity(resource_id, entity)

        return operation

    def _upsert_op(
        self,
        *,
        resource_id: str,
        prompt_data: dict[str, str],
    ) -> _PromptOperation[StoredPrompt]:
        async def operation(session: AsyncSession) -> StoredPrompt:
            model_cls = self._resolve_model(resource_id)
            table = model_cls.__table__
            values = _validated_write_values(model_cls, prompt_data)
            statement = (
                postgres_insert(model_cls)
                .values(id=1, revision=1, updated_at=_utcnow(), **values)
                .on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        **values,
                        "revision": table.c.revision + 1,
                        "updated_at": _utcnow(),
                    },
                )
                .returning(model_cls)
            )
            result = await session.execute(statement)
            entity = result.scalars().first()
            if entity is None:
                msg = f"Prompt 写入后未返回记录: {resource_id}"
                raise RuntimeError(msg)
            stored = _stored_prompt_from_entity(resource_id, entity)
            await session.commit()
            return stored

        return operation

    def _update_if_unchanged_op(
        self,
        *,
        resource_id: str,
        prompt_data: dict[str, str],
        expected_updated_at: datetime,
    ) -> _PromptOperation[StoredPrompt | None]:
        async def operation(session: AsyncSession) -> StoredPrompt | None:
            model_cls = self._resolve_model(resource_id)
            table = model_cls.__table__
            values = _validated_write_values(model_cls, prompt_data)
            statement = (
                update(model_cls)
                .where(
                    table.c.id == 1,
                    table.c.updated_at == expected_updated_at,
                )
                .values(
                    revision=table.c.revision + 1,
                    updated_at=_utcnow(),
                    **values,
                )
                .returning(model_cls)
            )
            result = await session.execute(statement)
            entity = result.scalars().first()
            if entity is None:
                await session.commit()
                return None
            stored = _stored_prompt_from_entity(resource_id, entity)
            await session.commit()
            return stored

        return operation

    def _replace_if_revision_op(
        self,
        *,
        resource_id: str,
        prompt_data: dict[str, str],
        expected_revision: int,
    ) -> _PromptOperation[StoredPrompt | None]:
        async def operation(session: AsyncSession) -> StoredPrompt | None:
            model_cls = self._resolve_model(resource_id)
            table = model_cls.__table__
            values = _validated_write_values(model_cls, prompt_data)
            if expected_revision == 0:
                statement = (
                    postgres_insert(model_cls)
                    .values(id=1, revision=1, updated_at=_utcnow(), **values)
                    .on_conflict_do_nothing(index_elements=["id"])
                    .returning(model_cls)
                )
            else:
                statement = (
                    update(model_cls)
                    .where(
                        table.c.id == 1,
                        table.c.revision == expected_revision,
                    )
                    .values(
                        revision=table.c.revision + 1,
                        updated_at=_utcnow(),
                        **values,
                    )
                    .returning(model_cls)
                )
            result = await session.execute(statement)
            entity = result.scalars().first()
            if entity is None:
                await session.commit()
                return None
            stored = _stored_prompt_from_entity(resource_id, entity)
            await session.commit()
            return stored

        return operation

    def _update_field_if_revision_op(
        self,
        *,
        resource_id: str,
        field_name: str,
        value: str,
        expected_revision: int,
    ) -> _PromptOperation[StoredPrompt | None]:
        async def operation(session: AsyncSession) -> StoredPrompt | None:
            model_cls = self._resolve_model(resource_id)
            if field_name not in _public_field_names(model_cls):
                msg = f"存在未知提示词字段: {field_name}"
                raise ValueError(msg)
            table = model_cls.__table__
            statement = (
                update(model_cls)
                .where(
                    table.c.id == 1,
                    table.c.revision == expected_revision,
                )
                .values(
                    revision=table.c.revision + 1,
                    updated_at=_utcnow(),
                    **{field_name: value},
                )
                .returning(model_cls)
            )
            result = await session.execute(statement)
            entity = result.scalars().first()
            if entity is None:
                await session.commit()
                return None
            stored = _stored_prompt_from_entity(resource_id, entity)
            await session.commit()
            return stored

        return operation

    # ------------------------------ 公共 API ------------------------------

    def fetch(self, resource_id: str) -> StoredPrompt | None:
        """按资源 ID 读取 prompt 配置。"""
        self._require_open()
        return self._run_sync(self._fetch_op(resource_id))

    async def fetch_async(self, resource_id: str) -> StoredPrompt | None:
        """异步读取 Prompt 配置，不阻塞调用方事件循环。"""
        self._require_open()
        return await self._run_on_app_session(self._fetch_op(resource_id))

    def upsert(
        self,
        *,
        resource_id: str,
        prompt_data: dict[str, str],
    ) -> StoredPrompt:
        """写入或更新 prompt 配置。"""
        self._require_open()
        stored = self._run_sync(
            self._upsert_op(resource_id=resource_id, prompt_data=prompt_data)
        )
        self._notify_invalidators(resource_id)
        return stored

    async def upsert_async(
        self,
        *,
        resource_id: str,
        prompt_data: dict[str, str],
    ) -> StoredPrompt:
        """异步完整写入 Prompt 配置。"""
        self._require_open()
        stored = await self._run_on_app_session(
            self._upsert_op(resource_id=resource_id, prompt_data=prompt_data)
        )
        self._notify_invalidators(resource_id)
        return stored

    def update_if_unchanged(
        self,
        *,
        resource_id: str,
        prompt_data: dict[str, str],
        expected_updated_at: datetime,
    ) -> StoredPrompt | None:
        """仅在记录未被其他写入修改时更新 prompt 配置。"""
        self._require_open()
        stored = self._run_sync(
            self._update_if_unchanged_op(
                resource_id=resource_id,
                prompt_data=prompt_data,
                expected_updated_at=expected_updated_at,
            )
        )
        if stored is not None:
            self._notify_invalidators(resource_id)
        return stored

    async def update_if_unchanged_async(
        self,
        *,
        resource_id: str,
        prompt_data: dict[str, str],
        expected_updated_at: datetime,
    ) -> StoredPrompt | None:
        """记录未变化时异步更新完整 Prompt。"""
        self._require_open()
        stored = await self._run_on_app_session(
            self._update_if_unchanged_op(
                resource_id=resource_id,
                prompt_data=prompt_data,
                expected_updated_at=expected_updated_at,
            )
        )
        if stored is not None:
            self._notify_invalidators(resource_id)
        return stored

    async def replace_if_revision_async(
        self,
        *,
        resource_id: str,
        prompt_data: dict[str, str],
        expected_revision: int,
    ) -> StoredPrompt | None:
        """仅在 revision 匹配时替换完整 Prompt；0 表示仅允许首次创建。"""
        self._require_open()
        stored = await self._run_on_app_session(
            self._replace_if_revision_op(
                resource_id=resource_id,
                prompt_data=prompt_data,
                expected_revision=expected_revision,
            )
        )
        if stored is not None:
            self._notify_invalidators(resource_id)
        return stored

    async def update_field_if_revision_async(
        self,
        *,
        resource_id: str,
        field_name: str,
        value: str,
        expected_revision: int,
    ) -> StoredPrompt | None:
        """按 revision 原子更新一个 Prompt 正文字段。"""
        self._require_open()
        stored = await self._run_on_app_session(
            self._update_field_if_revision_op(
                resource_id=resource_id,
                field_name=field_name,
                value=value,
                expected_revision=expected_revision,
            )
        )
        if stored is not None:
            self._notify_invalidators(resource_id)
        return stored


class _StorageState:
    storage: ClassVar[PromptStorage | None] = None
    lock: ClassVar[threading.RLock] = threading.RLock()


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


def validate_prompt_values(
    resource: PromptResourceProtocol,
    values: Mapping[str, object],
) -> dict[str, str]:
    """按资源 Schema 校验字段载荷并返回清洗后的字段子集。

    字段白名单来自 ``ensure_typed_prompt_model(resource.resource_id)`` 对应
    的强类型 Schema（TSK-191）：未知/跨资源字段、空白字段与超预算字段一律
    拒绝。允许传入单个字段（PATCH），也允许传入完整字段集（保存/替换由
    调用方另行做集合完整性校验）。
    """
    allowed = prompt_resource_field_names(resource.resource_id)
    unknown_fields = sorted(set(values) - allowed)
    if unknown_fields:
        fields = ", ".join(unknown_fields)
        msg = f"存在未知提示词字段: {fields}"
        raise ValueError(msg)

    cleaned: dict[str, str] = {}
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


def _require_exact_prompt_fields(
    resource: PromptResourceProtocol,
    values: Mapping[str, object],
) -> None:
    """完整保存/替换必须与 Schema 字段集合恰好相等（缺失/未知均拒绝）。"""
    allowed = prompt_resource_field_names(resource.resource_id)
    unknown_fields = sorted(set(values) - allowed)
    if unknown_fields:
        fields = ", ".join(unknown_fields)
        msg = f"存在未知提示词字段: {fields}"
        raise ValueError(msg)
    missing_fields = sorted(allowed - set(values))
    if missing_fields:
        fields = ", ".join(missing_fields)
        msg = f"提示词写入缺少字段: {fields}"
        raise ValueError(msg)


def validate_prompt_snapshot(
    resource: PromptResourceProtocol,
    prompt_data: Mapping[str, object],
) -> dict[str, str]:
    """校验 PostgreSQL 快照足以冷启动并返回原样快照（TSK-191）。

    字段必须与 resource_id 对应 Schema 恰好一致、全部为非空字符串且通过
    共享内容预算；无行（空快照）/缺字段/空白字段/异资源（未知）字段或
    超预算值都抛 ``RuntimeError`` 并点名 Prompt 资源与字段。只校验不改写
    正文，绝不并入任何 Python 默认内容。
    """
    allowed = prompt_resource_field_names(resource.resource_id)
    missing = sorted(allowed - set(prompt_data))
    unknown = sorted(set(prompt_data) - allowed)
    blank = sorted(
        field
        for field in sorted(allowed & set(prompt_data))
        if (
            not isinstance(prompt_data[field], str)
            or not str(prompt_data[field]).strip()
        )
    )
    problems: list[str] = []
    if missing:
        problems.append(f"缺失字段: {', '.join(missing)}")
    if blank:
        problems.append(f"空白字段: {', '.join(blank)}")
    if unknown:
        problems.append(f"未知字段: {', '.join(unknown)}")
    if problems:
        msg = (
            f"komari_bot Prompt 资源 {resource.resource_id}"
            f"（{resource.display_name}）的 PostgreSQL 快照不是完整 Prompt，"
            f"无法冷启动: {'；'.join(problems)}"
        )
        raise RuntimeError(msg)

    normalized: dict[str, str] = {}
    for field in sorted(allowed):
        value = prompt_data[field]
        assert isinstance(value, str) and value.strip()
        try:
            validate_text_budget(
                value,
                label=f"Prompt 资源 {resource.resource_id} 字段 {field}",
                budget=CONTENT_TEXT_BUDGET,
            )
        except ContentValidationError as exc:
            msg = (
                f"komari_bot Prompt 资源 {resource.resource_id}"
                f"（{resource.display_name}）的 PostgreSQL 快照不符合内容预算，"
                f"无法冷启动: {exc}"
            )
            raise RuntimeError(msg) from exc
        normalized[field] = value
    return normalized


def load_prompt_values(resource: PromptResourceProtocol) -> StoredPrompt | None:
    """从 PostgreSQL 读取 Prompt 快照（不合并任何默认正文）。"""
    return get_prompt_storage().fetch(resource.resource_id)


async def load_prompt_values_async(
    resource: PromptResourceProtocol,
) -> StoredPrompt | None:
    """异步读取 Prompt 快照（不合并任何默认正文）。"""
    return await get_prompt_storage().fetch_async(resource.resource_id)


def save_prompt_values(
    resource: PromptResourceProtocol,
    values: Mapping[str, object],
) -> StoredPrompt:
    """校验（字段必须与 Schema 恰好相等）并完整保存 prompt 配置。"""
    _require_exact_prompt_fields(resource, values)
    cleaned = validate_prompt_values(resource, values)
    return get_prompt_storage().upsert(
        resource_id=resource.resource_id,
        prompt_data=cleaned,
    )


async def save_prompt_values_async(
    resource: PromptResourceProtocol,
    values: Mapping[str, object],
) -> StoredPrompt:
    """异步校验并完整保存 Prompt（字段集来自资源 Schema）。"""
    _require_exact_prompt_fields(resource, values)
    cleaned = validate_prompt_values(resource, values)
    return await get_prompt_storage().upsert_async(
        resource_id=resource.resource_id,
        prompt_data=cleaned,
    )


async def replace_prompt_values_async(
    resource: PromptResourceProtocol,
    values: Mapping[str, object],
    *,
    expected_revision: int,
) -> StoredPrompt | None:
    """校验后按 revision 原子替换完整 Prompt（字段集必须与 Schema 恰好相等）。"""
    _require_exact_prompt_fields(resource, values)
    cleaned = validate_prompt_values(resource, values)
    return await get_prompt_storage().replace_if_revision_async(
        resource_id=resource.resource_id,
        prompt_data=cleaned,
        expected_revision=expected_revision,
    )


async def update_prompt_field_async(
    resource: PromptResourceProtocol,
    field_name: str,
    value: object,
    *,
    expected_revision: int,
) -> StoredPrompt | None:
    """校验并按 revision 更新单个 Prompt 字段（白名单来自资源 Schema）。"""
    if field_name not in prompt_resource_field_names(resource.resource_id):
        msg = f"存在未知提示词字段: {field_name}"
        raise ValueError(msg)
    cleaned = validate_prompt_values(resource, {field_name: value})
    if expected_revision == 0:
        return await get_prompt_storage().replace_if_revision_async(
            resource_id=resource.resource_id,
            prompt_data=cleaned,
            expected_revision=0,
        )
    return await get_prompt_storage().update_field_if_revision_async(
        resource_id=resource.resource_id,
        field_name=field_name,
        value=cleaned[field_name],
        expected_revision=expected_revision,
    )


class PromptTemplateLoader:
    """运行时 prompt 模板加载器（TSK-191：不再接受 Python 默认正文）。"""

    def __init__(
        self,
        *,
        resource_id: str,
        display_name: str,
        log_prefix: str,
    ) -> None:
        self.resource_id = resource_id
        self.display_name = display_name
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
        """数据库读取异常时回退最后有效缓存；无缓存则冷启动明确失败。

        TSK-191：不存在的 Python 默认正文，冷启动只可能来自 PostgreSQL；
        无行/不完整是校验错误而非读取异常，不经过本路径。
        """
        with self._cache_lock:
            if self._cache:
                self._cache_checked_at = monotonic()
                self._invalidated = False
                return dict(self._cache)
            msg = (
                f"{self._log_prefix} komari_bot Prompt 资源 {self.resource_id}"
                f"（{self.display_name}）的 PostgreSQL 初始数据不可用且无缓存，"
                "无法冷启动"
            )
            raise RuntimeError(msg)

    def _accept_loaded(self, stored: StoredPrompt | None) -> dict[str, str]:
        """缓存前校验 PostgreSQL 快照完整性（无行/缺字段/空白/异资源字段失败）。

        校验失败时绝不写入缓存，异常向上传播；完整快照按 revision 为基线
        写入新鲜缓存。最后有效缓存只在读取异常（DB 故障）时由
        ``_fallback_template`` 容灾，恢复后按 revision 刷新。
        """
        prompt_data = stored.prompt_data if stored is not None else {}
        values = validate_prompt_snapshot(self, prompt_data)
        updated_at = stored.updated_at if stored is not None else None
        revision = stored.revision if stored is not None else 0
        with self._cache_lock:
            changed = not self._cache or revision != self._cache_revision
            self._cache = dict(values)
            self._cache_updated_at = updated_at
            self._cache_revision = revision
            self._cache_checked_at = monotonic()
            self._invalidated = False
            result = dict(self._cache)

        if changed:
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
                "{} Prompt 配置读取失败，使用最后有效缓存",
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
                    "{} Prompt 配置异步读取失败，使用最后有效缓存",
                    self._log_prefix,
                    exc_info=True,
                )
                return self._fallback_template()
            return self._accept_loaded(loaded)
