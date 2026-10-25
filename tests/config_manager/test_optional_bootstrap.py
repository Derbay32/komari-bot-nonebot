"""TSK-182：配置管理器启动预热对显式可选资源的失败隔离。

全部用例使用真实 ``ConfigManager`` 与真实插件 Schema，只替换外部存储边界
（PostgreSQL 访问被替换为可注入故障的替身），预热编排、Schema 校验、快照
发布与缓存写入全部走生产实现。
"""

from __future__ import annotations

import asyncio
import importlib
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, cast

import pytest
from loguru import logger as loguru_logger
from pydantic import BaseModel, ValidationError

from komari_bot.config.typed_config import (
    TypedConfigModel,
    ensure_typed_config_model,
    get_config_section_metadata,
    public_config_field_names,
)
from komari_bot.plugins.config_manager import manager as manager_module
from komari_bot.plugins.config_manager.manager import (
    ConfigManager,
    get_config_manager,
    initialize_registered_config_managers_async,
)
from komari_bot.plugins.config_manager.storage import StoredConfig
from komari_bot.plugins.komari_management import config_api

if TYPE_CHECKING:
    from collections.abc import Iterator

_UPDATED_AT = datetime(2026, 10, 24, 9, 0, 0, tzinfo=UTC)
_SENTINEL = "SENTINEL-TSK182-4f2c1b"
"""外部失败体里的哨兵：任何安全诊断都不得回显它。"""


def _schema(plugin_name: str) -> type[TypedConfigModel]:
    """取得真实插件配置 Schema（走生产加载器，不执行插件包 ``__init__``）。"""
    schema = ensure_typed_config_model(plugin_name)
    assert schema is not None, f"插件 {plugin_name} 的配置 Schema 未注册"
    return schema


def _valid_data(schema: type[TypedConfigModel], **overrides: Any) -> dict[str, Any]:
    """用真实 Schema 生成一份完整、可校验通过的持久化数据。"""
    return schema(**overrides).model_dump(mode="json")


def _stored(plugin_name: str, data: dict[str, Any], revision: int) -> StoredConfig:
    return StoredConfig(
        plugin_name=plugin_name,
        config_data=data,
        revision=revision,
        updated_at=_UPDATED_AT,
    )


class _FaultStorage:
    """只替换外部存储边界：故障注入点就是「从 PostgreSQL 读到什么」。

    ``entries`` 的取值可以是 ``StoredConfig``（正常返回）或异常实例（该次读取
    抛出）。写路径全部 fail-if-called：本切片不得写入任何默认配置。
    """

    def __init__(self, entries: dict[str, StoredConfig | BaseException]) -> None:
        self.entries = entries
        self.fetch_calls: list[str] = []

    def register_watcher(
        self,
        _plugin_name: str,
        _callback: Any,
        *,
        max_staleness_seconds: float,
    ) -> None:
        del _plugin_name, _callback, max_staleness_seconds

    def _read(self, plugin_name: str) -> StoredConfig:
        self.fetch_calls.append(plugin_name)
        entry = self.entries[plugin_name]
        if isinstance(entry, BaseException):
            raise entry
        return entry

    def fetch(self, plugin_name: str) -> StoredConfig:
        return self._read(plugin_name)

    async def fetch_async(self, plugin_name: str) -> StoredConfig:
        return self._read(plugin_name)

    def insert_if_absent(self, **_kwargs: Any) -> StoredConfig:
        raise AssertionError("预热失败后不得写入默认配置")

    async def insert_if_absent_async(self, **_kwargs: Any) -> StoredConfig:
        raise AssertionError("预热失败后不得写入默认配置")

    def update_if_unchanged(self, **_kwargs: Any) -> StoredConfig | None:
        raise AssertionError("本用例不应触发配置归一化写回")

    async def update_if_unchanged_async(self, **_kwargs: Any) -> StoredConfig | None:
        raise AssertionError("本用例不应触发配置归一化写回")


