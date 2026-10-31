"""TSK-182/189：判定配置读取失败的启动隔离与公共健康态投影。

全部用例使用真实 ``DecisionRuntime``、真实 ``ConfigManager`` 与真实
``KomariDecisionConfigSchema``，只替换外部存储、共享 ORM 会话、自有 Redis 与
Prompt 加载器边界。配置失败时不得构造任何旧场景子系统，也不得写入默认配置；
公共健康态只投影稳定的原因码。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from loguru import logger as loguru_logger
from pydantic import ValidationError

from komari_bot.config.prompt_storage import PromptTemplateLoader
from komari_bot.plugins.config_manager import manager as manager_module
from komari_bot.plugins.config_manager.manager import ConfigManager
from komari_bot.plugins.config_manager.storage import StoredConfig
from komari_bot.plugins.komari_decision.config_schema import (
    KomariDecisionConfigSchema,
)
from komari_bot.plugins.komari_decision.prompt_schema import (
    DISPLAY_NAME as DECISION_PROMPT_DISPLAY_NAME,
)
from komari_bot.plugins.komari_decision.prompt_schema import (
    RESOURCE_ID as DECISION_PROMPT_RESOURCE_ID,
)
from komari_bot.plugins.komari_decision.services import config_interface
from komari_bot.plugins.komari_decision.services.jev_runtime import DecisionRuntime

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.ext.asyncio import AsyncSession

_UPDATED_AT = datetime(2026, 10, 24, 11, 0, 0, tzinfo=UTC)
_SENTINEL = "SENTINEL-TSK189-c0ffee"
_EXPECTED_FAILED_REASON = "configuration_invalid"


def _valid_data(**overrides: Any) -> dict[str, Any]:
    """用真实 Schema 生成一份完整、可校验通过的持久化数据。"""
    return KomariDecisionConfigSchema(**overrides).model_dump(mode="json")


def _stored(data: dict[str, Any], revision: int) -> StoredConfig:
    return StoredConfig(
        plugin_name="komari_decision",
        config_data=data,
        revision=revision,
        updated_at=_UPDATED_AT,
    )


class _FaultStorage:
    """只替换外部存储边界：读取结果或被注入的故障由用例决定。"""

    def __init__(self, entry: StoredConfig | BaseException) -> None:
        self.entry = entry
        self.fetch_calls = 0

    def register_watcher(
        self,
        _plugin_name: str,
        _callback: Any,
        *,
        max_staleness_seconds: float,
    ) -> None:
        del _plugin_name, _callback, max_staleness_seconds

    def _read(self, _plugin_name: str) -> StoredConfig:
        self.fetch_calls += 1
        if isinstance(self.entry, BaseException):
            raise self.entry
        return self.entry

    def fetch(self, plugin_name: str) -> StoredConfig:
        return self._read(plugin_name)

    async def fetch_async(self, plugin_name: str) -> StoredConfig:
        return self._read(plugin_name)

    def insert_if_absent(self, **_kwargs: Any) -> StoredConfig:
        raise AssertionError("配置读取失败后不得写入默认配置")

    async def insert_if_absent_async(self, **_kwargs: Any) -> StoredConfig:
        raise AssertionError("配置读取失败后不得写入默认配置")

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


class _FakeRedis:
    """自有 Redis 边界替身：只实现运行时会调用的 ping / aclose。"""

    def __init__(self) -> None:
        self.closed = False

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        self.closed = True


def _broken_session() -> AsyncSession:
    """共享 ORM 会话边界：本文件不提供真实数据库，初始化必须安全失败。"""
    msg = "synthetic database outage"
    raise ConnectionError(msg)


def _install_storage(
    monkeypatch: pytest.MonkeyPatch,
    storage: _FaultStorage,
) -> ConfigManager:
    manager = ConfigManager("komari_decision", KomariDecisionConfigSchema)
    monkeypatch.setattr(config_interface, "_config_manager", manager)
    monkeypatch.setattr(manager_module, "get_config_storage", lambda: storage)
    return manager


def _runtime(
    monkeypatch: pytest.MonkeyPatch, storage: _FaultStorage
) -> DecisionRuntime:
    manager = _install_storage(monkeypatch, storage)
    return DecisionRuntime(
        config_manager=manager,
        session_factory=_broken_session,
        redis_factory=lambda: cast("Any", _FakeRedis()),
        prompt_loader=PromptTemplateLoader(
            resource_id=DECISION_PROMPT_RESOURCE_ID,
            display_name=DECISION_PROMPT_DISPLAY_NAME,
            log_prefix="[test]",
        ),
        bot_resolver=lambda _account_id: None,
    )


async def test_initial_read_failure_is_isolated_and_projected_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """初始配置读取失败被隔离：不启动场景，公共健康态只投影稳定原因码。"""
    storage = _FaultStorage(OSError(f"config source unavailable {_SENTINEL}"))
    runtime = _runtime(monkeypatch, storage)

    try:
        with _capture_logs() as messages:
            await runtime.startup()
            logged_after_initialize = len(messages)
            first = runtime.get_health()
            second = runtime.get_health()
            assert len(messages) == logged_after_initialize

        assert first.runtime_status == "failed"
        assert first.reason_code == _EXPECTED_FAILED_REASON
        assert second == first
        assert _SENTINEL not in first.reason_code
        joined = "".join(messages)
        assert _SENTINEL not in joined
        assert "OSError" in joined
    finally:
        await runtime.close()


async def test_invalid_persisted_config_fails_health_without_default_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 Schema 校验失败同样被隔离，且配置读取接口仍拒绝非法输入。"""
    storage = _FaultStorage(_stored({"jev_request_timeout_seconds": 0}, 4))
    runtime = _runtime(monkeypatch, storage)

    try:
        with _capture_logs() as messages:
            await runtime.startup()
            state = runtime.get_health()

        assert state.runtime_status == "failed"
        assert state.reason_code == _EXPECTED_FAILED_REASON
        assert "ValidationError" in "".join(messages)

        # 失败态只是投影，绝不缓存默认值：真实配置接口仍然抛出校验错误。
        with pytest.raises(ValidationError):
            await config_interface.get_config_async()
    finally:
        await runtime.close()


async def test_repaired_source_reaches_normal_path_on_later_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """修复外部数据源后，后续刷新不再报告配置非法，无需重建运行时。"""
    storage = _FaultStorage(_stored({"jev_request_timeout_seconds": 0}, 4))
    runtime = _runtime(monkeypatch, storage)

    try:
        await runtime.startup()
        assert runtime.get_health().runtime_status == "failed"

        storage.entry = _stored(_valid_data(), 5)
        reads_before_retry = storage.fetch_calls
        await runtime.refresh()

        state = runtime.get_health()
        assert state.runtime_status == "failed"
        # 修复后只剩共享存储/所有权前置条件失败，不再报告配置非法。
        assert state.reason_code != _EXPECTED_FAILED_REASON
        assert storage.fetch_calls == reads_before_retry + 1
    finally:
        await runtime.close()


async def test_config_failure_reason_is_fixed_and_shared_by_both_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启动失败写入的运行态与健康观测返回同一条稳定安全原因码。"""
    storage = _FaultStorage(OSError(f"boom {_SENTINEL}"))
    runtime = _runtime(monkeypatch, storage)

    try:
        await runtime.startup()
        projected = runtime.get_health()

        assert projected.runtime_status == "failed"
        assert projected.reason_code == _EXPECTED_FAILED_REASON
        assert runtime._failure_reason == projected.reason_code
        assert _SENTINEL not in projected.reason_code
    finally:
        await runtime.close()
