"""Komari Sentry 插件初始化边界测试。"""

from __future__ import annotations

import sys
from importlib import import_module
from types import MappingProxyType, SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

if TYPE_CHECKING:
    from nonebug import App


def _config(*, send_default_pii: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        plugin_enable=True,
        dsn="https://public@example.invalid/1",
        environment="test",
        release="",
        debug=False,
        error_sample_rate=1.0,
        traces_sample_rate=1.0,
        profiles_sample_rate=0.0,
        attach_stacktrace=True,
        send_default_pii=send_default_pii,
        max_breadcrumbs=100,
        breadcrumb_level="WARNING",
        sentry_logs_level="WARNING",
        event_level="ERROR",
        shutdown_timeout=0.0,
    )


@pytest.fixture
def sentry_plugin(app: App, monkeypatch: pytest.MonkeyPatch) -> Any:
    del app
    module_name = "komari_bot.plugins.komari_sentry"
    sys.modules.pop(module_name, None)
    module = import_module(module_name)
    module_any = cast("Any", module)

    async def _get_config_async() -> SimpleNamespace:
        return _config()

    monkeypatch.setattr(module_any, "get_config_async", _get_config_async)
    module_any._initialized_by_plugin = False
    return module_any


class _FakeSentrySdk:
    def __init__(self, *, initialized: bool, options: object) -> None:
        self.initialized = initialized
        self.client = SimpleNamespace(options=options, close_calls=[])
        self.init_calls: list[dict[str, object]] = []

        def _close(*, timeout: float) -> None:
            self.client.close_calls.append(timeout)

        self.client.close = _close

    def is_initialized(self) -> bool:
        return self.initialized

    def get_client(self) -> object:
        return self.client

    def init(self, **options: object) -> None:
        self.init_calls.append(options)
        self.client.options = options
        self.initialized = True


@pytest.mark.asyncio
async def test_external_initialized_client_receives_verified_privacy_hooks(
    sentry_plugin: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = _FakeSentrySdk(
        initialized=True,
        options={
            "before_send": None,
            "before_breadcrumb": None,
            "before_send_transaction": None,
            "before_send_log": None,
        },
    )
    monkeypatch.setattr(sentry_plugin, "sentry_sdk", sdk)

    await sentry_plugin.startup()

    assert sdk.init_calls == []
    transaction_hook = sdk.client.options["before_send_transaction"]
    sanitized = transaction_hook(
        {
            "transaction": "external-transaction-canary",
            "spans": [{"description": "external-span-canary"}],
        },
        {},
    )
    assert "external-transaction-canary" not in str(sanitized)
    assert "external-span-canary" not in str(sanitized)


@pytest.mark.asyncio
async def test_external_client_respects_pii_full_log_policy(
    sentry_plugin: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _get_config_async() -> SimpleNamespace:
        return _config(send_default_pii=True)

    monkeypatch.setattr(sentry_plugin, "get_config_async", _get_config_async)
    sdk = _FakeSentrySdk(
        initialized=True,
        options={
            "before_send": None,
            "before_breadcrumb": None,
            "before_send_transaction": None,
            "before_send_log": None,
        },
    )
    monkeypatch.setattr(sentry_plugin, "sentry_sdk", sdk)

    await sentry_plugin.startup()

    log = {
        "body": "external-full-log-canary",
        "attributes": {"user_id": "external-user-canary"},
    }
    assert sdk.client.options["before_send_log"](log, {}) is log

    issue = {
        "logentry": {
            "message": "external-template-canary",
            "formatted": "external-formatted-canary",
            "params": ["external-param-canary"],
        },
        "request": {"method": "get", "data": "external-request-canary"},
    }
    sanitized = sdk.client.options["before_send"](
        issue,
        {"log_record": object()},
    )
    assert sanitized["logentry"] == issue["logentry"]
    assert sanitized["request"] == {"method": "GET"}
    assert "external-request-canary" not in str(sanitized)


@pytest.mark.asyncio
async def test_external_client_without_mutable_options_fails_closed(
    sentry_plugin: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = _FakeSentrySdk(
        initialized=True,
        options=MappingProxyType({}),
    )
    monkeypatch.setattr(sentry_plugin, "sentry_sdk", sdk)

    with pytest.raises(TypeError, match="无法安装隐私钩子"):
        await sentry_plugin.startup()

    assert sdk.init_calls == []


@pytest.mark.asyncio
async def test_plugin_initialized_client_has_transaction_privacy_hook(
    sentry_plugin: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = _FakeSentrySdk(initialized=False, options={})
    monkeypatch.setattr(sentry_plugin, "sentry_sdk", sdk)

    await sentry_plugin.startup()

    assert len(sdk.init_calls) == 1
    assert callable(sdk.init_calls[0]["before_send_transaction"])
    assert sentry_plugin._initialized_by_plugin is True


@pytest.mark.asyncio
async def test_plugin_initialized_client_enables_full_logs_with_pii(
    sentry_plugin: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _get_config_async() -> SimpleNamespace:
        return _config(send_default_pii=True)

    monkeypatch.setattr(sentry_plugin, "get_config_async", _get_config_async)
    sdk = _FakeSentrySdk(initialized=False, options={})
    monkeypatch.setattr(sentry_plugin, "sentry_sdk", sdk)

    await sentry_plugin.startup()

    assert len(sdk.init_calls) == 1
    log = {
        "body": "plugin-full-log-canary",
        "attributes": {"custom": "plugin-attribute-canary"},
    }
    before_send_log = sdk.init_calls[0]["before_send_log"]
    assert callable(before_send_log)
    assert before_send_log(log, {}) is log