@contextmanager
def _capture_logs() -> Iterator[list[str]]:
    """捕获 loguru 日志（``nonebot.logger`` 就是 loguru 全局 logger）。"""
    messages: list[str] = []

    def _sink(message: Any) -> None:
        messages.append(str(message))

    handler_id = loguru_logger.add(_sink, level="DEBUG")
    try:
        yield messages
    finally:
        loguru_logger.remove(handler_id)


@pytest.fixture(autouse=True)
def _clear_manager_registry() -> Iterator[None]:
    manager_module._config_managers.clear()
    yield
    manager_module._config_managers.clear()


def _install_storage(
    monkeypatch: pytest.MonkeyPatch,
    storage: _FaultStorage,
) -> None:
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)


@pytest.mark.asyncio
async def test_optional_failure_does_not_block_later_required_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """可选资源校验失败后，后面注册的必需资源仍被真实初始化。"""
    decision_schema = _schema("komari_decision")
    required_schema = _schema("komari_help")
    storage = _FaultStorage(
        {
            "komari_decision": _stored(
                "komari_decision", {"optional_reply_probability": 7.5}, 4
            ),
            "komari_help": _stored(
                "komari_help", _valid_data(required_schema, total_limit=7), 2
            ),
        }
    )
    _install_storage(monkeypatch, storage)
    optional_manager = get_config_manager("komari_decision", decision_schema)
    required_manager = get_config_manager("komari_help", required_schema)

    assert optional_manager.startup_required is False
    assert required_manager.startup_required is True

    await initialize_registered_config_managers_async()

    assert storage.fetch_calls == ["komari_decision", "komari_help"]
    required_snapshot = required_manager.get_cached_versioned_snapshot()
    assert cast("Any", required_snapshot.value).total_limit == 7
    assert required_snapshot.revision == 2

    with pytest.raises(RuntimeError):
        optional_manager.get_cached_versioned_snapshot()
    with pytest.raises(ValidationError):
        await optional_manager.get_async()


@pytest.mark.asyncio
async def test_optional_failure_diagnostics_carry_no_exception_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """可选资源的失败诊断只含资源名与异常类型，不含异常正文或凭据。"""
    decision_schema = _schema("komari_decision")
    required_schema = _schema("komari_help")
    storage = _FaultStorage(
        {
            "komari_decision": RuntimeError(f"pg dsn=postgres://u:pw {_SENTINEL}"),
            "komari_help": _stored(
                "komari_help", _valid_data(required_schema, total_limit=4), 3
            ),
        }
    )
    _install_storage(monkeypatch, storage)
    get_config_manager("komari_decision", decision_schema)
    required_manager = get_config_manager("komari_help", required_schema)

    with _capture_logs() as messages:
        await initialize_registered_config_managers_async()

    joined = "".join(messages)
    assert _SENTINEL not in joined
    assert "komari_decision" in joined
    assert "RuntimeError" in joined
    assert storage.fetch_calls == ["komari_decision", "komari_help"]
    assert (
        cast("Any", required_manager.get_cached_versioned_snapshot().value).total_limit
        == 4
    )


@pytest.mark.asyncio
async def test_repaired_source_publishes_real_snapshot_on_explicit_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """修复外部数据源后，显式重试必须真实初始化并发布实际快照。"""
    decision_schema = _schema("komari_decision")
    storage = _FaultStorage(
        {
            "komari_decision": _stored(
                "komari_decision", {"optional_reply_probability": 7.5}, 4
            )
        }
    )
    _install_storage(monkeypatch, storage)
    manager = get_config_manager("komari_decision", decision_schema)

    await initialize_registered_config_managers_async()
    with pytest.raises(RuntimeError):
        manager.get_cached_versioned_snapshot()

    observed: list[Any] = []
    manager.register_snapshot_listener(observed.append)
    storage.entries["komari_decision"] = _stored(
        "komari_decision",
        _valid_data(decision_schema, optional_reply_probability=0.65),
        9,
    )

    config = await manager.get_async()

    snapshot = manager.get_cached_versioned_snapshot()
    assert snapshot.revision == 9
    assert snapshot.updated_at == _UPDATED_AT
    assert snapshot.value is config
    assert cast("Any", config).optional_reply_probability == 0.65
    assert observed == [snapshot]


