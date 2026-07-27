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


@pytest.fixture(autouse=True)
def _reset_sentry_support_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个测试前重置 sentry_support 模块级状态，避免测试间污染。"""
    import komari_bot.common.sentry_support as ss

    ss._registered_sensitive_values.clear()
    ss._sensitive_value_collector = None
    # 还原 set_sensitive_value_collector 的副作用（避免 _collect_sensitive_values
    # 执行时调用前一次测试注入的 collector）
    del monkeypatch


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
    """外部已初始化 Client：验证隐私钩子安装成功，黑名单脱敏保留诊断但净化凭据。"""
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
    # 构造包含诊断信息和凭据字段的事务事件
    event = {
        "transaction": "external-transaction-canary",
        "spans": [{"description": "external-span-canary"}],
        "request": {
            "headers": {"authorization": "Bearer this-must-be-scrubbed-sentinel"},
        },
    }
    sanitized = transaction_hook(event, {})
    # 黑名单脱敏：transaction 名和 span description 无条件保留
    assert "external-transaction-canary" in str(sanitized)
    assert "external-span-canary" in str(sanitized)
    # 凭据字段被替换为 [Filtered]
    assert sanitized["request"]["headers"]["authorization"] == "[Filtered]"


@pytest.mark.asyncio
async def test_external_client_preserves_diagnostics_and_scrubs_credentials(
    sentry_plugin: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外部已初始化 Client（PII 模式）：诊断数据全量保留，凭据字段被净化。"""
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

    # before_send_log：返回新 dict，内容保留，凭据字段被净化
    log = {
        "body": "external-full-log-canary",
        "attributes": {"user_id": "external-user-canary"},
    }
    result = sdk.client.options["before_send_log"](log, {})
    assert result is not log            # 总是返回新 dict 对象
    assert result == log                 # 内容等价（无敏感字段）

    # before_send：诊断数据全量保留，凭据字段被净化
    issue = {
        "logentry": {
            "message": "external-template-canary",
            "formatted": "external-formatted-canary",
            "params": ["external-param-canary"],
        },
        "request": {"method": "get", "data": "external-request-canary"},
        "api_key": "sk-proj-this-is-a-long-secret-key-value",
        "user": {"id": "kept-in-pii-mode"},
    }
    sanitized = sdk.client.options["before_send"](
        issue,
        {"log_record": object()},
    )
    # logentry 无条件保留
    assert sanitized["logentry"] == issue["logentry"]
    # request 全量保留（不再只保留 method）
    assert sanitized["request"] == {"method": "get", "data": "external-request-canary"}
    # request 中的诊断内容可见
    assert "external-request-canary" in str(sanitized)
    # PII 模式下 user 上下文保留
    assert sanitized.get("user") == {"id": "kept-in-pii-mode"}
    # 凭据字段名命中黑名单，整值替换为 [Filtered]
    assert sanitized["api_key"] == "[Filtered]"


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
async def test_plugin_initialized_client_preserves_log_content(
    sentry_plugin: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """插件初始化的 Client（PII 模式）：日志正文保留，凭据字段被净化。"""
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
    # 类型收窄：init_calls 的值类型为 object，cast 到 callable
    before_send_log_fn: Any = before_send_log
    result = before_send_log_fn(log, {})
    assert result is not log               # 总是返回新 dict 对象
    assert result == log                    # 内容等价（无敏感字段）

    # 验证凭据字段仍被净化
    log_with_cred = {
        "body": "plugin-cred-log-canary",
        "attributes": {"custom": "plugin-attribute-canary"},
        "token": "this-is-a-sensitive-token-value-that-should-be-filtered",
    }
    scrubbed = before_send_log_fn(log_with_cred, {})
    assert scrubbed["token"] == "[Filtered]"
    assert "plugin-cred-log-canary" in str(scrubbed)
