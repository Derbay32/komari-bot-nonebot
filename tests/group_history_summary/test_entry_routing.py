"""群总结入口的鉴权顺序与传播路由测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class _Event:
    group_id = 10000

    def __init__(self, text: str = "帮我总结一下今天群里聊了什么") -> None:
        self._text = text

    def get_plaintext(self) -> str:
        return self._text


class _Bot:
    self_id = "20000"

    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, _event: object, message: object) -> None:
        self.sent.append(message)


class _Matcher:
    def __init__(self) -> None:
        self.block = False

    def stop_propagation(self) -> None:
        self.block = True


async def _handle(
    summary_module: Any,
    matcher: _Matcher,
    bot: _Bot,
    event: _Event,
) -> None:
    token = summary_module.current_matcher.set(cast("Any", matcher))
    try:
        await summary_module.handle_group_history_summary(
            cast("Any", bot),
            cast("Any", event),
        )
    finally:
        summary_module.current_matcher.reset(token)


def _install_config(
    summary_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    **values: Any,
) -> Any:
    config_values = {
        "plugin_enable": True,
        "min_summary_count": 10,
        "max_summary_count": 200,
    }
    config_values.update(values)
    config = SimpleNamespace(**config_values)
    monkeypatch.setattr(
        summary_module,
        "config_manager",
        SimpleNamespace(get=lambda: config),
    )
    return config


def _install_permission(
    summary_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    allowed: bool,
    calls: list[str],
) -> None:
    async def _check_permission(*_args: object) -> tuple[bool, str]:
        calls.append("permission")
        return allowed, "无权限"

    monkeypatch.setattr(
        summary_module,
        "permission_manager_plugin",
        SimpleNamespace(check_runtime_permission=_check_permission),
    )


def _tracked_async_result(
    calls: list[str],
    name: str,
    *,
    result: object,
) -> Callable[..., Awaitable[object]]:
    async def _tracked(*_args: object, **_kwargs: object) -> object:
        calls.append(name)
        return result

    return _tracked


@pytest.mark.asyncio
async def test_disabled_entry_returns_before_permission_and_remote_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    _install_config(summary_module, monkeypatch, plugin_enable=False)
    _install_permission(summary_module, monkeypatch, allowed=True, calls=calls)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=True),
    )
    monkeypatch.setattr(
        summary_module,
        "_is_summary_request",
        _tracked_async_result(calls, "classify", result=True),
    )
    matcher = _Matcher()

    await _handle(summary_module, matcher, _Bot(), _Event())

    assert calls == []
    assert matcher.block is False
    assert summary_module.summary_matcher.block is False


@pytest.mark.asyncio
async def test_denied_entry_returns_before_capability_and_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    _install_config(summary_module, monkeypatch)
    _install_permission(summary_module, monkeypatch, allowed=False, calls=calls)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=True),
    )
    monkeypatch.setattr(
        summary_module,
        "_is_summary_request",
        _tracked_async_result(calls, "classify", result=True),
    )
    matcher = _Matcher()

    await _handle(summary_module, matcher, _Bot(), _Event())

    assert calls == ["permission"]
    assert matcher.block is False


@pytest.mark.asyncio
async def test_unsupported_capability_returns_before_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    _install_config(summary_module, monkeypatch)
    _install_permission(summary_module, monkeypatch, allowed=True, calls=calls)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=False),
    )
    monkeypatch.setattr(
        summary_module,
        "_is_summary_request",
        _tracked_async_result(calls, "classify", result=True),
    )
    matcher = _Matcher()

    await _handle(summary_module, matcher, _Bot(), _Event())

    assert calls == ["permission", "capability"]
    assert matcher.block is False


@pytest.mark.asyncio
async def test_non_summary_classification_keeps_propagation_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    _install_config(summary_module, monkeypatch)
    _install_permission(summary_module, monkeypatch, allowed=True, calls=calls)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=True),
    )
    monkeypatch.setattr(
        summary_module,
        "_is_summary_request",
        _tracked_async_result(calls, "classify", result=False),
    )
    monkeypatch.setattr(
        summary_module,
        "execute_group_summary",
        _tracked_async_result(calls, "execute", result=None),
    )
    matcher = _Matcher()

    await _handle(summary_module, matcher, _Bot(), _Event())

    assert calls == ["permission", "capability", "classify"]
    assert matcher.block is False


@pytest.mark.asyncio
async def test_classifier_failure_keeps_propagation_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    _install_config(summary_module, monkeypatch)
    _install_permission(summary_module, monkeypatch, allowed=True, calls=calls)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=True),
    )

    async def _fail_rank(*_args: object, **_kwargs: object) -> object:
        calls.append("classify")
        msg = "模拟 scene 服务失败"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        summary_module._scene_rerank_service,
        "rank_message",
        _fail_rank,
    )
    matcher = _Matcher()

    await _handle(summary_module, matcher, _Bot(), _Event("总结一下"))

    assert calls == ["permission", "capability", "classify"]
    assert matcher.block is False


@pytest.mark.asyncio
async def test_confirmed_summary_stops_propagation_and_reuses_capability_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import komari_bot.plugins.group_history_summary as summary_module

    calls: list[str] = []
    config = _install_config(summary_module, monkeypatch)
    _install_permission(summary_module, monkeypatch, allowed=True, calls=calls)
    monkeypatch.setattr(
        summary_module,
        "check_group_history_supported",
        _tracked_async_result(calls, "capability", result=True),
    )
    monkeypatch.setattr(
        summary_module,
        "_is_summary_request",
        _tracked_async_result(calls, "classify", result=True),
    )
    matcher = _Matcher()

    async def _execute(**kwargs: object) -> object:
        calls.append("execute")
        assert matcher.block is True
        assert kwargs["config"] is config
        assert kwargs["history_capability_confirmed"] is True
        return SimpleNamespace(image_base64="aW1hZ2U=", summary_text="总结正文")

    monkeypatch.setattr(summary_module, "execute_group_summary", _execute)
    bot = _Bot()

    await _handle(summary_module, matcher, bot, _Event())

    assert calls == ["permission", "capability", "classify", "execute"]
    assert matcher.block is True
    assert len(bot.sent) == 1