@pytest.mark.asyncio
async def test_failing_required_manager_aborts_remaining_warmup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未声明可选的资源失败仍然向上传播，并阻止后续资源被初始化。"""
    help_schema = _schema("komari_help")
    knowledge_schema = _schema("komari_knowledge")
    storage = _FaultStorage(
        {
            "komari_help": _stored("komari_help", {"total_limit": 0}, 1),
            "komari_knowledge": _stored(
                "komari_knowledge", _valid_data(knowledge_schema), 1
            ),
        }
    )
    _install_storage(monkeypatch, storage)
    get_config_manager("komari_help", help_schema)
    knowledge_manager = get_config_manager("komari_knowledge", knowledge_schema)

    with pytest.raises(ValidationError):
        await initialize_registered_config_managers_async()

    assert storage.fetch_calls == ["komari_help"]
    with pytest.raises(RuntimeError):
        knowledge_manager.get_cached_versioned_snapshot()


class _FatalSignal(BaseException):
    """模拟非 CancelledError 的 BaseException 信号。"""


@pytest.mark.parametrize(
    "signal",
    [
        pytest.param(asyncio.CancelledError(), id="cancelled"),
        pytest.param(_FatalSignal(), id="fatal"),
    ],
)
@pytest.mark.asyncio
async def test_optional_manager_never_swallows_base_exceptions(
    monkeypatch: pytest.MonkeyPatch,
    signal: BaseException,
) -> None:
    """可选资源的隔离只捕获 Exception，取消与其他 BaseException 必须原样传播。"""
    decision_schema = _schema("komari_decision")
    help_schema = _schema("komari_help")
    storage = _FaultStorage(
        {
            "komari_decision": signal,
            "komari_help": _stored("komari_help", _valid_data(help_schema), 1),
        }
    )
    _install_storage(monkeypatch, storage)
    get_config_manager("komari_decision", decision_schema)
    help_manager = get_config_manager("komari_help", help_schema)

    with pytest.raises(type(signal)):
        await initialize_registered_config_managers_async()

    assert storage.fetch_calls == ["komari_decision"]
    with pytest.raises(RuntimeError):
        help_manager.get_cached_versioned_snapshot()


class _UndeclaredSchema(BaseModel):
    """没有声明启动策略的 Schema。"""

    value: int = 1


class _LiteralFalseSchema(BaseModel):
    """显式声明可选。"""

    startup_required: ClassVar[bool] = False
    value: int = 1


@pytest.mark.parametrize(
    "declaration",
    [
        pytest.param(0, id="zero"),
        pytest.param(None, id="none"),
        pytest.param("", id="empty"),
        pytest.param("false", id="string-false"),
        pytest.param(1, id="true"),
    ],
)
def test_only_literal_false_opts_out(declaration: Any) -> None:
    """任意假值都不是 opt-out，只有字面量 False 表示可选。"""

    class _DeclaredSchema(BaseModel):
        startup_required: ClassVar[Any] = declaration
        value: int = 1

    assert ConfigManager("declared-policy", _DeclaredSchema).startup_required is True


def test_missing_declaration_defaults_to_required() -> None:
    """未声明启动策略的 Schema 一律按启动必需处理。"""
    assert (
        ConfigManager("undeclared-policy", _UndeclaredSchema).startup_required is True
    )


def test_literal_false_declaration_opts_out() -> None:
    """字面量 False 声明使该资源可选。"""
    assert (
        ConfigManager("optional-policy", _LiteralFalseSchema).startup_required is False
    )


def test_optional_declaration_is_not_persisted_or_writable() -> None:
    """启动策略是静态 ClassVar 政策，不是数据库列、不可管理写入。"""
    schema = _schema("komari_decision")
    instance = schema()

    assert TypedConfigModel.startup_required is True
    assert schema.startup_required is False
    assert _schema("komari_help").startup_required is True

    assert "startup_required" not in schema.model_fields
    assert "startup_required" not in public_config_field_names(instance)
    assert "startup_required" not in instance.model_dump()
    assert "startup_required" not in instance.model_dump_json()
    assert "startup_required" not in {
        column.name for column in schema.__table__.columns
    }
    assert (
        "startup_required" not in get_config_section_metadata(schema).field_section_ids
    )

    # 管理接口的可写字段就是 model_fields ∩ 对外 model_dump 的交集。
    assert "startup_required" not in config_api._get_fields(instance)
    assert "startup_required" not in config_api._get_field_metadata(
        instance, get_config_section_metadata(schema)
    )


@pytest.mark.asyncio
async def test_real_lifespan_runs_later_startup_hooks_after_isolated_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 NoneBot 生命周期按顺序执行启动钩子，隔离后后续钩子照常运行。"""
    lifespan_module = cast(
        "Any", importlib.import_module("nonebot.internal.driver._lifespan")
    )
    decision_schema = _schema("komari_decision")
    required_schema = _schema("komari_help")
    storage = _FaultStorage(
        {
            "komari_decision": _stored(
                "komari_decision", {"optional_reply_probability": 7.5}, 4
            ),
            "komari_help": _stored(
                "komari_help", _valid_data(required_schema, total_limit=9), 5
            ),
        }
    )
    _install_storage(monkeypatch, storage)
    optional_manager = get_config_manager("komari_decision", decision_schema)
    required_manager = get_config_manager("komari_help", required_schema)

    lifespan = lifespan_module.Lifespan()
    lifespan.on_startup(initialize_registered_config_managers_async)

    observed: list[int] = []

    async def _business_startup() -> None:
        """模拟依赖配置的业务启动钩子：同步读取已预热的必需配置。"""
        observed.append(cast("Any", required_manager.get()).total_limit)

    lifespan.on_startup(_business_startup)

    await lifespan.startup()
    try:
        assert observed == [9]
    finally:
        await lifespan.shutdown()

    assert storage.fetch_calls == ["komari_decision", "komari_help"]
    with pytest.raises(RuntimeError):
        optional_manager.get_cached_versioned_snapshot()


@pytest.mark.asyncio
async def test_memory_config_failure_does_not_block_chat_config_warmup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TSK-192 phase B: old-memory config is optional; chat stays required."""
    memory_schema = _schema("komari_memory")
    chat_schema = _schema("komari_chat")
    context_schema = _schema("komari_context")
    storage = _FaultStorage(
        {
            "komari_context": _stored(
                "komari_context",
                _valid_data(context_schema, bot_nickname="上下文实例"),
                4,
            ),
            "komari_chat": _stored(
                "komari_chat",
                _valid_data(
                    chat_schema,
                    plugin_enable=True,
                    context_messages_limit=17,
                ),
                6,
            ),
            "komari_memory": _stored(
                "komari_memory",
                {"llm_temperature_summary": 99.0},
                8,
            ),
        }
    )
    _install_storage(monkeypatch, storage)
    memory_manager = get_config_manager("komari_memory", memory_schema)
    chat_manager = get_config_manager("komari_chat", chat_schema)
    context_manager = get_config_manager("komari_context", context_schema)

    assert memory_manager.startup_required is False
    assert chat_manager.startup_required is True
    assert context_manager.startup_required is True

    await initialize_registered_config_managers_async()

    context_snapshot = context_manager.get_cached_versioned_snapshot()
    assert cast("Any", context_snapshot.value).bot_nickname == "上下文实例"
    chat_snapshot = chat_manager.get_cached_versioned_snapshot()
    assert cast("Any", chat_snapshot.value).context_messages_limit == 17
    assert chat_snapshot.revision == 6
    with pytest.raises(RuntimeError):
        memory_manager.get_cached_versioned_snapshot()
